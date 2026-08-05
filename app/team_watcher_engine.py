# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The module has been moved to
# app.team_watcher.team_watcher_engine. Import from there for new code.
from app.team_watcher.team_watcher_engine import *  # noqa: F401, F403
from app.team_watcher.team_watcher_engine import (  # noqa: F401
    init_tw_tables,
    get_team_weights,
    team_watcher_signal,
    record_tw_prediction,
    grade_tw_predictions,
    update_tw_weights,
    generate_weekly_analysis,
    monitor_team_performance,
    regrade_void_predictions,
)
