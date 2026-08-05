# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.league_memory — redirects to app.storage.league_memory.
This file will be removed in v2.0. Update imports to: from app.storage.league_memory import ...
"""
from app.storage.league_memory import *  # noqa: F401, F403
from app.storage.db import _init_db  # noqa: F401  # re-exported for buffer shim compatibility
