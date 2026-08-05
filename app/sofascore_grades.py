# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.sofascore_grades — redirects to app.data_clients.sofascore_grades.
This file will be removed in v2.0. Update imports to: from app.data_clients.sofascore_grades import ...
"""
from app.data_clients.sofascore_grades import *  # noqa: F401, F403
from app.data_clients.sofascore_grades import (  # noqa: F401
    _init_grades_table,
    extract_team_grades,
    _extract_side_grades,
    store_match_grades,
    get_team_rating_trend,
    grade_signal_for_match,
)
