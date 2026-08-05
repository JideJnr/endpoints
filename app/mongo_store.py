# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.mongo_store — redirects to app.storage.mongo_store.
This file will be removed in v2.0. Update imports to: from app.storage.mongo_store import ...
"""
from app.storage.mongo_store import *  # noqa: F401, F403
