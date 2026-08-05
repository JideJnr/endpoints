# predictx/app/agentic_prediction.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.agentic_prediction import *  # re-export full public API
from app.ai.agentic_prediction import (
    AgentAction,
    AgentExecutionError,
    run_agentic_match_prediction,
)
