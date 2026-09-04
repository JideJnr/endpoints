from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


from app.utils.primitives import _first_present, _to_int, _optional_int
from app.utils.match_helpers import _norm, _played_seconds, _to_datetime_utc

NOT_STARTED_PERIODS = {
    "",
    "not start",
    "not started",
    "notstart",
    "notstarted",
    "scheduled",
    "ns",
    "pre match",
    "prematch",
}

FINISHED_PERIODS = {
    "ft",
    "finished",
    "ended",
    "full time",
    "aet",
    "ap",
    "after penalties",
    "after extra time",
}

POSTPONED_PERIODS = {"postponed", "ppd"}
CANCELLED_PERIODS = {"cancelled", "canceled", "abandoned"}
SUSPENDED_PERIODS = {"suspended", "interrupted", "delayed"}
LIVE_PERIODS = {"h1", "1h", "h2", "2h", "ht", "et", "penalty", "pen", "live", "inplay", "in play"}


def classify_match_state(doc: dict[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    """Classify match lifecycle from provider state, preferring Sporty/Sportradar.

    Sporty status/period/kickoff are the primary source because every buffered
    row already carries the `sr:match:*` id. SofaScore is used only as a fallback
    when Sporty has no state at all.
    """
    doc = doc or {}
    sporty = _sporty_view(doc)
    sofa = _sofa_view(doc)
    source = "sportybet" if sporty.get("has_state") else "sofascore" if sofa.get("has_state") else "unknown"
    view = sporty if sporty.get("has_state") else sofa
    now_utc = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)

    period = _norm(view.get("period"))
    status_text = _norm(view.get("status_text"))
    status_type = _norm(view.get("status_type"))
    status_code = _to_int(view.get("status_code"))
    played_seconds = _played_seconds(view.get("played_seconds"))
    start_dt = _to_datetime_utc(view.get("start_time"))

    text_blob = " ".join(t for t in (period, status_text, status_type) if t)
    state = "prematch"
    reason = "official_not_started"

    if _contains_any(text_blob, CANCELLED_PERIODS):
        state, reason = "cancelled", "provider_cancelled"
    elif _contains_any(text_blob, POSTPONED_PERIODS) or status_code == 60:
        state, reason = "postponed", "provider_postponed"
    elif _contains_any(text_blob, SUSPENDED_PERIODS):
        state, reason = "suspended", "provider_suspended"
    elif status_code == 100 or status_type in {"finished", "ended"} or period in FINISHED_PERIODS:
        state, reason = "finished", "provider_finished"
    elif (
        status_type in {"inprogress", "in progress", "live"}
        or status_text in {"inprogress", "in progress", "live"}
        or period in LIVE_PERIODS
        or (played_seconds > 0 and period not in NOT_STARTED_PERIODS)
    ):
        state, reason = "live", "provider_live"
    elif period in NOT_STARTED_PERIODS or status_code == 0:
        state, reason = "prematch", "official_not_started"
    elif period and period not in FINISHED_PERIODS:
        # Unknown non-empty provider period should be treated as live only if
        # it is not a known prematch/terminal state.
        state, reason = "live", "provider_period_active"

    minutes_until = None
    kickoff_passed = False
    if start_dt:
        minutes_until = round((start_dt - now_utc).total_seconds() / 60)
        kickoff_passed = minutes_until < 0

    return {
        "state": state,
        "mode": "live" if state == "live" else "prematch" if state == "prematch" else state,
        "is_prematch": state == "prematch",
        "is_live": state == "live",
        "is_finished": state == "finished",
        "is_terminal": state in {"finished", "postponed", "cancelled"},
        "source": source,
        "reason": reason,
        "period": view.get("period"),
        "status": view.get("status_raw"),
        "status_type": view.get("status_type"),
        "status_code": status_code,
        "played_seconds": played_seconds,
        "start_time": view.get("start_time"),
        "kickoff_utc": start_dt.isoformat() if start_dt else None,
        "minutes_until_kickoff": minutes_until,
        "kickoff_passed": kickoff_passed,
        "stale_not_started": state == "prematch" and kickoff_passed and period in NOT_STARTED_PERIODS,
        "verified_live": state == "live" and reason.startswith("provider_"),
    }


def is_live_match(doc: dict[str, Any] | None) -> bool:
    return bool(classify_match_state(doc).get("is_live"))


def is_finished_match(doc: dict[str, Any] | None) -> bool:
    state = classify_match_state(doc)
    return bool(
        (doc or {}).get("is_finished")
        or state.get("is_finished")
        or state.get("state") in {"postponed", "cancelled"}
    )


def is_prematch(doc: dict[str, Any] | None) -> bool:
    return bool(classify_match_state(doc).get("is_prematch"))


def _sporty_view(doc: dict[str, Any]) -> dict[str, Any]:
    raw = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else {}
    detail = doc.get("sportybet_detail") if isinstance(doc.get("sportybet_detail"), dict) else {}
    raw_event = raw.get("raw_event") if isinstance(raw.get("raw_event"), dict) else {}
    status_raw = _first_present(raw.get("status"), detail.get("status"), raw_event.get("status"), doc.get("status"))
    period = _first_present(raw.get("period"), detail.get("period"), raw_event.get("matchStatus"), doc.get("period"))
    start_time = _first_present(raw.get("start_time"), detail.get("start_time"), raw_event.get("estimateStartTime"), doc.get("start_time"))
    played_seconds = _first_present(raw.get("played_seconds"), detail.get("played_seconds"), raw_event.get("playedSeconds"), doc.get("played_seconds"))
    status_type, status_text, status_code = _status_parts(status_raw)
    return {
        "has_state": any(value not in (None, "") for value in (period, status_raw, start_time, played_seconds)),
        "period": period,
        "start_time": start_time,
        "played_seconds": played_seconds,
        "status_raw": status_raw,
        "status_type": status_type,
        "status_text": status_text,
        "status_code": status_code,
    }


def _sofa_view(doc: dict[str, Any]) -> dict[str, Any]:
    detail = doc.get("sofascore_detail") if isinstance(doc.get("sofascore_detail"), dict) else {}
    event = doc.get("sofascore_event") if isinstance(doc.get("sofascore_event"), dict) else {}
    raw_event = doc.get("raw_sofascore_event") if isinstance(doc.get("raw_sofascore_event"), dict) else {}
    status_raw = _first_present(detail.get("status"), event.get("status"), raw_event.get("status"))
    status_type, status_text, status_code = _status_parts(status_raw)
    period = _first_present(status_text, status_type)
    start_time = _first_present(detail.get("start_timestamp"), detail.get("startTimestamp"), event.get("start_timestamp"), event.get("startTimestamp"), raw_event.get("startTimestamp"))
    played_seconds = _first_present(detail.get("played_seconds"), detail.get("playedSeconds"), event.get("played_seconds"), event.get("playedSeconds"))
    return {
        "has_state": any(value not in (None, "") for value in (status_raw, period, start_time, played_seconds)),
        "period": period,
        "start_time": start_time,
        "played_seconds": played_seconds,
        "status_raw": status_raw,
        "status_type": status_type,
        "status_text": status_text,
        "status_code": status_code,
    }


def _status_parts(value: Any) -> tuple[str | None, str | None, int | None]:
    if isinstance(value, dict):
        return value.get("type"), value.get("description") or value.get("name"), _to_int(value.get("code"))
    return None, str(value) if value not in (None, "") else None, _to_int(value)


def _contains_any(text: str, needles: set[str]) -> bool:
    if not text:
        return False
    return any(needle in text for needle in needles)


