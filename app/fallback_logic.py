# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/risk/fallback_logic.py
# This shim will be removed in v2.0.

from app.risk.fallback_logic import *  # noqa: F401,F403
from app.risk.fallback_logic import (  # noqa: F401
    FALLBACK_CONFIG,
    FallbackHandler,
    get_fallback_pick,
)
