#!/usr/bin/env bash
set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${BLUE}==>${NC} $1"; }
ok()   { echo -e "${GREEN} ok ${NC} $1"; }
fail() { echo -e "${RED}fail${NC} $1"; exit 1; }

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            cat <<'USAGE'
Usage: bash setup.sh

No options, no credentials, no network, no model. The accuracy problem here is string
normalisation and clustering, and both are arithmetic.
USAGE
            exit 0 ;;
        *) fail "unknown option: $arg (try --help)" ;;
    esac
done

info "Checking Python"
command -v python3 >/dev/null 2>&1 || fail "python3 not found (macOS: brew install python@3.12)"
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
[ "$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')" = "1" ] \
    || fail "Python 3.11+ required (StrEnum, itertools.pairwise). Found $PY_VER."
ok "Python $PY_VER"

info "Creating virtual environment"
[ -d .venv ] || python3 -m venv .venv || fail "could not create .venv"
ok ".venv ready"

info "Installing dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[dev]" || fail "pip install failed"
ok "dependencies installed"

info "Verifying imports"
.venv/bin/python - <<'PYCHECK' || fail "core imports failed"
import click                                              # noqa: F401
from pydantic_settings import BaseSettings                 # noqa: F401
from case_search.cluster import resolve                    # noqa: F401
from case_search.dedupe import dedupe                      # noqa: F401
from case_search.measure import pairwise                   # noqa: F401
from case_search.normalise import canonical                # noqa: F401
PYCHECK
ok "imports resolve"

info "Deriving what each normalisation step is for, rather than asserting it"
.venv/bin/python - <<'PYCHECK' || fail "a normalisation claim does not hold"
from case_search.normalise import canonical, fold

# Every row is defeated by omitting ONE step. Nothing here is decorative.
cases = [
    ("fullwidth letters", "\uff21\uff23\uff2d\uff25 Corp", "acme corp"),
    ("sharp s",           "Wei\u00dfbr\u00e4u",            "weissbr\u00e4u"),
    ("non-breaking space","ACME\u00a0Corp",                "acme corp"),
]
for label, raw, expected in cases:
    got = fold(raw)
    assert got == expected, f"{label}: {got!r} != {expected!r}"
    print(f"     {label:<20} {raw!r} -> {got!r}")

# str.lower() is NOT enough, and this is the single most cited reason why.
assert "Wei\u00dfbr\u00e4u".lower() == "wei\u00dfbr\u00e4u"
assert fold("Wei\u00dfbr\u00e4u") == fold("Weissbr\u00e4u")
print("     str.lower() keeps the sharp s; casefold maps it to 'ss', so the German and")
print("       the transliterated spelling agree only under casefold")

# Two spellings a human cannot tell apart, at different LENGTHS. Any edit distance on the
# raw strings therefore reports a difference that does not exist.
decomposed, precomposed = "Cafe\u0301 Noir", "Caf\u00e9 Noir"
assert len(decomposed) == 10 and len(precomposed) == 9
assert decomposed.casefold() != precomposed.casefold()
assert fold(decomposed) == fold(precomposed)
print(f"     'Cafe'+U+0301 is {len(decomposed)} chars and the precomposed form is "
      f"{len(precomposed)}, for IDENTICAL glyphs")
print("       -> normalisation must happen BEFORE similarity, never after")

# Canonicalisation is an EQUIVALENCE RELATION: string equality on a derived value, so it is
# transitive and order-independent. This is exactly the property fuzzy similarity lacks.
family = ["ACME Corp", "acme, inc.", "The ACME Ltd", "ACME"]
forms = {canonical(name) for name in family}
assert forms == {"acme"}, forms
print(f"     {len(family)} spellings collapse to one canonical form: {forms.pop()!r}")
PYCHECK
ok "normalisation behaves as documented"

info "Deriving the non-transitivity that forces a linkage decision"
.venv/bin/python - <<'PYCHECK' || fail "the chain does not behave as documented"
from itertools import pairwise

from case_search.normalise import canonical
from case_search.similarity import similarity

THRESHOLD = 0.85
CHAIN = ["Northwind Trading", "Northwund Trading", "Northwund Troding",
         "Norihwund Troding", "Norihwuad Troding"]

for left, right in pairwise(CHAIN):
    score = similarity(left, right)
    assert score >= THRESHOLD, (left, right, score)
    print(f"     {left!r:20} ~ {right!r:20} {score:.3f}  PASS")

ends = similarity(CHAIN[0], CHAIN[-1])
assert ends < THRESHOLD, ends
print(f"     {CHAIN[0]!r:20} ~ {CHAIN[-1]!r:20} {ends:.3f}  the matcher says NO")
print()
print("     So similarity is reflexive and symmetric but NOT transitive, and therefore not")
print("     an equivalence relation. Single linkage merges all five anyway, including a")
print("     pair it explicitly rejects.")

# And it is arithmetic, not a corpus artefact: one edit in a 17-character name costs about
# 0.059, so four of them exceed any 0.15 budget. Any threshold below 1.0 admits a long
# enough chain.
length = len(canonical(CHAIN[0]))
per_edit = 1 / length
assert per_edit < (1 - THRESHOLD) < 4 * per_edit
print(f"     one edit in {length} characters costs {per_edit:.3f}; the budget at "
      f"threshold {THRESHOLD} is {1 - THRESHOLD:.3f}")
print(f"     -> {int((1 - THRESHOLD) // per_edit) + 1} edits break it, and the chain has 4")
PYCHECK
ok "non-transitivity derived from the numbers"

info "Measuring the shipped corpus"
.venv/bin/python - <<'PYCHECK' || fail "the corpus did not behave as documented"
import random
from pathlib import Path

from case_search.block import combined_key, pair_completeness, prefix_key, token_key
from case_search.cluster import chained_entities, resolve
from case_search.dedupe import audit_pages, dedupe
from case_search.measure import cluster_level, pairwise
from case_search.models import Completeness, Linkage, Region
from case_search.sources import load_cases, load_truth

THRESHOLD = 0.85
rows, problems = load_cases(Path("data/cases.jsonl"))
truth = load_truth(Path("data/truth.jsonl"))
assert not problems, problems
print(f"     {len(rows)} rows, {len(truth)} known assignments across "
      f"{len(set(truth.values()))} entities")

# Exact-key dedup: an equivalence relation, so no threshold and no linkage rule.
kept, dropped = dedupe(rows)
assert len(dropped) == 3, len(dropped)
assert sum(1 for case in kept if case.status == "closed") == 3
print(f"     dedup on (case_id, region): {len(rows)} -> {len(kept)}, {len(dropped)} dropped")
print("       the LATER observation survived each time, carrying the updated status")

# One case_id issued independently in two regions is two cases, not a duplicate.
shared = [case for case in kept if case.case_id == "C9001"]
assert len(shared) == 2 and {c.region for c in shared} == {Region.NA, Region.EU}
print("       one case_id present in two regions stayed TWO cases")

audits = {audit.region: audit for audit in audit_pages(kept)}
assert audits[Region.FE].verdict is Completeness.GAP_IN_PAGES
assert audits[Region.FE].missing_pages == (2,)
assert audits[Region.EU].verdict is Completeness.UNVERIFIABLE
assert audits[Region.NA].verdict is Completeness.COMPLETE
for region in (Region.FE, Region.EU, Region.NA):
    audit = audits[region]
    declared = "none" if audit.declared_total is None else audit.declared_total
    print(f"     {region.value}: {audit.verdict.value:<13} seen={audit.cases_seen:<3} "
          f"declared={declared:<5} pages={list(audit.pages_seen)}")
print("       eu is UNVERIFIABLE, which is not the same as complete -- nothing can be")
print("       concluded, and most regions in real data look like this")

print()
print("     blocking sets a CEILING on recall that no matcher can exceed:")
for label, key in [("prefix only", prefix_key), ("token only", token_key),
                   ("combined", combined_key)]:
    score, lost = pair_completeness(kept, truth, key=key)
    print(f"       {label:<12} {score:.1%}  ({len(lost)} true pair(s) never compared)")
combined_score, combined_lost = pair_completeness(kept, truth, key=combined_key)
prefix_score, _ = pair_completeness(kept, truth, key=prefix_key)
assert prefix_score < combined_score < 1.0
names = {case.key: case.merchant_name for case in kept}
assert len(combined_lost) == 1
spellings = sorted(names[key] for key in combined_lost[0])
assert spellings == ["Nordwind AB", "Norwind AB"], spellings
print(f"       the one pair even the combined key loses: {spellings}")
print("         they diverge at character 4 and share no token, so no key here offers them")

print()
print("     three linkage rules, same input, same threshold:")
results = {}
for linkage in Linkage:
    entities, _, _ = resolve(kept, threshold=THRESHOLD, linkage=linkage)
    pair_score = pairwise(entities, truth)
    cluster_score = cluster_level(entities, truth)
    chained = chained_entities(entities, THRESHOLD)
    results[linkage] = (pair_score, cluster_score, chained)
    print(f"       {linkage.value:<10} entities={len(entities):<3} chained={len(chained)}  "
          f"pairwise P={pair_score.precision:.3f} R={pair_score.recall:.3f} "
          f"F1={pair_score.f1:.3f}  clusterF1={cluster_score.f1:.3f}")

# Only single linkage chains, and ALL of its false pairs come from the one chain.
single_pairs, _, single_chained = results[Linkage.SINGLE]
assert len(single_chained) == 1 and single_chained[0].size == 5
inside = single_chained[0].members
wrong = sum(
    1
    for index, left in enumerate(sorted(inside))
    for right in sorted(inside)[index + 1:]
    if truth.get(left) != truth.get(right)
)
assert wrong == single_pairs.false_positives == 6, (wrong, single_pairs.false_positives)
print(f"       every one of single linkage's {single_pairs.false_positives} false pairs comes "
      "from that single chain of five")
print(f"         {[names[m] for m in sorted(inside)]}")

for linkage in (Linkage.COMPLETE, Linkage.CANONICAL):
    assert results[linkage][2] == []
assert results[Linkage.CANONICAL][0].precision == 1.0
assert results[Linkage.CANONICAL][0].recall < 0.4
print("       canonical grouping never merges wrongly (P=1.000) and finds barely a third")
print("         of the true pairs, which is what perfect precision costs here")

# No rule dominates: the best cluster F1 is also the only one that chains, and it is not the
# most precise. That is the honest headline.
best_cluster = max(results, key=lambda k: results[k][1].f1)
best_precision = max(results, key=lambda k: results[k][0].precision)
assert best_cluster is Linkage.SINGLE and results[best_cluster][2]
assert best_precision is not best_cluster
print(f"       best cluster F1 = {best_cluster.value} (and the ONLY one that chains)")
print(f"       best precision  = {best_precision.value}")
print("       -> no rule dominates, so all three are reported rather than one chosen")

print()
for linkage in Linkage:
    partitions = set()
    for seed in range(6):
        shuffled = list(kept)
        random.Random(seed).shuffle(shuffled)
        entities, _, _ = resolve(shuffled, threshold=THRESHOLD, linkage=linkage)
        partitions.add(tuple(sorted(tuple(e.members) for e in entities)))
    assert len(partitions) == 1, f"{linkage} is order-dependent"
print("     all three rules are order-independent across 6 shuffles")
print("       complete linkage was NOT, until the merge choice stopped being 'the first")
print("       that qualifies' and became 'the one with the strongest weakest link'")
PYCHECK
ok "the corpus behaves as documented"

info "Running the test suite"
.venv/bin/python -m pytest -q || fail "tests failed"
ok "tests pass"

cat <<'DONE'

Setup complete.

  source .venv/bin/activate

  case-search doctor
  case-search resolve -i data/cases.jsonl
  case-search resolve -i data/cases.jsonl -l complete       # see what chaining costs
  case-search compare -i data/cases.jsonl -k data/truth.jsonl
  case-search compare -i data/cases.jsonl -k data/truth.jsonl -b prefix
  case-search explain "ACME Foods" "Foods ACME"

`resolve` exits non-zero for a DEMONSTRABLE completeness problem -- a gap in page numbers or
a shortfall against a declared total. It does NOT fail on a region that declares no total,
because most regions are like that and the signal would be worthless. It says so in the
output instead: unverifiable is not the same as complete.

`compare` exits non-zero only when no linkage rule is both order-stable and measurable,
because a preference is then genuinely unavailable rather than merely close.
DONE
