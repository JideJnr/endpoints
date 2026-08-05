# predictx/app/portfolio.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.utils.portfolio import *  # re-export full public API
from app.utils.portfolio import (
    MAX_PER_DIRECTION,
    MAX_PER_LEAGUE,
    MAX_PER_WINDOW,
    MAX_PORTFOLIO_SIZE,
    WINDOW_SECONDS,
    filter_correlated,
    portfolio_summary,
)
