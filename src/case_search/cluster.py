"""Turning pairwise matches into entities.

This is where the non-transitivity of similarity has to be confronted rather than worked
around. Exact-key dedup is an equivalence relation, so grouping on it needs no decision at
all. Fuzzy matching is not, so grouping on it needs a rule -- and every available rule gives
up something. `Linkage` names the three and this module measures what each costs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from itertools import combinations

from case_search.block import BlockKey, candidate_pairs, combined_key
from case_search.models import Case, Entity, Linkage, Pair, Region
from case_search.normalise import canonical
from case_search.similarity import similarity

Key = tuple[str, Region]


def score_pairs(
    cases: Iterable[Case],
    threshold: float,
    key: BlockKey = combined_key,
) -> tuple[list[Pair], list[str]]:
    """Score every blocked pair, keeping those at or above the threshold."""
    candidates, oversized = candidate_pairs(cases, key=key)
    matched = [
        Pair(left=left.key, right=right.key, score=score)
        for left, right in candidates
        if (score := similarity(left.merchant_name, right.merchant_name)) >= threshold
    ]
    return (matched, oversized)


class _Components:
    """Union-find, iterative rather than recursive.

    Path compression by recursion blows the stack on a long chain, and a long chain is
    exactly what single linkage produces on near-duplicate names -- so the recursive form
    fails on precisely the input this exists to handle.
    """

    def __init__(self) -> None:
        self._parent: dict[Key, Key] = {}
        self._size: dict[Key, int] = {}

    def add(self, item: Key) -> None:
        if item not in self._parent:
            self._parent[item] = item
            self._size[item] = 1

    def find(self, item: Key) -> Key:
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: Key, right: Key) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if self._size[left_root] < self._size[right_root]:
            left_root, right_root = right_root, left_root
        self._parent[right_root] = left_root
        self._size[left_root] += self._size[right_root]

    def groups(self) -> list[list[Key]]:
        found: dict[Key, list[Key]] = defaultdict(list)
        for item in self._parent:
            found[self.find(item)].append(item)
        # Members sorted within a group and groups sorted by first member, so the output does
        # not depend on dict insertion order. Singletons are KEPT: a merchant seen once is a
        # real entity, and dropping it would make the entity count incomparable to a truth
        # set and quietly change the denominator of every metric.
        return sorted([sorted(members) for members in found.values()])


def _single(cases: list[Case], pairs: list[Pair]) -> list[list[Key]]:
    """Transitive closure: merge if any pair links.

    Order-independent -- the connected components of a graph do not depend on edge order, and
    there is a test asserting that over shuffled input. What it gives up is precision, and it
    gives it up in a specific and measurable way: a run of near-misses chains two entities
    together through intermediates. On real names, with a 0.85 threshold:

        'Northwind Trading' ~ 'Northwund Trading'  0.941
        'Northwund Trading' ~ 'Northwund Troding'  0.941
        'Northwund Troding' ~ 'Norihwund Troding'  0.941
        'Norihwund Troding' ~ 'Norihwuad Troding'  0.941
        'Northwind Trading' ~ 'Norihwuad Troding'  0.765   <- the matcher says NO

    Single linkage merges all five into one entity, including a pair it explicitly rejects.
    Each link is one edit in a seventeen-character name, so this is arithmetic on a fixed
    threshold rather than bad luck: any threshold below 1.0 admits a chain long enough.

    `weakest_internal_score` is what exposes it in the report -- a value below the threshold
    means the group contains a rejected pair.
    """
    components = _Components()
    for case in cases:
        components.add(case.key)
    for pair in pairs:
        components.union(pair.left, pair.right)
    return components.groups()


def _complete(cases: list[Case], pairs: list[Pair]) -> list[list[Key]]:
    """Cliques: merge two groups only if EVERY cross pair clears the threshold.

    Cannot chain, by construction -- a group is a clique, so it can never contain a pair the
    matcher rejected.

    **The obvious greedy implementation is order-dependent, and measuring it is what forced
    this one.** Scanning candidate merges and taking the FIRST that qualifies gave three
    different partitions across six shuffles of the same input, and the divergence was not
    cosmetic -- it landed on the group that matters:

        partition A   {Palisade Holdings, Palisadr Holdings, Palisadr Holdimgs}
                      {Palisadr Holdimgz, Palisade Holdingz}          <- matches the truth
        partition B   {Palisade Holdings, Palisadr Holdings, Palisade Holdingz}
                      {Palisadr Holdimgs, Palisadr Holdimgz}          <- does not

    So the rule was sometimes exactly right and sometimes wrong on nothing but row order,
    which means its reported accuracy was an artefact of one particular ordering rather than a
    property of the method. A number that changes when you shuffle the input is not a
    measurement.

    The fix is to stop taking the first qualifying merge and take the BEST one: the pair of
    groups whose weakest cross-link is strongest. Ties break on the sorted member keys, which
    is arbitrary but total -- and arbitrary-but-total is what determinism requires. Every
    merge is now chosen by the data rather than by arrival order.

    Maximal cliques would be an alternative and are worse here: a case can belong to several,
    so the output stops being a partition and every downstream count becomes ambiguous.
    """
    scores = {pair.unordered: pair.score for pair in pairs}
    groups: list[list[Key]] = sorted([case.key] for case in cases)

    while True:
        best: tuple[float, list[Key], int, int] | None = None
        for left_index, right_index in combinations(range(len(groups)), 2):
            left, right = groups[left_index], groups[right_index]
            crossing = [scores.get(frozenset({a, b})) for a in left for b in right]
            if any(score is None for score in crossing):
                continue
            weakest = min(score for score in crossing if score is not None)
            candidate = (weakest, sorted(left + right), left_index, right_index)
            # Strongest weakest-link wins; the member list breaks ties deterministically.
            # Comparing NEGATED keys would prefer the last -- taking the minimum member list
            # keeps the choice explainable as "the alphabetically first of the equally good".
            if best is None or (candidate[0], [str(k) for k in candidate[1]]) > (
                best[0], [str(k) for k in best[1]]
            ):
                best = candidate
        if best is None:
            break
        _, merged, left_index, right_index = best
        groups[left_index] = merged
        groups.pop(right_index)

    return sorted(groups)


def _canonical(cases: list[Case], pairs: list[Pair]) -> list[list[Key]]:
    """Group by exact canonical form. The fuzzy pairs are ignored entirely.

    Provably an equivalence relation, because it is string equality on a derived value. So it
    is transitive, order-independent, and needs no threshold -- the only rule here with all
    three properties.

    It is included as a baseline rather than a fallback, because the honest question is
    whether the fuzzy machinery earns its keep. If canonicalisation alone scores as well,
    the threshold and the linkage argument are ceremony.
    """
    grouped: dict[str, list[Key]] = defaultdict(list)
    for case in cases:
        grouped[canonical(case.merchant_name)].append(case.key)
    return sorted([sorted(members) for members in grouped.values()])


_RULES = {Linkage.SINGLE: _single, Linkage.COMPLETE: _complete, Linkage.CANONICAL: _canonical}


def resolve(
    cases: list[Case],
    threshold: float = 0.85,
    linkage: Linkage = Linkage.SINGLE,
    key: BlockKey = combined_key,
) -> tuple[list[Entity], list[Pair], list[str]]:
    """Group cases into entities. Returns the entities, the matched pairs, and skipped blocks.

    The pairs come back alongside the entities so a reader can audit a merge rather than
    taking it on trust -- which for single linkage is the difference between a number and an
    explanation.
    """
    pairs, oversized = score_pairs(cases, threshold, key=key)
    scores = {pair.unordered: pair.score for pair in pairs}
    names = {case.key: case.merchant_name for case in cases}
    groups = _RULES[linkage](cases, pairs)

    entities: list[Entity] = []
    for members in groups:
        internal = [
            scores.get(frozenset({a, b}), similarity(names[a], names[b]))
            for a, b in combinations(members, 2)
        ]
        entities.append(Entity(
            members=tuple(members),
            # The longest name, as the most complete spelling -- ties broken alphabetically so
            # the choice is deterministic. It is presentation only and never compared against.
            display_name=max((names[m] for m in members), key=lambda n: (len(n), n)),
            linkage=linkage,
            weakest_internal_score=min(internal) if internal else None,
        ))
    return (entities, pairs, oversized)


def chained_entities(entities: Iterable[Entity], threshold: float) -> list[Entity]:
    """Entities holding a pair the matcher itself rejected.

    Direct evidence of chaining, and the reason `weakest_internal_score` is carried on the
    model. Empty under COMPLETE and CANONICAL by construction, which is a property worth
    asserting rather than assuming.
    """
    return [
        entity for entity in entities
        if entity.weakest_internal_score is not None
        and entity.weakest_internal_score < threshold
    ]
