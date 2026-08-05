# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation has moved to
# app/market/market.py. Import from app.market.market or app.market directly.
from app.market.market import *  # noqa: F401, F403
from app.market.market import (  # noqa: F401
    snapshot_odds,
    get_movement,
    get_all_movements,
)
