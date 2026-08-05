# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation has moved to
# app/market/season_stage.py. Import from app.market.season_stage or app.market directly.
from app.market.season_stage import *  # noqa: F401, F403
from app.market.season_stage import (  # noqa: F401
    SMALL_LEAGUE_MAX,
    MEDIUM_LEAGUE_MAX,
    classify_table_size,
    detect_season_stage,
    season_aware_table_weight,
)
