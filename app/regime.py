# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation has moved to
# app/market/regime.py. Import from app.market.regime or app.market directly.
from app.market.regime import *  # noqa: F401, F403
from app.market.regime import (  # noqa: F401
    Regime,
    TIER_1,
    TIER_2,
    TIER_3,
    TIER_4,
    get_regime,
    get_regime_for_doc,
    passes_regime_gate,
    apply_regime_stake_cap,
    regime_summary_for_predictions,
)
