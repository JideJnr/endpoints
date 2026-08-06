# predictx/app/prediction_agent.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.prediction_agent import *  # re-export full public API
from app.ai.prediction_agent import (
    predict_sofascore_event,
    predict_sporty_match,
    form_trajectory_signal,
    _apply_time_decay,
    _is_high_late_goal_league,
    _time_decay_multiplier,
)
try:
    from app.team_watcher import team_watch_signal  # noqa: F401
except Exception:
    pass
