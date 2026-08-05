"""
Team watcher domain package.

Team watcher AI analysis — team briefs, injury/form insights via LLM.
Passive match observation, profile building, and prediction engine.
"""
# noqa: F401

from app.team_watcher.team_watcher import (  # noqa: F401
    init_team_watcher_tables,
    list_watchers,
    get_watcher,
    inspect_sporty_team_ids,
    observe_match,
    observe_finished_match_by_id,
    rebuild_all_profiles,
    backfill_from_finished,
    backfill_team_watcher_ids,
    team_context_for_match,
    team_watchers_for_match,
)

from app.team_watcher.team_watcher_engine import (  # noqa: F401
    init_tw_tables,
    get_team_weights,
    team_watcher_signal,
    record_tw_prediction,
    grade_tw_predictions,
    update_tw_weights,
    generate_weekly_analysis,
    monitor_team_performance,
    regrade_void_predictions,
)
