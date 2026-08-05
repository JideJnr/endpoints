# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.ensemble — redirects to app.models.ensemble.
This file will be removed in v2.0. Update imports to: from app.models.ensemble import ...
"""
from app.models.ensemble import *  # noqa: F401, F403
from app.models.ensemble import (  # noqa: F401
    ensemble_prediction,
    compute_ensemble_diversity,
    WEIGHTS,
    _get_weights,
    _BASE_WEIGHTS,
)
