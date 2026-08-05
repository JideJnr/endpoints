# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The module has been moved to
# app.team_watcher.team_watcher. Import from there for new code.
from app.team_watcher.team_watcher import *  # noqa: F401, F403
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
