from __future__ import annotations

from datetime import date as dt
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from app.league_memory import (
    get_league_memory,
    get_snapshot_memory,
    list_duplicate_matches,
    observe_match,
    observe_matches,
    run_memory_maintenance,
)
from app.ai_brain import oversee_prediction
from app.prediction_agent import predict_sofascore_event, predict_sporty_match
from app.sofascore_client import fetch_all_scheduled_events, fetch_event_detail, fetch_scheduled_events, fetch_team_history
from app.sportybet_client import fetch_live_and_upcoming_matches_post, fetch_live_matches_post, fetch_upcoming_matches_post

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/memory/leagues")
def get_memory_leagues():
    return {"status": "success", **get_league_memory()}


@router.get("/memory/leagues/{league}")
def get_memory_league(league: str):
    return {"status": "success", "memory": get_league_memory(league)}


@router.get("/memory/snapshots")
def get_memory_snapshots(
    league: Optional[str] = None,
    minute_bucket: Optional[str] = None,
    score_state: Optional[str] = None,
    min_samples: int = Query(default=1, ge=1, le=1000),
):
    return {
        "status": "success",
        **get_snapshot_memory(
            league=league,
            minute_bucket=minute_bucket,
            score_state=score_state,
            min_samples=min_samples,
        ),
    }


@router.get("/memory/duplicates")
def get_memory_duplicates(limit: int = Query(default=200, ge=1, le=1000)):
    return {"status": "success", **list_duplicate_matches(limit=limit)}


@router.post("/memory/maintenance")
def post_memory_maintenance(
    raw_retention_days: int = Query(default=30, ge=1, le=365),
    odds_retention_days: int = Query(default=60, ge=1, le=365),
):
    return run_memory_maintenance(raw_retention_days=raw_retention_days, odds_retention_days=odds_retention_days)


@router.post("/memory/observe")
def post_memory_observation(source: str = "manual", match: dict = Body(...)):
    try:
        return {"status": "success", **observe_match(source, match)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/memory/sofascore/{date}")
def post_sofascore_memory_observation(
    date: str,
    tournament_id: Optional[int] = None,
    all_matches: bool = False,
):
    try:
        events = fetch_all_scheduled_events(date) if all_matches else fetch_scheduled_events(date, tournament_id or 17)
        return {"status": "success", "date": date, **observe_matches("sofascore", events)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/memory/sporty/live")
def post_sporty_live_memory_observation():
    try:
        matches = fetch_live_matches_post()
        return {"status": "success", **observe_matches("sportybet", matches)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/sporty/live-predictions")
def get_sporty_live_predictions():
    try:
        matches = fetch_live_matches_post()
        predictions = [_with_ai_brain(predict_sporty_match(match), match) for match in matches]
        return {"status": "success", "count": len(predictions), "predictions": predictions}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/sporty/upcoming-predictions")
def get_sporty_upcoming_predictions():
    try:
        matches = fetch_upcoming_matches_post()
        predictions = [_with_ai_brain(predict_sporty_match(match), match) for match in matches]
        return {"status": "success", "count": len(predictions), "predictions": predictions}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/sporty/all-predictions")
def get_sporty_all_predictions():
    try:
        matches = fetch_live_and_upcoming_matches_post()
        predictions = [_with_ai_brain(predict_sporty_match(match), match) for match in matches]
        return {"status": "success", "count": len(predictions), "predictions": predictions}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/sofascore/predictions/{date}")
def get_sofascore_predictions(
    date: str,
    tournament_id: Optional[int] = None,
    all_matches: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
    include_history: bool = True,
):
    try:
        events = fetch_all_scheduled_events(date) if all_matches else fetch_scheduled_events(date, tournament_id or 17)
        predictions = [
            _predict_sofascore_with_detail(event, date, include_history)
            for event in events[:limit]
        ]
        return {"status": "success", "date": date, "count": len(predictions), "predictions": predictions}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/sofascore/event/{event_id}/prediction")
def get_sofascore_event_prediction(event_id: int, date: Optional[str] = None, include_history: bool = True):
    try:
        target_date = date or dt.today().isoformat()
        events = fetch_all_scheduled_events(target_date)
        event = next((item for item in events if item["id"] == event_id), None)
        if not event:
            raise HTTPException(status_code=404, detail=f"Event {event_id} not found on {target_date}")
        return {"status": "success", **_predict_sofascore_with_detail(event, target_date, include_history)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


def _predict_sofascore_with_detail(event: dict, date: str, include_history: bool) -> dict:
    detail = fetch_event_detail(event)
    home_history = []
    away_history = []
    if include_history:
        home_history = fetch_team_history(detail["home_team"]["id"]).get("events", [])
        away_history = fetch_team_history(detail["away_team"]["id"]).get("events", [])
    prediction = predict_sofascore_event(detail, home_history, away_history)
    _with_ai_brain(prediction, detail)
    prediction["date"] = date
    return prediction


def _with_ai_brain(prediction: dict, detail: dict | None = None) -> dict:
    brain = oversee_prediction(prediction, detail)
    prediction["ai_brain"] = brain
    adjustment = _to_int(brain.get("confidence_adjustment"), 0)
    if adjustment:
        for pick in prediction.get("picks") or []:
            pick["confidence"] = max(1, min(95, _to_int(pick.get("confidence"), 50) + adjustment))
    prediction["signals"].append({
        "name": "ai_brain_review",
        "value": {"provider": brain.get("provider"), "status": brain.get("status"), "risks": brain.get("risks")},
        "impact": adjustment,
    })
    return prediction


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
