from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.utils.match_state import classify_match_state


from app.utils.match_helpers import _to_datetime_utc

DEFAULT_LOCAL_TZ = "Africa/Lagos"

COUNTRY_TIMEZONES = {
    "argentina": "America/Argentina/Buenos_Aires",
    "australia": "Australia/Sydney",
    "austria": "Europe/Vienna",
    "belgium": "Europe/Brussels",
    "brazil": "America/Sao_Paulo",
    "bulgaria": "Europe/Sofia",
    "canada": "America/Toronto",
    "chile": "America/Santiago",
    "china": "Asia/Shanghai",
    "colombia": "America/Bogota",
    "croatia": "Europe/Zagreb",
    "czech republic": "Europe/Prague",
    "denmark": "Europe/Copenhagen",
    "ecuador": "America/Guayaquil",
    "england": "Europe/London",
    "finland": "Europe/Helsinki",
    "france": "Europe/Paris",
    "germany": "Europe/Berlin",
    "ghana": "Africa/Accra",
    "greece": "Europe/Athens",
    "india": "Asia/Kolkata",
    "indonesia": "Asia/Jakarta",
    "ireland": "Europe/Dublin",
    "israel": "Asia/Jerusalem",
    "italy": "Europe/Rome",
    "japan": "Asia/Tokyo",
    "kenya": "Africa/Nairobi",
    "mexico": "America/Mexico_City",
    "morocco": "Africa/Casablanca",
    "netherlands": "Europe/Amsterdam",
    "nigeria": "Africa/Lagos",
    "norway": "Europe/Oslo",
    "paraguay": "America/Asuncion",
    "peru": "America/Lima",
    "poland": "Europe/Warsaw",
    "portugal": "Europe/Lisbon",
    "romania": "Europe/Bucharest",
    "russia": "Europe/Moscow",
    "saudi arabia": "Asia/Riyadh",
    "scotland": "Europe/London",
    "serbia": "Europe/Belgrade",
    "south africa": "Africa/Johannesburg",
    "south korea": "Asia/Seoul",
    "spain": "Europe/Madrid",
    "sweden": "Europe/Stockholm",
    "switzerland": "Europe/Zurich",
    "turkey": "Europe/Istanbul",
    "ukraine": "Europe/Kyiv",
    "uruguay": "America/Montevideo",
    "usa": "America/New_York",
    "united states": "America/New_York",
    "wales": "Europe/London",
}


def timezone_for_match(match: dict[str, Any]) -> str:
    """Infer the match-country timezone from SportyBet/SofaScore metadata."""
    candidates: list[str] = []
    for key in ("country", "category", "tournament", "uniqueTournament"):
        value = match.get(key)
        if isinstance(value, dict):
            candidates.extend(str(value.get(k) or "") for k in ("name", "slug"))
        elif value:
            candidates.append(str(value))

    sofa = match.get("sofascore_event")
    if isinstance(sofa, dict):
        tournament = sofa.get("tournament") or {}
        category = tournament.get("category") if isinstance(tournament, dict) else {}
        if isinstance(category, dict):
            candidates.extend(str(category.get(k) or "") for k in ("name", "slug"))
        if isinstance(tournament, dict):
            candidates.extend(str(tournament.get(k) or "") for k in ("name", "slug"))

    detail = match.get("sofascore_detail")
    if isinstance(detail, dict):
        tournament = detail.get("tournament") or {}
        category = tournament.get("category") if isinstance(tournament, dict) else {}
        if isinstance(category, dict):
            candidates.extend(str(category.get(k) or "") for k in ("name", "slug"))

    text = " ".join(candidates).lower().replace("-", " ")
    for country, tz in COUNTRY_TIMEZONES.items():
        if country in text:
            return tz
    return DEFAULT_LOCAL_TZ


def match_time_context(match: dict[str, Any], local_tz: str | None = None) -> dict[str, Any]:
    """Return UTC and local kickoff metadata for SportyBet/SofaScore timestamps."""
    start_time = (
        match.get("start_time")
        or match.get("start_timestamp")
        or match.get("startTimestamp")
        or ((match.get("sofascore_event") or {}).get("start_timestamp") if isinstance(match.get("sofascore_event"), dict) else None)
    )
    resolved_tz = local_tz or timezone_for_match(match)
    dt_utc = _to_datetime_utc(start_time)
    now_utc = datetime.now(timezone.utc)
    local_zone = ZoneInfo(resolved_tz)
    now_local = now_utc.astimezone(local_zone)

    context: dict[str, Any] = {
        "timezone": resolved_tz,
        "timezone_source": "explicit" if local_tz else "match_country",
        "now_utc": now_utc.isoformat(),
        "now_local": now_local.isoformat(),
        "start_raw": start_time,
        "match_state": classify_match_state(match, now=now_utc),
    }
    if dt_utc:
        start_local = dt_utc.astimezone(local_zone)
        context.update(
            {
                "start_utc": dt_utc.isoformat(),
                "start_local": start_local.isoformat(),
                "local_date": start_local.date().isoformat(),
                "local_time": start_local.strftime("%H:%M"),
                "utc_date": dt_utc.date().isoformat(),
                "utc_time": dt_utc.strftime("%H:%M"),
                "minutes_until_kickoff": round((dt_utc - now_utc).total_seconds() / 60),
            }
        )
    return context


