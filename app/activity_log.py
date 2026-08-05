# predictx/app/activity_log.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.utils.activity_log import *  # re-export full public API
from app.utils.activity_log import (
    record_activity,
    mark_idle,
    get_activity,
)
