# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.odds_predictor — redirects to app.models.odds_predictor.
This file will be removed in v2.0. Update imports to: from app.models.odds_predictor import ...
"""
from app.models.odds_predictor import *  # noqa: F401, F403
from app.models.odds_predictor import (  # noqa: F401
    odds_only_prediction,
    _extract_1x2,
    _tournament_name,
)
