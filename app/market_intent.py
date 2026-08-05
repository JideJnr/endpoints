# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation has moved to
# app/market/market_intent.py. Import from app.market.market_intent or app.market directly.
from app.market.market_intent import *  # noqa: F401, F403
from app.market.market_intent import (  # noqa: F401
    normalise_market_text,
    classify_market_intent,
    parse_total_line,
    grade_market_intent,
    selection_key,
)
