from __future__ import annotations

import re
from statistics import mean
from typing import Any


from app.utils.primitives import _first_present_key as _first_present, _optional_int, _to_int

LIVE_STAT_NAMES = {
    # ── Existing attack / possession stats ────────────────────────────────
    "ball_possession":   ("ball possession", "possession"),
    "shots_on_target":   ("shots on target",),
    "shots_off_target":  ("shots off target",),
    "total_shots":       ("total shots", "shots"),
    "corner_kicks":      ("corner kicks", "corners"),
    "big_chances":       ("big chances",),
    "xg":                ("expected goals", "xg"),
    "attacks":           ("attacks",),
    "dangerous_attacks": ("dangerous attacks",),
    "fouls":             ("fouls",),
    "yellow_cards":      ("yellow cards",),
    "red_cards":         ("red cards",),
    # ── Pressing / defensive intensity ────────────────────────────────────
    # These are served by SofaScore for most top-tier matches. They feed
    # into the pressing_profile built in team_watcher._compute_pressing_profile()
    # and into the richer half-pressure verdict in live_pressure.py.
    # New entries extend live_stat_snapshots automatically (columns are
    # derived from this dict at table-creation time in live_stat_history.py).
    "tackles":            ("total tackles", "tackles"),
    "tackles_won":        ("tackles won",),
    "interceptions":      ("interceptions",),
    "recoveries":         ("recoveries", "ball recoveries"),
    "aerial_duels_won":   ("aerial duels won", "aerial duels"),
    "final_third_entries":("final third entries", "final third phase"),
    "sprints":            ("number of sprints", "sprints"),
    "accurate_passes":    ("accurate passes",),
    "long_balls":         ("long balls",),
    "clearances":         ("clearances",),
    "goalkeeper_saves":   ("goalkeeper saves", "total saves"),
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


# ── Statistical dominance ─────────────────────────────────────────────────────

# Stat weights: higher = more diagnostic of who "should" win
_DOMINANCE_STAT_WEIGHTS: dict[str, float] = {
    "xg":                 3.0,
    "big_chances":        2.5,
    "shots_on_target":    2.0,
    "dangerous_attacks":  1.2,
    "total_shots":        0.8,
    "ball_possession":    0.7,
    "attacks":            0.5,
}


def compute_statistical_dominance(
    live_statistics: dict[str, Any],
    final_home: int | None,
    final_away: int | None,
) -> dict[str, Any]:
    """Determine which side dominated statistically and whether that side won.

    Parameters
    ----------
    live_statistics:
        The normalised live_statistics dict produced by
        ``normalize_live_statistics`` (keys: "summary", "periods", "source").
        Accepts an empty dict gracefully.
    final_home / final_away:
        Final score integers. Pass ``None`` when the match is not yet finished;
        the function will still return dominance info but ``better_side_won``
        will be ``None``.

    Returns
    -------
    dict with:
    - ``dominant_side``          – "home" | "away" | "even" | None
    - ``dominance_gap``          – weighted score gap (0.0+ means home ahead)
    - ``dominance_confidence``   – "high" | "moderate" | "marginal" | "none"
    - ``dominance_basis``        – which stats were available, or "no_stats"
    - ``stat_scores``            – raw per-side weighted scores
    - ``stats_used``             – list of stat keys that contributed
    - ``actual_winner``          – "home" | "away" | "draw" | None
    - ``better_side_won``        – True | False | None (None = not finished)
    - ``stat_contradiction``     – True when dominant side LOST (clear upset)
    """
    summary: dict[str, Any] = (live_statistics or {}).get("summary", {})
    home_score = 0.0
    away_score = 0.0
    stats_used: list[str] = []

    for stat_key, weight in _DOMINANCE_STAT_WEIGHTS.items():
        stat = summary.get(stat_key)
        if not stat:
            continue
        try:
            home_val = float(stat.get("home") or 0)
            away_val = float(stat.get("away") or 0)
        except (TypeError, ValueError):
            continue
        total = home_val + away_val
        if total <= 0:
            continue
        home_score += (home_val / total) * weight
        away_score += (away_val / total) * weight
        stats_used.append(stat_key)

    if not stats_used:
        return {
            "dominant_side": None,
            "dominance_gap": 0.0,
            "dominance_confidence": "none",
            "dominance_basis": "no_stats",
            "stat_scores": {"home": 0.0, "away": 0.0},
            "stats_used": [],
            "actual_winner": _actual_winner(final_home, final_away),
            "better_side_won": None,
            "stat_contradiction": False,
        }

    gap = home_score - away_score
    abs_gap = abs(gap)
    dominant = "home" if gap > 0.15 else "away" if gap < -0.15 else "even"
    confidence = (
        "high" if abs_gap >= 0.5
        else "moderate" if abs_gap >= 0.25
        else "marginal" if abs_gap >= 0.15
        else "none"
    )

    actual = _actual_winner(final_home, final_away)
    better_side_won: bool | None = None
    stat_contradiction = False
    if actual is not None and dominant not in ("even", None):
        better_side_won = dominant == actual
        stat_contradiction = not better_side_won and confidence in ("high", "moderate")

    return {
        "dominant_side": dominant,
        "dominance_gap": round(gap, 4),
        "dominance_confidence": confidence,
        "dominance_basis": "weighted_stats",
        "stat_scores": {"home": round(home_score, 4), "away": round(away_score, 4)},
        "stats_used": stats_used,
        "actual_winner": actual,
        "better_side_won": better_side_won,
        "stat_contradiction": stat_contradiction,
    }


def _actual_winner(final_home: int | None, final_away: int | None) -> str | None:
    """Return 'home', 'away', 'draw', or None when score is unavailable."""
    if final_home is None or final_away is None:
        return None
    if final_home > final_away:
        return "home"
    if final_away > final_home:
        return "away"
    return "draw"


