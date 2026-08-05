# predictx/app/match_state.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.utils.match_state import *  # re-export full public API
from app.utils.match_state import (
    NOT_STARTED_PERIODS,
    FINISHED_PERIODS,
    POSTPONED_PERIODS,
    CANCELLED_PERIODS,
    SUSPENDED_PERIODS,
    LIVE_PERIODS,
    classify_match_state,
    is_live_match,
    is_finished_match,
    is_prematch,
)
