"""How alike two merchant names are.

The important property of everything in this module: **similarity is not transitive.** It is
reflexive and symmetric, so it looks like an equivalence relation and is not one. `a ~ b` and
`b ~ c` does not give `a ~ c` for any threshold below 1.0, which is the whole difficulty of
clustering on it. See `cluster.py`.
"""

from __future__ import annotations

from case_search.normalise import canonical, tokens


def edit_distance(left: str, right: str) -> int:
    """Levenshtein distance, two rows rather than a full matrix.

    Rolling two rows keeps memory at O(min(n, m)) instead of O(n*m). Names are short so it
    hardly matters here, but the all-pairs comparison this feeds is quadratic and the
    constant factor is the only thing standing between a 10,000-case run and a slow one.
    """
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    # Iterate over the shorter string so the row length is the smaller dimension.
    if len(left) > len(right):
        left, right = right, left

    previous = list(range(len(left) + 1))
    for j, right_char in enumerate(right, start=1):
        current = [j]
        for i, left_char in enumerate(left, start=1):
            current.append(min(
                previous[i] + 1,                                    # deletion
                current[i - 1] + 1,                                 # insertion
                previous[i - 1] + (left_char != right_char),        # substitution
            ))
        previous = current
    return previous[-1]


def character_similarity(left: str, right: str) -> float:
    """Edit distance scaled to 0..1 by the longer string.

    Dividing by the LONGER length rather than the average keeps the result a proper
    similarity: 'ab' against 'abcdefgh' scores 0.25, not the 0.4 an average would give. The
    average flatters short-against-long pairs, and short-against-long is exactly the shape
    of a truncated field -- which is a data problem, not a match.
    """
    if not left and not right:
        return 1.0
    longest = max(len(left), len(right))
    return 1.0 - (edit_distance(left, right) / longest)


def token_similarity(left: str, right: str) -> float:
    """Jaccard overlap of the token sets: intersection over union.

    Order-insensitive, so 'ACME Foods' and 'Foods ACME' score 1.0 where a character
    distance scores far lower. That case is real -- a name reassembled from separate fields
    comes out in whatever order the fields were read.

    Jaccard rather than containment (intersection over the SMALLER set). Containment gives
    'ACME' against 'ACME Foods Holdings International' a perfect 1.0, because every token of
    the first appears in the second. Those are plausibly different entities and a metric
    that cannot tell them apart is worse than useless: it is confidently wrong.
    """
    left_tokens, right_tokens = tokens(left), tokens(right)
    if not left_tokens and not right_tokens:
        return 1.0
    union = left_tokens | right_tokens
    if not union:
        return 1.0
    return len(left_tokens & right_tokens) / len(union)


def similarity(left: str, right: str) -> float:
    """The score used for matching: the MAXIMUM of character and token similarity.

    Maximum, not average. The two measures fail on opposite inputs -- character distance
    fails on reordered tokens, token overlap fails on a typo inside a token -- so each is
    evidence FOR a match and neither is evidence against. Averaging lets the one that
    happens to be blind drag down the one that can see:

        'ACME Foods'  vs 'Foods ACME'    char 0.00  token 1.00   max 1.00  avg 0.50
        'ACME Foods'  vs 'ACME Fods'     char 0.90  token 0.33   max 0.90  avg 0.62

    Both are the same merchant, and an average would put both on the wrong side of a
    plausible 0.85 threshold while the maximum clears it comfortably.

    The first row is worth a second look: the character score is not merely low, it is
    ZERO. Both canonical forms are ten characters and they differ at every position, so
    there is no partial credit at all. I had written 0.40 in this docstring from intuition;
    the measurement said 0.00.

    The cost, stated plainly: taking the maximum can only raise scores, so it raises false
    positives too. It buys recall with precision, and the measurement in `measure.py` is
    what decides whether that trade paid.

    Names are canonicalised first, for the reason in `normalise.fold`: an edit distance
    across two Unicode spellings of the same glyphs is meaningless.
    """
    left_canonical, right_canonical = canonical(left), canonical(right)
    return max(
        character_similarity(left_canonical, right_canonical),
        token_similarity(left, right),
    )
