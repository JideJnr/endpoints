# predictx/app/ai_router.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.ai_router import *  # re-export full public API
from app.ai.ai_router import (
    AIRouter,
    parse_json_response,
    parse_json_safe,
    get_router,
)
