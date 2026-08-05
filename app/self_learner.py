# predictx/app/self_learner.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.monitoring.self_learner import *  # re-export full public API
from app.monitoring.self_learner import (
    run_learning_cycle,
    get_signal_weights,
    get_learned_weights,
    get_league_accuracy,
    get_top_signals,
    get_learning_summary,
)
