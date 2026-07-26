from __future__ import annotations

from datetime import date as dt
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from app.league_memory import (
    get_enriched_match,
    get_enriched_matches,
    get_country_from_memory,
    get_engine_states,
    get_league_detail_from_memory,
    get_memory_match,
    get_snapshot_memory,
    list_betbuilder_history,
    list_countries_from_memory,
    list_duplicate_matches,
    list_memory_matches,
    list_prediction_history,
    observe_matches,
    record_prediction,
    save_betbuilder,
    set_engine_status,
    grade_betbuilder_history,
    run_memory_maintenance,
)
from app.ai_brain import oversee_prediction
from app.bot2 import run_bot2
from app.enrichment import run_enrichment
from app.dixon_coles import run_dixon_coles
from app.elo import elo_prediction
from app.ensemble import ensemble_prediction
from app.kelly import kelly_fraction
from app.market import get_all_movements, get_movement
from app.poisson import run_poisson
from app.prediction_agent import predict_sofascore_event
from app.sofascore_client import fetch_all_scheduled_events, fetch_event_detail, fetch_team_history
from app.sos import compare_schedules, analyse_schedule
from app.sportybet_client import fetch_live_and_upcoming_matches_post, fetch_live_matches_post
from app.web_context import context_for_match, search_match_context

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
    {
        "id": "ai-brain",
        "name": "AI Brain",
        "description": "Optional local AI reviewer that oversees rule signals, H2H, league quality, and risk.",
        "markets": ["match_result", "live_goals", "value_bets", "betbuilder"],
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
                "id": "dixon-coles-model",
                "name": "Dixon-Coles Goal Model",
                "description": "Applies a low-score correction over Poisson so 0-0, 1-0, 0-1, and 1-1 outcomes are priced more realistically.",
                "signals": ["home_lambda", "away_lambda", "low_score_tau", "corrected_scoreline_probability"],
            },
            {
                "id": "strength-of-schedule",
                "name": "Strength Of Schedule",
                "description": "Weights recent form by opponent quality and league strength so cross-league form is not misread.",
                "signals": ["quality_wins", "soft_losses", "schedule_difficulty", "league_strength_edge"],
            },
            {
                "id": "h2h-context",
                "name": "Team H2H Context",
                "description": "Adds direct team duel history when Sofascore supplies it.",
                "signals": ["home_wins", "away_wins", "draws", "sample_size"],
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
            {
                "id": "ai-brain",
                "name": "AI Brain",
                "description": "Uses a free local Ollama model when available, otherwise uses deterministic review.",
                "signals": ["ai_brain.status", "ai_brain.risks", "ai_brain.confidence_adjustment"],
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


@router.get("/matches/duplicates")
def get_matches_duplicates(limit: int = Query(default=200, ge=1, le=1000)):
    return {"status": "success", **list_duplicate_matches(limit=limit)}


@router.get("/matches/{match_id}/prediction")
def get_match_prediction(
    match_id: str,
    source: Optional[str] = None,
    date: Optional[str] = None,
    include_web_context: bool = True,
):
    return _prediction_for_match_id(
        match_id=match_id,
        source=source,
        date=date,
        include_web_context=include_web_context,
    )


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


@router.get("/predictions/decisions")
def get_prediction_decisions(limit: int = Query(default=200, ge=1, le=1000), match_id: Optional[str] = None):
    from app.league_memory import list_prediction_decisions

    return {"status": "success", **list_prediction_decisions(limit=limit, match_id=match_id)}


@router.get("/predictions/{match_id}")
def get_prediction(
    match_id: str,
    source: Optional[str] = None,
    date: Optional[str] = None,
    include_web_context: bool = True,
):
    return _prediction_for_match_id(
        match_id=match_id,
        source=source,
        date=date,
        include_web_context=include_web_context,
    )


def _prediction_for_match_id(
    match_id: str,
    source: Optional[str],
    date: Optional[str],
    include_web_context: bool,
):
    from app.buffer import get_buffered_match, refresh_sporty_match_state, store_enriched
    from app.enriched_prediction import prediction_readiness
    from app.match_enrichment import MatchEnrichmentError, enrich_buffered_match
    from app.prediction_flow import apply_prediction_state

    refresh = refresh_sporty_match_state(match_id)
    if not refresh.get("active"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Prediction blocked because the match is not active in the SportyBet buffer",
                "refresh": refresh,
            },
        )

    doc = get_buffered_match(match_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {match_id} not found in active buffer")

    try:
        enrich_buffered_match(match_id, auto_predict=False)
        doc = get_buffered_match(match_id) or doc
    except MatchEnrichmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except Exception:
        pass

    readiness = prediction_readiness(doc)
    if not readiness.get("ready"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Prediction deferred until SofaScore/SportyBet enrichment is ready",
                "readiness": readiness,
            },
        )

    state = apply_prediction_state(
        doc,
        match_id=match_id,
        match_date=doc.get("match_date") or date or dt.today().isoformat(),
        source="enriched_ensemble",
    )
    store_enriched(match_id, doc)
    if state.get("status") == "deferred":
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Prediction deferred until SofaScore/SportyBet enrichment is ready",
                "readiness": state.get("readiness"),
            },
        )
    if state.get("status") == "error":
        raise HTTPException(status_code=500, detail=state.get("message") or "Prediction failed")
    return {"status": "success", "source": "buffer_enriched", "readiness": state.get("readiness") or readiness, "prediction": state.get("prediction")}


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
    bet = save_betbuilder(selections, round(combined_odds, 3), confidence, payload.get("request") or payload.get("builder_request"))
    return {"status": "success", "bet": bet}


@router.post("/betbuilder/book")
def post_betbuilder_book(payload: dict[str, Any] = Body(...)):
    """Build the SportyBet booking request and optionally obtain a share code.

    SPORTYBET_SHARE_CODE_URL is intentionally opt-in because SportyBet's
    booking endpoint can vary by market/app version. Without it, clients get
    a fully resolved payload they can inspect or submit through their approved
    integration.
    """
    from app.sportybet_booking import build_booking_payload, request_share_code

    selections = payload.get("selections") or []
    try:
        stake = int(payload.get("stake") or 0)
        booking_payload = build_booking_payload(
            selections,
            stake=stake,
            loading_share_code=payload.get("loadingShareCode"),
        )
        return request_share_code(booking_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/betbuilder/history")
def get_betbuilder_history(limit: int = Query(default=100, ge=1, le=1000), auto_grade: bool = True):
    return {"status": "success", **list_betbuilder_history(limit=limit, auto_grade=auto_grade)}


@router.post("/betbuilder/grade")
def post_grade_betbuilder(limit: int = Query(default=300, ge=1, le=1000)):
    result = grade_betbuilder_history(limit=limit)
    return {"status": "success", **result}


@router.post("/matches/{sportybet_id}/enriched-analysis")
def post_enriched_match_analysis(sportybet_id: str, payload: dict[str, Any] = Body(default_factory=dict)):
    try:
        from app.ai_betbuilder import enriched_match_analysis

        result = enriched_match_analysis(sportybet_id, force_refresh=bool(payload.get("force_refresh")))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if result.get("status") in {"groq_unavailable", "agent_build_failed", "error"}:
        raise HTTPException(status_code=503, detail=result.get("message") or "AI analysis is unavailable")
    return result


@router.post("/betbuilder/sure-picks")
def post_sure_picks_synthesis(payload: dict[str, Any] = Body(...)):
    analyses = payload.get("analyses") or []
    if len(analyses) < 2:
        raise HTTPException(status_code=400, detail="At least two completed Enriched Analysis results are required")
    try:
        from app.ai_betbuilder import synthesize_sure_picks
        target_odds = max(1.01, _to_float(payload.get("target_odds")) or 5.0)
        max_total_odds = max(target_odds, _to_float(payload.get("max_total_odds")) or target_odds * 1.35)

        return synthesize_sure_picks(
            analyses[:20],
            target_odds=target_odds,
            max_total_odds=max_total_odds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/betbuilder/auto")
def post_auto_betbuilder(payload: dict[str, Any] = Body(...)):
    """Build a Groq-powered slip from upcoming prediction-engine candidates."""
    from app.ai_betbuilder import build_ai_betbuilder

    result = build_ai_betbuilder(payload)
    if result.get("status") == "error":
        raise HTTPException(status_code=503, detail=result)
    return result


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


@router.get("/risk/desk")
def get_risk_desk(limit: int = Query(default=200, ge=20, le=1000)):
    from app.desk_analytics import desk_observability

    return desk_observability(limit=limit)


@router.get("/risk/backtest-gate")
def get_risk_backtest_gate(limit: int = Query(default=1000, ge=50, le=10000), min_samples: int = Query(default=50, ge=10, le=1000)):
    from app.desk_analytics import backtest_gate

    return backtest_gate(limit=limit, min_samples=min_samples)


@router.get("/risk/signal-attribution")
def get_risk_signal_attribution(min_samples: int = Query(default=5, ge=1, le=100), limit: int = Query(default=5000, ge=100, le=20000)):
    from app.desk_analytics import signal_attribution_report

    return signal_attribution_report(min_samples=min_samples, limit=limit)


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
    skipped_not_ready = 0
    skipped_already_predicted = 0
    if docs:
        from app.prediction_flow import apply_prediction_state

        predictions = []
        for doc in docs:
            state = apply_prediction_state(
                doc,
                match_id=str(doc.get("sportybet_id") or doc.get("id") or ""),
                match_date=doc.get("match_date") or target_date,
                source="enriched_ensemble",
                attach_brain=True,
            )
            if state.get("status") == "predicted":
                predictions.append(state["prediction"])
            elif state.get("status") == "skipped":
                skipped_already_predicted += 1
            else:
                skipped_not_ready += 1
    else:
        predictions = _safe_predictions_for_date(target_date, limit=limit, include_history=True)
    return {
        "status": "success",
        "date": target_date,
        "new_predictions": len(predictions),
        "skipped_not_ready": skipped_not_ready,
        "skipped_already_predicted": skipped_already_predicted,
        "value_bets": sum(1 for item in predictions if _is_value_prediction(item)),
        "predictions": predictions,
    }


@router.post("/run/bot2")
def post_run_bot2(date: Optional[str] = None):
    return run_bot2(date)


@router.post("/maintenance/memory")
def post_maintenance_memory(
    raw_retention_days: int = Query(default=30, ge=1, le=365),
    odds_retention_days: int = Query(default=60, ge=1, le=365),
):
    return run_memory_maintenance(raw_retention_days=raw_retention_days, odds_retention_days=odds_retention_days)


@router.get("/odds/movement/{match_id}")
def get_odds_movement(match_id: str):
    return get_movement(match_id)


@router.get("/odds/movements")
def get_odds_movements(date: Optional[str] = None):
    return {"status": "success", "movements": get_all_movements(date)}


@router.get("/models/poisson")
def get_poisson_model(home_team_id: int, away_team_id: int):
    return {"status": "success", "poisson": run_poisson(home_team_id, away_team_id)}


@router.get("/models/dixon-coles")
def get_dixon_coles_model(home_team_id: int, away_team_id: int):
    return {"status": "success", "dixon_coles": run_dixon_coles(home_team_id, away_team_id)}


@router.get("/models/elo")
def get_elo_model(home_team_id: str, away_team_id: str):
    return {"status": "success", "elo": elo_prediction(home_team_id, away_team_id)}


@router.get("/models/kelly")
def get_kelly_model(probability: float = Query(ge=0, le=1), decimal_odds: float = Query(gt=1), fraction: float = Query(default=0.25, gt=0, le=1)):
    return {"status": "success", "kelly": kelly_fraction(probability, decimal_odds, fraction)}


@router.get("/models/ensemble")
def get_ensemble_model(
    home_team_id: int,
    away_team_id: int,
    rules_confidence: int = Query(default=50, ge=0, le=95),
    rules_pick: str = "home",
):
    poisson = run_poisson(home_team_id, away_team_id)
    dixon = run_dixon_coles(home_team_id, away_team_id)
    elo = elo_prediction(str(home_team_id), str(away_team_id))
    ensemble = ensemble_prediction(dixon, elo, poisson, rules_confidence, rules_pick)
    return {"status": "success", "ensemble": ensemble, "models": {"poisson": poisson, "dixon_coles": dixon, "elo": elo}}


@router.get("/models/schedule")
def get_schedule_model(home_team_id: int, away_team_id: int):
    return {"status": "success", "schedule": compare_schedules(home_team_id, away_team_id)}


@router.get("/models/web-context")
def get_web_context(home: str, away: str, tournament: str = ""):
    return {"status": "success", "web_context": search_match_context(home, away, tournament)}


def _predictions_for_date(date: Optional[str], limit: int, include_history: bool) -> list[dict[str, Any]]:
    """Compatibility path for historical SofaScore-only date predictions."""
    target_date = date or dt.today().isoformat()
    events = fetch_all_scheduled_events(target_date)[:limit]
    predictions = [_predict_sofascore(event, include_history=include_history) for event in events]
    for prediction in predictions:
        record_prediction(prediction)
    return predictions


def _safe_predictions_for_date(date: Optional[str], limit: int, include_history: bool) -> list[dict[str, Any]]:
    target_date = date or dt.today().isoformat()
    today = dt.today().isoformat()

    try:
        from app.current_predictions import list_recent_dashboard_predictions

        dashboard_rows = list_recent_dashboard_predictions(hours=72, limit=max(limit, 200))
        if dashboard_rows:
            return dashboard_rows[:limit]
    except Exception:
        pass

    # Do not generate new predictions from legacy SofaScore-only fallback here.
    # The enriched buffer is the production contract; history is read-only
    # context when there are no current enriched predictions.
    history = list_prediction_history(limit=limit)["predictions"] if include_history else []
    return [
        {
            "match_id": item.get("match_id"),
            "name": item.get("match_name"),
            "source": item.get("source"),
            "tournament": {"name": item.get("league_name")},
            "league_name": item.get("league_name"),
            "country_name": item.get("country_name"),
            "signals": item.get("signals", []),
            "picks": item.get("picks", []),
            "created_at": item.get("created_at"),
            "history_fallback": True,
        }
        for item in history
    ]


def _predict_sofascore(event: dict[str, Any], include_history: bool, include_web_context: bool = False) -> dict[str, Any]:
    detail = fetch_event_detail(event)
    if include_web_context:
        detail["web_context"] = context_for_match(detail)
    home_history = []
    away_history = []
    if include_history:
        home_history = fetch_team_history(detail["home_team"]["id"]).get("events", [])
        away_history = fetch_team_history(detail["away_team"]["id"]).get("events", [])
    prediction = predict_sofascore_event(detail, home_history, away_history)
    _attach_deep_analysis(prediction, detail)
    return prediction


def _attach_deep_analysis(prediction: dict[str, Any], detail: dict[str, Any], attach_brain: bool = True) -> None:
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
    if attach_brain:
        _attach_ai_brain(prediction, detail)
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


def _attach_ai_brain(prediction: dict[str, Any], detail: dict[str, Any] | None = None) -> None:
    brain = oversee_prediction(prediction, detail)
    prediction["ai_brain"] = brain
    adjustment = _to_int(brain.get("confidence_adjustment"), 0)
    if adjustment:
        for pick in prediction.get("picks") or []:
            pick["confidence"] = max(1, min(95, _to_int(pick.get("confidence"), 50) + adjustment))
    prediction["signals"].append({
        "name": "ai_brain_review",
        "value": {
            "provider": brain.get("provider"),
            "status": brain.get("status"),
            "risks": brain.get("risks"),
        },
        "impact": adjustment,
    })


def _is_value_prediction(prediction: dict[str, Any]) -> bool:
    return bool((prediction.get("elite_verdict") or {}).get("value_bet"))


def _curated_picks(predictions: list[dict[str, Any]], min_confidence: int) -> list[dict[str, Any]]:
    picks = []
    for prediction in predictions:
        best = (prediction.get("picks") or [{}])[0]
        if best.get("confidence", 0) >= min_confidence and best.get("type") != "no_bet":
            picks.append({
                "match_id": prediction.get("match_id"),
                "match": prediction.get("name") or prediction.get("match_name"),
                "tournament": prediction.get("tournament") or {"name": prediction.get("league_name")},
                "league_name": prediction.get("league_name"),
                "country_name": prediction.get("country_name"),
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


def _pick_decimal_odds(pick: dict[str, Any]) -> float:
    stake = pick.get("stake") if isinstance(pick.get("stake"), dict) else {}
    odds = _to_float(stake.get("decimal_odds")) or _to_float(pick.get("odds")) or _to_float(pick.get("decimal_odds"))
    if odds and odds > 1:
        return odds
    return _estimate_odds(pick.get("confidence"))


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
