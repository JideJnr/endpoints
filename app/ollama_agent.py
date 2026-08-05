# predictx/app/ollama_agent.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.ollama_agent import *  # re-export full public API
from app.ai.ollama_agent import (
    is_ollama_available,
    run_ollama_match_analysis,
    run_ollama_all_models,
    run_ollama_predictions,
)
