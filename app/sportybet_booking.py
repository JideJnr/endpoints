# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.sportybet_booking — redirects to app.data_clients.sportybet_booking.
This file will be removed in v2.0. Update imports to: from app.data_clients.sportybet_booking import ...
"""
from app.data_clients.sportybet_booking import *  # noqa: F401, F403
from app.data_clients.sportybet_booking import (  # noqa: F401
    _resolve_sportybet_id,
    build_booking_payload,
    request_share_code,
    _resolve_leg,
    _find_market_outcome,
    _market_named,
    _outcome_matches,
    _find_share_code,
    _provider_error,
)
