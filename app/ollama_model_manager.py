# predictx/app/ollama_model_manager.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.ollama_model_manager import *  # re-export full public API
from app.ai.ollama_model_manager import (
    preload_model,
    preload_all_models,
    is_model_loaded,
    start_keep_alive,
    stop_keep_alive,
    get_model_status,
)
