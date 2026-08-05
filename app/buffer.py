# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.buffer — redirects to app.storage.buffer.
This file will be removed in v2.0. Update imports to: from app.storage.buffer import ...
"""
from app.storage.buffer import *  # noqa: F401, F403
