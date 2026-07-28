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


@router.get("/value-bets")
def get_value_bets(
    date: Optional[str] = None,
    min_edge: float = Query(default=3.0, ge=0, le=100),
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Scan buffered/enriched matches for 1X2 prices where model probability beats implied odds.
    """
    from app.buffer import get_buffered_matches
    from app.kelly import kelly_fraction
    from app.poisson import run_poisson
    from concurrent.futures import ThreadPoolExecutor, as_completed

    target_date = date or dt.today().isoformat()
    docs = get_buffered_matches(target_date, limit=limit)
    # only scan upcoming and live — finished matches have no betting value
    docs = [
        doc for doc in docs
        if not _is_finished_doc(doc)
    ]

    def _scan_doc(doc: dict) -> list[dict]:
        detail = doc.get("sofascore_detail") or {}
        home_team = detail.get("home_team") or detail.get("homeTeam") or {}
        away_team = detail.get("away_team") or detail.get("awayTeam") or {}
        home_id = home_team.get("id")
        away_id = away_team.get("id")
        markets = doc.get("sportybet_markets") or doc.get("markets") or []
        if not home_id or not away_id or not markets:
            return []

        try:
            model = run_poisson(int(home_id), int(away_id))
        except Exception:
            return []

        bets = []
        probabilities = model.get("probabilities") or {}
        for market in markets:
            name = (market.get("name") or "").lower()
            if not (market.get("id") == "1" or "1x2" in name or name == "match result"):
                continue
            for selection in market.get("selections", []):
                decimal = _to_float(selection.get("odds"), 0)
                if decimal < 1.1:
                    continue
                selection_name = str(selection.get("name") or "")
                if selection_name in ("Home", "1"):
                    model_prob = float(probabilities.get("home_win") or 0)
                    side = "Home"
                elif selection_name in ("Away", "2"):
                    model_prob = float(probabilities.get("away_win") or 0)
                    side = "Away"
                elif selection_name in ("Draw", "X"):
                    model_prob = float(probabilities.get("draw") or 0)
                    side = "Draw"
                else:
                    continue

                implied = 1 / decimal * 100
                edge = model_prob - implied
                if edge >= min_edge:
                    bets.append(
                        {
                            "match": doc.get("sportybet_name") or doc.get("name"),
                            "sportybet_id": str(doc.get("sportybet_id") or doc.get("id") or ""),
                            "tournament": doc.get("tournament"),
                            "selection": side,
                            "decimal_odds": decimal,
                            "model_probability": round(model_prob, 1),
                            "implied_probability": round(implied, 1),
                            "edge": round(edge, 1),
                            "kelly": kelly_fraction(model_prob / 100, decimal),
                        }
                    )
        return bets

    value_bets = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_scan_doc, doc) for doc in docs]
        for future in as_completed(futures):
            value_bets.extend(future.result())

    value_bets.sort(key=lambda item: item["edge"], reverse=True)
    return {"status": "success", "date": target_date, "count": len(value_bets), "value_bets": value_bets}


@router.get("/analytics/performance")
def get_performance_analytics():
    """Win rates, grading status, market types, and recent graded results."""
    from app.league_memory import get_grading_metrics

    return {"status": "success", **get_grading_metrics()}


@router.get("/analytics/roi")
def get_roi_analysis():
    """ROI analysis.

    Real ROI needs entry odds. When odds are missing, expose win-rate and
    break-even odds instead of treating every win as a 1.00 return.
    """
    import sqlite3
    from app.league_memory import DB_PATH, _init_db

    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select
                ph.match_id,
                ph.pick_type,
                ph.selection,
                ph.confidence,
                ph.result,
                (
                    select ce.entry_odds
                    from clv_entries ce
                    where ce.match_id = ph.match_id
                      and ce.pick_type = ph.pick_type
                      and ce.selection = ph.selection
                      and ce.entry_odds is not null
                    order by ce.created_at desc
                    limit 1
                ) as entry_odds
            from prediction_history ph
            where ph.graded_at is not null
              and ph.pick_type != 'no_bet'
            """
        ).fetchall()

    settled = [row for row in rows if row["result"] in ("win", "loss")]
    wins = sum(1 for row in settled if row["result"] == "win")
    losses = sum(1 for row in settled if row["result"] == "loss")
    voids = sum(1 for row in rows if row["result"] == "void")
    settled_count = wins + losses
    win_rate = wins / settled_count if settled_count else 0.0
    break_even_odds = round(1 / win_rate, 3) if win_rate else None

    odds_rows = [row for row in settled if row["entry_odds"] and float(row["entry_odds"]) > 1]
    odds_staked = len(odds_rows)
    odds_returned = sum(float(row["entry_odds"]) if row["result"] == "win" else 0.0 for row in odds_rows)
    odds_roi = round((odds_returned - odds_staked) / odds_staked * 100, 2) if odds_staked else None

    # Backward-compatible field for the frontend: prefer real odds ROI when
    # available; otherwise show an even-money unit proxy, clearly marked below.
    even_money_roi = round((wins - settled_count) / settled_count * 100, 2) if settled_count else 0
    roi = odds_roi if odds_roi is not None else even_money_roi

    bands: dict[str, list[sqlite3.Row]] = {"50-59": [], "60-69": [], "70-79": [], "80+": []}
    for row in rows:
        if row["result"] not in ("win", "loss"):
            continue
        confidence = row["confidence"] or 0
        band = "80+" if confidence >= 80 else "70-79" if confidence >= 70 else "60-69" if confidence >= 60 else "50-59"
        bands[band].append(row)

    return {
        "status": "success",
        "total_predictions": len(rows),
        "settled_predictions": settled_count,
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "win_rate": round(win_rate * 100, 1) if settled_count else 0,
        "roi_percent": roi,
        "roi_basis": "entry_odds" if odds_roi is not None else "even_money_proxy",
        "entry_odds_covered": odds_staked,
        "odds_roi_percent": odds_roi,
        "even_money_roi_percent": even_money_roi,
        "break_even_average_odds": break_even_odds,
        "note": "True ROI requires entry odds. Voids are excluded from stake; missing odds rows use win-rate/break-even context only.",
        "by_confidence": {
            band: {
                "count": len(results),
                "wins": sum(1 for row in results if row["result"] == "win"),
                "losses": sum(1 for row in results if row["result"] == "loss"),
                "win_rate": round(sum(1 for row in results if row["result"] == "win") / len(results) * 100, 1) if results else 0,
            }
            for band, results in bands.items()
        },
    }


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


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_finished_doc(doc: dict) -> bool:
    period = (doc.get("period") or "").lower()
    return period in ("ft", "finished", "ended", "aet", "ap", "full time")


# ── Groq LangChain agent endpoints ───────────────────────────────────────────
# Ported from migrated predictz/agent.py
# Requires GROQ_API_KEY in .env

@router.post("/groq/predict")
def post_groq_predictions(
    match_date: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=100),
):
    """
    Run the Groq LangChain agent over today's enriched matches.
    Full 10-step reasoning: standings, H2H, Poisson, odds movement, SOS, web context.
    Requires GROQ_API_KEY.
    """
    try:
        from app.groq_agent import run_groq_predictions
        return run_groq_predictions(match_date=match_date, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/groq/status")
def get_groq_status():
    """Check if Groq LLM is configured and available."""
    try:
        from app.llm import is_groq_available, get_llm
        available = is_groq_available()
        return {
            "status": "success",
            "groq_available": available,
            "message": "Groq ready" if available else "Set GROQ_API_KEY in .env to enable Groq agent",
        }
    except Exception as e:
        return {"status": "error", "groq_available": False, "detail": str(e)}


# ── Ollama local LLM endpoints ────────────────────────────────────────────────
# Requires Ollama running locally. Pull models:
#   ollama pull qwen3:8b
#   ollama pull deepseek-r1:8b

@router.post("/ollama/predict")
def post_ollama_predictions(
    match_date: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=100),
    model: str = Query(default="qwen3:8b"),
):
    """
    Run Ollama local LLM predictions over today's enriched matches.
    Supported models: qwen3:8b (Best Overall), deepseek-r1:8b (Best Reasoning).
    Requires Ollama running locally.
    """
    try:
        from app.ollama_agent import run_ollama_predictions
        return run_ollama_predictions(match_date=match_date, limit=limit, model=model)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/ollama/status")
def get_ollama_status():
    """Check if Ollama is running and which models are available."""
    try:
        from app.ollama_agent import is_ollama_available, OLLAMA_MODELS
        from app.ollama_model_manager import get_model_status, is_model_loaded
        reachable = is_ollama_available()
        model_status = {}
        if reachable:
            for model in OLLAMA_MODELS:
                info = OLLAMA_MODELS[model]
                model_status[model] = {
                    **info,
                    "available": is_ollama_available(model),
                    "resident": is_model_loaded(model),
                    "pull_command": f"ollama pull {model}",
                }
        return {
            "status": "success",
            "ollama_running": reachable,
            "message": "Ollama ready" if reachable else "Ollama not running. Start with: ollama serve",
            "models": model_status,
            "model_manager": get_model_status(),
        }
    except Exception as e:
        return {"status": "error", "ollama_running": False, "detail": str(e)}
