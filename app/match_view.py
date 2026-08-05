# predictx/app/match_view.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.utils.match_view import *  # re-export full public API
from app.utils.match_view import (
    match_summary,
    extract_1x2,
    home_team,
    away_team,
    team_from_name,
)
