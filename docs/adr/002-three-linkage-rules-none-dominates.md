# ADR 002: Three linkage rules, all reported, because none dominates

## Status

Accepted

## Context

Once similarity has produced a set of matching pairs, those pairs have to become a set of
entities. Because similarity is not transitive (ADR 001), that step needs a rule — and there is
no universally correct one. The three candidates each fail differently:

- **Single linkage** merges on any link. Maximum recall, and it **chains**: a run of
  near-misses joins two entities through intermediates.
- **Complete linkage** merges only cliques. Cannot chain, and it splits a genuine entity whose
  members are written too differently from each other.
- **Canonical grouping** ignores the fuzzy pairs entirely and groups on exact canonical form.
  Transitive, order-independent, threshold-free — and it misses anything canonicalisation
  cannot reach.

Shipping one and reporting a single F1 hides the choice. Worse, it makes the choice
unreviewable: with one number there is no way to tell whether the rule is discarding real
matches or correctly refusing to merge.

There is also a constraint that turns out to be a genuine trilemma. **No chaining,
order-independence, and one group per case cannot all hold at once.** Maximal cliques would
relax the third, and then a case belongs to several groups, the output stops being a partition,
and every downstream count becomes ambiguous.

## Decision

**All three rules are implemented, scored two ways each, and tested for order stability. The
report shows every rule side by side.**

### Two scoring schemes, because they disagree and the disagreement is the finding

| scheme | behaviour |
|:--|:--|
| pairwise | over the pairs the entities imply. Forgiving — one wrong merge costs only the pairs it wrongly creates |
| cluster | exact set equality against a true entity. Unforgiving — one wrong member and the entity is wrong |

A single wrong merge between two large entities is cheap pairwise and catastrophic by cluster,
which is *precisely* the failure single linkage produces. Reporting only pairwise F1 — the
convention in entity-resolution work — would hide it.

### Order stability is measured, and disqualifies rather than penalises

`summarise()` ranks on cluster F1 and excludes order-unstable rules **outright**. A partition
that changes when you shuffle the input is not a candidate at any accuracy, because its
reported accuracy is a property of the ordering rather than of the method. It returns `None`
when nothing qualifies, so the caller handles "no answer" instead of receiving a fabricated one.

### `weakest_internal_score` makes chaining visible

Carried on every `Entity`: the lowest pairwise score inside it, or `None` for a singleton —
not 1.0, because a singleton has no internal pair and 1.0 would claim agreement never measured.
A value **below the threshold** means the entity contains a pair the matcher itself rejected,
which is direct evidence of chaining rather than an inference.

### Singletons are kept

A merchant seen once is a real entity. Dropping singletons — as a sibling project in this
portfolio does for image clusters — would change the denominator of every metric and make the
entity count incomparable to a truth set.

## The bug, and a fix that made accuracy worse

The obvious greedy complete-linkage implementation takes the **first** qualifying merge. That
depends on row order, and it produced **three different partitions across six shuffles**,
diverging on exactly the group that matters:

```
partition A   {Palisade Holdings, Palisadr Holdings, Palisadr Holdimgs}
              {Palisadr Holdimgz, Palisade Holdingz}          matches the truth
partition B   {Palisade Holdings, Palisadr Holdings, Palisade Holdingz}
              {Palisadr Holdimgs, Palisadr Holdimgz}          does not
```

Sometimes exactly right, sometimes wrong, on nothing but row order.

The fix takes the *best* merge — the pair of groups whose weakest cross-link is strongest, ties
broken on sorted member keys (arbitrary, but total, which is what determinism requires):

| | pairwise F1 | cluster F1 | order |
|:--|--:|--:|:--|
| first-qualifying-merge | **0.824** | **0.846** | 3 partitions / 6 shuffles |
| strongest-weakest-link | 0.647 | 0.795 | stable |

**The worse one was kept**, because an unreproducible 0.846 is not a measurement — it is one
ordering's luck, and the next run reports something else. Reproducibly mediocre beats
unreproducibly good when the number is the point.

The cost is real and named: the deterministic rule groups by *which typo the names share*
rather than by merchant.

## The result: no rule dominates

| rule | pairwise P | pairwise R | pairwise F1 | cluster F1 | chains? |
|:--|--:|--:|--:|--:|:--|
| single | 70.0% | 70.0% | 0.700 | **0.805** | **yes — 1** |
| complete | 78.6% | 55.0% | 0.647 | 0.795 | no |
| canonical | **100%** | 35.0% | 0.519 | 0.723 | no |

The rule with the best cluster F1 is **the only one that chains**, and it is **not** the most
precise. A test asserts this non-dominance, so if the corpus ever changes such that one rule
wins outright, the build says so.

Single linkage's chained entity has five members and therefore ten implied pairs. Four are
real; **six are wrong — exactly its false-positive count.** Its entire precision loss is one
chain, not diffuse noise, and there is a test asserting the two numbers are equal.

### Which means the ground truth for that chain is itself a judgement

Splitting five near-identical spellings 3+2 is an assertion, and a careful human might split it
differently. That is not a weakness of the corpus, it is the point: when the correct answer is
genuinely unclear, an algorithm that confidently merges all five is asserting something
unverifiable. `weakest_internal_score` is what stops that assertion being silent.

## Validation

- `TestLinkageRules` — single chains and the weakest score proves it; complete and canonical
  cannot chain by construction; every rule order-independent across six shuffles; singletons
  kept; `None` for a singleton's weakest score
- `TestMeasurement` — both `None` cases; a real zero F1 distinguished from unmeasured; a
  singleton partition implying no pairs; pairwise forgiving what cluster scoring does not;
  `summarise` excluding an unstable rule that scores *better*
- `TestPropertiesOverTheShippedCorpus` — exactly one chained entity of size five; all six false
  pairs traced to it; canonical grouping at precision 1.0 and recall below 0.4; **no rule
  dominating**; order independence over the whole corpus
- `setup.sh` and CI both re-derive the whole chain on a fresh clone

## Consequences

**Good**

- The precision/recall trade is visible across three rules on the same input, so the choice is
  reviewable rather than baked in
- Chaining is reported with the score that proves it, not merely counted
- An order-unstable rule cannot win, so a reported figure is reproducible
- Cluster-level scoring surfaces the failure pairwise F1 hides
- Keeping singletons keeps the entity count comparable to a truth set

**Bad**

- Three rules and two scoring schemes is six numbers, and a reader wanting one answer has to be
  told which. `summarise` gives one, and it is the number most likely to be quoted without its
  caveat
- The preferred rule is the one that chains. That is the honest reading of the data and it is
  an uncomfortable recommendation
- Complete linkage is `O(n^2)` per merge round and re-scans after every merge, so it is much
  slower than union-find. Fine at this scale, and it would need work at 10,000 cases
- `summarise` ranks on cluster F1 with `-chained` only as a tie-break, so a rule could win
  while chaining if its F1 were high enough. The count is printed, but the ranking does not
  weight it

**Neutral**

- `Linkage` is a `StrEnum` so the value round-trips through JSON and the CLI without mapping
- `display_name` takes the longest member name with alphabetical tie-breaking. Presentation
  only, never compared against
- Canonical grouping receives the fuzzy pairs and ignores them, keeping one uniform signature
  across the three rules at the cost of an unused argument
- `MAX_BLOCK` interacts with linkage: a skipped block means pairs were never offered, so a rule
  cannot merge what blocking withheld. The two limits are reported separately so they are not
  confused
