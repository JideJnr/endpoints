# predictx/app/ollama_pipeline.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.ollama_pipeline import *  # re-export full public API
from app.ai.ollama_pipeline import (
    is_ollama_available,
    run_form_specialist,
    run_h2h_specialist,
    run_odds_specialist,
    run_standings_specialist,
    run_model_specialist,
    run_final_synthesis,
    run_brain_review,
    run_ollama_pipeline,
    run_ollama_pipeline_batch,
)
