# predictx/app/system_supervisor.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.monitoring.system_supervisor import *  # re-export full public API
from app.monitoring.system_supervisor import (
    run_system_supervisor,
    latest_supervisor_snapshots,
)
