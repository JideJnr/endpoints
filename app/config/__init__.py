"""
Config domain package.

Application configuration — pydantic Settings, environment variable loading.

Modules will be moved here from predictx/app/ in tasks 2.3–2.15.
See docs/migration_checklist.md for the full list of planned moves.
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
