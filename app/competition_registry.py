# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/competition/competition_registry.py
# This shim will be removed in v2.0.

from app.competition.competition_registry import *  # noqa: F401,F403
from app.competition.competition_registry import (  # noqa: F401
    init_competition_registry_tables,
    ensure_competition,
    get_competition,
    list_competitions,
    ensure_team_competition,
    update_team_competition_stats,
    record_team_prediction_outcome,
    add_performance_note,
    get_team_performance_notes,
    get_team_competition_stats,
    get_team_competition_history,
)
