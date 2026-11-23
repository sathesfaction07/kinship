"""Exact-key deduplication, and auditing whether the pages arrived.

Distinct from `cluster.py` in a way that matters: identity here is string equality on a
composite key, which is a genuine equivalence relation. Transitive, order-independent, and
needing no threshold or linkage rule. Everything difficult about fuzzy matching is absent, so
the two are kept apart rather than blended into one "dedup" step.
"""

from __future__ import annotations

from collections.abc import Iterable

from case_search.models import Case, Completeness, Duplicate, PageAudit, Region


def dedupe(cases: Iterable[Case]) -> tuple[list[Case], list[Duplicate]]:
    """Collapse cases sharing (case_id, region), keeping the most recently observed.

    Recency, not arrival order. `observed_at` is excluded from identity -- see `Case.key` --
    but it is exactly the right tie-breaker: two observations of one case describe the same
    case at two moments, and the later one is the current truth about its status.

    That is not a cosmetic choice. Keeping the first arrival instead would report a stale
    status while still counting the case once, so the count would be right and the content
    wrong -- which is harder to notice than a wrong count.

    Ties on `observed_at` fall back to the input order, and the docstring says so rather than
    implying a total order exists. Two observations with identical timestamps are
    indistinguishable by the data, so any choice is arbitrary; being explicit at least makes
    it reproducible for a given input.

    Returns the dropped cases as `Duplicate` records rather than a count, so a surprising
    number can be investigated instead of merely believed.
    """
    best: dict[tuple[str, Region], Case] = {}
    duplicates: list[Duplicate] = []

    for case in cases:
        existing = best.get(case.key)
        if existing is None:
            best[case.key] = case
            continue
        if case.observed_at > existing.observed_at:
            best[case.key] = case
            duplicates.append(Duplicate(
                key=case.key,
                dropped_observed_at=existing.observed_at,
                kept_observed_at=case.observed_at,
            ))
        else:
            duplicates.append(Duplicate(
                key=case.key,
                dropped_observed_at=case.observed_at,
                kept_observed_at=existing.observed_at,
            ))

    kept = sorted(best.values(), key=lambda c: c.key)
    duplicates.sort(key=lambda d: (d.key, d.dropped_observed_at))
    return (kept, duplicates)


def audit_pages(cases: Iterable[Case]) -> list[PageAudit]:
    """Per region, whether the pages we hold look like all the pages there were.

    Kept separate from match accuracy because the two fail independently and have opposite
    remedies. A case on a page the platform never returned cannot be matched by any threshold
    -- reporting that as a recall miss sends you to tune a matcher that is working correctly.

    `GAP_IN_PAGES` outranks a total comparison. A missing page number proves data was lost; a
    total mismatch has innocent explanations, including a total that counts something slightly
    different from what we are counting.

    `UNVERIFIABLE` exists because most sources declare no total, and "we cannot tell" is a
    different claim from "we got everything". Collapsing it into either is a lie in one
    direction or the other.
    """
    by_region: dict[Region, list[Case]] = {}
    for case in cases:
        by_region.setdefault(case.region, []).append(case)

    audits: list[PageAudit] = []
    for region in sorted(by_region):
        group = by_region[region]
        pages = tuple(sorted({case.page for case in group if case.page is not None}))
        totals = {case.declared_total for case in group if case.declared_total is not None}
        # The maximum, on the reasoning that a paginated listing usually repeats the full
        # count on every page. A source reporting a PER-PAGE count would be misread and
        # nothing here can detect that.
        declared = max(totals) if totals else None

        missing: tuple[int, ...] = ()
        if pages:
            missing = tuple(p for p in range(pages[0], pages[-1] + 1) if p not in pages)

        if missing:
            verdict = Completeness.GAP_IN_PAGES
        elif declared is None:
            verdict = Completeness.UNVERIFIABLE
        elif len(group) < declared:
            verdict = Completeness.SHORT
        else:
            # Equal or over. Over is folded into COMPLETE here rather than given its own
            # verdict, because dedup has already run: a surplus after dedup means the
            # declared total counts something other than distinct cases, which is a
            # statement about the source and not a problem with this run.
            verdict = Completeness.COMPLETE

        audits.append(PageAudit(
            region=region,
            verdict=verdict,
            cases_seen=len(group),
            declared_total=declared,
            pages_seen=pages,
            missing_pages=missing,
        ))
    return audits
