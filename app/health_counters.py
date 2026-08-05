# predictx/app/health_counters.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.utils.health_counters import *  # re-export full public API
from app.utils.health_counters import (
    record_health_event,
    health_counter_snapshot,
)
