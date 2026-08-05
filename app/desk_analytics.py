# predictx/app/desk_analytics.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.utils.desk_analytics import *  # re-export full public API
from app.utils.desk_analytics import (
    signal_attribution_report,
    backtest_gate,
    desk_observability,
)
