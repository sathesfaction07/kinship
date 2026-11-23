"""Shared fixtures.

The corpus is loaded once per session and the property tests assert against it, so editing
`data/` and breaking an invariant fails the build rather than silently changing the numbers in
the docs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from case_search.dedupe import dedupe
from case_search.models import Case, Region
from case_search.sources import load_cases, load_truth

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 3, 2, 9, 0, 0)


def make_case(
    case_id: str,
    region: Region | str = Region.NA,
    name: str = "ACME Trading",
    *,
    hours: int = 0,
    page: int | None = None,
    declared_total: int | None = None,
    status: str = "open",
) -> Case:
    """A case with sensible defaults, so a test names only what it is about."""
    return Case(
        case_id=case_id,
        region=Region(region),
        merchant_name=name,
        observed_at=BASE + timedelta(hours=hours),
        page=page,
        declared_total=declared_total,
        status=status,
    )


@pytest.fixture(scope="session")
def corpus() -> list[Case]:
    cases, problems = load_cases(ROOT / "data" / "cases.jsonl")
    assert not problems, problems
    return cases


@pytest.fixture(scope="session")
def truth() -> dict[tuple[str, Region], str]:
    return load_truth(ROOT / "data" / "truth.jsonl")


@pytest.fixture(scope="session")
def distinct(corpus: list[Case]) -> list[Case]:
    kept, _ = dedupe(corpus)
    return kept
