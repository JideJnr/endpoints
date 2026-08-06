"""
Enrichment domain package.

Match enrichment pipeline — signal aggregation, confidence calibration, contextual intelligence.

Modules in this package (moved here from predictx/app/ in task 2.8):
  - enrichment.py            — sofascore/sportybet match matching and enrichment runner
  - enriched_prediction.py   — orchestration entry point for full match prediction enrichment
  - match_enrichment.py      — buffered match enrichment, sofascore resolution
  - match_intelligence.py    — build_match_intelligence: composite intelligence struct
  - signal_aggregator.py     — signal normalisation, categorisation, aggregation
  - confidence_calibrator.py — confidence calibration against graded history
  - contextual_intelligence.py — contextual adjustment, relationship intelligence
  - web_context.py           — web search context for matches, teams, and leagues
  - similar_matches.py       — find historically similar matches via ELO/odds proximity

See docs/migration_checklist.md for full migration history.

Import ordering note: enrichment.py is imported first because match_enrichment.py
imports its symbols (FUZZY_THRESHOLD, LLM_FALLBACK_THRESHOLD, _fuzzy_match, etc.)
via `from app.enrichment.enrichment import ...`. By populating this namespace from enrichment.py
before match_enrichment is loaded, we avoid a circular-import deadlock.
"""
# noqa: F401  # DEPRECATED shim — see migration_checklist.md

# ---- Must be first: match_enrichment.py imports these from app.enrichment.enrichment ----
from app.enrichment.enrichment import (  # noqa: F401
    FUZZY_THRESHOLD,
    LLM_FALLBACK_THRESHOLD,
    DETAIL_WORKERS,
    WEB_WORKERS,
    JUNK_MARKERS,
    run_enrichment,
    _fuzzy_match,
    _llm_match,
    _is_junk,
    _event_score,
)

# ---- signal_aggregator, confidence_calibrator, contextual_intelligence, web_context ----
# (no intra-package circular deps)
from app.enrichment.signal_aggregator import (  # noqa: F401
    SIGNAL_CATEGORIES,
    normalize_signal,
    SignalAggregator,
    calculate_win_probabilities,
    score_pick_direction,
)

from app.enrichment.confidence_calibrator import (  # noqa: F401
    MIN_SAMPLES,
    DOUBLE_DOWN_MIN_SAMPLES,
    BLEND_WEIGHT,
    UNIQUE_GRADED_HISTORY,
    rebuild_calibration,
    calibrate_confidence,
    get_calibration_table,
    compute_calibration_gap,
    get_calibration_gap_report,
    stake_multiplier,
)

from app.enrichment.web_context import (  # noqa: F401
    search_match_context,
    search_team_context,
    search_league_sentiment,
    context_for_match,
)

from app.enrichment.contextual_intelligence import (  # noqa: F401
    build_contextual_intelligence,
    apply_contextual_adjustment,
    builder_relationship_intelligence,
)

from app.enrichment.similar_matches import (  # noqa: F401
    find_similar_matches,
)

from app.enrichment.match_intelligence import (  # noqa: F401
    build_match_intelligence,
)

# ---- match_enrichment imports from app.enrichment.enrichment — must come after enrichment.py symbols ----
from app.enrichment.match_enrichment import (  # noqa: F401
    MatchEnrichmentError,
    enrich_buffered_match,
)

# ---- enriched_prediction is the largest module; import last ----
from app.enrichment.enriched_prediction import (  # noqa: F401
    LONGSHOT_MIN_DECIMAL_ODDS,
    NOISY_SUPPORT_SIGNALS,
    MODIFIER_ONLY_SIGNALS,
    BACKGROUND_CONTEXT_SIGNALS,
    RISK_SIGNALS,
    get_feature_importance,
    prediction_readiness,
    predict_enriched_match,
)

__all__ = [
    # enrichment
    "FUZZY_THRESHOLD",
    "LLM_FALLBACK_THRESHOLD",
    "DETAIL_WORKERS",
    "WEB_WORKERS",
    "JUNK_MARKERS",
    "run_enrichment",
    # enriched_prediction
    "LONGSHOT_MIN_DECIMAL_ODDS",
    "NOISY_SUPPORT_SIGNALS",
    "MODIFIER_ONLY_SIGNALS",
    "BACKGROUND_CONTEXT_SIGNALS",
    "RISK_SIGNALS",
    "get_feature_importance",
    "prediction_readiness",
    "predict_enriched_match",
    # match_enrichment
    "MatchEnrichmentError",
    "enrich_buffered_match",
    # match_intelligence
    "build_match_intelligence",
    # signal_aggregator
    "SIGNAL_CATEGORIES",
    "normalize_signal",
    "SignalAggregator",
    "calculate_win_probabilities",
    "score_pick_direction",
    # confidence_calibrator
    "MIN_SAMPLES",
    "DOUBLE_DOWN_MIN_SAMPLES",
    "BLEND_WEIGHT",
    "UNIQUE_GRADED_HISTORY",
    "rebuild_calibration",
    "calibrate_confidence",
    "get_calibration_table",
    "compute_calibration_gap",
    "get_calibration_gap_report",
    "stake_multiplier",
    # contextual_intelligence
    "build_contextual_intelligence",
    "apply_contextual_adjustment",
    "builder_relationship_intelligence",
    # web_context
    "search_match_context",
    "search_team_context",
    "search_league_sentiment",
    "context_for_match",
    # similar_matches
    "find_similar_matches",
]

