"""Domain types.

The two that carry the design: `Linkage`, which names the clustering rule because there is no
single right one, and `Completeness`, which distinguishes "we saw everything" from "we cannot
tell whether we saw everything".
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Region(StrEnum):
    NA = "na"
    EU = "eu"
    FE = "fe"


class Linkage(StrEnum):
    """How a set of pairwise matches becomes a set of entities.

    This is an enum rather than a boolean because the choice is a genuine judgement with no
    universally correct answer, and the report is expected to show what each one costs.

    SINGLE      merge if ANY pair links. Transitive closure. Maximum recall, and it CHAINS:
                a run of near-misses can merge two entities the matcher itself says are
                different. Measured on real names below.
    COMPLETE    merge only if EVERY pair in the resulting group clears the threshold -- a
                clique. Maximum precision, and it splits a genuine entity whose members are
                written too differently from each other.
    CANONICAL   group by exact canonical form only, no fuzzy step. Provably transitive and
                order-independent, and it misses anything canonicalisation cannot reach.
    """

    SINGLE = "single"
    COMPLETE = "complete"
    CANONICAL = "canonical"


class Completeness(StrEnum):
    """Whether the cases we hold are all the cases there were.

    Separate from match accuracy for the reason spelled out in `measure.py`: a page the
    platform never returned contains cases no matcher can find, and calling that a matching
    failure sends you to fix the wrong thing.
    """

    COMPLETE = "complete"
    SHORT = "short"
    GAP_IN_PAGES = "gap_in_pages"
    UNVERIFIABLE = "unverifiable"


class Case(BaseModel):
    """One case as one region reported it.

    `case_id` is NOT globally unique -- the same identifier is issued independently in each
    region, so two cases sharing one are usually different cases. That is why identity is
    composite.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    region: Region
    merchant_name: str
    observed_at: datetime
    page: int | None = None
    declared_total: int | None = None
    status: str = "open"

    @property
    def key(self) -> tuple[str, Region]:
        """Identity: case_id and region. **Deliberately excluding `observed_at`.**

        The spec I was given says the composite key is `case_id + region + timestamp`, and
        its own accuracy notes say `case_id + region`. The two contradict, so it needs
        deciding rather than copying.

        Including the timestamp defeats the entire purpose. `observed_at` is when we LOOKED,
        not what the case IS -- so the same case fetched twice produces two keys and dedup
        never fires. The bug would be invisible in the obvious direction too: the run
        completes, the report shows more cases than exist, and nothing errors.

        So the timestamp is not identity. It is used to choose WHICH copy survives, in
        `dedupe.py`, which is a different job.
        """
        return (self.case_id, self.region)


class Duplicate(BaseModel):
    """A case dropped by exact-key dedup, kept so the count is explainable."""

    model_config = ConfigDict(frozen=True)

    key: tuple[str, Region]
    dropped_observed_at: datetime
    kept_observed_at: datetime


class Pair(BaseModel):
    """Two distinct cases and how alike their merchant names are."""

    model_config = ConfigDict(frozen=True)

    left: tuple[str, Region]
    right: tuple[str, Region]
    score: float = Field(ge=0.0, le=1.0)

    @property
    def unordered(self) -> frozenset[tuple[str, Region]]:
        """Both endpoints as a set, so (a, b) and (b, a) compare equal."""
        return frozenset({self.left, self.right})


class Entity(BaseModel):
    """A group of cases judged to be the same merchant."""

    model_config = ConfigDict(frozen=True)

    members: tuple[tuple[str, Region], ...]
    display_name: str
    linkage: Linkage
    weakest_internal_score: float | None = None
    """Lowest pairwise score inside this entity, or None for a singleton.

    None rather than 1.0. A singleton has no internal pair, so there is no weakest link --
    and reporting 1.0 would claim perfect internal agreement that was never measured.
    Under SINGLE linkage this number is the evidence of chaining: a value below the
    threshold means the group contains a pair the matcher rejected.
    """

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def regions(self) -> tuple[Region, ...]:
        return tuple(sorted({region for _, region in self.members}))


class PageAudit(BaseModel):
    """Whether one region's pages arrived intact."""

    model_config = ConfigDict(frozen=True)

    region: Region
    verdict: Completeness
    cases_seen: int
    declared_total: int | None
    pages_seen: tuple[int, ...]
    missing_pages: tuple[int, ...]
