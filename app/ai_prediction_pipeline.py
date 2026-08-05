# predictx/app/ai_prediction_pipeline.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.ai_prediction_pipeline import *  # re-export full public API
from app.ai.ai_prediction_pipeline import (
    get_specialist_weights,
    record_specialist_outcome,
    grade_specialist_contributions,
    get_specialist_summary,
    TeamBehaviourProfile,
    ReasoningContext,
    MarketCandidate,
    classify_tournament_tier,
    sort_gate,
    apply_tier_filter,
    derive_team_profile,
    persist_team_profile,
    shortlist_markets,
    run_ai_prediction_with_fallback,
    job_ai_prediction_queue,
)
