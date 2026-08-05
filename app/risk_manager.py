# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/risk/risk_manager.py
# This shim will be removed in v2.0.

from app.risk.risk_manager import *  # noqa: F401,F403
from app.risk.risk_manager import (  # noqa: F401
    apply_risk_controls,
    MAX_SINGLE_BET_STAKE_PER_100,
    MAX_DEGRADED_STAKE_PER_100,
    MAX_HIGH_RISK_STAKE_PER_100,
    LONGSHOT_ODDS,
    EXTREME_LONGSHOT_ODDS,
    LEARNED_RISK_MIN_SAMPLES,
)
