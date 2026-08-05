"""
Storage domain package.

Persistence layer — SQLite (db.py), MongoDB (mongo_store.py), league memory, match buffer.

Import domain modules directly:
    from app.storage.db import DB_PATH, db_conn, get_db, _init_db
    from app.storage.mongo_store import is_configured, archive_finished_match_from_buffer
    from app.storage.league_memory import observe_match, record_prediction
    from app.storage.buffer import ingest_matches, get_buffered_matches

Note: Eager re-exports are intentionally omitted here to prevent circular imports.
The storage sub-modules import from each other (e.g. buffer imports league_memory,
mongo_store imports db) — Python's import system handles intra-package references
correctly when modules are imported directly.

Public symbols by module:

db.py
    DB_PATH, DEFAULT_TIMEOUT_SECONDS, DEFAULT_BUSY_TIMEOUT_MS
    configure_connection, connect_db, connect_readonly_db, get_db, close_db, db_conn
    is_sqlite_lock, _conn, _DB_SCHEMA_READY, _DB_SCHEMA_LOCK
    _ensure_column, _ensure_prediction_history_columns, _existing_schema_can_be_trusted
    _init_db, _init_db_unlocked, _is_sqlite_lock, _run_legacy_backfills, _run_schema_migrations

mongo_store.py
    is_configured, init_mongo, archive_finished_match_from_buffer
    list_finished_matches, get_finished_match, get_finished_match_by_sofascore_id
    get_team_finished_matches, store_signal_outcomes, get_signal_stats
    prune_old_finished_matches, mongo_status, flush_buffer_to_mongo
    cleanup_buffer, store_scheduled_matches, store_enriched_matches
    get_enriched_match, get_enriched_matches, save_odds_snapshot, save_finished_match

league_memory.py
    observe_match, observe_matches, league_memory_for_match, get_league_memory
    get_snapshot_memory, list_memory_matches, list_duplicate_matches, get_memory_match
    list_countries_from_memory, get_country_from_memory, get_league_detail_from_memory
    run_memory_maintenance, record_prediction, record_deferred_prediction_decision
    normalize_league, grade_prediction, grade_predictions_for_date, grade_overdue_predictions
    check_and_grade_match_result, get_grading_metrics, list_prediction_history
    list_prediction_decisions, save_betbuilder, list_betbuilder_history, grade_betbuilder_history
    betbuilder_pick_memory, weighted_prediction_memory, weighted_candidate_memory
    weighted_finished_match_memory, close_match_strength_context
    store_local_signal_outcomes, get_local_signal_stats
    patch_enriched_match_live, get_live_matches_from_buffer, late_goal_memory_signal
    get_cached_team_history, store_team_history, set_engine_status, get_engine_states
    track_user_behavior, get_user_behavior_summary, get_behavior_weighted_picks
    grade_orphaned_predictions

buffer.py
    ingest_matches, patch_live_scores, get_unenriched_batch, store_enriched
    get_buffered_matches, get_buffered_match, get_live_buffered_matches
    refresh_sporty_buffer_scope, refresh_sporty_match_state, purge_ghost_matches
    get_buffer_stats, run_enrichment_worker, run_date_aware_enrichment
    ENRICH_WORKERS, WEB_WORKERS, ENRICH_BATCH_SIZE
    NO_MATCH_RETRY_MINUTES, NO_MATCH_MIN_RETRY_LIVE_MINUTES
"""
