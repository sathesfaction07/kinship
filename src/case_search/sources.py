"""Reading the corpus.

Malformed rows are collected and returned rather than raised on, because one bad line in a
platform export should not abort a 10,000-case run -- but it must not vanish either, so the
count reaches the report and the exit code.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from case_search.models import Case, Region

Key = tuple[str, Region]


def load_cases(path: Path) -> tuple[list[Case], list[str]]:
    """Read JSON Lines cases. Returns the cases and one message per rejected line."""
    cases: list[Case] = []
    problems: list[str] = []

    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                cases.append(Case.model_validate_json(stripped))
            except ValidationError as error:
                problems.append(f"{path.name}:{number}: {error.error_count()} field error(s)")
            except json.JSONDecodeError as error:
                problems.append(f"{path.name}:{number}: not valid JSON ({error.msg})")

    return (cases, problems)


def load_truth(path: Path) -> dict[Key, str]:
    """Read the true entity assignment for each (case_id, region).

    A mapping rather than a list of groups, because that is the shape both scorers need and
    it makes a case belonging to two entities unrepresentable -- which is correct: the truth
    is a partition.
    """
    truth: dict[Key, str] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            truth[(row["case_id"], Region(row["region"]))] = row["entity"]
    return truth
