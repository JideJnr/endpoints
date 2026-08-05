# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/competition/competition_special.py
# This shim will be removed in v2.0.

from app.competition.competition_special import *  # noqa: F401,F403
from app.competition.competition_special import (  # noqa: F401
    DEFAULT_WORLD_CUP,
    TOP_30_COMPETITIONS,
    apply_known_competition_context,
    list_top_competitions,
    init_competition_tables,
    get_competition_settings,
    update_competition_settings,
    sync_competition_fixtures,
    list_competition_buffer,
    competition_status,
    list_team_watchers,
    get_team_watcher,
    enrich_predict_competition,
    ensure_competition_main_buffer,
    run_competition_special_cycle,
    run_enabled_competition_cycles,
    refresh_competition_context,
    purge_misclassified_competition_rows,
    list_all_competition_summaries,
)
