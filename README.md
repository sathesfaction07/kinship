# Kinship

The same case shows up three times: once per regional system, each copy fetched at a different
moment, the merchant's name spelled a little differently in each. Kinship collapses those
copies into one case, groups the cases into merchants, and tells you how far to trust the
grouping. Fuzzy name matching is not transitive, so "merge everything similar" can quietly chain
two different merchants together. The tool measures that instead of hiding it.

It is plain Python throughout: string normalisation, blocking and clustering, all deterministic.

---

## The Two Kinds of Duplicate Are Mathematically Different

**Exact-key dedup** is string equality on `(case_id, region)`. That is an equivalence relation — reflexive, symmetric, **transitive** — so grouping on it is order-independent, needs no threshold, and cannot go wrong.

**Fuzzy dedup** is not. Similarity is reflexive and symmetric, which makes it *look* like an equivalence relation, and it is not transitive:

```
'Northwind Trading' ~ 'Northwund Trading'   0.941  PASS
'Northwund Trading' ~ 'Northwund Troding'   0.941  PASS
'Northwund Troding' ~ 'Norihwund Troding'   0.941  PASS
'Norihwund Troding' ~ 'Norihwuad Troding'   0.941  PASS
'Northwind Trading' ~ 'Norihwuad Troding'   0.765  the matcher says NO
```

So "merge everything similar" is ill-defined without a linkage rule, and every rule gives something up. That is why the two are separate modules rather than one `dedup` step.

**And this is arithmetic, not bad luck.** One edit in a seventeen-character name costs 0.059. At a 0.85 threshold the budget is 0.15, so three edits break it — and any threshold below 1.0 admits a chain long enough.

---

## Two Source Documents Disagreed About the Key

One document said the composite key is `case_id + region + timestamp`. Another said `case_id + region`. That needed deciding rather than copying.

**The timestamp cannot be part of identity.** `observed_at` is when we *looked*, not what the case *is* — so the same case fetched twice gets two keys and dedup never fires. And the failure is silent in the worst direction: the run completes, the report shows more cases than exist, and nothing errors.

So identity is `(case_id, region)`, and the timestamp decides *which copy survives* — the most recent, because two observations of one case describe it at two moments and the later one is current. Keeping the first instead would report a stale status while still counting the case once, which is harder to notice than a wrong count.

The same `case_id` in two regions stays **two cases**. Regions issue identifiers independently.

---

## Blocking Sets a Ceiling No Matcher Can Exceed

Comparing all pairs of 10,000 cases is 49,995,000 edit distances. Blocking only compares cases sharing a cheap key — and silently loses any true pair that lands in two different blocks. **That loss is invisible in the output and unrecoverable at any threshold**, so it is measured separately:

| blocking key | pair completeness | true pairs never compared |
|:--|--:|--:|
| first 4 characters | 80.0% | 4 |
| every token | 85.0% | 3 |
| **both** | **95.0%** | **1** |

The one pair even the combined key loses is `Nordwind AB` / `Norwind AB` — they diverge at character 4 and share no token once the legal form is stripped. Nothing here can offer that pair, and the report says so rather than reporting a matcher failure.

Measuring this separately is the only way to tell *"the matcher is too strict"* from *"the matcher never saw the pair"*.

---

## Three Linkage Rules, and No Rule Wins

```
    rule      entities  largest  chained  order
    single          40        5        1  stable
    complete        41        3        0  stable
    canonical       46        2        0  stable
```

| rule | pairwise P | pairwise R | pairwise F1 | cluster F1 | chains? |
|:--|--:|--:|--:|--:|:--|
| single | 70.0% | 70.0% | 0.700 | **0.805** | **yes — 1** |
| complete | 78.6% | 55.0% | 0.647 | 0.795 | no |
| canonical | **100%** | 35.0% | 0.519 | 0.723 | no |

**The rule with the best cluster F1 is the only one that chains, and it is not the most precise one.** There is no dominant choice, so all three are reported rather than one being picked and the trade hidden. A test asserts this non-dominance, so if the corpus ever changes such that one rule wins outright, the build says so.

`single` wins the stated preference because it ranks best on cluster F1 among order-stable rules — and the report prints its chained entity, with the offending score, immediately above.

---

## The Chain Costs Exactly Six Pairs

Single linkage's entire precision loss is one group of five near-identical names:

```
  CHAINED -- contains a pair the matcher itself rejected

    weakest=0.824 < threshold 0.85: 'Palisadr Holdings' (5 members)
```

Those five span two genuinely different merchants. The entity implies 10 pairs; 4 are real; **6 are wrong — which is precisely single linkage's false-positive count.** Not diffuse noise, one chain. There is a test asserting the two numbers are equal.

`weakest_internal_score` is what exposes it: a value below the threshold means the entity contains a pair the matcher explicitly rejected. It is `None` for a singleton — not 1.0, because a singleton has no internal pair and reporting 1.0 would claim agreement that was never measured.

---

## Running

```bash
bash setup.sh                 # derives the Unicode cases and the chain, not just installs
source .venv/bin/activate

kinship doctor
kinship resolve -i data/cases.jsonl
kinship resolve -i data/cases.jsonl -l complete       # see what chaining costs
kinship compare -i data/cases.jsonl -k data/truth.jsonl
kinship compare -i data/cases.jsonl -k data/truth.jsonl -b prefix
kinship explain "ACME Foods" "Foods ACME"
```

| Command | Purpose | Exit |
|:--|:--|:--|
| `resolve` | Dedupe, then group into entities | **1** on a page gap or a shortfall |
| `compare` | Every linkage rule scored, plus order stability | **1** if no rule is stable and measurable |
| `explain` | Why two names score as they do | 0 |
| `doctor` | Config, inputs, and how to read the vocabulary | 1 if inputs missing |

---

## Completeness Is Not a Boolean

| Verdict | Meaning |
|:--|:--|
| `complete` | a declared total exists and was matched |
| `short` | a declared total exists and was not matched |
| `gap_in_pages` | page numbers are not contiguous. **Cases were definitely missed** |
| `unverifiable` | no declared total. Nothing can be concluded |

```
  [gap_in_pages ] fe: 9 case(s), declared 14, pages [1, 3]
                    page(s) [2] were never seen. Any case on them is missing and NO
                    matcher can find it.
  [unverifiable ] eu: 21 case(s), declared none declared, pages [1, 2, 3]
                    the region declares no total, so whether anything was missed CANNOT
                    be determined from this data.
  [complete     ] na: 23 case(s), declared 23, pages [1, 2, 3, 4]
```

`gap_in_pages` outranks the totals: a missing page *proves* loss, while a total mismatch has innocent explanations. It is also the only completeness signal available with no declared total — which is most regions.

`resolve` exits non-zero for a gap or a shortfall, but **not** for `unverifiable`, because a check that always fires carries no information.

---

## Testing

```bash
pytest                          # 107 tests, ~0.4s
pytest --cov=case_search --cov-report=term-missing
```

The property tests run against `data/`, so editing the corpus and breaking an invariant fails the build. Two exist to stop the others being vacuous: every region must be exercised, and the corpus must contain both singletons and multi-member entities. A pre-commit hook and a CI step both verify every truth key names a case that actually survives dedup — an orphan there would make every recall figure quietly wrong.

Nothing is mocked, because nothing talks to a network.

---

## Tradeoffs and Decisions I Made Along the Way

**`str.lower()` is not enough, and each Unicode omission loses a different real case.** These come from spreadsheet exports and copied web pages, which is where merchant names come from:

| input | needs | without it |
|:--|:--|:--|
| `ＡＣＭＥ Corp` (fullwidth) | NFKC | stays fullwidth, never matches `ACME` |
| `Weißbräu` vs `Weissbräu` | **casefold** | `lower()` keeps `ß`, so the two never agree |
| `ACME\xa0Corp` (non-breaking space) | NFKC | not a space, so tokens differ |
| `Cafe\u0301 Noir` vs `Café Noir` | NFKC | **10 chars vs 9, for identical glyphs** |

That last row is why normalisation happens *before* similarity: an edit distance across two Unicode spellings of the same glyphs reports a difference a human cannot see.

**I suspected `difflib.SequenceMatcher` was asymmetric and checked rather than asserting. It is not** — 0 of 8 probe pairs. So the reason I wrote my own edit distance is not asymmetry; it is that Ratcliff-Obershelp is not an edit distance at all, and the number needed to be one I could reason about per-edit. Having a per-edit cost of exactly `1/length` is what makes the chain analysis above arithmetic rather than empirical.

**A docstring number I wrote from intuition was wrong, and this is the third time across these projects.** I claimed the character similarity of `ACME Foods` against `Foods ACME` was 0.40. It is **0.00** — both canonical forms are ten characters differing at every position, so there is no partial credit whatsoever. The measurement corrected me, and the corrected figure makes the argument *stronger*: an average of character and token similarity would score a genuine match at 0.50, below any usable threshold. Hence the maximum rather than the average.

**Taking the maximum buys recall with precision, and the docs say so.** The two measures fail on opposite inputs — character distance on reordered tokens, token overlap on a typo inside a token — so each is evidence *for* a match and neither is evidence against. But a maximum can only raise scores, so it raises false positives too. The measurement is what decides whether that paid.

**Jaccard, not containment.** Containment scores `ACME` against `ACME Foods Holdings International` a perfect 1.0, because every token of the first appears in the second. Those are plausibly different entities, and a metric that cannot distinguish them is worse than useless — it is confidently wrong.

**Complete linkage was order-dependent, and my fix for it made accuracy worse.** The obvious greedy implementation takes the first qualifying merge, which depends on row order. It produced **three different partitions across six shuffles**, and the divergence landed exactly on the group that matters:

```
partition A   {Palisade Holdings, Palisadr Holdings, Palisadr Holdimgs}
              {Palisadr Holdimgz, Palisade Holdingz}          matches the truth
partition B   {Palisade Holdings, Palisadr Holdings, Palisade Holdingz}
              {Palisadr Holdimgs, Palisadr Holdimgz}          does not
```

So it was sometimes exactly right and sometimes wrong on nothing but row order. Making the merge choice deterministic — strongest weakest-link, ties broken on sorted keys — cost real accuracy:

| | pairwise F1 | cluster F1 | order |
|:--|--:|--:|:--|
| first-qualifying-merge | **0.824** | **0.846** | 3 partitions / 6 shuffles |
| strongest-weakest-link | 0.647 | 0.795 | stable |

**I kept the worse one.** An unreproducible 0.846 is not a measurement — it is one ordering's luck, and the next run would report something else. Reproducibly mediocre beats unreproducibly good when the number is the point. The deterministic rule now groups by *which typo the names share* rather than by merchant, and that is a real cost, stated rather than hidden.

**Which also means the ground truth for that chain is itself a judgement call.** Splitting five near-identical spellings into 3+2 is my assertion, and a careful human might split it differently. That is not a weakness of the corpus — it is the *point*. When the correct answer is genuinely unclear, an algorithm that confidently merges all five is asserting something unverifiable, and `weakest_internal_score` is what stops that assertion being silent.

**Singletons are kept.** Lookalike, a sibling project, drops groups of size 1 from its output. That would be wrong here: a merchant seen once is a real entity, and dropping singletons changes the denominator of every metric and makes the entity count incomparable to a truth set.

**Both pairwise and cluster-level scoring are reported.** Pairwise is the entity-resolution convention and it forgives a nearly-right entity — one wrong merge costs only the pairs it wrongly creates. Cluster scoring demands exact set equality. A single wrong merge between two large groups is cheap pairwise and catastrophic by cluster, which is *precisely* the failure single linkage produces, so reporting only pairwise F1 would hide it.

**Precision, recall and F1 are `float | None`.** Precision is undefined when nothing was predicted; recall when there was nothing to find. A partition into singletons implies no pairs at all, so pairwise precision comes back `None` — the run made no claims, which is different from making wrong ones. F1 guards the both-zero case separately, because two defined-but-zero inputs give a real zero.

**`summarise` excludes order-unstable rules outright rather than penalising them,** and returns `None` when nothing qualifies rather than a fabricated default. A partition that changes when you shuffle the input is not a candidate at any accuracy.

**`strip_legal_form` only touches trailing tokens.** `Co` appears inside real names (`Co-op Foods`), so a global removal would maul them. And a name that is *only* a legal form (`Limited`) keeps its last token, because stripping it to nothing would make it identical to every other such name.

**Oversized blocks are returned, not logged.** A block above the size cap is a deliberate recall sacrifice, so the report has to be able to say which ones — dropping them silently would make the ceiling unmeasurable.

**Union-find is iterative.** Recursive path compression blows the stack on a long chain, and a long chain is exactly what single linkage produces on near-duplicate names — so the recursive form fails on the input this exists to handle.

**No Excel pivot tables, despite the project brief asking.** CSV and JSON are what a pivot table is built *from*, and they assert in CI. A pivot table cannot.

---

## Configuration

```bash
CS_CASES=data/cases.jsonl
CS_TRUTH=data/truth.jsonl
CS_THRESHOLD=0.85
CS_LINKAGE=single
CS_MAX_BLOCK=60
```

---

## Stack

| Concern | Choice |
|:--|:--|
| Language | Python 3.11+ |
| Models + config | Pydantic, Pydantic Settings |
| CLI | Click |
| Output | text report, JSON, CSV |
| Tests | pytest, Click's CliRunner |
| Lint | ruff |
| Models/AI | none, deliberately |

The corpus is synthetic and reproducible via `data/generate.py`: 53 cases across three regions and 37 entities, planted with the local legal forms, Unicode spellings, reordered tokens, re-observations, a shared `case_id` across regions, a chain of near-misses, a pair no blocking key can offer, and one dropped page. Nothing is arranged to flatter the matcher — the chain that defeats single linkage is there because typo chains happen.
