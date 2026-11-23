"""Turning a written-down merchant name into something comparable.

Every function here is a decision about what counts as "the same name", and each one can
lose a real match or manufacture a false one. The docstrings say which.
"""

from __future__ import annotations

import re
import unicodedata

# Legal-form suffixes. A merchant filed as "ACME GmbH" in one region and "ACME Ltd" in
# another is the same trading entity under two local incorporations, so the suffix is noise
# for identity purposes -- but see the warning in `strip_legal_form`, because it is the most
# dangerous normalisation here.
LEGAL_FORMS = frozenset({
    "ab", "ag", "aps", "as", "bv", "co", "company", "corp", "corporation", "cv", "eood",
    "gmbh", "inc", "incorporated", "kft", "kg", "kk", "limited", "llc", "llp", "ltd", "ltda",
    "mbh", "nv", "oy", "oyj", "plc", "pte", "pty", "sa", "sarl", "sas", "sl", "spa", "srl",
    "sro", "ug", "zoo",
})

# Leading articles, dropped so that "The ACME Corp" and "ACME Corp" agree. This matters more
# than it looks: a blocking key built from the first characters of the name puts those two in
# DIFFERENT blocks, so they are never even compared. See `block.py`.
LEADING_ARTICLES = frozenset({"the", "a", "an", "la", "le", "el", "der", "die", "das"})

_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+", flags=re.UNICODE)


def fold(text: str) -> str:
    """NFKC, then casefold, then collapse whitespace.

    The order is not arbitrary and neither step is optional. Each of these real cases is
    lost by omitting one of them:

        'ACME Corp'  (fullwidth)  -> 'acme corp'      needs NFKC; casefold alone leaves
                                                      the fullwidth letters untouched
        'Weißbräu' vs 'Weissbräu'                     needs CASEFOLD, which maps the sharp
                                                      s to 'ss'; str.lower() leaves it
                                                      alone, so the two never agree
        'ACME\\xa0Corp' (non-breaking space)          needs NFKC to become a plain space
        'Cafe' + U+0301 vs 'Cafe' (precomposed)       needs NFKC; these are visually
                                                      IDENTICAL yet 10 and 9 characters
                                                      long, so any edit distance computed
                                                      on the raw strings reports a
                                                      difference a human cannot see

    That last case is why normalisation happens before similarity and not after: a
    character-level distance is meaningless across different Unicode spellings of the same
    glyphs.

    NFKC rather than NFC because the compatibility mappings are exactly the ones that
    matter here -- fullwidth forms and non-breaking spaces come from spreadsheet exports
    and copied web pages, which is where these names come from.
    """
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def strip_punctuation(text: str) -> str:
    """Drop punctuation, keeping word characters and spaces.

    'ACME, Inc.' and 'ACME Inc' should agree. Note `\\w` is Unicode-aware, so accented
    letters survive -- dropping them would merge genuinely distinct names in languages
    where the accent is the only difference.
    """
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", text)).strip()


def strip_legal_form(text: str) -> str:
    """Remove trailing legal-form tokens.

    DANGEROUS, and deliberately limited to TRAILING tokens. 'Co' appears inside real names
    ('Co-op Foods'), so a global removal would maul them. Even restricted to the tail this
    can be wrong: a name that is ONLY a legal form ('Limited') would be stripped to nothing,
    so the last token is kept if removing it would empty the string.

    Applied repeatedly, because 'ACME Pte Ltd' has two.
    """
    tokens = text.split()
    while len(tokens) > 1 and tokens[-1] in LEGAL_FORMS:
        tokens.pop()
    return " ".join(tokens)


def strip_leading_article(text: str) -> str:
    """Remove a leading article, unless it is the whole name."""
    tokens = text.split()
    if len(tokens) > 1 and tokens[0] in LEADING_ARTICLES:
        return " ".join(tokens[1:])
    return text


def canonical(name: str) -> str:
    """The full pipeline: the form used for exact-match grouping and for blocking.

    Canonicalisation is worth distinguishing from fuzzy similarity because it is
    TRANSITIVE. Two names with the same canonical form are the same by an equivalence
    relation, so grouping on it is order-independent and needs no clustering decision at
    all. Fuzzy similarity has neither property -- see `cluster.py`.

    So the split of labour is: canonicalisation does as much of the work as it can prove,
    and similarity is only consulted for what is left over.
    """
    return strip_leading_article(strip_legal_form(strip_punctuation(fold(name))))


def tokens(name: str) -> frozenset[str]:
    """Canonical form as a token set, for order-insensitive comparison.

    A set, not a list: 'ACME Foods' and 'Foods ACME' are the same merchant written in two
    orders, which happens when a name is reassembled from separate fields. The cost is that
    genuine repetition is lost -- but a merchant name with a meaningfully repeated token is
    rare enough to accept, and character similarity still sees it.
    """
    return frozenset(canonical(name).split())
