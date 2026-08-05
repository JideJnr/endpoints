# predictx/app/ai_betbuilder.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.ai_betbuilder import *  # re-export full public API
from app.ai.ai_betbuilder import (
    enriched_match_analysis,
    build_ai_betbuilder,
    synthesize_sure_picks,
    upcoming_prediction_candidates,
    similarity_gate,
)
