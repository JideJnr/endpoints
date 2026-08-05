# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.sportybet_client — redirects to app.data_clients.sportybet_client.
This file will be removed in v2.0. Update imports to: from app.data_clients.sportybet_client import ...
"""
from app.data_clients.sportybet_client import *  # noqa: F401, F403
from app.data_clients.sportybet_client import (  # noqa: F401
    SPORTYBET_HOME_URL,
    SPORTYBET_POST_URL,
    SPORTYBET_RESULTS_URL,
    _USER_AGENTS,
    _IMPERSONATIONS,
    _session,
    _session_warmed,
    _last_ua,
    _new_session,
    _get_session,
    _warm_session,
    _browser_headers,
    _post_with_retry,
    _get_with_retry,
    _MATCH_LIST_CACHE_TTL,
    _match_list_cache,
    _match_list_cache_get,
    _match_list_cache_set,
    fetch_matches_post,
    parse_events_response,
    fetch_live_matches_post,
    fetch_upcoming_matches_post,
    fetch_live_and_upcoming_matches_post,
    fetch_match_info,
    fetch_live_matches,
    fetch_upcoming_matches,
    fetch_live_and_upcoming_matches,
    fetch_results,
    _parse_result,
    _parse_event,
    _parse_market,
    _first_present,
)
