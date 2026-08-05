# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.dixon_coles — redirects to app.models.dixon_coles.
This file will be removed in v2.0. Update imports to: from app.models.dixon_coles import ...
"""
from app.models.dixon_coles import *  # noqa: F401, F403
from app.models.dixon_coles import (  # noqa: F401
    run_dixon_coles,
    _tau,
    MAX_GOALS,
    HOME_ADVANTAGE,
    RHO,
)
