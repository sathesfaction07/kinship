# ADR 001: Exact-key dedup and fuzzy entity resolution are separate operations

## Status

Accepted

## Context

"Deduplicate the cases" names two operations that are mathematically different, and blending
them into one step is where entity resolution goes wrong.

**Exact-key dedup** is string equality on a composite key. That is an equivalence relation —
reflexive, symmetric, and **transitive**. So grouping on it is order-independent, needs no
threshold, and cannot produce a surprising answer.

**Fuzzy matching** is reflexive and symmetric, which makes it *look* like an equivalence
relation. It is not transitive, and on real merchant names the failure is easy to produce:

```
'Northwind Trading' ~ 'Northwund Trading'   0.941  PASS
'Northwund Trading' ~ 'Northwund Troding'   0.941  PASS
'Northwund Troding' ~ 'Norihwund Troding'   0.941  PASS
'Norihwund Troding' ~ 'Norihwuad Troding'   0.941  PASS
'Northwind Trading' ~ 'Norihwuad Troding'   0.765  the matcher says NO
```

So "merge everything similar" is ill-defined without a rule for what to do with a chain, and
every available rule gives something up.

### The spec contradicted itself about the key

One document specified `case_id + region + timestamp`. Another specified `case_id + region`.
That needed deciding rather than copying.

### And the names arrive in several Unicode spellings

Merchant names come from spreadsheet exports and copied web pages, so they arrive with
fullwidth letters, non-breaking spaces, and both composed and decomposed accents.

## Decision

**Two modules. `dedupe.py` for the equivalence relation, `cluster.py` for the rule that needs
one, and `normalise.py` doing as much of the work as can be proved before either runs.**

### Identity is `(case_id, region)`, and the timestamp is not part of it

`observed_at` is when we *looked*, not what the case *is*. Including it gives the same case two
keys on a re-fetch, so dedup never fires — and the failure is silent in the worst direction:
the run completes, the report shows more cases than exist, and nothing errors.

The timestamp instead decides **which copy survives**: the most recent. Two observations
describe one case at two moments and the later one is current. Keeping the first would give a
*right count with stale content*, which is harder to notice than a wrong count.

The same `case_id` in two regions stays two cases, because regions issue identifiers
independently.

### Canonicalisation is the transitive part, and it does as much as it can

`canonical()` is `fold → strip punctuation → strip trailing legal forms → strip leading
article`. Because it is string equality on a derived value, it is transitive and
order-independent — the only grouping here with those properties for free.

`fold()` is NFKC then casefold, and **neither step is optional**:

| input | needs | without it |
|:--|:--|:--|
| `ＡＣＭＥ Corp` | NFKC | stays fullwidth, never matches `ACME` |
| `Weißbräu` vs `Weissbräu` | **casefold** | `str.lower()` keeps `ß`, so they never agree |
| `ACME\xa0Corp` | NFKC | not a space, so the tokens differ |
| `Cafe\u0301 Noir` vs `Café Noir` | NFKC | **10 characters vs 9, for identical glyphs** |

That last row is why normalisation runs *before* similarity: an edit distance across two
Unicode spellings of the same glyphs reports a difference a human cannot see.

Legal-form stripping is deliberately limited to **trailing** tokens, because `Co` appears
inside real names (`Co-op Foods`). And a name that is *only* a legal form (`Limited`) keeps its
last token, since stripping it to nothing would make it identical to every other such name.

### Similarity is the maximum of a character and a token measure

The two fail on **opposite** inputs, so each is evidence *for* a match and neither is evidence
against:

```
'ACME Foods' vs 'Foods ACME'   char 0.00   token 1.00   max 1.00   avg 0.50
'ACME Foods' vs 'ACME Fods'    char 0.90   token 0.33   max 0.90   avg 0.62
```

Both are the same merchant, and an average would put both on the wrong side of a plausible
0.85 threshold.

The first row is worth noting: the character score is not merely low, it is **zero** — both
canonical forms are ten characters differing at every position. I had written 0.40 in that
docstring from intuition and the measurement corrected me.

Jaccard rather than containment, because containment scores `ACME` against `ACME Foods
Holdings International` a perfect 1.0. Those are plausibly different entities, and a metric
that cannot distinguish them is confidently wrong rather than merely weak.

## Validation

- `TestFoldNeedsEveryStep` — each Unicode case individually, including `lower()` failing where
  `casefold()` succeeds, and the 10-versus-9 length assertion
- `TestCanonicalisation` — the equivalence-relation property over four spellings; the
  trailing-only and only-a-legal-form guards
- `TestSimilarity` — longest-not-average scaling; Jaccard-not-containment; the zero character
  score; symmetry and reflexivity
- `TestSimilarityIsNotTransitive` — every adjacent link passing, the ends failing, and the
  per-edit arithmetic showing this is not a corpus artefact
- `TestIdentityExcludesTheTimestamp`, `TestDedupe` — recency regardless of arrival order,
  cross-region non-duplicates, order independence
- `setup.sh` re-derives all of the above on a fresh clone

## Consequences

**Good**

- The easy half of the problem is provably correct and cannot be destabilised by a threshold
- A stale-content bug that keeps the count right is prevented by construction
- Each Unicode step has a named case that justifies it, so none is cargo-culted
- The `explain` command shows the fold, the canonical form, and both component scores, so a
  surprising match is a one-command investigation
- Taking the maximum recovers reordered tokens and interior typos, which an average loses

**Bad**

- Taking the maximum can only raise scores, so it raises false positives too. It buys recall
  with precision, and the trade is visible in the numbers rather than argued about
- `canonical()` strips `Corp`, so `ACME Corp` and `ACME` are identical. That is intended and it
  does mean a merchant genuinely named after a legal form is unrecoverable
- `LEGAL_FORMS` is a fixed list. A jurisdiction not on it is not stripped, and nothing detects
  the omission
- `tokens()` uses a set, so genuine repetition in a name is lost. Character similarity still
  sees it, but the token score does not

**Neutral**

- I suspected `difflib.SequenceMatcher` was asymmetric and probed it: it is not, 0 of 8 pairs.
  The reason for a hand-written edit distance is that Ratcliff-Obershelp is not an edit
  distance, and a per-edit cost of exactly `1/length` is what makes the chain analysis
  arithmetic rather than empirical
- `fold` and `canonical` are separate so a caller can choose how much normalisation to apply,
  and `explain` shows both stages
- The alphabet is not restricted. Real schemes sometimes drop confusable characters; merchant
  names cannot
