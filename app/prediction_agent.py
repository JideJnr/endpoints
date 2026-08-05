# predictx/app/prediction_agent.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.prediction_agent import *  # re-export full public API
from app.ai.prediction_agent import (
    predict_sofascore_event,
    predict_sporty_match,
    form_trajectory_signal,
)
