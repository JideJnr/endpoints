# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.sportradar_gismo_client — redirects to app.data_clients.sportradar_gismo_client.
This file will be removed in v2.0. Update imports to: from app.data_clients.sportradar_gismo_client import ...
"""
from app.data_clients.sportradar_gismo_client import *  # noqa: F401, F403
from app.data_clients.sportradar_gismo_client import (  # noqa: F401
    BASE,
    HEADERS,
    _season_cache,
    _SEASON_CACHE_TTL,
    _get_token,
    _fetch_fresh_token,
    store_token,
    _fetch,
    _doc_data,
    fetch_match_meta,
    fetch_season_data,
    extract_team_form,
    extract_standings_row,
    fetch_prematch_intelligence,
)
