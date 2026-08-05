from __future__ import annotations

from typing import Any

from app.match_state import classify_match_state


def match_summary(doc: dict[str, Any]) -> dict[str, Any]:
    detail = doc.get("sofascore_detail") or {}
    form = detail.get("pregameForm") or detail.get("pregame_form") or {}
    home_form = form.get("homeTeam") or form.get("home_team") or {}
    away_form = form.get("awayTeam") or form.get("away_team") or {}
    match_state = classify_match_state(doc)

    return {
        "sportybet_id": str(doc.get("sportybet_id") or doc.get("id") or ""),
        "sofascore_id": doc.get("sofascore_id"),
        "name": doc.get("sportybet_name") or doc.get("name"),
        "home_team": home_team(doc),
        "away_team": away_team(doc),
        "tournament": doc.get("tournament"),
        "category": doc.get("category"),
        "start_time": doc.get("start_time"),
        "period": doc.get("period"),
        "played_seconds": doc.get("played_seconds"),
        "is_live": bool(match_state.get("is_live")),
        "is_finished": bool(match_state.get("is_finished")),
        "match_state": match_state,
        "score": doc.get("score"),
        "venue": doc.get("venue"),
        "enriched_at": doc.get("enriched_at"),
        "home_form": home_form.get("form"),
        "away_form": away_form.get("form"),
        "home_position": home_form.get("position"),
        "away_position": away_form.get("position"),
        "odds_1x2": extract_1x2(doc.get("sportybet_markets") or doc.get("markets") or []),
        "has_sofascore": bool(detail),
        "has_h2h": bool(detail.get("h2h")),
        "has_standings": bool(detail.get("standings")),
        "has_statistics": bool(detail.get("statistics")),
        "has_lineups": bool(detail.get("lineups")),
        "has_last_matches": bool(detail.get("home_last_matches") or detail.get("away_last_matches")),
        "has_web_context": bool(doc.get("web_context")),
        "has_league_sentiment": bool(doc.get("league_sentiment")),
        "lifecycle": doc.get("lifecycle"),
    }


def extract_1x2(markets: list[dict[str, Any]]) -> dict[str, Any]:
    for market in markets:
        name = (market.get("name") or "").lower()
        if market.get("id") == "1" or "1x2" in name or name == "match result":
            odds = {selection.get("name"): selection.get("odds") for selection in market.get("selections", [])}
            return {
                "home": odds.get("Home") or odds.get("1"),
                "draw": odds.get("Draw") or odds.get("X"),
                "away": odds.get("Away") or odds.get("2"),
            }
    return {}


def home_team(doc: dict[str, Any]) -> str:
    team = doc.get("home_team")
    if isinstance(team, dict):
        return team.get("name") or ""
    if team:
        return str(team)
    return team_from_name(doc, 0)


def away_team(doc: dict[str, Any]) -> str:
    team = doc.get("away_team")
    if isinstance(team, dict):
        return team.get("name") or ""
    if team:
        return str(team)
    return team_from_name(doc, 1)


def team_from_name(doc: dict[str, Any], index: int) -> str:
    name = doc.get("sportybet_name") or doc.get("name") or ""
    parts = [part.strip() for part in name.split(" vs ", 1)]
    return parts[index] if len(parts) > index else ""
