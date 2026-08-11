from __future__ import annotations

import re
from statistics import mean
from typing import Any


LIVE_STAT_NAMES = {
    "ball_possession": ("ball possession", "possession"),
    "shots_on_target": ("shots on target",),
    "shots_off_target": ("shots off target",),
    "total_shots": ("total shots", "shots"),
    "corner_kicks": ("corner kicks", "corners"),
    "big_chances": ("big chances",),
    "xg": ("expected goals", "xg"),
    "attacks": ("attacks",),
    "dangerous_attacks": ("dangerous attacks",),
    "fouls": ("fouls",),
    "yellow_cards": ("yellow cards",),
    "red_cards": ("red cards",),
}


def enrich_match_facts(doc: dict[str, Any]) -> dict[str, Any]:
    """Attach durable match facts derived from provider detail/raw live feeds."""
    enriched = dict(doc or {})
    detail = enriched.get("sofascore_detail") or {}
    score = enriched.get("score") or {}

    half_time = extract_half_time_score(enriched)
    goal_events = extract_goal_events(detail.get("incidents") or [])
    goal_timing = goal_timing_summary(goal_events)
    live_statistics = normalize_live_statistics(detail.get("statistics") or detail.get("match_statistics") or [])
    capabilities = provider_live_capabilities(enriched, live_statistics, goal_events)

    if half_time:
        enriched["half_time_score"] = half_time
        score = {**score, "home_ht": half_time.get("home"), "away_ht": half_time.get("away")}
        enriched["score"] = score
    if goal_events:
        enriched["goal_events"] = goal_events
    if goal_timing:
        enriched["goal_timing"] = goal_timing
        enriched["average_goal_interval_minutes"] = goal_timing.get("average_interval_minutes")
    if live_statistics:
        enriched["live_statistics"] = live_statistics
    enriched["provider_live_capabilities"] = capabilities

    sources = dict(enriched.get("data_sources") or {})
    if "sofascore" in sources:
        sources["sofascore"] = {**(sources.get("sofascore") or {}), **capabilities.get("sofascore", {})}
    if "sportybet" in sources:
        sources["sportybet"] = {**(sources.get("sportybet") or {}), **capabilities.get("sportybet", {})}
    if sources:
        enriched["data_sources"] = sources
        enriched["data_source_detail"] = sources

    return enriched


def extract_half_time_score(doc: dict[str, Any]) -> dict[str, int] | None:
    score = doc.get("score") or {}
    home_ht = _first_present(score, "home_ht", "homeHt", "homeHT", "period1_home")
    away_ht = _first_present(score, "away_ht", "awayHt", "awayHT", "period1_away")
    if home_ht is not None or away_ht is not None:
        return {"home": _to_int(home_ht, 0), "away": _to_int(away_ht, 0), "source": "score_period1"}

    detail = doc.get("sofascore_detail") or {}
    detail_score = detail.get("score") or {}
    home_ht = _first_present(detail_score, "home_ht", "homeHt", "homeHT")
    away_ht = _first_present(detail_score, "away_ht", "awayHt", "awayHT")
    if home_ht is not None or away_ht is not None:
        return {"home": _to_int(home_ht, 0), "away": _to_int(away_ht, 0), "source": "sofascore_score"}

    home_score = detail.get("homeScore") or {}
    away_score = detail.get("awayScore") or {}
    if home_score.get("period1") is not None or away_score.get("period1") is not None:
        return {"home": _to_int(home_score.get("period1"), 0), "away": _to_int(away_score.get("period1"), 0), "source": "sofascore_period1"}

    period_scores = doc.get("period_scores") or (doc.get("raw_sporty") or {}).get("period_scores")
    parsed = _half_time_from_period_scores(period_scores)
    if parsed:
        return parsed

    goals = extract_goal_events(detail.get("incidents") or [])
    first_half_goals = [goal for goal in goals if _goal_minute_value(goal) <= 45]
    if first_half_goals:
        last_with_score = next((goal for goal in reversed(first_half_goals) if goal.get("home_score") is not None and goal.get("away_score") is not None), None)
        if last_with_score:
            return {"home": _to_int(last_with_score.get("home_score"), 0), "away": _to_int(last_with_score.get("away_score"), 0), "source": "goal_incidents_score"}
        return {
            "home": sum(1 for goal in first_half_goals if goal.get("side") == "home"),
            "away": sum(1 for goal in first_half_goals if goal.get("side") == "away"),
            "source": "goal_incidents_count",
        }
    return None


def extract_goal_events(incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    goals: list[dict[str, Any]] = []
    for incident in incidents or []:
        incident_type = str(incident.get("incidentType") or incident.get("type") or "").lower()
        if "goal" not in incident_type and incident_type not in {"penaltygoal", "owngoal"}:
            continue
        minute = _to_int(incident.get("time") or incident.get("minute"), 0)
        added = _to_int(incident.get("addedTime") or incident.get("injuryTime"), 0)
        side = "home" if incident.get("isHome") is True else "away" if incident.get("isHome") is False else None
        player = incident.get("player") or {}
        goals.append(
            {
                "minute": minute,
                "added_time": added,
                "minute_display": f"{minute}+{added}" if added else str(minute),
                "side": side,
                "home_score": _optional_int(incident.get("homeScore")),
                "away_score": _optional_int(incident.get("awayScore")),
                "type": incident_type or "goal",
                "player": player.get("name") or incident.get("playerName"),
            }
        )
    return sorted(goals, key=_goal_minute_value)


def goal_timing_summary(goal_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not goal_events:
        return None
    minutes = [_goal_minute_value(goal) for goal in goal_events]
    intervals = [round(minutes[idx] - minutes[idx - 1], 2) for idx in range(1, len(minutes))]
    return {
        "goal_count": len(minutes),
        "goal_minutes": minutes,
        "intervals_minutes": intervals,
        "average_interval_minutes": round(mean(intervals), 2) if intervals else None,
        "first_goal_minute": minutes[0],
        "last_goal_minute": minutes[-1],
    }


def normalize_live_statistics(statistics: list[dict[str, Any]]) -> dict[str, Any]:
    periods: list[dict[str, Any]] = []
    latest_summary: dict[str, dict[str, Any]] = {}
    for period in statistics or []:
        period_name = period.get("period") or period.get("periodName") or "ALL"
        items: dict[str, dict[str, Any]] = {}
        for item in _iter_stat_items(period):
            raw_name = str(item.get("name") or item.get("key") or "").strip()
            key = _stat_key(raw_name)
            if not key:
                continue
            value = {"home": item.get("home"), "away": item.get("away"), "name": raw_name}
            items[key] = value
            if str(period_name).upper() in {"ALL", "TOTAL", "MATCH"} or key not in latest_summary:
                latest_summary[key] = value
        if items:
            periods.append({"period": period_name, "stats": items})
    if not periods:
        return {}
    return {"source": "sofascore", "summary": latest_summary, "periods": periods}


def provider_live_capabilities(
    doc: dict[str, Any],
    live_statistics: dict[str, Any] | None = None,
    goal_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw_sporty = doc.get("raw_sporty") or {}
    raw_sporty_event = raw_sporty.get("raw_event") or doc.get("raw_event") or {}
    sporty_meta = raw_sporty.get("sporty_metadata") or doc.get("metadata") or {}
    detail = doc.get("sofascore_detail") or {}
    sofa_stats = live_statistics or normalize_live_statistics(detail.get("statistics") or detail.get("match_statistics") or [])
    sofa_incidents = detail.get("incidents") or []
    sporty_stats = (
        raw_sporty.get("statistics")
        or raw_sporty.get("matchStatistics")
        or raw_sporty.get("stats")
        or raw_sporty_event.get("statistics")
        or raw_sporty_event.get("matchStatistics")
        or raw_sporty_event.get("stats")
    )
    sporty_momentum = (
        raw_sporty.get("momentum")
        or raw_sporty.get("matchMomentum")
        or raw_sporty.get("goalMomentum")
        or raw_sporty_event.get("momentum")
        or raw_sporty_event.get("matchMomentum")
        or raw_sporty_event.get("goalMomentum")
    )
    sofa_momentum = detail.get("momentum") or detail.get("graph") or detail.get("matchMomentum") or detail.get("goalMomentum")
    return {
        "sofascore": {
            "live_statistics": bool(sofa_stats),
            "goal_incidents": bool(goal_events or sofa_incidents),
            "goal_momentum": bool(sofa_momentum),
            "goal_momentum_source": "detail" if sofa_momentum else None,
        },
        "sportybet": {
            "live_clock": bool(doc.get("played_seconds") or raw_sporty.get("played_seconds")),
            "live_statistics": bool(sporty_stats),
            "goal_momentum": bool(sporty_momentum),
            "match_tracker_available": bool(sporty_meta.get("match_tracker_available")),
        },
    }


def _half_time_from_period_scores(period_scores: Any) -> dict[str, int] | None:
    if isinstance(period_scores, dict):
        candidates = [
            period_scores.get("1"),
            period_scores.get("period1"),
            period_scores.get("firstHalf"),
            period_scores.get("HT"),
        ]
        for candidate in candidates:
            parsed = _score_pair(candidate)
            if parsed:
                return {"home": parsed[0], "away": parsed[1], "source": "period_scores"}
    if isinstance(period_scores, list) and period_scores:
        parsed = _score_pair(period_scores[0])
        if parsed:
            return {"home": parsed[0], "away": parsed[1], "source": "period_scores"}
    return None


def _score_pair(value: Any) -> tuple[int, int] | None:
    if isinstance(value, dict):
        home = _first_present(value, "home", "homeScore", "scoreHome")
        away = _first_present(value, "away", "awayScore", "scoreAway")
        if home is not None or away is not None:
            return _to_int(home, 0), _to_int(away, 0)
    if isinstance(value, str):
        parts = re.split(r"\s*[:\-]\s*", value.strip())
        if len(parts) == 2:
            return _to_int(parts[0], 0), _to_int(parts[1], 0)
    return None


def _iter_stat_items(period: dict[str, Any]):
    for group in period.get("groups") or []:
        for item in group.get("statisticsItems") or group.get("items") or []:
            yield item
    for item in period.get("statisticsItems") or period.get("items") or []:
        yield item


def _stat_key(name: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()
    for key, aliases in LIVE_STAT_NAMES.items():
        if normalized in aliases:
            return key
    if not normalized:
        return None
    return normalized.replace(" ", "_")


def _goal_minute_value(goal: dict[str, Any]) -> float:
    return _to_int(goal.get("minute"), 0) + (_to_int(goal.get("added_time"), 0) / 100)


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if isinstance(mapping, dict) and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return _to_int(value, 0)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
