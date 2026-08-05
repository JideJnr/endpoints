# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation has moved to
# app/market/odds_pattern.py. Import from app.market.odds_pattern or app.market directly.
from app.market.odds_pattern import *  # noqa: F401, F403
from app.market.odds_pattern import (  # noqa: F401
    extract_pattern,
    grade_patterns_for_date,
    pattern_signal,
    pattern_stats,
)
