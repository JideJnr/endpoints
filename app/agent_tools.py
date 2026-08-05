# predictx/app/agent_tools.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.agent_tools import *  # re-export full public API
from app.ai.agent_tools import (
    get_scheduled_matches,
    get_event_detail,
    get_team_history,
    get_standings,
    get_event_h2h,
    get_pregame_form,
    get_event_odds,
    get_featured_odds,
    get_live_matches,
    get_all_sportybet_matches,
    poisson_model,
    get_odds_movement,
    get_all_odds_movements,
    strength_of_schedule,
    ALL_TOOLS,
)
