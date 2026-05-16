"""
LangChain Agent Tools
---------------------
Ported from migrated predictz/tools.py.
These are the tools the LangChain prediction agent can call during reasoning.

Requires: langchain, langchain-groq, GROQ_API_KEY
"""
from __future__ import annotations

from langchain.tools import tool

from app.poisson import run_poisson
from app.market import get_movement, get_all_movements
from app.sos import compare_schedules, analyse_schedule
from app.sofascore_client import (
    fetch_all_scheduled_events,
    fetch_event_detail,
    fetch_team_history,
    fetch_standings,
    fetch_h2h,
    fetch_pregame_form,
    fetch_odds,
    fetch_odds_featured,
)
from app.sportybet_client import (
    fetch_live_matches_post,
    fetch_live_and_upcoming_matches_post,
)


@tool
def get_scheduled_matches(date: str) -> dict:
    """Get all football matches scheduled for a given date (YYYY-MM-DD) across all tournaments."""
    try:
        return {"status": "success", "events": fetch_all_scheduled_events(date)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def get_event_detail(event_id: int, date: str) -> dict:
    """Get full match detail for a SofaScore event — includes h2h, pregame form, managers,
    featured players, odds, and standings. Use this as the primary tool for any match analysis.
    Requires event_id (integer) and the match date (YYYY-MM-DD)."""
    try:
        events = fetch_all_scheduled_events(date)
        event = next((e for e in events if e["id"] == event_id), None)
        if not event:
            return {"status": "error", "detail": f"Event {event_id} not found on {date}"}
        return {"status": "success", **fetch_event_detail(event)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def get_team_history(team_id: int, page: int = 0) -> dict:
    """Get a team's last 30 match results. page=0 is most recent."""
    try:
        return {"status": "success", **fetch_team_history(team_id, page)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def get_standings(tournament_id: int, season_id: int) -> dict:
    """Get the full league table for a tournament and season."""
    try:
        rows = fetch_standings(tournament_id, season_id)
        return {"status": "success", "standings": rows}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def get_event_h2h(event_id: int) -> dict:
    """Get head-to-head record between the two teams in a given event."""
    try:
        return {"status": "success", **fetch_h2h(event_id)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def get_pregame_form(event_id: int) -> dict:
    """Get recent form, league position, and avg rating for both teams before a match."""
    try:
        return {"status": "success", **fetch_pregame_form(event_id)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def get_event_odds(event_id: int) -> dict:
    """Get all available pre-match odds markets for an event."""
    try:
        return {"status": "success", "markets": fetch_odds(event_id)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def get_featured_odds(event_id: int) -> dict:
    """Get the 3 key odds markets (1X2, Asian Handicap, Full Time) for a match."""
    try:
        return {"status": "success", **fetch_odds_featured(event_id)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def get_live_matches() -> dict:
    """Get all currently live matches from SportyBet Nigeria with scores and betting markets."""
    try:
        matches = fetch_live_matches_post()
        return {"status": "success", "count": len(matches), "matches": matches}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def get_all_sportybet_matches() -> dict:
    """Get all live and upcoming matches from SportyBet Nigeria including pre-match."""
    try:
        matches = fetch_live_and_upcoming_matches_post()
        return {"status": "success", "count": len(matches), "matches": matches}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def poisson_model(home_team_id: int, away_team_id: int) -> dict:
    """Run the Poisson goal model for a match using team history.
    Returns home win %, draw %, away win %, over 2.5 %, btts %, and top scorelines.
    Use this for any match where you have both team IDs from SofaScore.
    This is pure maths — no LLM bias."""
    try:
        return {"status": "success", **run_poisson(home_team_id, away_team_id)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def get_odds_movement(sportybet_id: str) -> dict:
    """Get odds movement analysis for a match — compares opening odds vs current odds.
    Returns sharp money signals, line movement direction (shortened/drifted/stable).
    A significant odds drop = smart money backing that team."""
    try:
        return {"status": "success", **get_movement(sportybet_id)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def get_all_odds_movements(match_date: str) -> dict:
    """Get odds movement for all tracked matches on a given date (YYYY-MM-DD)."""
    try:
        return {"status": "success", "movements": get_all_movements(match_date)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@tool
def strength_of_schedule(home_team_id: int, away_team_id: int) -> dict:
    """Analyse and compare both teams' recent form weighted by opponent quality.
    Solves the key problem: a team that lost 5 in a row against top sides is NOT
    in bad form. A team that won 5 in a row against weak sides is NOT in great form.
    Also detects division gaps — higher division team has structural advantage.
    Always call this before making a prediction."""
    try:
        return {"status": "success", **compare_schedules(home_team_id, away_team_id)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


# ── All tools list — import this in the agent ─────────────────────────────────

ALL_TOOLS = [
    get_scheduled_matches,
    get_event_detail,
    get_team_history,
    get_standings,
    get_event_h2h,
    get_pregame_form,
    get_event_odds,
    get_featured_odds,
    get_live_matches,
    get_all_sportybet_matches,
    poisson_model,
    get_odds_movement,
    get_all_odds_movements,
    strength_of_schedule,
]
