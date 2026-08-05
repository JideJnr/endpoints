# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/competition/sos.py
# This shim will be removed in v2.0.

from app.competition.sos import *  # noqa: F401,F403
from app.competition.sos import (  # noqa: F401
    LEAGUE_TIERS,
    OPPONENT_WEIGHT,
    analyse_schedule,
    compare_schedules,
)
