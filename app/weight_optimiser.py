# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.weight_optimiser — redirects to app.models.weight_optimiser.
This file will be removed in v2.0. Update imports to: from app.models.weight_optimiser import ...
"""
from app.models.weight_optimiser import *  # noqa: F401, F403
from app.models.weight_optimiser import (  # noqa: F401
    optimise_ensemble_weights,
    get_current_weights,
)
