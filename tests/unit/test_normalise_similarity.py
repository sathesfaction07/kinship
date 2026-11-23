"""Normalisation, similarity, and the non-transitivity that forces a linkage decision."""

from __future__ import annotations

import unicodedata
from itertools import pairwise

import pytest

from case_search.normalise import (
    canonical,
    fold,
    strip_leading_article,
    strip_legal_form,
    strip_punctuation,
    tokens,
)
from case_search.similarity import (
    character_similarity,
    edit_distance,
    similarity,
    token_similarity,
)

THRESHOLD = 0.85


class TestFoldNeedsEveryStep:
    """Each case here is lost by omitting one specific step, which is why all three run."""

    def test_fullwidth_needs_nfkc(self):
        # casefold alone leaves fullwidth letters untouched, so the two never agree.
        assert fold("\uff21\uff23\uff2d\uff25 Corp") == "acme corp"
        assert "\uff21\uff23\uff2d\uff25 Corp".casefold() != "acme corp"

    def test_sharp_s_needs_casefold_not_lower(self):
        # str.lower() keeps the sharp s; casefold maps it to 'ss'. This is the whole
        # difference between matching a German merchant and not.
        assert fold("Wei\u00dfbr\u00e4u") == "weissbr\u00e4u"
        assert "Wei\u00dfbr\u00e4u".lower() == "wei\u00dfbr\u00e4u"
        assert fold("Wei\u00dfbr\u00e4u") == fold("Weissbr\u00e4u")

    def test_non_breaking_space_needs_nfkc(self):
        assert fold("ACME\u00a0Corp") == "acme corp"

    def test_combining_accent_is_visually_identical_but_a_different_length(self):
        decomposed, precomposed = "Cafe\u0301 Noir", "Caf\u00e9 Noir"
        # Ten characters against nine, for glyphs a human cannot tell apart. Any edit
        # distance on the raw strings therefore reports a difference that does not exist,
        # which is why normalisation happens BEFORE similarity.
        assert len(decomposed) == 10
        assert len(precomposed) == 9
        assert decomposed != precomposed
        assert decomposed.casefold() != precomposed.casefold()
        assert fold(decomposed) == fold(precomposed)

    def test_whitespace_is_collapsed_and_trimmed(self):
        assert fold("  ACME   Foods  ") == "acme foods"


class TestCanonicalisation:
    def test_punctuation_goes_but_accents_stay(self):
        assert strip_punctuation("ACME, Inc.") == "ACME Inc"
        # Accents survive: in some languages they are the only difference between two
        # genuinely distinct names, so stripping them would merge real entities.
        assert "\u00e9" in strip_punctuation(unicodedata.normalize("NFKC", "Caf\u00e9!"))

    def test_trailing_legal_forms_go_and_repeat(self):
        assert strip_legal_form("acme pte ltd") == "acme"
        assert strip_legal_form("acme foods gmbh") == "acme foods"

    def test_a_name_that_is_only_a_legal_form_survives(self):
        # Stripping to nothing would make every such name identical to every other.
        assert strip_legal_form("limited") == "limited"

    def test_interior_legal_words_are_untouched(self):
        # 'co' appears inside real names; only the tail is considered.
        assert strip_legal_form("co op foods") == "co op foods"

    def test_leading_article_goes_unless_it_is_the_whole_name(self):
        assert strip_leading_article("the bluebell group") == "bluebell group"
        assert strip_leading_article("the") == "the"

    def test_canonical_makes_local_incorporations_agree(self):
        assert canonical("Northwind Trading Ltd") == canonical("Northwind Trading GmbH")
        assert canonical("The ACME Corp") == canonical("ACME, Inc.") == "acme"

    def test_canonical_is_an_equivalence_relation(self):
        # Reflexive, symmetric and TRANSITIVE, because it is string equality on a derived
        # value. This is the property fuzzy similarity lacks.
        names = ["ACME Corp", "acme, inc.", "The ACME Ltd", "ACME"]
        forms = [canonical(name) for name in names]
        assert len(set(forms)) == 1

    def test_tokens_are_order_insensitive(self):
        assert tokens("ACME Foods") == tokens("Foods ACME")


class TestSimilarity:
    def test_edit_distance_basics(self):
        assert edit_distance("", "") == 0
        assert edit_distance("abc", "abc") == 0
        assert edit_distance("", "abc") == 3
        assert edit_distance("kitten", "sitting") == 3

    def test_edit_distance_is_symmetric(self):
        for left, right in [("acme", "acne"), ("ab", "abcdefgh"), ("", "x")]:
            assert edit_distance(left, right) == edit_distance(right, left)

    def test_character_similarity_scales_by_the_longer_string(self):
        # Dividing by the average would report 0.4 here and flatter a short-against-long
        # pair, which is the shape of a TRUNCATED field rather than a match.
        assert character_similarity("ab", "abcdefgh") == pytest.approx(0.25)

    def test_token_similarity_is_jaccard_not_containment(self):
        # Containment would score this 1.0 because every token of the left appears in the
        # right, making two plausibly different entities indistinguishable.
        score = token_similarity("ACME", "ACME Foods Holdings International")
        assert score < 0.5

    def test_combined_takes_the_maximum_because_the_two_measures_are_blind_differently(self):
        # Reordered tokens: the character score is not merely low, it is ZERO -- both
        # canonical forms are ten characters differing at every position.
        assert character_similarity(canonical("ACME Foods"), canonical("Foods ACME")) == 0.0
        assert token_similarity("ACME Foods", "Foods ACME") == 1.0
        assert similarity("ACME Foods", "Foods ACME") == 1.0
        # An average would put a genuine match at 0.50, below any usable threshold.
        assert THRESHOLD > (0.0 + 1.0) / 2

    def test_a_typo_inside_a_token_is_caught_by_characters_not_tokens(self):
        assert token_similarity("ACME Foods", "ACME Fods") < 0.5
        assert character_similarity(canonical("ACME Foods"), canonical("ACME Fods")) > 0.85

    def test_similarity_is_symmetric_and_reflexive(self):
        pairs = [("ACME Corp", "ACME Copr"), ("Northwind", "Nordwind"), ("A", "B")]
        for left, right in pairs:
            assert similarity(left, right) == similarity(right, left)
            assert similarity(left, left) == 1.0

    def test_unicode_variants_match_after_canonicalisation(self):
        assert similarity("Wei\u00dfbr\u00e4u GmbH", "Weissbr\u00e4u Ltd") == 1.0
        assert similarity("Cafe\u0301 Noir SARL", "Caf\u00e9 Noir Ltd") == 1.0
        assert similarity("\uff21\uff23\uff2d\uff25 Electronics", "ACME Electronics Pte Ltd") == 1.0


# Each spelling is one edit from the next, so every adjacent pair scores 0.941 at seventeen
# characters while the two ends score 0.765.
CHAIN = [
    "Northwind Trading",
    "Northwund Trading",
    "Northwund Troding",
    "Norihwund Troding",
    "Norihwuad Troding",
]


class TestSimilarityIsNotTransitive:
    """The property that makes clustering a decision rather than a lookup."""

    def test_every_adjacent_link_passes_the_threshold(self):
        for left, right in pairwise(CHAIN):
            assert similarity(left, right) >= THRESHOLD

    def test_but_the_two_ends_do_not(self):
        # So similarity at 0.85 is reflexive and symmetric but NOT transitive, and therefore
        # not an equivalence relation. Single linkage would merge all five anyway.
        assert similarity(CHAIN[0], CHAIN[-1]) < THRESHOLD

    def test_the_chain_is_arithmetic_rather_than_luck(self):
        # Each step is one edit in a seventeen-character name, so one edit costs about
        # 0.059 and four of them cost more than a 0.15 budget. Any threshold below 1.0
        # admits a chain long enough -- this is not a corpus artefact.
        length = len(canonical(CHAIN[0]))
        per_edit = 1 / length
        assert per_edit < (1 - THRESHOLD)
        assert 4 * per_edit > (1 - THRESHOLD)
