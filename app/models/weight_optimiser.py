from __future__ import annotations

from typing import Any


def optimise_ensemble_weights() -> dict[str, Any]:
    """Refresh model weights from graded prediction history."""
    from app.monitoring.self_learner import get_learned_weights, run_learning_cycle

    result = run_learning_cycle()
    return {
        "status": result.get("status"),
        "learning": result,
        "weights": get_learned_weights(),
    }
