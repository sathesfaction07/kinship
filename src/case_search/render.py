"""Text and machine-readable output.

The rules that matter, and they are the same ones in both renderers: an unmeasured metric
prints `n/a` and exports as `null`, never `0.0`; and a chained entity is shown with the pair
that proves it rather than merely counted.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path

from case_search.measure import Comparison, Score
from case_search.models import Case, Completeness, Duplicate, Entity, PageAudit


def _percent(value: float | None) -> str:
    """`n/a` for unmeasured, never 0.0%.

    A metric over an empty denominator is not zero. Printing 0.0% would read as total failure
    and would sort an abstaining run above a working one.
    """
    return "n/a" if value is None else f"{value:>6.1%}"


def _decimal(value: float | None) -> str:
    return "n/a" if value is None else f"{value:>5.3f}"


def _score_row(label: str, score: Score) -> str:
    return (
        f"    {label:<10}"
        f"{_percent(score.precision)}  {_percent(score.recall)}  {_decimal(score.f1)}"
        f"   TP={score.true_positives:<4} FP={score.false_positives:<4} FN={score.false_negatives}"
    )


def render_dedupe(
    rows: int,
    kept: list[Case],
    duplicates: list[Duplicate],
    audits: list[PageAudit],
    problems: list[str],
) -> str:
    lines = ["", "deduplication", ""]
    lines.append(f"  {rows} row(s) in, {len(kept)} distinct case(s), {len(duplicates)} dropped")
    lines.append("  identity is (case_id, region) -- NOT including the timestamp, which is")
    lines.append("  when we looked rather than what the case is. Including it would give the")
    lines.append("  same case two keys on a re-fetch and dedup would never fire.")
    if problems:
        lines.append("")
        lines.append(f"  {len(problems)} unreadable row(s):")
        lines.extend(f"    {problem}" for problem in problems[:5])

    lines.extend(["", "completeness, worst first", ""])
    order = {
        Completeness.GAP_IN_PAGES: 0,
        Completeness.SHORT: 1,
        Completeness.UNVERIFIABLE: 2,
        Completeness.COMPLETE: 3,
    }
    for audit in sorted(audits, key=lambda a: order[a.verdict]):
        declared = "none declared" if audit.declared_total is None else str(audit.declared_total)
        lines.append(
            f"  [{audit.verdict.value:<13}] {audit.region.value}: {audit.cases_seen} case(s), "
            f"declared {declared}, pages {list(audit.pages_seen)}"
        )
        if audit.verdict is Completeness.GAP_IN_PAGES:
            lines.append(
                f"                    page(s) {list(audit.missing_pages)} were never seen. "
                "Any case on them is missing and NO matcher can find it."
            )
        elif audit.verdict is Completeness.UNVERIFIABLE:
            lines.append(
                "                    the region declares no total, so whether anything was "
                "missed CANNOT be determined from this data."
            )
    return "\n".join(lines) + "\n"


def render_entities(entities: list[Entity], threshold: float, oversized: list[str]) -> str:
    multi = [entity for entity in entities if entity.size > 1]
    chained = [
        entity for entity in entities
        if entity.weakest_internal_score is not None
        and entity.weakest_internal_score < threshold
    ]

    lines = ["", "entities", ""]
    lines.append(
        f"  {len(entities)} entit(ies) from {sum(e.size for e in entities)} case(s): "
        f"{len(multi)} with more than one member, {len(entities) - len(multi)} singleton(s)"
    )
    lines.append("  singletons are KEPT -- a merchant seen once is a real entity, and dropping")
    lines.append("  them would change the denominator of every metric below.")

    if multi:
        lines.extend(["", "  merged entities", ""])
        for entity in sorted(multi, key=lambda e: (-e.size, e.display_name)):
            weakest = _decimal(entity.weakest_internal_score)
            regions = ",".join(r.value for r in entity.regions)
            lines.append(
                f"    {entity.size} member(s)  weakest={weakest}  [{regions}]  "
                f"{entity.display_name!r}"
            )

    if chained:
        lines.extend(["", "  CHAINED -- contains a pair the matcher itself rejected", ""])
        for entity in chained:
            lines.append(
                f"    weakest={_decimal(entity.weakest_internal_score)} < threshold "
                f"{threshold}: {entity.display_name!r} ({entity.size} members)"
            )
        lines.append("")
        lines.append("    Single linkage merges on ANY link, so a run of near-misses joins two")
        lines.append("    entities through intermediates. The weakest score above is the proof:")
        lines.append("    it is a pair inside one entity that scored below the threshold.")

    if oversized:
        lines.extend(["", f"  {len(oversized)} block(s) skipped for exceeding the size cap:"])
        lines.append(f"    {', '.join(sorted(oversized)[:8])}")
        lines.append("    Pairs inside them were never compared, so this is a deliberate")
        lines.append("    recall sacrifice rather than an absence of matches.")

    return "\n".join(lines) + "\n"


def render_comparison(
    comparisons: list[Comparison],
    blocking: float | None,
    lost_pairs: int,
    preferred: str | None,
) -> str:
    lines = ["", "blocking -- the ceiling on recall", ""]
    lines.append(
        f"  pair completeness  {_percent(blocking)}   "
        f"({lost_pairs} true pair(s) never compared)"
    )
    lines.append("  A pair the blocker never offered cannot be matched at any threshold, so")
    lines.append("  this is a hard ceiling. Measuring it separately is the only way to tell")
    lines.append("  'the matcher is too strict' from 'the matcher never saw the pair'.")

    lines.extend(["", "linkage rules compared", ""])
    lines.append("    rule      entities  largest  chained  order")
    for comparison in comparisons:
        stable = {True: "stable", False: "UNSTABLE", None: "untested"}[comparison.order_stable]
        lines.append(
            f"    {comparison.linkage:<10}{comparison.entities:>8}{comparison.largest_entity:>9}"
            f"{comparison.chained:>9}  {stable}"
        )

    for comparison in comparisons:
        lines.extend(["", f"  {comparison.linkage} (threshold {comparison.threshold})"])
        lines.append("    scheme    precision  recall     F1")
        lines.append(_score_row("pairwise", comparison.pairwise))
        lines.append(_score_row("cluster", comparison.cluster))

    lines.extend(["", "  Pairwise and cluster scoring disagree on purpose. Pairwise forgives a"])
    lines.append("  nearly-right entity; cluster scoring requires exact set equality. A single")
    lines.append("  wrong merge is cheap pairwise and fatal by cluster, which is precisely the")
    lines.append("  failure single linkage produces -- so reporting only pairwise F1, the usual")
    lines.append("  convention, would hide it.")

    lines.extend(["", "preferred rule", ""])
    if preferred is None:
        lines.append("  none -- no rule is both order-stable and measurable on this input.")
    else:
        lines.append(f"  {preferred}, ranked on CLUSTER F1 among order-stable rules.")
        lines.append("  Order-unstable rules are excluded outright rather than penalised: a")
        lines.append("  partition that changes when you shuffle the input is not a measurement.")
    return "\n".join(lines) + "\n"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_entities_csv(path: Path, entities: Iterable[Entity]) -> None:
    """One row per entity. An unmeasured weakest score is an EMPTY cell, never 0.

    A spreadsheet reader cannot distinguish a zero from a missing value once written, and 0.0
    in a similarity column would read as "completely dissimilar" -- the opposite of the truth
    for a singleton, which simply has no internal pair.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["display_name", "size", "regions", "weakest_internal_score", "members"])
        for entity in sorted(entities, key=lambda e: (-e.size, e.display_name)):
            writer.writerow([
                entity.display_name,
                entity.size,
                ";".join(r.value for r in entity.regions),
                "" if entity.weakest_internal_score is None
                else f"{entity.weakest_internal_score:.4f}",
                ";".join(f"{cid}/{region.value}" for cid, region in entity.members),
            ])


def score_payload(score: Score) -> dict:
    """Scores as JSON, with None preserved as null rather than coerced to 0."""
    return {
        "true_positives": score.true_positives,
        "false_positives": score.false_positives,
        "false_negatives": score.false_negatives,
        "precision": score.precision,
        "recall": score.recall,
        "f1": score.f1,
    }
