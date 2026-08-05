# predictx/app/system_audit.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.monitoring.system_audit import *  # re-export full public API
from app.monitoring.system_audit import (
    prediction_system_audit,
)
