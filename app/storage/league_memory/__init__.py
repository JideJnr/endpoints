"""
league_memory package
~~~~~~~~~~~~~~~~~~~~~
Sub-package that replaces the monolithic league_memory.py.

Public import surface is identical to the original flat module so that
``from app.storage.league_memory import X`` continues to work everywhere.

Sub-modules:
    schema.py  — schema-ensure helpers (_ensure_signal_outcomes_table, etc.)
    _helpers.py — pure utility / row-building helpers
    crud.py    — direct SQLite read/write operations
    queries.py — domain-level analytical and grading queries
"""
from __future__ import annotations

# schema helpers (re-export _init_db for backward compat)
from .schema import _init_db, _ensure_signal_outcomes_table, _ensure_signal_combination_outcomes_table, _ensure_buffer_tables  # noqa: F401

# all public helper utilities
from ._helpers import (  # noqa: F401
    normalize_league,
)

# all public CRUD functions
from .crud import (  # noqa: F401
    observe_match,
    observe_matches,
    run_memory_maintenance,
    record_prediction,
    record_deferred_prediction_decision,
    store_local_signal_outcomes,
    list_memory_matches,
    list_duplicate_matches,
    get_memory_match,
    list_countries_from_memory,
    get_country_from_memory,
    get_league_detail_from_memory,
    list_prediction_history,
    list_prediction_decisions,
    save_betbuilder,
    list_betbuilder_history,
    grade_betbuilder_history,
    get_cached_team_history,
    store_team_history,
    set_engine_status,
    get_engine_states,
    store_enriched_matches,
    get_enriched_matches,
    get_enriched_match,
    patch_enriched_match_live,
    get_live_matches_from_buffer,
    track_user_behavior,
    get_user_behavior_summary,
    get_behavior_weighted_picks,
)

# all public query functions
from .queries import (  # noqa: F401
    get_league_memory,
    league_memory_for_match,
    get_snapshot_memory,
    late_goal_memory_signal,
    weighted_prediction_memory,
    weighted_candidate_memory,
    weighted_finished_match_memory,
    close_match_strength_context,
    grade_prediction,
    grade_predictions_for_date,
    grade_overdue_predictions,
    check_and_grade_match_result,
    grade_orphaned_predictions,
    get_grading_metrics,
    get_local_signal_stats,
    get_local_signal_combination_stats,
    weighted_signal_combination_memory,
    betbuilder_pick_memory,
)
