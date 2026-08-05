"""
Utils domain package.

Cross-cutting utilities — normalisation, time context, activity log, health counters,
match state/view, prediction flow, portfolio, bot2, mobile bridge, and desk analytics.

All modules have been migrated from predictx/app/ into this domain package.
See docs/migration_checklist.md for the full list of moves.

Submodules:
  - normalise           REPLACEMENTS, normalise
  - time_context        timezone_for_match, match_time_context
  - activity_log        record_activity, mark_idle, get_activity
  - health_counters     record_health_event, health_counter_snapshot
  - match_state         classify_match_state, is_live_match, is_finished_match, is_prematch
  - match_view          match_summary, extract_1x2, home_team, away_team, team_from_name
  - prediction_flow     PredictionDeferred, predict_and_record_enriched, apply_prediction_state
  - current_predictions list_recent_dashboard_predictions
  - live_retry_queue    mark_pending, mark_resolved, expire_stale_entries, list_active
  - bot2                run_bot2
  - mobile_bridge       receive_provider_packet, ingest_packet, list_provider_packets
  - portfolio           filter_correlated, portfolio_summary
  - desk_analytics      signal_attribution_report, backtest_gate, desk_observability
"""
# Exports are available via direct submodule imports to avoid circular import chains.
# Example: from app.utils.normalise import normalise
