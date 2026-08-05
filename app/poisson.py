# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.poisson — redirects to app.models.poisson.
This file will be removed in v2.0. Update imports to: from app.models.poisson import ...
"""
from app.models.poisson import *  # noqa: F401, F403
from app.models.poisson import (  # noqa: F401
    run_poisson,
    MAX_GOALS,
    HOME_ADVANTAGE,
    _team_stats,
    _local_team_matches,
    _poisson_prob,
    _to_int,
)
