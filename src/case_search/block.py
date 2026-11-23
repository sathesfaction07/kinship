"""Blocking: deciding which pairs are worth comparing at all.

Comparing every case against every other is O(n^2). At the 10,000 cases the spec describes
that is 49,995,000 comparisons, and each one runs an edit distance. Blocking cuts it down by
only comparing cases that share a cheap key.

**The cost is a false-negative rate you cannot see in the output.** If two records of the same
merchant land in different blocks they are never compared, so no threshold and no better
similarity function can ever recover them. The blocking key therefore sets a hard CEILING on
recall, and that ceiling is measurable independently of the matcher -- which is what
`pair_completeness` is for.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from itertools import combinations

from case_search.models import Case, Region
from case_search.normalise import canonical, tokens

BlockKey = Callable[[Case], frozenset[str]]


def prefix_key(case: Case, length: int = 4) -> frozenset[str]:
    """The first `length` characters of the canonical name.

    The obvious choice, and a bad one on its own. Any difference in the opening characters
    puts two records in different blocks:

        'The ACME Corp'  -> canonical 'acme'      -> 'acme'
        'ACME Corp'      -> canonical 'acme'      -> 'acme'      same, because the article
                                                                 is stripped BEFORE blocking
        'Nordwind AB'    -> 'nordwind'            -> 'nord'
        'Northwind AB'   -> 'northwind'           -> 'nort'      DIFFERENT block, never
                                                                 compared, unrecoverable

    Article stripping in `canonical` is what saves the first case, which is why
    normalisation has to happen before blocking rather than after. The second case it cannot
    save at all.
    """
    return frozenset({canonical(case.merchant_name)[:length]})


def token_key(case: Case) -> frozenset[str]:
    """Every token of the canonical name, as separate keys.

    A case joins one block per token, so two names sharing ANY token get compared. That
    fixes the truncated-prefix problem completely for multi-token names -- 'Nordwind
    Trading' and 'Northwind Trading' both emit 'trading'.

    The cost is block size. A common token like 'foods' pulls in every merchant using it, and
    a block of size k contributes k(k-1)/2 comparisons. So this trades work for recall in the
    opposite direction to `prefix_key`, and `MAX_BLOCK` caps the damage.
    """
    return tokens(case.merchant_name) or frozenset({""})


def combined_key(case: Case) -> frozenset[str]:
    """Prefix AND every token.

    Union, not intersection. Each key is an independent opportunity to be compared, so
    adding keys can only raise recall. Intersecting them would require agreement on both and
    lose the cases each is individually good at.
    """
    return prefix_key(case) | token_key(case)


# A block larger than this is not a useful grouping, it is a common word. Comparing all pairs
# inside it costs more than it finds -- and unlike the recall ceiling, this cost is visible,
# so it is capped rather than hoped about.
MAX_BLOCK = 60


def build_blocks(cases: Iterable[Case], key: BlockKey) -> dict[str, list[Case]]:
    """Group cases by every key each one emits."""
    blocks: dict[str, list[Case]] = {}
    for case in cases:
        for block_key in key(case):
            blocks.setdefault(block_key, []).append(case)
    return blocks


def candidate_pairs(
    cases: Iterable[Case],
    key: BlockKey = combined_key,
    max_block: int = MAX_BLOCK,
) -> tuple[list[tuple[Case, Case]], list[str]]:
    """Pairs worth comparing, plus the names of blocks that were skipped for being too big.

    Returns the skipped blocks rather than logging them, because a skipped block is a
    deliberate recall sacrifice and the report has to be able to say so. Silently dropping
    them would make the ceiling unmeasurable.

    Cross-region pairs are NOT filtered out here even though the same merchant in two regions
    is the interesting case, because the same merchant also appears twice WITHIN a region
    under two spellings, and excluding that would answer a narrower question than the one
    asked.
    """
    blocks = build_blocks(cases, key)
    seen: set[frozenset[tuple[str, Region]]] = set()
    pairs: list[tuple[Case, Case]] = []
    oversized: list[str] = []

    for block_key in sorted(blocks):
        members = blocks[block_key]
        if len(members) > max_block:
            oversized.append(block_key)
            continue
        for left, right in combinations(members, 2):
            if left.key == right.key:
                continue
            unordered = frozenset({left.key, right.key})
            if unordered in seen:
                continue
            seen.add(unordered)
            pairs.append((left, right))

    # Sorted so two runs over differently ordered input produce the same pair list. Without
    # this the pair ORDER varies, and while an order-independent clustering rule would not
    # care, `Linkage.SINGLE` visits pairs in sequence and a reader comparing two reports
    # would see gratuitous churn.
    pairs.sort(key=lambda p: (p[0].key, p[1].key))
    return (pairs, oversized)


def all_pairs(cases: Iterable[Case]) -> Iterator[tuple[Case, Case]]:
    """Every pair, no blocking. The baseline that defines the ceiling.

    Only for measurement and small inputs -- this is the quadratic cost blocking exists to
    avoid. It is what `pair_completeness` compares against.
    """
    for left, right in combinations(cases, 2):
        if left.key != right.key:
            yield (left, right)


def pair_completeness(
    cases: list[Case],
    truth: dict[tuple[str, Region], str],
    key: BlockKey = combined_key,
    max_block: int = MAX_BLOCK,
) -> tuple[float | None, list[frozenset[tuple[str, Region]]]]:
    """What fraction of genuinely-matching pairs blocking kept, and which it lost.

    This is the ceiling on recall, and it is the number a matcher cannot argue with: a pair
    that was never compared cannot be matched however good the threshold is. Measuring it
    separately is the only way to tell "the matcher is too strict" from "the blocker never
    showed it the pair".

    Returns None when the truth set contains no matching pairs at all, rather than 1.0.
    Perfect completeness over zero opportunities is not perfection, it is an absence of
    evidence -- the same reasoning as precision over an empty extraction.
    """
    wanted = {
        frozenset({left.key, right.key})
        for left, right in combinations(cases, 2)
        if left.key != right.key
        and truth.get(left.key) is not None
        and truth.get(left.key) == truth.get(right.key)
    }
    if not wanted:
        return (None, [])

    pairs, _ = candidate_pairs(cases, key=key, max_block=max_block)
    offered = {frozenset({left.key, right.key}) for left, right in pairs}
    lost = sorted(wanted - offered, key=lambda s: sorted(s))
    return (len(wanted & offered) / len(wanted), lost)
