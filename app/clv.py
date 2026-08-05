# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/risk/clv.py
# This shim will be removed in v2.0.

from app.risk.clv import *  # noqa: F401,F403
from app.risk.clv import (  # noqa: F401
    CLV_MIN_SAMPLES,
    record_clv_entry,
    compute_clv_for_date,
    get_clv_summary,
    clv_stake_multiplier,
)
