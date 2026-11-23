# Kinship — Technical Architecture

## Overview

Deduplicates cases across three regions, then groups the survivors into merchants and measures
whether the grouping is right.

**Design principle:** exact-key dedup and fuzzy entity resolution are mathematically different
operations and are kept in separate modules. The first is an equivalence relation; the second
is not, and pretending otherwise is where entity resolution goes wrong.

No model — the accuracy problem is string normalisation and clustering, both arithmetic.

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          CLI (kinship)                                    │
│                                                                              │
│  resolve  dedupe on (case_id, region) -> group into entities                   │
│           EXIT 1 on a DEMONSTRABLE problem: a page gap or a shortfall.         │
│           EXIT 0 on a region declaring no total -- most do, so failing on      │
│           those would fail always and mean nothing                            │
│  compare  every linkage rule scored, plus ORDER STABILITY across shuffles      │
│           EXIT 1 only if no rule is both stable and measurable                 │
│  explain  why two names score as they do, step by step                         │
│  doctor   config, inputs, and what `unverifiable` and `chained` mean            │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                           normalise.py                                        │
│                                                                              │
│  fold()  NFKC -> casefold -> collapse whitespace. NONE of these is optional:   │
│    'ACME Corp' fullwidth      needs NFKC     casefold leaves it fullwidth      │
│    'Weissbrau' vs sharp-s     needs CASEFOLD str.lower() keeps the sharp s     │
│    'ACME\xa0Corp'             needs NFKC     a non-breaking space is not a     │
│                                              space, so the tokens differ       │
│    'Cafe'+U+0301 vs 'Cafe'    needs NFKC     10 chars vs 9 for IDENTICAL       │
│                                              glyphs -- so an edit distance on  │
│                                              the raw strings reports a         │
│                                              difference a human cannot see     │
│    -> normalisation happens BEFORE similarity, never after                     │
│                                                                              │
│  canonical() = fold -> strip punctuation -> strip TRAILING legal forms ->      │
│                strip leading article                                           │
│    an EQUIVALENCE RELATION: string equality on a derived value, so it is       │
│    transitive and order-independent. Exactly what similarity is not.           │
│    'co' is only stripped from the TAIL -- 'Co-op Foods' would be mauled        │
│    'Limited' keeps its last token -- stripping to nothing merges everything    │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │
        ┌────────────────────────┴────────────────────────┐
        ▼                                                 ▼
┌────────────────────────────────┐   ┌──────────────────────────────────────┐
│        similarity.py           │   │              dedupe.py               │
│                                │   │                                      │
│  edit_distance   two rows, not │   │  identity is (case_id, region).      │
│    a matrix: O(min(n,m)) space │   │  DELIBERATELY NOT the timestamp --    │
│                                │   │  the spec contradicted itself and     │
│  character  1 - dist/LONGEST   │   │  including it means the same case     │
│    longest, not average: 'ab'  │   │  re-fetched gets two keys and dedup   │
│    vs 'abcdefgh' is 0.25 not   │   │  NEVER FIRES. Silent in the worst     │
│    0.40, and short-vs-long is  │   │  direction: run completes, count is   │
│    a TRUNCATED FIELD not a     │   │  too high, nothing errors.            │
│    match                       │   │                                      │
│                                │   │  RECENCY picks the survivor: two      │
│  token      Jaccard, NOT       │   │  observations describe one case at     │
│    containment. Containment    │   │  two moments; the later is current.   │
│    scores 'ACME' vs 'ACME      │   │  Keeping the first gives a RIGHT       │
│    Foods Holdings Intl' at 1.0 │   │  count with STALE content -- harder    │
│                                │   │  to notice than a wrong count.        │
│  similarity  the MAXIMUM.      │   │                                      │
│    The two fail on OPPOSITE    │   │  audit_pages() 4 verdicts:            │
│    inputs, so each is evidence │   │    complete | short | gap_in_pages |  │
│    FOR and neither is against. │   │    unverifiable                       │
│    'ACME Foods' vs 'Foods      │   │  GAP outranks totals: a missing page  │
│    ACME' -> char 0.00 (!),     │   │    PROVES loss; a total mismatch has  │
│    token 1.00. An AVERAGE      │   │    innocent explanations              │
│    would score a real match    │   │  UNVERIFIABLE is the COMMON case and  │
│    0.50, below any threshold.  │   │    is not the same as complete        │
│    Cost: a max can only raise  │   └──────────────────────────────────────┘
│    scores, so it raises FPs.   │
└────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              block.py                                         │
│                                                                              │
│  All pairs of 10,000 cases = 49,995,000 edit distances. Blocking cuts that    │
│  down -- and LOSES any true pair landing in two different blocks.             │
│                                                                              │
│  **That loss is invisible in the output and unrecoverable at ANY threshold.**  │
│  So it is measured separately as pair_completeness, the CEILING on recall:     │
│                                                                              │
│    prefix_key    first 4 chars       80.0%   4 true pairs never compared      │
│    token_key     every token         85.0%   3                                │
│    combined_key  UNION of both       95.0%   1                                │
│                                                                              │
│  Union, not intersection: each key is an independent chance to be compared,   │
│  so adding keys can only help. Intersecting would need agreement on both.     │
│                                                                              │
│  The one pair even the union loses: 'Nordwind AB' / 'Norwind AB' -- diverging │
│  at char 4 AND sharing no token. Reported, not silently absorbed.            │
│                                                                              │
│  pair_completeness returns None over zero true pairs, not 1.0: perfect        │
│  completeness over no opportunities is an absence of evidence.                │
│                                                                              │
│  MAX_BLOCK caps a block at 60. Skipped blocks are RETURNED, because a skip is │
│  a deliberate recall sacrifice the report must be able to name.               │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                             cluster.py                                        │
│                                                                              │
│  Similarity is reflexive and symmetric -- so it LOOKS like an equivalence     │
│  relation -- and is NOT transitive:                                           │
│                                                                              │
│    'Northwind Trading' ~ 'Northwund Trading'  0.941   PASS                     │
│    'Northwund Trading' ~ 'Northwund Troding'  0.941   PASS                     │
│    'Northwund Troding' ~ 'Norihwund Troding'  0.941   PASS                     │
│    'Norihwund Troding' ~ 'Norihwuad Troding'  0.941   PASS                     │
│    'Northwind Trading' ~ 'Norihwuad Troding'  0.765   the matcher says NO      │
│                                                                              │
│  ARITHMETIC, not luck: one edit in 17 chars costs 0.059, the budget at 0.85   │
│  is 0.15, so 3 edits break it. Any threshold below 1.0 admits a long chain.   │
│                                                                              │
│  SINGLE     transitive closure via ITERATIVE union-find (recursion blows the  │
│             stack on a long chain -- the exact input this handles). Order-     │
│             independent, and it CHAINS.                                       │
│  COMPLETE   cliques. Cannot chain by construction. See the bug below.         │
│  CANONICAL  exact canonical form. Transitive, order-independent, threshold-    │
│             free -- the only rule with all three. Included as a BASELINE, to  │
│             ask whether the fuzzy machinery earns its keep.                    │
│                                                                              │
│  SINGLETONS ARE KEPT. A merchant seen once is a real entity; dropping them    │
│  changes the denominator of every metric.                                     │
│                                                                              │
│  weakest_internal_score = None for a singleton, NOT 1.0: no internal pair     │
│  exists, and 1.0 would claim agreement never measured. Below the threshold it │
│  is direct proof of chaining.                                                 │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│               measure.py                          render.py                   │
│                                                                              │
│  PAIRWISE  over implied pairs. Forgiving:    n/a NEVER 0.0%, null NEVER 0,     │
│    one wrong merge costs only the pairs it     empty CSV cell NEVER 0          │
│    creates. The ER convention.               a CHAINED entity is shown with    │
│  CLUSTER   exact set equality. Unforgiving.    the score that proves it        │
│    One wrong member and the entity is         rather than merely counted       │
│    wrong.                                    completeness printed WORST-FIRST  │
│                                                                              │
│  A single wrong merge is cheap pairwise and CATASTROPHIC by cluster -- which  │
│  is exactly what single linkage produces. Reporting only pairwise F1 hides it.│
│                                                                              │
│  P/R/F1 are float | None. F1 guards both-zero SEPARATELY from None, because   │
│  two defined-but-zero inputs give a real zero.                                │
│                                                                              │
│  summarise() ranks on CLUSTER F1 and EXCLUDES order-unstable rules outright   │
│    rather than penalising them. Returns None when nothing qualifies.          │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## The Bug: Complete Linkage Was Order-Dependent, and Fixing It Cost Accuracy

The obvious greedy implementation scans candidate merges and takes the **first** that
qualifies. That depends on row order, and it produced **three different partitions across six
shuffles** of identical input — diverging precisely on the group that matters:

```
partition A   {Palisade Holdings, Palisadr Holdings, Palisadr Holdimgs}
              {Palisadr Holdimgz, Palisade Holdingz}          matches the truth
partition B   {Palisade Holdings, Palisadr Holdings, Palisade Holdingz}
              {Palisadr Holdimgs, Palisadr Holdimgz}          does not
```

Sometimes exactly right, sometimes wrong, on nothing but row order. **So its reported accuracy
was an artefact of one ordering rather than a property of the method.**

The fix takes the *best* merge instead — the pair of groups whose weakest cross-link is
strongest, ties broken on sorted member keys. Arbitrary but total, which is what determinism
requires. And it cost real accuracy:

| | pairwise F1 | cluster F1 | order |
|:--|--:|--:|:--|
| first-qualifying-merge | **0.824** | **0.846** | 3 partitions / 6 shuffles |
| strongest-weakest-link | 0.647 | 0.795 | stable |

**The worse one was kept.** An unreproducible 0.846 is not a measurement; it is one ordering's
luck and the next run reports something else. The deterministic rule groups by *which typo the
names share* rather than by merchant — a real cost, stated rather than hidden.

### And the trilemma is genuine

No chaining, order-independence, and one group per case cannot all hold at once. Single gives
up the first, complete gave up the second until forced, and canonical declines to merge at all
without exact agreement. Maximal cliques would let a case belong to several groups, so the
output stops being a partition and every count becomes ambiguous.

---

## Data Flow

```
1.  kinship compare -i data/cases.jsonl -k data/truth.jsonl
2.  load 56 rows; 53 known assignments across 37 entities
3.  dedupe on (case_id, region) -> 53 distinct, 3 dropped
      each survivor is the LATER observation, carrying the updated 'closed' status
      one case_id present in NA and EU stayed TWO cases
4.  audit_pages per region
      na  complete      23 seen, declared 23, pages [1,2,3,4]
      eu  unverifiable  21 seen, NO declared total, pages [1,2,3]
      fe  gap_in_pages   9 seen, declared 14, pages [1,3]   <- page 2 never arrived
5.  pair_completeness -> 95.0%, losing exactly ['Nordwind AB', 'Norwind AB']
6.  for each linkage rule: score pairs, cluster, score twice, shuffle 6x
      single     40 entities  chained=1  pairwise 0.700  cluster 0.805
      complete   41 entities  chained=0  pairwise 0.647  cluster 0.795
      canonical  46 entities  chained=0  pairwise 0.519  cluster 0.723
7.  all three order-stable
8.  preferred = single, on cluster F1 among stable rules -- with its chained entity
      printed directly above, so the trade is visible rather than buried
9.  exit 0: a preference was available
```

### The finding that makes the chain concrete

Single linkage's chained entity has 5 members, so 10 implied pairs. Four are real (3 in
entity A, 1 in entity B). **Six are wrong — precisely its false-positive count.** Its entire
precision loss is one chain of five near-identical names, not diffuse noise. A test asserts
the two numbers are equal, so if that ever stops being true the build says so.

---

## Testing

```bash
pytest                          # 107 tests, ~0.4s
pytest --cov=case_search --cov-report=term-missing
```

| File | Covers |
|:--|:--|
| `conftest.py` | the corpus as session fixtures; `make_case` so a test names only what it is about |
| `test_normalise_similarity.py` | each Unicode step individually; the equivalence-relation property; longest-not-average scaling; Jaccard-not-containment; the zero character score; **the chain, asserted as arithmetic** |
| `test_dedupe_block_cluster.py` | timestamp excluded from identity; recency regardless of arrival order; all four completeness verdicts and the precedence; the blocking ceiling per key; all three linkage rules including order-independence; both `None` cases; pairwise-vs-cluster disagreement; `n/a`/null/empty rendering; the corpus properties |
| `integration/test_cli.py` | all four commands, both exit-code reasons, JSON and CSV contents |

The property tests run against `data/`, so editing the corpus and breaking an invariant fails
the build. Two exist to stop the others being vacuous: every region must be exercised, and the
corpus must hold both singletons and multi-member entities. A pre-commit hook and a CI step
both verify every truth key names a case that survives dedup — an orphan would make every
recall figure quietly wrong.

Nothing is mocked, because nothing talks to a network.

---

## Configuration

`CS_`-prefixed environment variables or a `.env` file. No list-typed fields, since Pydantic
Settings parses those as JSON and `CS_REGIONS=na,eu` would fail with a decode error reading
like a bug in the tool.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CS_", env_file=".env", extra="ignore")

    cases: Path = Path("data/cases.jsonl")
    truth: Path = Path("data/truth.jsonl")
    threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    linkage: str = "single"
    max_block: int = Field(default=60, ge=2)
```

`threshold` is bounded by the model rather than checked at the call site, so an out-of-range
value fails at load with a message naming the field instead of silently matching everything
or nothing.
