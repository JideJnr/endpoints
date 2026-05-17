from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_LOCAL_TZ = "Africa/Lagos"


def match_time_context(match: dict[str, Any], local_tz: str = DEFAULT_LOCAL_TZ) -> dict[str, Any]:
    """Return UTC and local kickoff metadata for SportyBet/SofaScore timestamps."""
    start_time = (
        match.get("start_time")
        or match.get("start_timestamp")
        or match.get("startTimestamp")
        or ((match.get("sofascore_event") or {}).get("start_timestamp") if isinstance(match.get("sofascore_event"), dict) else None)
    )
    dt_utc = _to_datetime_utc(start_time)
    now_utc = datetime.now(timezone.utc)
    local_zone = ZoneInfo(local_tz)
    now_local = now_utc.astimezone(local_zone)

    context: dict[str, Any] = {
        "timezone": local_tz,
        "now_utc": now_utc.isoformat(),
        "now_local": now_local.isoformat(),
        "start_raw": start_time,
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


def _to_datetime_utc(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 1e12:
                timestamp /= 1000
            elif timestamp > 1e10:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        text = str(value)
        if text.isdigit():
            return _to_datetime_utc(int(text))
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None
