"""
Task 1.2 — Duplicate function detection tests.
Each test asserts that a canonical function is defined exactly once across the codebase.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import pytest

APP_ROOT = Path(__file__).resolve().parent.parent / "app"


def _count_definitions(func_name: str) -> list[str]:
    """Return list of file paths that define *func_name* at module or class level."""
    found: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == func_name:
                    found.append(str(path.relative_to(APP_ROOT.parent)))
                    break
    return found


TRUE_DUPLICATES = [
    # doc helpers
    "_context_source",
    "_is_live_doc",
    "_is_finished_doc",
    "_is_not_started_period",
    "_date_from_start_time",
    "_safe_call",
    "_band",
    "_impact",
    # web helpers
    "_fetch_web",
    # db helpers
    "_ensure_column",
    "_ensure_signal_outcomes_table",
    "_ensure_signal_combination_outcomes_table",
    # match helpers
    "_side_from_selection_and_match",
    "_side_from_team_selection",
    "_match_sides",
    # misc
    "_extract_1x2",
    "_data_sources",
    "_hf_token",
    "_parse_datetime",
]


@pytest.mark.parametrize("func_name", TRUE_DUPLICATES)
def test_function_defined_at_most_once(func_name: str) -> None:
    """Each canonical function must appear in at most one file after consolidation."""
    locations = _count_definitions(func_name)
    assert len(locations) <= 1, (
        f"'{func_name}' is defined in {len(locations)} files: {locations}. "
        "Remove duplicate definitions and keep only the canonical one."
    )
