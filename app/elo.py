# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.elo — redirects to app.models.elo.
This file will be removed in v2.0. Update imports to: from app.models.elo import ...
"""
from app.models.elo import *  # noqa: F401, F403
from app.models.elo import (  # noqa: F401
    get_elo,
    update_elo,
    record_match_result_once,
    elo_prediction,
    K_FACTOR,
    HOME_ADVANTAGE_ELO,
    _init_elo_table,
)
