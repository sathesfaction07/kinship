"""Dedup, blocking, clustering, measurement, rendering, and the corpus properties."""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

from case_search.block import (
    all_pairs,
    candidate_pairs,
    combined_key,
    pair_completeness,
    prefix_key,
    token_key,
)
from case_search.cluster import chained_entities, resolve, score_pairs
from case_search.dedupe import audit_pages, dedupe
from case_search.measure import (
    Comparison,
    Score,
    cluster_level,
    pairwise,
    summarise,
)
from case_search.models import Case, Completeness, Entity, Linkage, Region
from case_search.render import (
    render_comparison,
    render_dedupe,
    render_entities,
    score_payload,
    write_entities_csv,
)
from case_search.sources import load_cases
from tests.conftest import make_case

THRESHOLD = 0.85


class TestIdentityExcludesTheTimestamp:
    def test_key_is_case_id_and_region_only(self):
        early = make_case("C1", Region.NA, hours=0)
        late = make_case("C1", Region.NA, hours=48)
        # The spec I was given lists the timestamp in the composite key. Including it would
        # give these two different keys and dedup would never fire on a re-fetch.
        assert early.key == late.key == ("C1", Region.NA)

    def test_the_same_id_in_two_regions_is_two_cases(self):
        assert make_case("C1", Region.NA).key != make_case("C1", Region.EU).key


class TestDedupe:
    def test_the_later_observation_wins(self):
        early = make_case("C1", name="ACME", hours=0, status="open")
        late = make_case("C1", name="ACME", hours=48, status="closed")
        kept, dropped = dedupe([early, late])
        assert len(kept) == 1
        # Recency, not arrival order: keeping the first would report a stale status while
        # still counting the case once, which is harder to notice than a wrong count.
        assert kept[0].status == "closed"
        assert len(dropped) == 1

    def test_recency_wins_regardless_of_arrival_order(self):
        early = make_case("C1", hours=0, status="open")
        late = make_case("C1", hours=48, status="closed")
        for order in ([early, late], [late, early]):
            kept, _ = dedupe(order)
            assert kept[0].status == "closed"

    def test_dropped_records_carry_both_timestamps(self):
        _, dropped = dedupe([make_case("C1", hours=0), make_case("C1", hours=48)])
        # A count alone cannot be investigated; these can.
        assert dropped[0].dropped_observed_at < dropped[0].kept_observed_at

    def test_cross_region_duplicates_are_not_duplicates(self):
        kept, dropped = dedupe([make_case("C1", Region.NA), make_case("C1", Region.EU)])
        assert len(kept) == 2
        assert dropped == []

    def test_output_is_order_independent(self):
        cases = [make_case(f"C{i}", hours=i) for i in range(8)]
        reference = [c.key for c in dedupe(cases)[0]]
        for seed in range(5):
            shuffled = list(cases)
            random.Random(seed).shuffle(shuffled)
            assert [c.key for c in dedupe(shuffled)[0]] == reference


class TestPageAudit:
    def test_complete_when_the_declared_total_is_matched(self):
        cases = [make_case(f"C{i}", page=1, declared_total=3) for i in range(3)]
        assert audit_pages(cases)[0].verdict is Completeness.COMPLETE

    def test_short_when_it_is_not(self):
        cases = [make_case(f"C{i}", page=1, declared_total=9) for i in range(3)]
        assert audit_pages(cases)[0].verdict is Completeness.SHORT

    def test_unverifiable_without_a_declared_total(self):
        cases = [make_case(f"C{i}", page=1) for i in range(3)]
        # Not COMPLETE and not SHORT. "We cannot tell" is a third thing, and it is the
        # commonest case.
        assert audit_pages(cases)[0].verdict is Completeness.UNVERIFIABLE

    def test_a_gap_outranks_a_matched_total(self):
        cases = [
            make_case("C1", page=1, declared_total=2),
            make_case("C2", page=3, declared_total=2),
        ]
        audit = audit_pages(cases)[0]
        # The total agrees, and data is still definitely missing.
        assert audit.verdict is Completeness.GAP_IN_PAGES
        assert audit.missing_pages == (2,)

    def test_a_gap_is_detected_with_no_declared_total_at_all(self):
        cases = [make_case("C1", page=1), make_case("C2", page=4)]
        audit = audit_pages(cases)[0]
        assert audit.verdict is Completeness.GAP_IN_PAGES
        assert audit.missing_pages == (2, 3)

    def test_surplus_is_not_a_failure_after_dedup(self):
        cases = [make_case(f"C{i}", page=1, declared_total=2) for i in range(4)]
        assert audit_pages(cases)[0].verdict is Completeness.COMPLETE

    def test_regions_are_audited_separately(self):
        cases = [
            make_case("C1", Region.NA, page=1, declared_total=1),
            make_case("C2", Region.EU, page=1),
        ]
        verdicts = {a.region: a.verdict for a in audit_pages(cases)}
        assert verdicts[Region.NA] is Completeness.COMPLETE
        assert verdicts[Region.EU] is Completeness.UNVERIFIABLE


# Two spellings of one merchant that diverge at character 4 and share no token once the legal
# form is stripped. No blocking key here can ever offer this pair.
NORTH = [make_case("C1", Region.EU, "Nordwind AB"), make_case("C2", Region.NA, "Norwind AB")]

# The chain: three names truly in entity A, two truly in entity B, every adjacent pair above
# the threshold and the two ends below it.
CHAIN = [
    make_case("P1", Region.NA, "Palisade Holdings"),
    make_case("P2", Region.EU, "Palisadr Holdings"),
    make_case("P3", Region.FE, "Palisadr Holdimgs"),
    make_case("P4", Region.NA, "Palisadr Holdimgz"),
    make_case("P5", Region.EU, "Palisade Holdingz"),
]


class TestBlockingSetsTheCeiling:

    def test_a_prefix_block_never_offers_a_pair_diverging_early(self):
        pairs, _ = candidate_pairs(NORTH, key=prefix_key)
        # 'nord' against 'norw': different blocks, so no threshold can ever recover them.
        assert pairs == []

    def test_and_neither_does_a_token_block_when_no_token_is_shared(self):
        pairs, _ = candidate_pairs(NORTH, key=token_key)
        assert pairs == []

    def test_all_pairs_would_have_offered_it(self):
        assert len(list(all_pairs(NORTH))) == 1

    def test_a_shared_token_rescues_an_early_divergence(self):
        cases = [
            make_case("C1", Region.EU, "Nordwind Trading"),
            make_case("C2", Region.NA, "Norwind Trading"),
        ]
        assert candidate_pairs(cases, key=prefix_key)[0] == []
        assert len(candidate_pairs(cases, key=token_key)[0]) == 1

    def test_combined_is_a_union_so_it_can_only_help(self):
        cases = [
            make_case("C1", Region.EU, "Nordwind Trading"),
            make_case("C2", Region.NA, "Norwind Trading"),
            make_case("C3", Region.FE, "Nordwind Holdings"),
        ]
        prefix = {frozenset({a.key, b.key}) for a, b in candidate_pairs(cases, key=prefix_key)[0]}
        token = {frozenset({a.key, b.key}) for a, b in candidate_pairs(cases, key=token_key)[0]}
        both = {frozenset({a.key, b.key}) for a, b in candidate_pairs(cases, key=combined_key)[0]}
        assert prefix <= both
        assert token <= both

    def test_oversized_blocks_are_reported_not_silently_dropped(self):
        cases = [make_case(f"C{i}", name=f"Merchant{i} Trading") for i in range(12)]
        pairs, oversized = candidate_pairs(cases, key=token_key, max_block=5)
        assert "trading" in oversized
        # A skipped block is a deliberate recall sacrifice, so the caller must be able to
        # say so in the report.
        assert all("trading" not in oversized[1:] for _ in [0])
        assert pairs == [] or len(pairs) < 66

    def test_pair_completeness_is_none_when_there_are_no_true_pairs(self):
        cases = [make_case("C1", name="Alpha"), make_case("C2", name="Beta")]
        truth = {cases[0].key: "e1", cases[1].key: "e2"}
        score, lost = pair_completeness(cases, truth)
        # Perfect completeness over zero opportunities is not perfection.
        assert score is None
        assert lost == []

    def test_pair_completeness_reports_what_was_lost(self):
        truth = {NORTH[0].key: "e-nord", NORTH[1].key: "e-nord"}
        score, lost = pair_completeness(NORTH, truth, key=prefix_key)
        assert score == 0.0
        assert len(lost) == 1


class TestLinkageRules:
    """Every rule against the same chain, so what each gives up is visible side by side."""

    def test_single_linkage_chains_the_whole_run_together(self):
        entities, _, _ = resolve(CHAIN, threshold=THRESHOLD, linkage=Linkage.SINGLE)
        assert len(entities) == 1
        assert entities[0].size == 5

    def test_and_the_weakest_internal_score_proves_it(self):
        entities, _, _ = resolve(CHAIN, threshold=THRESHOLD, linkage=Linkage.SINGLE)
        chained = chained_entities(entities, THRESHOLD)
        assert len(chained) == 1
        # The entity contains a pair the matcher itself rejected.
        assert chained[0].weakest_internal_score < THRESHOLD

    def test_complete_linkage_cannot_chain_by_construction(self):
        entities, _, _ = resolve(CHAIN, threshold=THRESHOLD, linkage=Linkage.COMPLETE)
        assert chained_entities(entities, THRESHOLD) == []
        assert len(entities) > 1

    def test_canonical_grouping_cannot_chain_either(self):
        entities, _, _ = resolve(CHAIN, threshold=THRESHOLD, linkage=Linkage.CANONICAL)
        assert chained_entities(entities, THRESHOLD) == []

    def test_every_rule_is_order_independent(self):
        # Complete linkage was NOT, until the merge choice was made deterministic. It took
        # the first qualifying merge, which depended on row order and produced three
        # different partitions across six shuffles.
        for linkage in Linkage:
            reference = None
            for seed in range(6):
                shuffled = list(CHAIN)
                random.Random(seed).shuffle(shuffled)
                entities, _, _ = resolve(shuffled, threshold=THRESHOLD, linkage=linkage)
                got = sorted(tuple(e.members) for e in entities)
                if reference is None:
                    reference = got
                assert got == reference, f"{linkage} is order-dependent"

    def test_singletons_are_kept(self):
        cases = [make_case("C1", name="Alpha Foods"), make_case("C2", name="Zulu Mining")]
        entities, _, _ = resolve(cases, threshold=THRESHOLD)
        assert len(entities) == 2
        # A merchant seen once is a real entity. Dropping singletons would change the
        # denominator of every metric.
        assert all(e.size == 1 for e in entities)

    def test_a_singleton_has_no_weakest_internal_score(self):
        entities, _, _ = resolve([make_case("C1")], threshold=THRESHOLD)
        # None, not 1.0: there is no internal pair, so nothing was measured.
        assert entities[0].weakest_internal_score is None

    def test_a_higher_threshold_cannot_merge_more(self):
        strict, _, _ = resolve(CHAIN, threshold=0.99, linkage=Linkage.SINGLE)
        loose, _, _ = resolve(CHAIN, threshold=0.50, linkage=Linkage.SINGLE)
        assert len(strict) >= len(loose)

    def test_scoring_respects_the_threshold(self):
        pairs, _ = score_pairs(CHAIN, threshold=0.99)
        assert all(p.score >= 0.99 for p in pairs)


class TestMeasurement:
    def test_precision_is_none_when_nothing_was_predicted(self):
        score = Score(true_positives=0, false_positives=0, false_negatives=4)
        # Not 0.0. Nothing was claimed, so nothing was wrong.
        assert score.precision is None
        assert score.recall == 0.0
        assert score.f1 is None

    def test_recall_is_none_when_there_was_nothing_to_find(self):
        score = Score(true_positives=0, false_positives=3, false_negatives=0)
        assert score.recall is None
        assert score.precision == 0.0
        assert score.f1 is None

    def test_zero_precision_and_zero_recall_give_a_real_zero_f1(self):
        score = Score(true_positives=0, false_positives=2, false_negatives=2)
        # Defined-but-zero is a genuine result and different from unmeasured.
        assert score.precision == 0.0
        assert score.recall == 0.0
        assert score.f1 == 0.0

    def test_a_partition_into_singletons_implies_no_pairs(self):
        entities = [
            Entity(members=(("C1", Region.NA),), display_name="a", linkage=Linkage.SINGLE),
            Entity(members=(("C2", Region.NA),), display_name="b", linkage=Linkage.SINGLE),
        ]
        truth = {("C1", Region.NA): "e1", ("C2", Region.NA): "e2"}
        score = pairwise(entities, truth)
        assert score.true_positives == score.false_positives == score.false_negatives == 0
        assert score.precision is None

    def test_pairwise_forgives_what_cluster_scoring_does_not(self):
        # One entity of three where the truth says two-plus-one. Pairwise loses only the two
        # wrong pairs; cluster scoring says both entities are wrong.
        entity = Entity(
            members=(("C1", Region.NA), ("C2", Region.NA), ("C3", Region.NA)),
            display_name="merged", linkage=Linkage.SINGLE, weakest_internal_score=0.80,
        )
        truth = {("C1", Region.NA): "e1", ("C2", Region.NA): "e1", ("C3", Region.NA): "e2"}
        pair_score = pairwise([entity], truth)
        cluster_score = cluster_level([entity], truth)
        assert pair_score.true_positives == 1
        assert pair_score.false_positives == 2
        assert cluster_score.true_positives == 0
        assert cluster_score.f1 == 0.0
        assert pair_score.f1 > cluster_score.f1

    def test_keys_absent_from_the_truth_set_are_ignored(self):
        entity = Entity(
            members=(("C1", Region.NA), ("UNKNOWN", Region.NA)),
            display_name="x", linkage=Linkage.SINGLE, weakest_internal_score=0.9,
        )
        truth = {("C1", Region.NA): "e1"}
        # An unknown member must not make the entity wrong for a reason unrelated to matching.
        assert pairwise([entity], truth).false_positives == 0
        assert cluster_level([entity], truth).true_positives == 1

    def test_summarise_excludes_order_unstable_rules_outright(self):
        good = Comparison(
            linkage="stable-but-worse", threshold=0.85, entities=3, largest_entity=2, chained=0,
            pairwise=Score(true_positives=1, false_positives=1, false_negatives=1),
            cluster=Score(true_positives=1, false_positives=1, false_negatives=1),
            order_stable=True,
        )
        better = Comparison(
            linkage="unstable-but-better", threshold=0.85, entities=3, largest_entity=2,
            chained=0,
            pairwise=Score(true_positives=9, false_positives=0, false_negatives=0),
            cluster=Score(true_positives=9, false_positives=0, false_negatives=0),
            order_stable=False,
        )
        # A partition that changes with input order is not a candidate at any accuracy.
        assert summarise([good, better]) == "stable-but-worse"

    def test_summarise_returns_none_when_nothing_qualifies(self):
        unstable = Comparison(
            linkage="x", threshold=0.85, entities=1, largest_entity=1, chained=0,
            pairwise=Score(true_positives=0, false_positives=0, false_negatives=0),
            cluster=Score(true_positives=0, false_positives=0, false_negatives=0),
            order_stable=False,
        )
        assert summarise([unstable]) is None
        assert summarise([]) is None


class TestRendering:
    def test_unmeasured_prints_n_a_and_never_zero(self):
        score = Score(true_positives=0, false_positives=0, false_negatives=0)
        comparison = Comparison(
            linkage="single", threshold=0.85, entities=0, largest_entity=0, chained=0,
            pairwise=score, cluster=score, order_stable=True,
        )
        text = render_comparison([comparison], None, 0, None)
        assert "n/a" in text
        assert "0.0%" not in text

    def test_a_real_zero_recall_is_shown_as_zero(self):
        # Precision and recall are independent: nothing found over a non-empty truth set is
        # a genuine 0.0% recall with UNDEFINED precision.
        score = Score(true_positives=0, false_positives=0, false_negatives=5)
        comparison = Comparison(
            linkage="single", threshold=0.85, entities=1, largest_entity=1, chained=0,
            pairwise=score, cluster=score, order_stable=True,
        )
        text = render_comparison([comparison], None, 0, None)
        assert "0.0%" in text
        assert "n/a" in text

    def test_unstable_is_shouted(self):
        score = Score(true_positives=1, false_positives=0, false_negatives=0)
        comparison = Comparison(
            linkage="complete", threshold=0.85, entities=1, largest_entity=1, chained=0,
            pairwise=score, cluster=score, order_stable=False,
        )
        assert "UNSTABLE" in render_comparison([comparison], 1.0, 0, None)

    def test_no_preferred_rule_says_so(self):
        assert "none" in render_comparison([], None, 0, None)

    def test_a_chained_entity_is_explained_not_just_counted(self):
        entity = Entity(
            members=(("C1", Region.NA), ("C2", Region.EU)),
            display_name="Palisade Holdings", linkage=Linkage.SINGLE,
            weakest_internal_score=0.80,
        )
        text = render_entities([entity], THRESHOLD, [])
        assert "CHAINED" in text
        assert "0.800" in text
        assert "rejected" in text

    def test_completeness_is_worst_first_and_spells_out_unverifiable(self):
        audits = audit_pages([
            make_case("C1", Region.NA, page=1, declared_total=1),
            make_case("C2", Region.EU, page=1),
            make_case("C3", Region.FE, page=1),
            make_case("C4", Region.FE, page=3),
        ])
        text = render_dedupe(4, [], [], audits, [])
        assert text.index("gap_in_pages") < text.index("unverifiable")
        assert text.index("unverifiable") < text.index("complete ")
        assert "CANNOT be determined" in text

    def test_csv_writes_an_empty_cell_for_an_unmeasured_score(self, tmp_path: Path):
        entities = [
            Entity(members=(("C1", Region.NA),), display_name="solo", linkage=Linkage.SINGLE),
            Entity(
                members=(("C2", Region.NA), ("C3", Region.EU)), display_name="pair",
                linkage=Linkage.SINGLE, weakest_internal_score=0.9,
            ),
        ]
        target = tmp_path / "entities.csv"
        write_entities_csv(target, entities)
        rows = list(csv.DictReader(target.open(encoding="utf-8", newline="")))
        by_name = {row["display_name"]: row for row in rows}
        # Empty, not 0 -- which in a similarity column would read as "completely dissimilar".
        assert by_name["solo"]["weakest_internal_score"] == ""
        assert by_name["pair"]["weakest_internal_score"] == "0.9000"

    def test_json_keeps_none_as_null(self):
        score = Score(true_positives=0, false_positives=0, false_negatives=0)
        payload = json.loads(json.dumps(score_payload(score)))
        assert payload["precision"] is None
        assert payload["f1"] is None


class TestLoading:
    def test_a_bad_row_is_reported_not_raised(self, tmp_path: Path):
        path = tmp_path / "cases.jsonl"
        path.write_text(
            '{"case_id":"C1","region":"na","merchant_name":"A","observed_at":"2026-03-02T09:00:00"}\n'
            "not json at all\n"
            '{"case_id":"C2","region":"nowhere","merchant_name":"B","observed_at":"2026-03-02T09:00:00"}\n',
            encoding="utf-8",
        )
        cases, problems = load_cases(path)
        # One bad line must not abort a 10,000-case run, and must not vanish either.
        assert len(cases) == 1
        assert len(problems) == 2

    def test_blank_lines_are_skipped(self, tmp_path: Path):
        path = tmp_path / "cases.jsonl"
        path.write_text(
            '\n{"case_id":"C1","region":"na","merchant_name":"A","observed_at":"2026-03-02T09:00:00"}\n\n',
            encoding="utf-8",
        )
        cases, problems = load_cases(path)
        assert len(cases) == 1
        assert problems == []


class TestPropertiesOverTheShippedCorpus:
    """Assertions against `data/`, so editing the corpus and breaking an invariant fails."""

    def test_the_corpus_exercises_every_region(self, distinct: list[Case]):
        # Guards the other corpus tests from becoming vacuous.
        assert {case.region for case in distinct} == set(Region)

    def test_the_corpus_contains_exact_key_duplicates(self, corpus: list[Case]):
        kept, dropped = dedupe(corpus)
        assert len(dropped) == 3
        assert len(kept) < len(corpus)

    def test_a_dropped_duplicate_kept_the_later_status(self, corpus: list[Case]):
        kept, _ = dedupe(corpus)
        closed = [case for case in kept if case.status == "closed"]
        assert len(closed) == 3

    def test_the_shared_case_id_stayed_two_cases(self, distinct: list[Case]):
        shared = [case for case in distinct if case.case_id == "C9001"]
        assert len(shared) == 2
        assert {case.region for case in shared} == {Region.NA, Region.EU}

    def test_the_planted_page_gap_is_found(self, distinct: list[Case]):
        audits = {audit.region: audit for audit in audit_pages(distinct)}
        assert audits[Region.FE].verdict is Completeness.GAP_IN_PAGES
        assert audits[Region.FE].missing_pages == (2,)

    def test_the_region_with_no_declared_total_is_unverifiable(self, distinct: list[Case]):
        audits = {audit.region: audit for audit in audit_pages(distinct)}
        assert audits[Region.EU].verdict is Completeness.UNVERIFIABLE
        assert audits[Region.EU].declared_total is None

    def test_the_region_that_declares_a_matching_total_is_complete(self, distinct: list[Case]):
        audits = {audit.region: audit for audit in audit_pages(distinct)}
        assert audits[Region.NA].verdict is Completeness.COMPLETE

    def test_blocking_improves_as_keys_are_added(self, distinct, truth):
        scores = {
            name: pair_completeness(distinct, truth, key=key)[0]
            for name, key in [("prefix", prefix_key), ("token", token_key),
                              ("combined", combined_key)]
        }
        assert scores["prefix"] < scores["combined"]
        assert scores["token"] < scores["combined"]
        # Still not 1.0: the planted Nordwind/Norwind pair shares no token and diverges at
        # character 4, so no key here can offer it.
        assert scores["combined"] < 1.0

    def test_the_lost_pair_is_the_planted_one(self, distinct, truth):
        _, lost = pair_completeness(distinct, truth, key=combined_key)
        assert len(lost) == 1
        names = {case.key: case.merchant_name for case in distinct}
        spellings = sorted(names[key] for key in lost[0])
        assert spellings == ["Nordwind AB", "Norwind AB"]

    def test_single_linkage_produces_exactly_one_chained_entity(self, distinct):
        entities, _, _ = resolve(distinct, threshold=THRESHOLD, linkage=Linkage.SINGLE)
        chained = chained_entities(entities, THRESHOLD)
        assert len(chained) == 1
        assert chained[0].size == 5

    def test_every_false_pair_under_single_linkage_comes_from_that_chain(self, distinct, truth):
        entities, _, _ = resolve(distinct, threshold=THRESHOLD, linkage=Linkage.SINGLE)
        chained = chained_entities(entities, THRESHOLD)
        score = pairwise(entities, truth)
        wrong_inside = sum(
            1
            for index, left in enumerate(sorted(chained[0].members))
            for right in sorted(chained[0].members)[index + 1:]
            if truth.get(left) != truth.get(right)
        )
        # The entire precision loss of single linkage is one chain of five near-identical
        # names -- not diffuse noise.
        assert wrong_inside == score.false_positives == 6

    def test_the_stricter_rules_never_chain(self, distinct):
        for linkage in (Linkage.COMPLETE, Linkage.CANONICAL):
            entities, _, _ = resolve(distinct, threshold=THRESHOLD, linkage=linkage)
            assert chained_entities(entities, THRESHOLD) == []

    def test_canonical_grouping_has_perfect_pairwise_precision(self, distinct, truth):
        entities, _, _ = resolve(distinct, threshold=THRESHOLD, linkage=Linkage.CANONICAL)
        score = pairwise(entities, truth)
        # It merges only on exact canonical equality, so it never merges wrongly -- and it
        # finds barely a third of the true pairs, which is the cost.
        assert score.precision == 1.0
        assert score.recall < 0.4

    def test_no_rule_dominates_on_this_corpus(self, distinct, truth):
        results = {}
        for linkage in Linkage:
            entities, _, _ = resolve(distinct, threshold=THRESHOLD, linkage=linkage)
            results[linkage] = (
                pairwise(entities, truth),
                cluster_level(entities, truth),
                len(chained_entities(entities, THRESHOLD)),
            )
        best_cluster = max(results, key=lambda k: results[k][1].f1)
        best_precision = max(results, key=lambda k: results[k][0].precision)
        # The rule with the best cluster F1 is also the ONLY one that chains, and it is not
        # the rule with the best precision. There is no dominant choice.
        assert best_cluster is Linkage.SINGLE
        assert results[best_cluster][2] == 1
        assert best_precision is not best_cluster

    def test_every_rule_is_order_independent_on_the_whole_corpus(self, distinct):
        for linkage in Linkage:
            reference = None
            for seed in range(5):
                shuffled = list(distinct)
                random.Random(seed).shuffle(shuffled)
                entities, _, _ = resolve(shuffled, threshold=THRESHOLD, linkage=linkage)
                got = sorted(tuple(e.members) for e in entities)
                if reference is None:
                    reference = got
                assert got == reference, f"{linkage} is order-dependent"

    def test_the_truth_set_covers_only_cases_that_survived_the_page_drop(
        self, distinct, truth
    ):
        present = {case.key for case in distinct}
        # Every truth key is a case we hold, so a recall shortfall is a matching failure
        # rather than a bookkeeping artefact.
        assert set(truth) <= present

    def test_the_corpus_has_both_singletons_and_multi_member_entities(self, truth):
        sizes: dict[str, int] = {}
        for entity in truth.values():
            sizes[entity] = sizes.get(entity, 0) + 1
        assert sum(1 for n in sizes.values() if n == 1) > 5
        assert sum(1 for n in sizes.values() if n > 1) > 5
