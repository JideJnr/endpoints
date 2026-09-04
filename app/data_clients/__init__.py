"""
Data clients domain package.

External sports data clients — SofaScore, SportyBet, Sportradar, sofa pipeline.

Public API is re-exported here so callers can do:
    from app.data_clients import fetch_event_detail
    from app.data_clients import fetch_matches_post
    from app.data_clients import fetch_match_intelligence
    from app.data_clients import fetch_prematch_intelligence
    from app.data_clients import ingest_from_sofascore
"""

# SofaScore client
from app.data_clients.sofascore_client import (  # noqa: F401
    fetch_all_scheduled_events,
    fetch_event_detail,
    fetch_live_events,
    is_usable_event_for_mode,
    is_terminal_event,
)

# SofaScore grades
from app.data_clients.sofascore_grades import (  # noqa: F401
    get_team_rating_trend,
    grade_signal_for_match,
)

# SportyBet client
from app.data_clients.sportybet_client import (  # noqa: F401
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
)

# SportyBet booking
from app.data_clients.sportybet_booking import (  # noqa: F401
    build_booking_payload,
    request_share_code,
)

# Sportradar client
from app.data_clients.sportradar_client import (  # noqa: F401
    fetch_match_intelligence,
    normalize_match_id,
    summarize_match_payload,
)

# Sportradar GISMO client
from app.data_clients.sportradar_gismo_client import (  # noqa: F401
    fetch_prematch_intelligence,
    fetch_match_meta,
    fetch_season_data,
    extract_team_form,
    extract_standings_row,
    store_token,
)

# SofaScore-only pipeline
from app.data_clients.sofa_pipeline import (  # noqa: F401
    get_sofa_pipeline_mode,
    set_sofa_pipeline_mode,
    ingest_from_sofascore,
    enrich_sofa_pipeline,
    run_sofa_pipeline_cycle,
)
