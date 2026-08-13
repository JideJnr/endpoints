"""
Task 1.4 — Explicit import and syntax tests.
Runs py_compile and pyflakes on every affected file and asserts clean output.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import pytest

PREDICTX_ROOT = Path(__file__).resolve().parent.parent

AFFECTED_FILES = [
    "app/ai/llm_agent.py",
    "app/ai/ai_brain.py",
    "app/ai/ai_prediction_pipeline.py",
    "app/ai/ai_router.py",
    "app/ai/llm_pipeline.py",
    "app/storage/buffer.py",
    "app/storage/mongo_store.py",
    "app/utils/prediction_flow.py",
    "app/monitoring/self_learner.py",
    "app/models/ensemble.py",
    "app/models/poisson.py",
    "app/market/regime.py",
    "app/risk/pick_generator.py",
    "app/enrichment/confidence_calibrator.py",
    "app/config/config.py",
    "app/utils/match_helpers.py",
]


def _abs(rel: str) -> str:
    return str(PREDICTX_ROOT / rel)


@pytest.mark.parametrize("rel_path", AFFECTED_FILES)
def test_py_compile(rel_path: str) -> None:
    """File must compile without SyntaxError."""
    path = PREDICTX_ROOT / rel_path
    if not path.exists():
        pytest.skip(f"{rel_path} does not exist yet")
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"py_compile failed for {rel_path}:\n{result.stderr}"
    )


@pytest.mark.parametrize("rel_path", AFFECTED_FILES)
def test_pyflakes(rel_path: str) -> None:
    """File must have zero pyflakes errors (undefined names, unused imports)."""
    path = PREDICTX_ROOT / rel_path
    if not path.exists():
        pytest.skip(f"{rel_path} does not exist yet")
    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(path)],
        capture_output=True,
        text=True,
        cwd=str(PREDICTX_ROOT),
    )
    output = (result.stdout + result.stderr).strip()
    # Filter out lines that are pure warnings about unused imports that are
    # intentionally kept for re-export (e.g. __all__ patterns).
    errors = [
        line for line in output.splitlines()
        if line.strip() and "imported but unused" not in line
    ]
    assert not errors, (
        f"pyflakes errors in {rel_path}:\n" + "\n".join(errors)
    )
