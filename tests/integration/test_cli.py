"""End-to-end CLI behaviour: output, exit codes, and machine-readable files."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from click.testing import CliRunner

from case_search.cli import cli

ROOT = Path(__file__).resolve().parents[2]
CASES = str(ROOT / "data" / "cases.jsonl")
TRUTH = str(ROOT / "data" / "truth.jsonl")


class TestResolve:
    def test_it_fails_on_the_planted_page_gap(self):
        result = CliRunner().invoke(cli, ["resolve", "-i", CASES])
        # A gap proves cases are missing, which is worth failing a build over.
        assert result.exit_code == 1
        assert "gap_in_pages" in result.output

    def test_but_it_says_unverifiable_rather_than_failing_on_it(self):
        result = CliRunner().invoke(cli, ["resolve", "-i", CASES])
        assert "unverifiable" in result.output
        assert "CANNOT be determined" in result.output

    def test_it_reports_the_chained_entity(self):
        result = CliRunner().invoke(cli, ["resolve", "-i", CASES])
        assert "CHAINED" in result.output
        assert "rejected" in result.output

    def test_complete_linkage_reports_no_chaining(self):
        result = CliRunner().invoke(cli, ["resolve", "-i", CASES, "-l", "complete"])
        assert "CHAINED" not in result.output

    def test_json_and_csv_are_written(self, tmp_path: Path):
        result = CliRunner().invoke(cli, ["resolve", "-i", CASES, "-o", str(tmp_path)])
        assert result.exit_code == 1  # still the page gap
        payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert payload["summary"]["distinct_cases"] == 53
        assert payload["summary"]["duplicates_dropped"] == 3
        assert payload["summary"]["chained_entities"] == 1
        verdicts = {row["region"]: row["verdict"] for row in payload["completeness"]}
        assert verdicts == {"na": "complete", "eu": "unverifiable", "fe": "gap_in_pages"}

        rows = list(csv.DictReader((tmp_path / "entities.csv").open(encoding="utf-8", newline="")))
        assert len(rows) == payload["summary"]["entities"]
        singletons = [r for r in rows if r["size"] == "1"]
        assert all(r["weakest_internal_score"] == "" for r in singletons)

    def test_a_missing_input_fails_cleanly(self):
        result = CliRunner().invoke(cli, ["resolve", "-i", "/nope/cases.jsonl"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_an_out_of_range_threshold_is_rejected(self):
        result = CliRunner().invoke(cli, ["resolve", "-i", CASES, "-t", "1.5"])
        assert result.exit_code != 0


class TestCompare:
    def test_it_scores_every_rule_and_names_a_preference(self):
        result = CliRunner().invoke(cli, ["compare", "-i", CASES, "-k", TRUTH])
        assert result.exit_code == 0
        for rule in ("single", "complete", "canonical"):
            assert rule in result.output
        assert "preferred rule" in result.output

    def test_it_reports_the_blocking_ceiling_and_what_was_lost(self):
        result = CliRunner().invoke(cli, ["compare", "-i", CASES, "-k", TRUTH])
        assert "pair completeness" in result.output
        assert "never compared" in result.output

    def test_the_json_preserves_nulls_and_the_stability_flags(self, tmp_path: Path):
        result = CliRunner().invoke(
            cli, ["compare", "-i", CASES, "-k", TRUTH, "-o", str(tmp_path)]
        )
        assert result.exit_code == 0
        payload = json.loads((tmp_path / "comparison.json").read_text(encoding="utf-8"))
        rules = {row["linkage"]: row for row in payload["rules"]}
        assert set(rules) == {"single", "complete", "canonical"}
        assert all(row["order_stable"] is True for row in rules.values())
        # Only single linkage chains.
        assert rules["single"]["chained"] == 1
        assert rules["complete"]["chained"] == 0
        assert rules["canonical"]["chained"] == 0
        # Canonical grouping never merges wrongly, and finds barely a third of the pairs.
        assert rules["canonical"]["pairwise"]["precision"] == 1.0
        assert rules["canonical"]["pairwise"]["recall"] < 0.4
        assert payload["blocking"]["pair_completeness"] < 1.0
        assert len(payload["blocking"]["lost_pairs"]) == 1

    def test_the_prefix_block_has_a_lower_ceiling(self, tmp_path: Path):
        for key in ("prefix", "combined"):
            target = tmp_path / key
            CliRunner().invoke(
                cli, ["compare", "-i", CASES, "-k", TRUTH, "-b", key, "-o", str(target)]
            )
        def read(name):
            return json.loads((tmp_path / name / "comparison.json").read_text(encoding="utf-8"))

        prefix = read("prefix")
        combined = read("combined")
        assert (
            prefix["blocking"]["pair_completeness"]
            < combined["blocking"]["pair_completeness"]
        )

    def test_shuffles_can_be_skipped(self):
        result = CliRunner().invoke(
            cli, ["compare", "-i", CASES, "-k", TRUTH, "--shuffles", "0"]
        )
        assert result.exit_code == 0
        assert "untested" in result.output

    def test_a_missing_truth_file_fails_cleanly(self):
        result = CliRunner().invoke(cli, ["compare", "-i", CASES, "-k", "/nope/truth.jsonl"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestExplain:
    def test_it_shows_the_zero_character_score_on_reordered_tokens(self):
        result = CliRunner().invoke(cli, ["explain", "ACME Foods", "Foods ACME"])
        assert result.exit_code == 0
        assert "0.000" in result.output
        assert "ZERO" in result.output

    def test_it_shows_the_normalisation_steps(self):
        result = CliRunner().invoke(
            cli, ["explain", "Wei\u00dfbr\u00e4u GmbH", "Weissbr\u00e4u Ltd"]
        )
        assert "folded" in result.output
        assert "canonical" in result.output
        assert "1.000" in result.output


class TestDoctor:
    def test_it_runs_with_no_credentials_and_explains_the_vocabulary(self):
        result = CliRunner().invoke(cli, ["doctor", "-i", CASES, "-k", TRUTH])
        assert result.exit_code == 0
        for term in ("unverifiable", "gap_in_pages", "chained", "n/a"):
            assert term in result.output
        assert "no credentials, no network, no model" in result.output

    def test_it_fails_when_an_input_is_missing(self):
        result = CliRunner().invoke(cli, ["doctor", "-i", "/nope.jsonl", "-k", TRUTH])
        assert result.exit_code == 1
        assert "MISSING" in result.output
