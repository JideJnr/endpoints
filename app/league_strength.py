# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/competition/league_strength.py
# This shim will be removed in v2.0.

from app.competition.league_strength import *  # noqa: F401,F403
from app.competition.league_strength import (  # noqa: F401
    COUNTRY_STRENGTH,
    COMPETITION_STRENGTH,
    DIVISION_OFFSETS,
    LOWER_CONTEXT_OFFSETS,
    league_strength_score,
    history_league_strength,
    league_strength_edge,
)
