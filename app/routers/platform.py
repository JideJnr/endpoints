from __future__ import annotations

from datetime import date as dt
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from app.league_memory import (
    get_country_from_memory,
    get_engine_states,
    get_league_detail_from_memory,
    get_memory_match,
    get_snapshot_memory,
    list_betbuilder_history,
    list_countries_from_memory,
    list_memory_matches,
    list_prediction_history,
    observe_matches,
    record_prediction,
    save_betbuilder,
    set_engine_status,
)
from app.bot2 import run_bot2
from app.enrichment import run_enrichment
from app.market import get_all_movements, get_movement
from app.poisson import run_poisson
from app.prediction_agent import predict_sofascore_event, predict_sporty_match
from app.sofascore_client import fetch_all_scheduled_events, fetch_event_detail, fetch_team_history
from app.sos import compare_schedules, analyse_schedule
from app.sportybet_client import fetch_live_and_upcoming_matches_post, fetch_live_matches_post

router = APIRouter(tags=["platform"])

ENGINES = [
    {
        "id": "late-goal-memory",
        "name": "Late Goal Memory",
        "description": "Learns league/minute/score-state next-goal rates from live snapshots.",
        "markets": ["live_goals", "over_0_5", "next_goal"],
    },
    {
        "id": "market-steam",
        "name": "Market Steam",
        "description": "Tracks odds shortening and favorite pressure.",
        "markets": ["match_result", "double_chance", "value_bets"],
    },
    {
        "id": "poisson-model",
        "name": "Poisson Model",
        "description": "Runs scoreline probability math from recent goals scored/conceded.",
        "markets": ["match_result", "over_2_5", "btts", "correct_score"],
    },
    {
        "id": "strength-of-schedule",
        "name": "Strength Of Schedule",
        "description": "Rates form by opponent quality, quality wins, soft losses, and rough league tier.",
        "markets": ["match_result", "value_bets"],
    },
    {
        "id": "bot2-value-selector",
        "name": "Bot 2 Value Selector",
        "description": "Reviews Bot 1 prediction history and selects the cleanest high-value picks.",
        "markets": ["value_bets", "betbuilder"],
    },
    {
        "id": "red-card-state",
        "name": "Red Card State",
        "description": "Adjusts live predictions when one team has a card disadvantage.",
        "markets": ["live_goals", "match_result"],
    },
]


@router.get("/logic")
def get_logic():
    return {
        "status": "success",
        "logic": [
            {
                "id": "poisson-model",
                "name": "Poisson Goal Model",
                "description": "Calculates home/draw/away, BTTS, over 2.5 and top scorelines from recent team goals.",
                "signals": ["home_lambda", "away_lambda", "scoreline_probability"],
            },
            {
                "id": "strength-of-schedule",
                "name": "Strength Of Schedule",
                "description": "Weights recent form by opponent quality so easy wins and hard losses are not misread.",
                "signals": ["quality_wins", "soft_losses", "schedule_difficulty", "division_tier"],
            },
            {
                "id": "league-memory",
                "name": "Full League Snapshot Memory",
                "description": "Stores every live state and resolves it against final match outcomes.",
                "signals": ["minute_bucket", "score_state", "favorite_side", "red_card_state"],
            },
            {
                "id": "live-chase-pressure",
                "name": "Live Chase Pressure",
                "description": "Raises next-goal confidence when a strong side is drawing or losing in a close game.",
                "signals": ["minute", "score_diff", "home_power", "goal_pressure"],
            },
            {
                "id": "market-steam",
                "name": "Market Steam",
                "description": "Uses odds movement as confirmation when it agrees with form/table edge.",
                "signals": ["current_probability", "opening_probability", "probability_move"],
            },
            {
                "id": "red-card-state",
                "name": "Red Card State",
                "description": "Changes live confidence when a favorite or underdog receives a red card.",
                "signals": ["home_red_cards", "away_red_cards", "score_state"],
            },
        ],
        "memory": get_snapshot_memory(min_samples=1),
    }


@router.get("/matches")
def get_matches(date: Optional[str] = None, limit: int = Query(default=100, ge=1, le=500)):
    target_date = date or dt.today().isoformat()
    try:
        events = fetch_all_scheduled_events(target_date)[:limit]
        observe_matches("sofascore", events)
        return {"status": "success", "date": target_date, "count": len(events), "matches": events}
    except Exception:
        memory = list_memory_matches(limit=limit)
        return {"status": "success", "date": target_date, "source": "memory_fallback", "count": len(memory["matches"]), **memory}


@router.get("/matches/live")
def get_live_matches(limit: int = Query(default=300, ge=1, le=500)):
    try:
        matches = fetch_live_matches_post()[:limit]
        observe_matches("sportybet", matches)
        return {"status": "success", "count": len(matches), "matches": matches}
    except Exception:
        memory = list_memory_matches(limit=limit, source="sportybet")
        return {"status": "success", "source": "memory_fallback", "count": len(memory["matches"]), **memory}


@router.get("/matches/memory")
def get_matches_memory(limit: int = Query(default=200, ge=1, le=1000), league: Optional[str] = None, source: Optional[str] = None):
    return {"status": "success", **list_memory_matches(limit=limit, league=league, source=source)}


@router.get("/matches/{match_id}")
def get_match_detail(match_id: str, source: Optional[str] = None):
    enriched = get_enriched_match(match_id)
    if enriched:
        memory = get_memory_match(match_id, source=source)
        return {"status": "success", "match": {**enriched, "memory": memory, "odds_movement": get_movement(match_id)}}
    match = get_memory_match(match_id, source=source)
    if not match:
        raise HTTPException(status_code=404, detail=f"Match {match_id} not found in memory")
    return {"status": "success", "match": match}


@router.get("/countries")
def get_countries():
    return {"status": "success", **list_countries_from_memory()}


@router.get("/countries/{country_id}")
def get_country(country_id: str):
    return {"status": "success", "country": get_country_from_memory(country_id)}


@router.get("/leagues/{league_id}")
def get_league(league_id: str):
    return {"status": "success", "league": get_league_detail_from_memory(league_id)}


@router.get("/teams/{team_id}")
def get_team(team_id: int, history_pages: int = Query(default=1, ge=1, le=3)):
    try:
        pages = [fetch_team_history(team_id, page).get("events", []) for page in range(history_pages)]
        events = [event for page in pages for event in page]
        team_name = _team_name_from_history(team_id, events)
        return {
            "status": "success",
            "team": {
                "id": team_id,
                "name": team_name,
                "recent_matches": events,
                "stats": _team_stats(team_id, events),
                "schedule": analyse_schedule(team_id),
                "squad": [],
            },
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/players/{player_id}")
def get_player(player_id: int):
    return {
        "status": "success",
        "player": {
            "id": player_id,
            "name": None,
            "stats": {},
            "form": [],
            "note": "Player profile provider is not wired yet; endpoint is stable for UI integration.",
        },
    }


@router.get("/predictions/suggestions")
def get_prediction_suggestions(date: Optional[str] = None, limit: int = Query(default=20, ge=1, le=100)):
    predictions = _safe_predictions_for_date(date, limit=limit, include_history=True)
    picks = _curated_picks(predictions, min_confidence=62)
    return {"status": "success", "count": len(picks), "suggestions": picks}


@router.get("/bot2/picks")
def get_bot2_picks(date: Optional[str] = None):
    return run_bot2(date)


@router.get("/predictions/value-bets")
def get_value_bets(date: Optional[str] = None, limit: int = Query(default=20, ge=1, le=100)):
    predictions = _safe_predictions_for_date(date, limit=limit, include_history=True)
    picks = [
        item for item in _curated_picks(predictions, min_confidence=55)
        if any(signal.get("name") in {"market_steam", "odds_edge", "market_favorite"} for signal in item.get("signals", []))
    ]
    return {"status": "success", "count": len(picks), "value_bets": picks}


@router.get("/predictions/history")
def get_predictions_history(limit: int = Query(default=200, ge=1, le=1000), match_id: Optional[str] = None):
    return {"status": "success", **list_prediction_history(limit=limit, match_id=match_id)}


@router.get("/predictions/{match_id}")
def get_prediction(match_id: str, date: Optional[str] = None):
    target_date = date or dt.today().isoformat()
    try:
        events = fetch_all_scheduled_events(target_date)
        event = next((item for item in events if str(item.get("id")) == str(match_id)), None)
        if event:
            prediction = _predict_sofascore(event, include_history=True)
            record_prediction(prediction)
            return {"status": "success", "prediction": prediction}
    except Exception:
        pass
    history = list_prediction_history(limit=1, match_id=match_id)["predictions"]
    if history:
        return {"status": "success", "source": "memory", "prediction": history[0]}
    raise HTTPException(status_code=404, detail=f"Prediction for {match_id} not found")


@router.get("/predictions")
def get_predictions(date: Optional[str] = None, limit: int = Query(default=50, ge=1, le=200), include_history: bool = True):
    predictions = _safe_predictions_for_date(date, limit=limit, include_history=include_history)
    return {"status": "success", "date": date or dt.today().isoformat(), "count": len(predictions), "predictions": predictions}


@router.post("/betbuilder")
def post_betbuilder(payload: dict[str, Any] = Body(...)):
    selections = payload.get("selections") or []
    if not selections:
        raise HTTPException(status_code=400, detail="selections is required")
    combined_odds = 1.0
    confidences = []
    for selection in selections:
        odds = _to_float(selection.get("odds")) or _estimate_odds(selection.get("confidence"))
        combined_odds *= odds
        if selection.get("confidence") is not None:
            confidences.append(_to_int(selection.get("confidence"), 50))
    confidence = round(sum(confidences) / len(confidences)) if confidences else max(1, min(95, round(100 / combined_odds)))
    bet = save_betbuilder(selections, round(combined_odds, 3), confidence)
    return {"status": "success", "bet": bet}


@router.get("/betbuilder/history")
def get_betbuilder_history(limit: int = Query(default=100, ge=1, le=1000)):
    return {"status": "success", **list_betbuilder_history(limit=limit)}


@router.get("/engines/metrics")
def get_engine_metrics():
    history = list_prediction_history(limit=1000)["predictions"]
    total = len(history)
    high_confidence = [item for item in history if (item.get("best_pick") or {}).get("confidence", 0) >= 65]
    return {
        "status": "success",
        "metrics": {
            "predictions_recorded": total,
            "high_confidence_predictions": len(high_confidence),
            "success_rate": None,
            "win_percent": None,
            "biggest_combo": None,
            "note": "Win metrics need settled bet/prediction grading, which is the next layer after memory resolution.",
        },
    }


@router.get("/engines/{engine_id}")
def get_engine(engine_id: str):
    engines = _engines_with_state()
    engine = next((item for item in engines if item["id"] == engine_id), None)
    if not engine:
        raise HTTPException(status_code=404, detail=f"Engine {engine_id} not found")
    engine["prediction_history"] = list_prediction_history(limit=50)["predictions"]
    return {"status": "success", "engine": engine}


@router.post("/engines/{engine_id}/start")
def start_engine(engine_id: str):
    if engine_id not in {engine["id"] for engine in ENGINES}:
        raise HTTPException(status_code=404, detail=f"Engine {engine_id} not found")
    return {"status": "success", "engine": set_engine_status(engine_id, "running")}


@router.post("/engines/{engine_id}/stop")
def stop_engine(engine_id: str):
    if engine_id not in {engine["id"] for engine in ENGINES}:
        raise HTTPException(status_code=404, detail=f"Engine {engine_id} not found")
    return {"status": "success", "engine": set_engine_status(engine_id, "stopped")}


@router.get("/engines")
def get_engines():
    return {"status": "success", "engines": _engines_with_state()}


@router.post("/run/enrich")
def post_run_enrich(date: Optional[str] = None, force: bool = False, limit: int = Query(default=300, ge=1, le=1000)):
    try:
        return run_enrichment(match_date=date, force=force, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/run/predict")
def post_run_predict(date: Optional[str] = None, limit: int = Query(default=20, ge=1, le=300)):
    target_date = date or dt.today().isoformat()
    docs = get_enriched_matches(target_date, limit=limit)
    if docs:
        predictions = [_predict_enriched(doc) for doc in docs]
    else:
        predictions = _safe_predictions_for_date(target_date, limit=limit, include_history=True)
    for prediction in predictions:
        record_prediction(prediction)
    return {
        "status": "success",
        "date": target_date,
        "new_predictions": len(predictions),
        "value_bets": sum(1 for item in predictions if _is_value_prediction(item)),
        "predictions": predictions,
    }


@router.post("/run/bot2")
def post_run_bot2(date: Optional[str] = None):
    return run_bot2(date)


@router.get("/odds/movement/{match_id}")
def get_odds_movement(match_id: str):
    return get_movement(match_id)


@router.get("/odds/movements")
def get_odds_movements(date: Optional[str] = None):
    return {"status": "success", "movements": get_all_movements(date)}


@router.get("/models/poisson")
def get_poisson_model(home_team_id: int, away_team_id: int):
    return {"status": "success", "poisson": run_poisson(home_team_id, away_team_id)}


@router.get("/models/schedule")
def get_schedule_model(home_team_id: int, away_team_id: int):
    return {"status": "success", "schedule": compare_schedules(home_team_id, away_team_id)}


def _predictions_for_date(date: Optional[str], limit: int, include_history: bool) -> list[dict[str, Any]]:
    target_date = date or dt.today().isoformat()
    events = fetch_all_scheduled_events(target_date)[:limit]
    predictions = [_predict_sofascore(event, include_history=include_history) for event in events]
    for prediction in predictions:
        record_prediction(prediction)
    return predictions


def _safe_predictions_for_date(date: Optional[str], limit: int, include_history: bool) -> list[dict[str, Any]]:
    try:
        return _predictions_for_date(date, limit=limit, include_history=include_history)
    except Exception:
        history = list_prediction_history(limit=limit)["predictions"]
        return [
            {
                "match_id": item.get("match_id"),
                "name": item.get("match_name"),
                "source": item.get("source"),
                "tournament": {"name": item.get("league_name")},
                "signals": item.get("signals", []),
                "picks": item.get("picks", []),
            }
            for item in history
        ]


def _predict_sofascore(event: dict[str, Any], include_history: bool) -> dict[str, Any]:
    detail = fetch_event_detail(event)
    home_history = []
    away_history = []
    if include_history:
        home_history = fetch_team_history(detail["home_team"]["id"]).get("events", [])
        away_history = fetch_team_history(detail["away_team"]["id"]).get("events", [])
    prediction = predict_sofascore_event(detail, home_history, away_history)
    _attach_deep_analysis(prediction, detail)
    return prediction


def _predict_enriched(doc: dict[str, Any]) -> dict[str, Any]:
    detail = doc.get("sofascore_detail")
    if detail:
        prediction = predict_sofascore_event(detail)
        _attach_deep_analysis(prediction, detail)
    else:
        prediction = predict_sporty_match({
            "id": doc.get("sportybet_id"),
            "name": doc.get("sportybet_name"),
            "score": doc.get("score"),
            "played_seconds": None,
            "period": doc.get("period"),
            "tournament": doc.get("tournament"),
            "category": doc.get("category"),
            "markets": doc.get("sportybet_markets", []),
        })
    prediction["enriched"] = {
        "sportybet_id": doc.get("sportybet_id"),
        "sofascore_id": doc.get("sofascore_id"),
        "match_date": doc.get("match_date"),
        "web_context": doc.get("web_context"),
        "odds_movement": get_movement(str(doc.get("sportybet_id"))),
    }
    return prediction


def _attach_deep_analysis(prediction: dict[str, Any], detail: dict[str, Any]) -> None:
    home_id = (detail.get("home_team") or {}).get("id")
    away_id = (detail.get("away_team") or {}).get("id")
    if not home_id or not away_id:
        return
    try:
        poisson = run_poisson(home_id, away_id)
        prediction["poisson"] = poisson
        prediction["signals"].append({
            "name": "poisson_model",
            "value": poisson.get("probabilities"),
            "impact": round((poisson.get("probabilities", {}).get("home_win", 0) - poisson.get("probabilities", {}).get("away_win", 0)) / 5, 2),
        })
    except Exception as e:
        prediction["poisson"] = {"error": str(e)}
    try:
        schedule = compare_schedules(home_id, away_id)
        prediction["strength_of_schedule"] = schedule
        prediction["signals"].append({"name": "strength_of_schedule", "value": schedule.get("verdict"), "impact": 4})
    except Exception as e:
        prediction["strength_of_schedule"] = {"error": str(e)}
    _attach_value_fields(prediction)


def _attach_value_fields(prediction: dict[str, Any]) -> None:
    best = (prediction.get("picks") or [{}])[0]
    confidence = (best.get("confidence") or 0) / 100
    odds = _estimate_odds(best.get("confidence"))
    prediction["elite_verdict"] = {
        "status": "predicted" if confidence >= 0.60 and best.get("type") != "no_bet" else "low_confidence",
        "prediction": best.get("selection"),
        "odds": str(odds),
        "confidence": confidence,
        "value_bet": confidence >= 0.70 and odds >= 2.5,
        "key_factors": [signal.get("name") for signal in (prediction.get("signals") or [])[:5]],
        "reasoning": {
            "verdict": best.get("reason"),
            "poisson": "Poisson model included when team IDs are available.",
            "schedule": "Strength-of-schedule model included when histories are available.",
            "odds_signal": "Market movement included when odds snapshots exist.",
        },
    }


def _is_value_prediction(prediction: dict[str, Any]) -> bool:
    return bool((prediction.get("elite_verdict") or {}).get("value_bet"))


def _curated_picks(predictions: list[dict[str, Any]], min_confidence: int) -> list[dict[str, Any]]:
    picks = []
    for prediction in predictions:
        best = (prediction.get("picks") or [{}])[0]
        if best.get("confidence", 0) >= min_confidence and best.get("type") != "no_bet":
            picks.append({
                "match_id": prediction.get("match_id"),
                "match": prediction.get("name"),
                "tournament": prediction.get("tournament"),
                "pick": best,
                "signals": prediction.get("signals", []),
            })
    return sorted(picks, key=lambda item: item["pick"]["confidence"], reverse=True)


def _engines_with_state() -> list[dict[str, Any]]:
    states = get_engine_states()
    return [{**engine, "status": states.get(engine["id"], "stopped")} for engine in ENGINES]


def _team_name_from_history(team_id: int, events: list[dict[str, Any]]) -> str | None:
    for event in events:
        for side in ("home_team", "away_team"):
            team = event.get(side) or {}
            if team.get("id") == team_id:
                return team.get("name")
    return None


def _team_stats(team_id: int, events: list[dict[str, Any]]) -> dict[str, Any]:
    goals_for = goals_against = wins = draws = losses = played = 0
    for event in events:
        if event.get("status", {}).get("type") != "finished":
            continue
        is_home = event.get("home_team", {}).get("id") == team_id
        home_goals = _to_int(event.get("score", {}).get("home"), 0)
        away_goals = _to_int(event.get("score", {}).get("away"), 0)
        gf = home_goals if is_home else away_goals
        ga = away_goals if is_home else home_goals
        played += 1
        goals_for += gf
        goals_against += ga
        if gf > ga:
            wins += 1
        elif gf == ga:
            draws += 1
        else:
            losses += 1
    return {
        "played": played,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "goals_for": goals_for,
        "goals_against": goals_against,
    }


def _estimate_odds(confidence: Any) -> float:
    value = max(1, min(95, _to_int(confidence, 50))) / 100
    return round(1 / value, 3)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
    get_enriched_match,
    get_enriched_matches,
