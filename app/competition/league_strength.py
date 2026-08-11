from __future__ import annotations

from statistics import mean
from typing import Any


COUNTRY_STRENGTH = {
    "england": 95,
    "spain": 93,
    "germany": 91,
    "italy": 90,
    "france": 86,
    "portugal": 80,
    "netherlands": 78,
    "belgium": 73,
    "turkey": 72,
    "brazil": 72,
    "argentina": 71,
    "scotland": 69,
    "austria": 68,
    "switzerland": 68,
    "denmark": 67,
    "croatia": 66,
    "greece": 66,
    "norway": 65,
    "sweden": 65,
    "poland": 64,
    "czech": 63,
    "romania": 61,
    "serbia": 61,
    "ukraine": 60,
    "usa": 59,
    "mexico": 59,
    "japan": 58,
    "south korea": 57,
    "nigeria": 54,
}

COMPETITION_STRENGTH = {
    "champions league": 96,
    "europa league": 84,
    "conference league": 74,
    "club world cup": 82,
    "copa libertadores": 76,
    "copa sudamericana": 66,
}

DIVISION_OFFSETS = {
    "premier league": 0,
    "first division": 0,
    "1st division": 0,
    "division 1": 0,
    "super league": 0,
    "serie a": 0,
    "bundesliga": 0,
    "la liga": 0,
    "ligue 1": 0,
    "eredivisie": 0,
    "championship": -10,
    "second division": -12,
    "2nd division": -12,
    "division 2": -12,
    "league one": -18,
    "third division": -20,
    "league two": -26,
    "fourth division": -28,
}

LOWER_CONTEXT_OFFSETS = {
    "u23": -18,
    "u21": -20,
    "u20": -20,
    "u19": -24,
    "u18": -26,
    "women": -12,
    "reserves": -18,
    "srl": -35,
    "virtual": -45,
}


def league_strength_score(name: str | None) -> dict[str, Any]:
    text = _clean(name)
    if not text:
        return {"name": name, "score": 55, "country": None, "basis": "unknown league"}

    for key, score in COMPETITION_STRENGTH.items():
        if key in text:
            return {"name": name, "score": score, "country": None, "basis": key}

    country, base = _country_base(text)
    division, offset = _division_offset(text)
    context_offset = sum(value for key, value in LOWER_CONTEXT_OFFSETS.items() if key in text)
    score = max(20, min(98, base + offset + context_offset))
    return {
        "name": name,
        "score": score,
        "country": country,
        "division": division,
        "basis": "country/division estimate",
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


def _country_base(text: str) -> tuple[str | None, int]:
    for country, score in COUNTRY_STRENGTH.items():
        if country in text:
            return country, score
    return None, 58


def _division_offset(text: str) -> tuple[str | None, int]:
    for division, offset in DIVISION_OFFSETS.items():
        if division in text:
            return division, offset
    return None, -6


def _clean(value: str | None) -> str:
    return " ".join(str(value or "").lower().replace("-", " ").split())
