from __future__ import annotations

from datetime import date as date_cls, datetime, timezone
from typing import Any, Callable, TypeVar

from app.utils.match_state import classify_match_state


T = TypeVar("T")


def _context_source(home_stats: dict[str, Any], away_stats: dict[str, Any]) -> str:
    if home_stats.get("source") == "enriched_doc" and away_stats.get("source") == "enriched_doc":
        return "enriched_doc"
    if home_stats.get("source") == "enriched_doc" or away_stats.get("source") == "enriched_doc":
        return "mixed"
    return "model_fetch"


def _is_live_doc(doc: dict[str, Any]) -> bool:
    return bool(classify_match_state(doc).get("is_live"))


def _is_finished_doc(doc: dict[str, Any]) -> bool:
    state = classify_match_state(doc)
    return bool(doc.get("is_finished") or state.get("is_finished") or state.get("state") in {"postponed", "cancelled"})


def _is_not_started_period(period: str | None) -> bool:
    if not period:
        return True
    return str(period or "").lower().strip().replace("_", " ") in {
        "",
        "not start",
        "not started",
        "notstart",
        "notstarted",
        "scheduled",
        "ns",
    }


def _date_from_start_time(start_time: Any) -> str:
    try:
        ts = float(start_time)
        if ts > 1e12:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    except Exception:
        return date_cls.today().isoformat()


def _safe_call(func: Callable[..., T] | str, *args: Any, default: T | None = None, **kwargs: Any) -> T | None:
    if isinstance(func, str) and args and callable(args[0]):
        name = func
        fn = args[0]
        errors = args[1] if len(args) > 1 and isinstance(args[1], list) else None
        try:
            return fn()
        except Exception as exc:
            if errors is not None:
                errors.append(f"{name}: {exc}")
            return {}
    try:
        return func(*args, **kwargs)
    except Exception:
        return default


def _band(values: list[int], lo: int, hi: int) -> int:
    return sum(1 for value in values if lo <= value <= hi)


def _impact(signal: dict[str, Any]) -> float:
    try:
        return float(signal.get("impact") or 0)
    except Exception:
        return 0.0


def _data_sources(
    sofa: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
    sporty: dict[str, Any] | None = None,
    sportradar: dict[str, Any] | None = None,
    *,
    fresh: bool = True,
    include_meta: bool = True,
) -> dict[str, Any]:
    markets = (sporty or {}).get("markets") or []
    sporty_live = (sporty or {}).get("live_data_sportybet") or {}
    sofa_live = (detail or {}).get("live_data_sofascore") or {}
    sofa_has_stats = bool((detail or {}).get("statistics") or (detail or {}).get("match_statistics") or sofa_live)
    sources: dict[str, Any] = {
        "sportybet": {
            "available": bool(sporty),
            "detail": bool(sporty),
            "markets": bool(markets),
            "has_markets": bool(markets),
            "market_count": len(markets),
            "live_clock": bool(classify_match_state(sporty or {}).get("is_live")),
            "has_live_clock": bool(classify_match_state(sporty or {}).get("is_live")),
            "live_data_available": bool(sporty_live),
            "live_data_fetched_at": (sporty_live or {}).get("fetched_at"),
        },
        "sofascore": {
            "available": bool(sofa or detail),
            "matched": bool(sofa),
            "detail": bool(detail),
            "has_detail": bool(detail),
            "statistics": sofa_has_stats,
            "has_statistics": sofa_has_stats,
            "history": bool((detail or {}).get("home_last_matches") or (detail or {}).get("away_last_matches")),
            "has_history": bool((detail or {}).get("home_last_matches") or (detail or {}).get("away_last_matches")),
            "live_data_available": bool(sofa_live or sofa_has_stats),
            "live_data_fetched_at": (sofa_live or {}).get("fetched_at"),
        },
        "sportradar": {
            "available": bool((sportradar or {}).get("available")),
            "detail": bool((sportradar or {}).get("match")),
            "standings": bool((sportradar or {}).get("standings")),
            "error": (sportradar or {}).get("error") or (sportradar or {}).get("standings_error"),
        },
    }
    if include_meta:
        sources["sportybet_fresh"] = bool(fresh)
    return sources
