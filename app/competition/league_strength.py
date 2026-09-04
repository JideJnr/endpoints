from __future__ import annotations

from statistics import mean
from typing import Any


def league_strength_score(name: str | None) -> dict[str, Any]:
    if not _clean(name):
        return {"name": name, "score": 55, "country": None, "basis": "unknown league"}

    try:
        from app.monitoring.self_learner import get_tournament_priority

        learned = get_tournament_priority(name or "")
        if not learned.get("known"):
            return {"name": name, "score": 55, "country": None, "basis": "neutral_unknown"}
        priority = int(learned.get("priority", 4))
    except Exception:
        return {"name": name, "score": 55, "country": None, "basis": "neutral_fallback"}

    score = max(20, min(98, 85 - priority * 8))
    return {
        "name": name,
        "score": score,
        "country": None,
        "division": None,
        "basis": "learned_tournament_priority",
    }


def history_league_strength(history: list[dict[str, Any]] | None, last_n: int = 10) -> dict[str, Any]:
    events = history or []
    scores = []
    leagues = []
    for event in events:
        if event.get("status", {}).get("type") != "finished":
            continue
        tournament = event.get("tournament") or {}
        league_name = tournament.get("name") if isinstance(tournament, dict) else str(tournament or "")
        strength = league_strength_score(league_name)
        scores.append(strength["score"])
        leagues.append(strength)
        if len(scores) >= last_n:
            break
    if not scores:
        return {"sample_size": 0, "avg_score": 55, "leagues": []}
    return {
        "sample_size": len(scores),
        "avg_score": round(mean(scores), 1),
        "leagues": leagues[:5],
    }


def league_strength_edge(
    event: dict[str, Any],
    home_history: list[dict[str, Any]] | None,
    away_history: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    tournament = event.get("tournament") or {}
    tournament_name = tournament.get("name") if isinstance(tournament, dict) else str(tournament or "")
    match_league = league_strength_score(tournament_name)
    home = history_league_strength(home_history)
    away = history_league_strength(away_history)
    edge = 0.0
    if home["sample_size"] and away["sample_size"]:
        edge = (home["avg_score"] - away["avg_score"]) * 0.35
    return {
        "edge": round(max(-14, min(14, edge)), 2),
        "match_league": match_league,
        "home_recent_league_strength": home,
        "away_recent_league_strength": away,
    }


def _clean(value: str | None) -> str:
    return " ".join(str(value or "").lower().replace("-", " ").split())
