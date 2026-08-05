# predictx/app/prediction_monitor.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.monitoring.prediction_monitor import *  # re-export full public API
from app.monitoring.prediction_monitor import (
    run_prediction_monitor,
    latest_prediction_monitor_snapshots,
)
