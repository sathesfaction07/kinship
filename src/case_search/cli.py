"""Command line interface.

Exit codes gate on what is demonstrable. `resolve` fails on a page gap, because that proves
cases are missing; it does NOT fail on an unverifiable region, because most regions declare no
total and a check that always fires carries no information. `compare` fails when no linkage
rule is both order-stable and measurable, since a preference is then genuinely unavailable
rather than merely close.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import click

from case_search.block import combined_key, pair_completeness, prefix_key, token_key
from case_search.cluster import chained_entities, resolve
from case_search.config import Settings
from case_search.dedupe import audit_pages, dedupe
from case_search.measure import Comparison, cluster_level, pairwise, summarise
from case_search.models import Completeness, Linkage
from case_search.normalise import canonical, fold
from case_search.render import (
    render_comparison,
    render_dedupe,
    render_entities,
    score_payload,
    write_entities_csv,
    write_json,
)
from case_search.similarity import character_similarity, similarity, token_similarity
from case_search.sources import load_cases, load_truth

SETTINGS = Settings()
BLOCK_KEYS = {"prefix": prefix_key, "token": token_key, "combined": combined_key}


def _load(cases_path: Path) -> tuple[list, list[str]]:
    if not cases_path.exists():
        click.echo(f"cases file not found: {cases_path}", err=True)
        raise SystemExit(1)
    return load_cases(cases_path)


@click.group()
@click.version_option()
def cli() -> None:
    """Cross-region case deduplication with measured fuzzy entity resolution."""


@cli.command()
@click.option("-i", "--cases", type=click.Path(path_type=Path), default=SETTINGS.cases)
@click.option("-t", "--threshold", type=click.FloatRange(0.0, 1.0), default=SETTINGS.threshold)
@click.option("-l", "--linkage", type=click.Choice([e.value for e in Linkage]),
              default=SETTINGS.linkage)
@click.option("-b", "--block", type=click.Choice(sorted(BLOCK_KEYS)), default="combined")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None)
def resolve_command(
    cases: Path, threshold: float, linkage: str, block: str, output: Path | None
) -> None:
    """Dedupe on the composite key, then group the remainder into entities."""
    rows, problems = _load(cases)
    kept, duplicates = dedupe(rows)
    audits = audit_pages(kept)

    chosen = Linkage(linkage)
    entities, pairs, oversized = resolve(
        kept, threshold=threshold, linkage=chosen, key=BLOCK_KEYS[block]
    )
    chained = chained_entities(entities, threshold)

    click.echo(render_dedupe(len(rows), kept, duplicates, audits, problems))
    click.echo(render_entities(entities, threshold, oversized))

    if output:
        write_json(output / "report.json", {
            "summary": {
                "rows": len(rows),
                "distinct_cases": len(kept),
                "duplicates_dropped": len(duplicates),
                "entities": len(entities),
                "merged_entities": sum(1 for e in entities if e.size > 1),
                "chained_entities": len(chained),
                "matched_pairs": len(pairs),
                "oversized_blocks": sorted(oversized),
                "unreadable_rows": len(problems),
                "linkage": chosen.value,
                "threshold": threshold,
            },
            "completeness": [
                {
                    "region": a.region.value,
                    "verdict": a.verdict.value,
                    "cases_seen": a.cases_seen,
                    "declared_total": a.declared_total,
                    "pages_seen": list(a.pages_seen),
                    "missing_pages": list(a.missing_pages),
                }
                for a in audits
            ],
            "entities": [
                {
                    "display_name": e.display_name,
                    "size": e.size,
                    "regions": [r.value for r in e.regions],
                    "weakest_internal_score": e.weakest_internal_score,
                    "members": [[cid, r.value] for cid, r in e.members],
                }
                for e in entities
            ],
        })
        write_entities_csv(output / "entities.csv", entities)
        click.echo(f"wrote {output}/report.json and entities.csv")

    gaps = [a for a in audits if a.verdict is Completeness.GAP_IN_PAGES]
    short = [a for a in audits if a.verdict is Completeness.SHORT]
    if gaps or short or problems:
        for audit in gaps:
            click.echo(
                f"FAIL {audit.region.value}: page(s) {list(audit.missing_pages)} never seen",
                err=True,
            )
        for audit in short:
            click.echo(
                f"FAIL {audit.region.value}: {audit.cases_seen} case(s) against a declared "
                f"{audit.declared_total}",
                err=True,
            )
        if problems:
            click.echo(f"FAIL {len(problems)} unreadable row(s)", err=True)
        sys.exit(1)


@cli.command()
@click.option("-i", "--cases", type=click.Path(path_type=Path), default=SETTINGS.cases)
@click.option("-k", "--truth", type=click.Path(path_type=Path), default=SETTINGS.truth)
@click.option("-t", "--threshold", type=click.FloatRange(0.0, 1.0), default=SETTINGS.threshold)
@click.option("-b", "--block", type=click.Choice(sorted(BLOCK_KEYS)), default="combined")
@click.option("--shuffles", type=click.IntRange(0, 50), default=6,
              help="Reruns on shuffled input to test order stability. 0 to skip.")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None)
def compare(
    cases: Path, truth: Path, threshold: float, block: str, shuffles: int, output: Path | None
) -> None:
    """Score every linkage rule against a truth set, and test order stability."""
    rows, _ = _load(cases)
    if not truth.exists():
        click.echo(f"truth file not found: {truth}", err=True)
        raise SystemExit(1)

    kept, _ = dedupe(rows)
    known = load_truth(truth)
    key = BLOCK_KEYS[block]

    blocking, lost = pair_completeness(kept, known, key=key)

    comparisons: list[Comparison] = []
    for rule in Linkage:
        entities, _, _ = resolve(kept, threshold=threshold, linkage=rule, key=key)

        stable: bool | None = None
        if shuffles:
            reference = sorted(tuple(e.members) for e in entities)
            stable = True
            for seed in range(shuffles):
                shuffled = list(kept)
                random.Random(seed).shuffle(shuffled)
                again, _, _ = resolve(shuffled, threshold=threshold, linkage=rule, key=key)
                if sorted(tuple(e.members) for e in again) != reference:
                    stable = False
                    break

        comparisons.append(Comparison(
            linkage=rule.value,
            threshold=threshold,
            entities=len(entities),
            largest_entity=max((e.size for e in entities), default=0),
            chained=len(chained_entities(entities, threshold)),
            pairwise=pairwise(entities, known),
            cluster=cluster_level(entities, known),
            order_stable=stable,
        ))

    preferred = summarise(comparisons)
    click.echo(render_comparison(comparisons, blocking, len(lost), preferred))

    if lost:
        click.echo("  true pairs the blocker never offered:")
        for pair in lost[:5]:
            click.echo(f"    {' <-> '.join(f'{cid}/{r.value}' for cid, r in sorted(pair))}")
        click.echo("")

    if output:
        write_json(output / "comparison.json", {
            "blocking": {
                "key": block,
                "pair_completeness": blocking,
                "lost_pairs": [
                    [[cid, r.value] for cid, r in sorted(pair)] for pair in lost
                ],
            },
            "truth_entities": len(set(known.values())),
            "truth_keys": len(known),
            "preferred": preferred,
            "rules": [
                {
                    "linkage": c.linkage,
                    "entities": c.entities,
                    "largest_entity": c.largest_entity,
                    "chained": c.chained,
                    "order_stable": c.order_stable,
                    "pairwise": score_payload(c.pairwise),
                    "cluster": score_payload(c.cluster),
                }
                for c in comparisons
            ],
        })
        click.echo(f"wrote {output}/comparison.json")

    if preferred is None:
        click.echo("FAIL no linkage rule is both order-stable and measurable", err=True)
        sys.exit(1)


@cli.command()
@click.argument("left")
@click.argument("right")
def explain(left: str, right: str) -> None:
    """Show why two merchant names score as they do."""
    click.echo("")
    for label, value in [("left", left), ("right", right)]:
        click.echo(f"  {label:<12}{value!r}")
        click.echo(f"    folded    {fold(value)!r}")
        click.echo(f"    canonical {canonical(value)!r}")
    character = character_similarity(canonical(left), canonical(right))
    token = token_similarity(left, right)
    combined = similarity(left, right)
    click.echo("")
    click.echo(f"  character   {character:.3f}   edit distance over canonical forms")
    click.echo(f"  token       {token:.3f}   Jaccard over canonical token sets")
    click.echo(f"  combined    {combined:.3f}   the MAXIMUM, because the two measures fail on")
    click.echo("                      opposite inputs and neither is evidence against a match")
    if character == 0.0 and token > 0.0:
        click.echo("")
        click.echo("  Note the character score is ZERO while the tokens agree: the canonical")
        click.echo("  forms differ at every position. An average would report "
                   f"{(character + token) / 2:.3f} and")
        click.echo("  put a genuine match below any usable threshold.")
    click.echo("")


@cli.command()
@click.option("-i", "--cases", type=click.Path(path_type=Path), default=SETTINGS.cases)
@click.option("-k", "--truth", type=click.Path(path_type=Path), default=SETTINGS.truth)
def doctor(cases: Path, truth: Path) -> None:
    """Check configuration and inputs, and explain how to read the report."""
    click.echo("")
    click.echo("configuration")
    click.echo(f"  cases       {SETTINGS.cases}")
    click.echo(f"  truth       {SETTINGS.truth}")
    click.echo(f"  threshold   {SETTINGS.threshold}")
    click.echo(f"  linkage     {SETTINGS.linkage}")
    click.echo(f"  max_block   {SETTINGS.max_block}")
    click.echo("  no credentials, no network, no model: the accuracy problem here is")
    click.echo("  string normalisation and clustering, both of which are arithmetic")

    click.echo("")
    click.echo("inputs")
    failed = False
    if cases.exists():
        rows, problems = load_cases(cases)
        kept, duplicates = dedupe(rows)
        click.echo(f"  cases       {len(rows)} row(s) -> {len(kept)} distinct, "
                   f"{len(duplicates)} duplicate(s), {len(problems)} unreadable")
    else:
        click.echo(f"  cases       MISSING: {cases}")
        failed = True
    if truth.exists():
        known = load_truth(truth)
        click.echo(f"  truth       {len(known)} key(s) across "
                   f"{len(set(known.values()))} entit(ies)")
    else:
        click.echo(f"  truth       MISSING: {truth}")
        failed = True

    click.echo("")
    click.echo("how to read the report")
    click.echo("  unverifiable  the region declares no total. NOT the same as complete --")
    click.echo("                nothing can be concluded, and most regions are like this")
    click.echo("  gap_in_pages  a page was never seen. Cases on it are missing and no")
    click.echo("                threshold can recover them")
    click.echo("  chained       an entity holding a pair the matcher itself rejected. Only")
    click.echo("                single linkage can produce these")
    click.echo("  n/a           unmeasured, not zero. A metric over an empty denominator")
    click.echo("  pair          the ceiling on recall. A pair the blocker never offered")
    click.echo("  completeness  cannot be matched at any threshold")
    click.echo("")

    if failed:
        sys.exit(1)


cli.add_command(resolve_command, name="resolve")
