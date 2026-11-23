"""Measuring whether the entities are right.

Two scoring schemes, deliberately both reported, because they disagree and the disagreement
is the finding.

PAIRWISE     over the pairs the entities imply. Forgiving: one wrong merge costs only the
             pairs it wrongly creates, so a nearly-right clustering scores well.
CLUSTER      exact set equality against a true entity. Unforgiving: one wrong member and the
             whole entity is wrong.

A single wrong merge between two large entities is barely visible pairwise and catastrophic
by cluster. Reporting only pairwise F1 -- the convention in entity-resolution papers -- hides
exactly the failure single linkage produces.
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import combinations

from pydantic import BaseModel, ConfigDict

from case_search.models import Entity, Region

Key = tuple[str, Region]


class Score(BaseModel):
    """Precision, recall and F1, any of which may be undefined.

    `float | None` throughout, for the same reason in each case: a metric over an empty
    denominator is not zero, it is unmeasured. Precision is undefined when nothing was
    predicted, recall when there was nothing to find. Reporting 0.0 for either reads as total
    failure and would rank an abstaining run above a working one.
    """

    model_config = ConfigDict(frozen=True)

    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float | None:
        predicted = self.true_positives + self.false_positives
        return self.true_positives / predicted if predicted else None

    @property
    def recall(self) -> float | None:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else None

    @property
    def f1(self) -> float | None:
        """Harmonic mean, None if either input is None or both are zero.

        Guarding the both-zero case separately from the None case: two defined-but-zero
        inputs give a defined-but-zero F1, which is a real result and different from
        unmeasured.
        """
        precision, recall = self.precision, self.recall
        if precision is None or recall is None:
            return None
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)


def _pairs_of(groups: Iterable[Iterable[Key]]) -> set[frozenset[Key]]:
    """Every within-group pair. A singleton contributes none, correctly.

    A partition into singletons implies no pairs at all, so pairwise precision is undefined
    rather than zero -- the run made no claims. That is the honest reading and it falls out of
    the arithmetic instead of needing a special case.
    """
    return {
        frozenset({left, right})
        for group in groups
        for left, right in combinations(sorted(group), 2)
    }


def pairwise(entities: Iterable[Entity], truth: dict[Key, str]) -> Score:
    """Score the implied pairs.

    Only keys present in the truth set are considered. A case the truth set says nothing about
    can be neither a hit nor a miss, and counting it either way would let the corpus's
    coverage decide the score.
    """
    known = set(truth)
    predicted = {
        pair for pair in _pairs_of(entity.members for entity in entities)
        if pair <= known
    }

    groups: dict[str, list[Key]] = {}
    for key, entity_id in truth.items():
        groups.setdefault(entity_id, []).append(key)
    actual = _pairs_of(groups.values())

    return Score(
        true_positives=len(predicted & actual),
        false_positives=len(predicted - actual),
        false_negatives=len(actual - predicted),
    )


def cluster_level(entities: Iterable[Entity], truth: dict[Key, str]) -> Score:
    """Score whole entities by exact set equality.

    Unforgiving on purpose. Pairwise scoring lets a chained entity look almost right; this
    says it is wrong, which is the more useful answer when the output is a list of merchants
    someone will act on.

    Entities are restricted to known keys before comparison, so an entity is judged on the
    part the truth set can speak about. Without that, a single unknown member would make every
    entity containing it wrong for a reason unrelated to the matcher.
    """
    known = set(truth)
    predicted = {
        frozenset(k for k in entity.members if k in known)
        for entity in entities
    }
    predicted.discard(frozenset())

    groups: dict[str, set[Key]] = {}
    for key, entity_id in truth.items():
        groups.setdefault(entity_id, set()).add(key)
    actual = {frozenset(members) for members in groups.values()}

    return Score(
        true_positives=len(predicted & actual),
        false_positives=len(predicted - actual),
        false_negatives=len(actual - predicted),
    )


class Comparison(BaseModel):
    """One linkage rule's results, ready to be put beside another's."""

    model_config = ConfigDict(frozen=True)

    linkage: str
    threshold: float
    entities: int
    largest_entity: int
    chained: int
    """Entities containing a pair the matcher itself rejected. Zero except under SINGLE."""
    pairwise: Score
    cluster: Score
    order_stable: bool | None = None
    """Whether shuffling the input gave the same entities. None if not tested.

    Carried alongside accuracy because a rule that scores well but answers differently on
    reordered input is not usable in a pipeline where order varies -- and nothing in a
    precision figure reveals that.
    """


def summarise(comparisons: Iterable[Comparison]) -> str | None:
    """Which linkage rule to prefer, or None if the data cannot say.

    Ranks on cluster-level F1 rather than pairwise, because pairwise is the metric that hides
    chaining. Order-unstable rules are excluded outright rather than penalised: a clustering
    that changes with input order is not a candidate at any accuracy.

    Returns None rather than a default when every candidate is disqualified or unmeasurable,
    so the caller has to handle "no answer" instead of receiving a fabricated one.
    """
    eligible = [
        comparison for comparison in comparisons
        if comparison.cluster.f1 is not None and comparison.order_stable is not False
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda c: (c.cluster.f1 or 0.0, -c.chained)).linkage
