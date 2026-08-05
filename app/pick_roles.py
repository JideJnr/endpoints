# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/risk/pick_roles.py
# This shim will be removed in v2.0.

from app.risk.pick_roles import *  # noqa: F401,F403
from app.risk.pick_roles import (  # noqa: F401
    learned_best_pick,
    load_role_memory_rows,
    backfill_role_learning,
    fast_role_memory,
    attach_fast_learned_decision,
)
