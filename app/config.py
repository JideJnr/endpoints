# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.config — redirects to app.config.config.
This file will be removed in v2.0. Update imports to: from app.config.config import ...
"""
from app.config.config import (  # noqa: F401
    BASE_DIR,
    Settings,
    get_settings,
    invalidate_settings_cache,
    public_settings,
)

__all__ = [
    "BASE_DIR",
    "Settings",
    "get_settings",
    "invalidate_settings_cache",
    "public_settings",
]
