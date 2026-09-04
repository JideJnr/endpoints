"""Evidence-first AI prediction pipeline with a rules-engine fallback.

This module is now a thin backward-compatible facade. The implementation
was split out to app/ai/prediction_pipeline/ (weights.py, evidence.py,
teams.py, markets.py, orchestration.py) because this single file had grown
to 1,100+ lines mixing five unrelated concerns. This file is kept at the
same import path — and keeps re-exporting the same names — because several
modules import directly from ``app.ai.ai_prediction_pipeline`` (scheduler.py,
frontend.py, storage/league_memory/queries.py, monitoring/self_learner.py),
and routers/pipelines.py resolves ``job_ai_prediction_queue`` by dotted
string path (``"app.ai.ai_prediction_pipeline.job_ai_prediction_queue"``) at
runtime, so the path itself must keep working even though the code moved.

See app/ai/prediction_pipeline/__init__.py for the module-by-module
breakdown.
"""
from __future__ import annotations

# ── Specialist accuracy tracking ────────────────────────────────────────────
from app.ai.prediction_pipeline.weights import (
    SPECIALIST_NAMES,
    MIN_SPECIALIST_SAMPLES,
    get_specialist_weights,
    record_specialist_outcome,
    grade_specialist_contributions,
    get_specialist_summary,
)

# ── Evidence-gathering steps + shared match/odds/tier helpers ──────────────
from app.ai.prediction_pipeline.evidence import (
    H2H_FALLBACK,
    COMMON_FALLBACK,
    FORM_FALLBACK,
    ODDS_FALLBACK,
    SIMILAR_FALLBACK,
    classify_tournament_tier,
    sort_gate,
    apply_tier_filter,
    _evidence_status,
    _name,
    _teams,
    _tournament,
    _best_odds,
    _apply_recency_decay,
    _score,
    _build_h2h_statement,
    _classify_odds_movement,
    _step_h2h,
    _step_common_opponent,
    _step_form,
    _step_odds,
    _step_similar_matches,
    _step_team_history,
    _history_for_team,
    _previous_matches_for_team,
    _team_history_summary,
    _parse_sofa_last_matches,
)

# ── Team behaviour profiling ────────────────────────────────────────────────
from app.ai.prediction_pipeline.teams import (
    TeamBehaviourProfile,
    derive_team_profile,
    persist_team_profile,
)

# ── Market candidate shortlisting ───────────────────────────────────────────
from app.ai.prediction_pipeline.markets import (
    MarketCandidate,
    shortlist_markets,
)

# ── Orchestration: decider, rules fallback, entry point, job queue ─────────
from app.ai.prediction_pipeline.orchestration import (
    ReasoningContext,
    _truncate_competition_context,
    _get_competition_context,
    _get_structured_competition_intelligence,
    _call_decider,
    _convert_confidence,
    _rules_fallback,
    run_ai_prediction_with_fallback,
    job_ai_prediction_queue,
)

__all__ = [
    "SPECIALIST_NAMES",
    "MIN_SPECIALIST_SAMPLES",
    "get_specialist_weights",
    "record_specialist_outcome",
    "grade_specialist_contributions",
    "get_specialist_summary",
    "H2H_FALLBACK",
    "COMMON_FALLBACK",
    "FORM_FALLBACK",
    "ODDS_FALLBACK",
    "SIMILAR_FALLBACK",
    "classify_tournament_tier",
    "sort_gate",
    "apply_tier_filter",
    "TeamBehaviourProfile",
    "derive_team_profile",
    "persist_team_profile",
    "MarketCandidate",
    "shortlist_markets",
    "ReasoningContext",
    "run_ai_prediction_with_fallback",
    "job_ai_prediction_queue",
]
