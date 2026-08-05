# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.db — redirects to app.storage.db.
This file will be removed in v2.0. Update imports to: from app.storage.db import ...
"""
from app.storage.db import *  # noqa: F401, F403
from app.storage.db import (  # noqa: F401
    _conn,
    _init_db,
    _init_db_unlocked,
    _ensure_column,
    _is_sqlite_lock,
    _DB_SCHEMA_READY,
    _DB_SCHEMA_LOCK,
    _run_schema_migrations,
    _run_legacy_backfills,
    _existing_schema_can_be_trusted,
    _ensure_prediction_history_columns,
)
