# predictx/app/time_context.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.utils.time_context import *  # re-export full public API
from app.utils.time_context import (
    DEFAULT_LOCAL_TZ,
    COUNTRY_TIMEZONES,
    timezone_for_match,
    match_time_context,
)
