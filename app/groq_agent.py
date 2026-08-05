# predictx/app/groq_agent.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.groq_agent import *  # re-export full public API
from app.ai.groq_agent import (
    run_groq_match_analysis,
    run_groq_predictions,
)
