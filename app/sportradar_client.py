# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.sportradar_client — redirects to app.data_clients.sportradar_client.
This file will be removed in v2.0. Update imports to: from app.data_clients.sportradar_client import ...
"""
from app.data_clients.sportradar_client import *  # noqa: F401, F403
from app.data_clients.sportradar_client import (  # noqa: F401
    SPORTRADAR_ALIAS,
    SPORTRADAR_LANGUAGE,
    SPORTRADAR_S5_BASE,
    SPORTRADAR_WIDGET_CLIENT_ID,
    SPORTRADAR_TIMEOUT_SECONDS,
    SPORTRADAR_MAX_RESPONSE_BYTES,
    fetch_match_intelligence,
    normalize_match_id,
    summarize_match_payload,
    _s5_url,
    _fetch_json,
    _headers,
    _find_first,
    _path_get,
    _walk_keys,
)
