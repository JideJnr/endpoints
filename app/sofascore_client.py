# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.sofascore_client — redirects to app.data_clients.sofascore_client.
This file will be removed in v2.0. Update imports to: from app.data_clients.sofascore_client import ...
"""
from app.data_clients.sofascore_client import *  # noqa: F401, F403
from app.data_clients.sofascore_client import (  # noqa: F401
    _HOME_HEADERS,
    _LIST_CACHE_TTL_SCHEDULED,
    _LIST_CACHE_TTL_LIVE,
    _list_cache,
    _category_cache,
    DB_CACHE_TTL_TODAY_SECONDS,
    DB_CACHE_TTL_FUTURE_SECONDS,
    DB_CACHE_TTL_PAST_SECONDS,
    _ttl_for_date,
    _db_cache_get,
    _db_cache_set,
    _list_cache_get,
    _list_cache_set,
    _new_session,
    _session,
    _status_text,
    _get,
    _events_from_response,
    _get_events,
    _CORE_TOURNAMENT_IDS,
    _get_learned_tournament_ids,
    _fetch_tournament_events,
    _fetch_category_events,
    _scheduled_category_ids,
    _team_history_events,
    _parse_standing_row,
    _parse_manager,
    _parse_odds_market,
    _parse_event,
)
