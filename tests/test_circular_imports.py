"""
Task 1.1 — Circular import cycle detection tests.
Each test imports a hub module in a clean subprocess and asserts no ImportError.
"""
from __future__ import annotations

import subprocess
import sys
import pytest

HUB_MODULES = [
    "app.storage.buffer",
    "app.storage.mongo_store",
    "app.utils.prediction_flow",
    "app.ai.ai_brain",
    "app.ai.ai_prediction_pipeline",
    "app.monitoring.self_learner",
]

CYCLE_PAIRS = [
    ("app.storage.buffer", "app.monitoring.self_learner"),
    ("app.storage.buffer", "app.storage.mongo_store"),
    ("app.storage.buffer", "app.ai.ai_brain"),
    ("app.storage.buffer", "app.ai.ai_prediction_pipeline"),
    ("app.storage.buffer", "app.utils.prediction_flow"),
    ("app.storage.mongo_store", "app.storage.buffer"),
    ("app.utils.prediction_flow", "app.ai.ai_brain"),
    ("app.utils.prediction_flow", "app.ai.ai_router"),
    ("app.utils.prediction_flow", "app.ai.ai_prediction_pipeline"),
    ("app.utils.prediction_flow", "app.monitoring.self_learner"),
    ("app.ai.ai_brain", "app.ai.ai_router"),
    ("app.ai.ai_prediction_pipeline", "app.ai.ai_router"),
    ("app.ai.ai_prediction_pipeline", "app.storage.buffer"),
    ("app.ai.ai_prediction_pipeline", "app.storage.mongo_store"),
    ("app.ai.ai_prediction_pipeline", "app.monitoring.self_learner"),
    ("app.monitoring.self_learner", "app.ai.ai_brain"),
    ("app.monitoring.self_learner", "app.storage.buffer"),
    ("app.monitoring.self_learner", "app.utils.prediction_flow"),
]


def _import_ok(module: str) -> bool:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@pytest.mark.parametrize("module_a,module_b", CYCLE_PAIRS)
def test_no_cycle_between_pair(module_a: str, module_b: str) -> None:
    """Both modules in a pair must be importable without circular ImportError."""
    assert _import_ok(module_a), f"ImportError when importing {module_a}"
    assert _import_ok(module_b), f"ImportError when importing {module_b}"


@pytest.mark.parametrize("module", HUB_MODULES)
def test_hub_module_importable(module: str) -> None:
    assert _import_ok(module), f"ImportError when importing hub module {module}"
