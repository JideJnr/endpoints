from __future__ import annotations

from datetime import date as dt
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.buffer import (
    get_buffered_matches,
    get_buffered_match,
    get_live_buffered_matches,
    get_buffer_stats,
)
from app.league_memory import list_prediction_history
from app.market import get_movement
from app.scheduler import scheduler_status


router = APIRouter(tags=["frontend"])


@router.get("/matches/today")
def get_matches_today():
    target_date = dt.today().isoformat()
    docs = get_buffered_matches(target_date)
    return {
        "status": "success",
        "date": target_date,
        "count": len(docs),
        "matches": [_match_summary(doc) for doc in docs],
    }


@router.get("/matches/live")
def get_live_matches():
    """All currently live matches from the buffer regardless of date."""
    docs = get_live_buffered_matches()
    return {
        "status": "success",
        "count": len(docs),
        "matches": [_match_summary(doc) for doc in docs],
    }


@router.get("/matches/by-date/{match_date}")
def get_matches_by_date(match_date: str, limit: int = Query(default=500, ge=1, le=1000)):
    docs = get_buffered_matches(match_date, limit=limit)
    return {
        "status": "success",
        "date": match_date,
        "count": len(docs),
        "matches": [_match_summary(doc) for doc in docs],
    }


@router.get("/matches/today/{sportybet_id}")
def get_today_match_detail(sportybet_id: str):
    doc = get_buffered_match(sportybet_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found")
    return _match_detail(doc)


@router.get("/buffer/status")
def get_buffer_status():
    """Scheduler health + buffer counts. Use to verify background jobs are running."""
    return {
        "status": "success",
        "scheduler": scheduler_status(),
        "buffer": get_buffer_stats(),
    }


@router.post("/matches/{sportybet_id}/enrich")
def enrich_single_match(sportybet_id: str):
    """Force-enrich a single match immediately (bypasses staleness check)."""
    from app.buffer import get_buffered_match, store_enriched
    from app.sofascore_client import fetch_all_scheduled_events, fetch_event_detail
    from app.enrichment import _fuzzy_match, _llm_match, _is_junk, FUZZY_THRESHOLD, LLM_FALLBACK_THRESHOLD
    from app.web_context import search_match_context
    from app.market import snapshot_odds
    from datetime import date, datetime, timezone
    import json

    doc = get_buffered_match(sportybet_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found in buffer")

    sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
    match_date = doc.get("match_date") or date.today().isoformat()

    try:
        sofa_events = fetch_all_scheduled_events(match_date)
    except Exception:
        sofa_events = []

    sofa, score = _fuzzy_match(sporty, sofa_events)
    if score < FUZZY_THRESHOLD and score >= LLM_FALLBACK_THRESHOLD and not _is_junk(sporty.get("name") or ""):
        sofa = _llm_match(sporty, sofa_events) or sofa

    detail = None
    if sofa:
        try:
            detail = fetch_event_detail(sofa)
        except Exception:
            pass

    web_context = {}
    try:
        web_context = search_match_context(
            sporty.get("home_team") or "",
            sporty.get("away_team") or "",
            sporty.get("tournament") or "",
        )
    except Exception:
        pass

    now = datetime.now(timezone.utc).isoformat()
    enriched_doc = {
        **sporty,
        "sportybet_id":      sporty.get("id") or sportybet_id,
        "sportybet_name":    sporty.get("name"),
        "match_date":        match_date,
        "sofascore_id":      sofa.get("id") if sofa else None,
        "sofascore_name":    sofa.get("name") if sofa else None,
        "sofascore_detail":  detail,
        "web_context":       web_context,
        "match_score":       round(score, 3),
        "enriched_at":       now,
    }

    snapshot_odds(enriched_doc)
    store_enriched(sportybet_id, enriched_doc)

    return {
        "status": "success",
        "sportybet_id": sportybet_id,
        "matched_sofascore": bool(sofa),
        "sofascore_id": sofa.get("id") if sofa else None,
        "fuzzy_score": round(score, 3),
        "has_detail": bool(detail),
        "has_web_context": bool(web_context.get("snippets")),
        "enriched_at": now,
    }


@router.post("/matches/{sportybet_id}/predict")
def predict_single_match(sportybet_id: str):
    """Run the prediction agent on a single match and return the result."""
    from app.buffer import get_buffered_match
    from app.prediction_agent import predict_sporty_match
    from app.ai_brain import oversee_prediction
    from app.league_memory import record_prediction
    from datetime import date

    doc = get_buffered_match(sportybet_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found")

    # rules-based prediction (always available)
    try:
        prediction = predict_sporty_match(doc)
        brain = oversee_prediction(prediction, doc)
        prediction["ai_brain"] = brain
        adj = int(brain.get("confidence_adjustment") or 0)
        if adj:
            for pick in prediction.get("picks") or []:
                pick["confidence"] = max(1, min(95, int(pick.get("confidence", 50)) + adj))
        prediction["signals"].append({
            "name": "ai_brain_review",
            "value": brain,
            "impact": adj,
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Prediction failed: {exc}")

    # try to save to history
    try:
        record_prediction({
            **prediction,
            "match_id": sportybet_id,
            "match_date": doc.get("match_date") or date.today().isoformat(),
            "source": "rules",
        })
    except Exception:
        pass

    return {"status": "success", "sportybet_id": sportybet_id, "prediction": prediction}


def _match_detail(doc: dict[str, Any]) -> dict[str, Any]:
    detail = doc.get("sofascore_detail") or {}
    managers = detail.get("managers") or {}
    form = detail.get("pregameForm") or detail.get("pregame_form") or {}
    home_form = form.get("homeTeam") or form.get("home_team") or {}
    away_form = form.get("awayTeam") or form.get("away_team") or {}
    web = doc.get("web_context") or {}
    sportybet_id = str(doc.get("sportybet_id") or doc.get("id") or "")

    return {
        "sportybet_id": sportybet_id,
        "sofascore_id": doc.get("sofascore_id"),
        "name": doc.get("sportybet_name") or doc.get("name"),
        "home_team": _home_team(doc),
        "away_team": _away_team(doc),
        "tournament": doc.get("tournament"),
        "category": doc.get("category"),
        "start_time": doc.get("start_time"),
        "period": doc.get("period"),
        "score": doc.get("score"),
        "venue": doc.get("venue"),
        "enriched_at": doc.get("enriched_at"),
        "odds_1x2": _extract_1x2(doc.get("sportybet_markets") or doc.get("markets") or []),
        "all_markets": doc.get("sportybet_markets") or doc.get("markets") or [],
        "home_manager": _manager_name(managers, "home"),
        "away_manager": _manager_name(managers, "away"),
        "home_form": home_form.get("form"),
        "away_form": away_form.get("form"),
        "home_position": home_form.get("position"),
        "away_position": away_form.get("position"),
        "home_avg_rating": home_form.get("avgRating") or home_form.get("avg_rating"),
        "away_avg_rating": away_form.get("avgRating") or away_form.get("avg_rating"),
        "h2h": detail.get("h2h"),
        "standings": detail.get("standings"),
        "home_players": detail.get("homeFeaturedPlayers") or detail.get("home_featured_players"),
        "away_players": detail.get("awayFeaturedPlayers") or detail.get("away_featured_players"),
        "incidents": detail.get("incidents"),
        "web_context": {
            "query": web.get("query"),
            "snippets": web.get("snippets", []),
            "articles": web.get("scraped", []) or web.get("articles", []),
        },
        "odds_movement": get_movement(sportybet_id) if sportybet_id else {"snapshots": 0, "movement": None},
        "prediction": _latest_prediction(sportybet_id),
        "raw": doc,
    }


def _match_summary(doc: dict[str, Any]) -> dict[str, Any]:
    detail = doc.get("sofascore_detail") or {}
    form = detail.get("pregameForm") or detail.get("pregame_form") or {}
    home_form = form.get("homeTeam") or form.get("home_team") or {}
    away_form = form.get("awayTeam") or form.get("away_team") or {}

    return {
        "sportybet_id": str(doc.get("sportybet_id") or doc.get("id") or ""),
        "sofascore_id": doc.get("sofascore_id"),
        "name": doc.get("sportybet_name") or doc.get("name"),
        "home_team": _home_team(doc),
        "away_team": _away_team(doc),
        "tournament": doc.get("tournament"),
        "category": doc.get("category"),
        "start_time": doc.get("start_time"),
        "period": doc.get("period"),
        "score": doc.get("score"),
        "venue": doc.get("venue"),
        "enriched_at": doc.get("enriched_at"),
        "home_form": home_form.get("form"),
        "away_form": away_form.get("form"),
        "home_position": home_form.get("position"),
        "away_position": away_form.get("position"),
        "odds_1x2": _extract_1x2(doc.get("sportybet_markets") or doc.get("markets") or []),
        "has_sofascore": bool(detail),
        "has_h2h": bool(detail.get("h2h")),
        "has_standings": bool(detail.get("standings")),
        "has_web_context": bool(doc.get("web_context")),
    }


def _extract_1x2(markets: list[dict[str, Any]]) -> dict[str, Any]:
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


def _home_team(doc: dict[str, Any]) -> str:
    team = doc.get("home_team")
    if isinstance(team, dict):
        return team.get("name") or ""
    if team:
        return str(team)
    return _team_from_name(doc, 0)


def _away_team(doc: dict[str, Any]) -> str:
    team = doc.get("away_team")
    if isinstance(team, dict):
        return team.get("name") or ""
    if team:
        return str(team)
    return _team_from_name(doc, 1)


def _team_from_name(doc: dict[str, Any], index: int) -> str:
    name = doc.get("sportybet_name") or doc.get("name") or ""
    parts = [part.strip() for part in name.split(" vs ", 1)]
    return parts[index] if len(parts) > index else ""


def _manager_name(managers: dict[str, Any], side: str) -> str | None:
    key = "homeTeam" if side == "home" else "awayTeam"
    alt_key = "home_manager" if side == "home" else "away_manager"
    manager = managers.get(key) or managers.get(alt_key) or {}
    return manager.get("name") if isinstance(manager, dict) else None


def _latest_prediction(match_id: str) -> dict[str, Any] | None:
    if not match_id:
        return None
    history = list_prediction_history(limit=1, match_id=match_id).get("predictions") or []
    return history[0] if history else None
