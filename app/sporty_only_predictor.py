# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.sporty_only_predictor — redirects to app.models.sporty_only_predictor.
This file will be removed in v2.0. Update imports to: from app.models.sporty_only_predictor import ...
"""
from app.models.sporty_only_predictor import *  # noqa: F401, F403
from app.models.sporty_only_predictor import (  # noqa: F401
    predict_from_sporty,
    extract_sporty_signals,
    _find_market,
    _outcome_prob,
    _outcome_odds,
)
