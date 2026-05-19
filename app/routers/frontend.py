from __future__ import annotations

from datetime import date as dt
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

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
        archived = _archived_match_detail(sportybet_id)
        if archived:
            return archived
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found")
    return _match_detail(doc)


@router.get("/matches/{sportybet_id}/sofascore-candidates")
def get_sofascore_candidates(sportybet_id: str):
    """Return the full SofaScore pool for this SportyBet match state."""
    from app.buffer import _is_ghost_match, _sofascore_date_candidates
    from app.sofascore_client import fetch_all_scheduled_events, fetch_live_events, is_usable_event_for_mode

    doc = get_buffered_match(sportybet_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found in buffer")

    target_date = doc.get("match_date") or dt.today().isoformat()
    live = _is_live_doc(doc)
    raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else {}
    period = doc.get("period") or raw_sporty.get("period")
    if not live and _is_ghost_match(doc.get("start_time"), period):
        return {
            "status": "success",
            "sportybet_id": sportybet_id,
            "match_date": target_date,
            "dates_scanned": [],
            "mode": "prematch",
            "count": 0,
            "candidates": [],
            "reason": "sportybet_match_start_time_is_stale",
        }

    scan_dates = [target_date]
    if not live:
        sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
        scan_dates = _sofascore_date_candidates(sporty, target_date)

    try:
        if live:
            events = fetch_live_events()
        else:
            events = []
            seen_ids: set[str] = set()
            for scan_date in scan_dates:
                for event in fetch_all_scheduled_events(scan_date):
                    eid = str(event.get("id") or "")
                    if eid and eid not in seen_ids:
                        events.append(event)
                        seen_ids.add(eid)
    except Exception as exc:
        # SofaScore unreachable / rate-limited — return empty list with a clear message
        return {
            "status": "error",
            "sportybet_id": sportybet_id,
            "match_date": target_date,
            "mode": "live" if live else "prematch",
            "count": 0,
            "candidates": [],
            "error": f"SofaScore scan failed: {exc}",
        }

    filtered = []
    for event in events:
        if not is_usable_event_for_mode(event, live=live):
            continue
        filtered.append(event)

    candidate_pool = sorted(
        (_candidate_summary(event, doc, target_date) for event in filtered),
        key=lambda item: item["score"],
        reverse=True,
    )
    candidates = [item for item in candidate_pool if float(item.get("score") or 0) >= 0.58]
    return {
        "status": "success",
        "sportybet_id": sportybet_id,
        "match_date": target_date,
        "dates_scanned": scan_dates,
        "mode": "live" if live else "prematch",
        "count": len(candidates),
        "rejected_low_score": len(candidate_pool) - len(candidates),
        "min_score": 0.58,
        "candidates": candidates,
    }


@router.post("/matches/{sportybet_id}/sofascore-match")
def match_sofascore_candidate(
    sportybet_id: str,
    payload: dict[str, Any] = Body(...),
):
    """Manually attach a SofaScore event and persist the correction."""
    from app.buffer import store_enriched
    from app.sofascore_client import fetch_all_scheduled_events, fetch_live_events, is_usable_event_for_mode

    sofa_id = payload.get("sofascore_id")
    if sofa_id is None:
        raise HTTPException(status_code=400, detail="sofascore_id is required")

    doc = get_buffered_match(sportybet_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found")

    dates = []
    for value in (payload.get("match_date"), doc.get("match_date"), dt.today().isoformat()):
        if value and value not in dates:
            dates.append(value)

    sofa = None
    if _is_live_doc(doc):
        try:
            sofa = next((event for event in fetch_live_events() if str(event.get("id")) == str(sofa_id)), None)
        except Exception:
            sofa = None
    for target_date in dates:
        if sofa:
            break
        try:
            events = fetch_all_scheduled_events(target_date)
        except Exception:
            continue
        sofa = next((event for event in events if str(event.get("id")) == str(sofa_id)), None)
        if sofa:
            break
    if not sofa and isinstance(payload.get("event"), dict):
        sofa = payload["event"]
    if not sofa:
        raise HTTPException(status_code=404, detail=f"SofaScore event {sofa_id} was not found")
    if not is_usable_event_for_mode(sofa, live=_is_live_doc(doc)):
        status = sofa.get("status") or {}
        raise HTTPException(
            status_code=400,
            detail=f"SofaScore event {sofa_id} is not usable for this match state: {status.get('description') or status.get('type')}",
        )

    now = datetime.now(timezone.utc).isoformat()
    enriched_doc = {
        **doc,
        "sportybet_id": sportybet_id,
        "sportybet_name": doc.get("sportybet_name") or doc.get("name"),
        "match_date": doc.get("match_date") or payload.get("match_date") or dt.today().isoformat(),
        "sofascore_id": sofa.get("id"),
        "sofascore_name": sofa.get("name"),
        "sofascore_event": sofa,
        "sofascore_detail": None,
        "web_context": {},
        "match_score": _candidate_score(sofa, doc),
        "manual_match": True,
        "manual_matched_at": now,
        "raw_sporty": doc.get("raw_sporty") or doc,
        "raw_sofascore_event": sofa.get("raw_event"),
        "enriched_at": now,
    }

    store_enriched(sportybet_id, enriched_doc)
    return {
        "status": "success",
        "sportybet_id": sportybet_id,
        "sofascore_id": sofa.get("id"),
        "matched_sofascore": True,
        "saved": True,
        "needs_enrichment": True,
        "match": _match_detail(enriched_doc),
    }


@router.post("/predictions/refresh")
def refresh_predictions_today():
    """
    Clear today's prediction history, re-enrich all buffered matches,
    and re-run predictions. Call this after engine changes.
    """
    from datetime import date
    from app.league_memory import DB_PATH, _init_db
    from app.buffer import get_buffered_matches
    from app.enriched_prediction import predict_enriched_match
    from app.league_memory import record_prediction
    import sqlite3 as _sqlite3

    today = date.today().isoformat()
    _init_db()

    # 1. Delete today's prediction history so we get a clean slate
    with _sqlite3.connect(DB_PATH) as conn:
        deleted = conn.execute(
            "delete from prediction_history where date(created_at) = ?", (today,)
        ).rowcount
        conn.commit()

    # 2. Re-predict all buffered matches for today
    docs = get_buffered_matches(today, limit=500)
    predicted = 0
    errors = 0
    for doc in docs:
        try:
            prediction = predict_enriched_match(doc)
            record_prediction({
                **prediction,
                "match_id": doc.get("sportybet_id") or doc.get("id"),
                "match_date": today,
                "source": "enriched_ensemble",
            })
            predicted += 1
        except Exception as exc:
            errors += 1

    return {
        "status": "success",
        "date": today,
        "deleted_old": deleted,
        "predicted": predicted,
        "errors": errors,
        "total_matches": len(docs),
    }


@router.get("/predictions/today")
def get_predictions_today():
    """Return today's latest predictions per match, sorted by confidence desc. Excludes no_bet."""
    from datetime import date, datetime, timedelta
    from app.league_memory import list_prediction_history, DB_PATH, _init_db
    import sqlite3 as _sqlite3

    today = date.today().isoformat()
    all_preds = list_prediction_history(limit=2000).get("predictions") or []
    cutoff = datetime.now() - timedelta(hours=36)
    today_preds = []
    for prediction in all_preds:
        created_raw = str(prediction.get("created_at") or "")
        try:
            created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            created_at = None
        if created_at and created_at >= cutoff:
            today_preds.append(prediction)

    # Build a lookup of match_id → buffer status (period, is_live, is_finished)
    _init_db()
    buffer_status: dict[str, dict] = {}
    try:
        with _sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = _sqlite3.Row
            rows = conn.execute(
                "select match_id, period, is_live, is_finished from match_buffer"
            ).fetchall()
            for row in rows:
                buffer_status[str(row["match_id"])] = {
                    "period":      row["period"],
                    "is_live":     bool(row["is_live"]),
                    "is_finished": bool(row["is_finished"]),
                }
    except Exception:
        pass

    # Keep latest prediction per match_id (list is already ordered by created_at desc)
    seen: dict[str, dict] = {}
    for p in today_preds:
        mid = str(p.get("match_id") or "")
        if not mid or mid in seen:
            continue
        # Skip no_bet picks
        picks = p.get("picks") or []
        real_picks = [pk for pk in picks if pk.get("type") != "no_bet"]
        if not real_picks:
            continue
        p = {**p, "picks": real_picks}
        # Update best_pick to the highest confidence real pick
        best = max(real_picks, key=lambda pk: int(pk.get("confidence") or 0))
        p["best_pick"] = best
        # Attach live/period status from buffer
        status = buffer_status.get(mid, {})
        p["period"]      = status.get("period") or p.get("period")
        p["is_live"]     = status.get("is_live", False)
        p["is_finished"] = status.get("is_finished", False)

        # Attach regime info + filter picks that don't meet regime threshold
        try:
            from app.regime import get_regime, passes_regime_gate
            tournament = p.get("league_name") or p.get("tournament") or ""
            regime = get_regime(tournament)
            p["regime"] = {"tier": regime.tier, "name": regime.name}
            # Filter picks below regime minimum confidence
            regime_picks = [
                pk for pk in real_picks
                if int(pk.get("confidence") or 0) >= regime.min_confidence
            ]
            if regime_picks:
                p["picks"] = regime_picks
                best = max(regime_picks, key=lambda pk: int(pk.get("confidence") or 0))
                p["best_pick"] = best
        except Exception:
            pass

        seen[mid] = p
    sorted_preds = sorted(
        seen.values(),
        key=lambda x: int((x.get("best_pick") or {}).get("confidence") or 0),
        reverse=True,
    )

    # ── Portfolio correlation filter ──────────────────────────────────────────
    try:
        from app.portfolio import filter_correlated, portfolio_summary
        sorted_preds = filter_correlated(sorted_preds)
        summary = portfolio_summary(sorted_preds)
    except Exception:
        summary = {}

    return {
        "status": "success",
        "date": today,
        "count": len(sorted_preds),
        "predictions": sorted_preds,
        "portfolio": summary,
    }


@router.get("/matches/upcoming-enriched-predicted")
def get_upcoming_enriched_predicted(limit: int = Query(default=500, ge=1, le=1000)):
    """Upcoming buffered matches with enrichment and latest prediction status."""
    from datetime import date
    from app.buffer import get_buffered_matches
    from app.league_memory import list_prediction_history

    today = date.today().isoformat()
    docs = get_buffered_matches(limit=limit)
    history = list_prediction_history(limit=3000).get("predictions") or []
    latest_by_match: dict[str, dict[str, Any]] = {}
    for prediction in history:
        match_id = str(prediction.get("match_id") or "")
        if match_id and match_id not in latest_by_match:
            latest_by_match[match_id] = prediction

    rows = []
    for doc in docs:
        time_context = doc.get("time_context") or {}
        match_date = time_context.get("local_date") or doc.get("match_date") or today
        if str(match_date) < today:
            continue
        if _is_live_doc(doc) or _is_finished_doc(doc):
            continue
        match_id = str(doc.get("sportybet_id") or doc.get("id") or "")
        prediction = latest_by_match.get(match_id)
        best_pick = (prediction or {}).get("best_pick") or ((prediction or {}).get("picks") or [{}])[0]
        summary = _match_summary(doc)
        rows.append(
            {
                **summary,
                "match_date": match_date,
                "time_context": doc.get("time_context"),
                "lifecycle": doc.get("lifecycle"),
                "enriched": bool(doc.get("enriched_at")),
                "manual_match": bool(doc.get("manual_match")),
                "predicted": bool(prediction),
                "prediction": prediction,
                "best_pick": best_pick if best_pick else None,
                "data_quality": {
                    "has_sofascore_detail": bool(doc.get("sofascore_detail")),
                    "has_web_context": bool((doc.get("web_context") or {}).get("snippets")),
                    "has_raw_sporty": bool(doc.get("raw_sporty")),
                    "has_raw_sofascore": bool(doc.get("raw_sofascore_event") or doc.get("sofascore_event")),
                },
            }
        )

    rows.sort(
        key=lambda item: (
            int((item.get("best_pick") or {}).get("confidence") or 0),
            1 if item.get("predicted") else 0,
            1 if item.get("enriched") else 0,
            -_sort_start(item.get("start_time")),
        ),
        reverse=True,
    )
    return {
        "status": "success",
        "date": today,
        "count": len(rows),
        "summary": {
            "upcoming": len(rows),
            "enriched": sum(1 for item in rows if item["enriched"]),
            "predicted": sum(1 for item in rows if item["predicted"]),
            "matched_sofascore": sum(1 for item in rows if item.get("sofascore_id")),
        },
        "matches": rows,
    }


@router.post("/matches/purge-ghosts")
def purge_ghost_matches_endpoint():
    """Delete stale not-started matches from the buffer whose kick-off has passed."""
    from app.buffer import purge_ghost_matches
    from app.mongo_store import cleanup_buffer
    purged = purge_ghost_matches()
    cleanup = cleanup_buffer()
    return {
        "status": "success",
        "purged_ghosts": purged,
        "deleted_finished": cleanup.get("deleted_finished", 0),
        "deleted_90_plus": cleanup.get("deleted_90_plus", 0),
        "deleted_stale": cleanup.get("deleted_stale_unenriched", 0),
    }


@router.post("/matches/cleanup")
def cleanup_finished_matches():
    """Immediately remove all finished, 90+, and ghost matches from the buffer."""
    from app.mongo_store import cleanup_buffer
    from app.buffer import purge_ghost_matches
    from app.league_memory import DB_PATH, _init_db
    import sqlite3 as _sqlite3

    # First pass: archive any is_finished=1 rows that weren't cleaned up
    _init_db()
    archived = 0
    with _sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "select match_id from match_buffer where is_finished = 1"
        ).fetchall()
    for (match_id,) in rows:
        try:
            from app.buffer import _archive_finished_locally
            _archive_finished_locally(match_id)
            archived += 1
        except Exception:
            pass

    # Second pass: cleanup by period string + stale rows + 90+ + ghost
    result = cleanup_buffer()
    ghost_deleted = purge_ghost_matches()

    return {
        "status": "success",
        "archived_locally":  archived,
        "deleted_finished":  result.get("deleted_finished", 0),
        "deleted_90_plus":   result.get("deleted_90_plus", 0),
        "deleted_ghost":     result.get("deleted_ghost", 0) + ghost_deleted,
        "deleted_stale":     result.get("deleted_stale_unenriched", 0),
    }


@router.get("/buffer/status")
def get_buffer_status():
    """Scheduler health + buffer counts. Use to verify background jobs are running."""
    return {
        "status": "success",
        "scheduler": scheduler_status(),
        "buffer": get_buffer_stats(),
    }


@router.get("/analytics/clv")
def get_clv_analytics(days: int = Query(default=30, ge=1, le=365)):
    """
    Closing Line Value analytics.
    avg_clv > 0 means you're consistently beating the closing price = real edge.
    """
    from app.clv import get_clv_summary
    return {"status": "success", **get_clv_summary(days=days)}


@router.post("/analytics/clv/compute")
def compute_clv_now(match_date: Optional[str] = None):
    """Force-compute CLV for a specific date (defaults to today)."""
    from datetime import date
    from app.clv import compute_clv_for_date
    target = match_date or date.today().isoformat()
    result = compute_clv_for_date(target)
    return {"status": "success", **result}


@router.get("/analytics/calibration")
def get_calibration():
    """Historical win rate per pick_type × confidence band. Used to size stakes."""
    from app.confidence_calibrator import get_calibration_table
    return {"status": "success", "calibration": get_calibration_table()}


@router.post("/analytics/calibration/rebuild")
def rebuild_calibration_endpoint():
    """Force-rebuild the calibration table from all graded predictions."""
    from app.confidence_calibrator import rebuild_calibration
    result = rebuild_calibration()
    return {"status": "success", **result}


@router.get("/analytics/odds-patterns")
def get_odds_pattern_stats():
    """Aggregate stats on odds movement patterns and their historical win rates."""
    from app.odds_pattern import pattern_stats
    return {"status": "success", **pattern_stats()}


@router.get("/analytics/odds-patterns/{match_id}")
def get_match_pattern_signal(match_id: str):
    """Get the odds pattern signal for a specific match."""
    from app.odds_pattern import pattern_signal
    from urllib.parse import unquote
    signal = pattern_signal(unquote(match_id))
    return {"status": "success", "match_id": match_id, "signal": signal}


@router.get("/analytics/regime")
def get_regime_info(tournament: str = Query(default=""), category: str = Query(default="")):
    """Look up the liquidity regime for a tournament name."""
    from app.regime import get_regime, TIER_1, TIER_2, TIER_3, TIER_4
    regime = get_regime(tournament, category)
    return {
        "status":    "success",
        "tournament": tournament,
        "tier":       regime.tier,
        "name":       regime.name,
        "min_confidence":  regime.min_confidence,
        "edge_threshold":  regime.edge_threshold,
        "stake_cap":       regime.stake_cap,
        "description":     regime.description,
    }


@router.get("/analytics/regime/tiers")
def get_all_tiers():
    """Return all regime tier definitions."""
    from app.regime import TIER_1, TIER_2, TIER_3, TIER_4
    return {
        "status": "success",
        "tiers": [
            {k: getattr(t, k) for k in
             ["tier", "name", "min_confidence", "edge_threshold", "stake_cap", "description"]}
            for t in [TIER_1, TIER_2, TIER_3, TIER_4]
        ],
    }


@router.post("/results/grade")
def grade_results(hours_back: int = 24):
    """
    Fetch finished results from SportyBet and SofaScore, grade pending predictions,
    and archive matched buffer rows to MongoDB.
    """
    import time as _time
    import json
    from datetime import date as _date, timedelta as _timedelta
    from app.sportybet_client import fetch_results
    from app.league_memory import DB_PATH, _init_db, _grade_pick_for_match, grade_predictions_for_date
    from app.mongo_store import archive_finished_match_from_buffer
    from app.sofascore_client import fetch_all_scheduled_events
    import sqlite3

    now_ms   = int(_time.time() * 1000)
    start_ms = now_ms - (hours_back * 3_600_000)

    try:
        results = fetch_results(start_ms, now_ms, count=500)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SportyBet results fetch failed: {exc}")

    result_map = {
        str(r["id"]): r
        for r in results
        if r.get("score", {}).get("home") is not None
    }

    _init_db()
    graded = archived = skipped = 0

    # grade pending predictions
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        pending = conn.execute(
            "select id, match_id, match_name, league_name, pick_type, selection, "
            "confidence, signals_json, "
            "(select match_date from match_buffer where match_id=ph.match_id limit 1) as match_date "
            "from prediction_history ph "
            "where graded_at is null and pick_type != 'no_bet'"
        ).fetchall()

    for row in pending:
        result = result_map.get(str(row["match_id"]))
        if not result:
            skipped += 1
            continue
        score = result["score"]
        outcome = _grade_pick_for_match(row["pick_type"], row["selection"], score["home"], score["away"], row["match_name"])
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "update prediction_history set result=?, final_home=?, final_away=?, "
                "graded_at=current_timestamp where id=?",
                (outcome, score["home"], score["away"], row["id"]),
            )
            conn.commit()
        # store signal outcomes in MongoDB
        try:
            from app.mongo_store import store_signal_outcomes, is_configured
            if is_configured():
                signals = json.loads(row["signals_json"]) if row["signals_json"] else []
                tournament = row["league_name"] or ""
                country = tournament.split(" - ")[0].strip() if " - " in tournament else ""
                store_signal_outcomes(
                    match_id=str(row["match_id"]),
                    match_name=row["match_name"],
                    tournament=tournament,
                    country=country,
                    match_date=row["match_date"],
                    signals=signals,
                    result=outcome,
                    pick_type=row["pick_type"],
                    selection=row["selection"],
                    confidence=row["confidence"],
                )
        except Exception:
            pass
        graded += 1

    # archive buffer rows that now have a result
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        buffer_rows = conn.execute(
            "select match_id from match_buffer where is_finished=0"
        ).fetchall()

    for row in buffer_rows:
        if str(row["match_id"]) not in result_map:
            continue
        score = result_map[str(row["match_id"])]["score"]
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "update match_buffer set period='Ended', score_home=?, score_away=?, "
                "is_live=0, is_finished=1 where match_id=?",
                (str(score["home"]), str(score["away"]), row["match_id"]),
            )
            conn.commit()
        try:
            archive_finished_match_from_buffer(str(row["match_id"]))
            archived += 1
        except Exception:
            pass

    sofascore = {"graded": 0, "skipped": 0, "dates": [], "errors": {}}
    days = max(1, min(7, (hours_back + 23) // 24 + 1))
    for offset in range(days):
        target_date = (_date.today() - _timedelta(days=offset)).isoformat()
        try:
            events = fetch_all_scheduled_events(target_date)
            result = grade_predictions_for_date(target_date, events)
            sofascore["graded"] += int(result.get("graded") or 0)
            sofascore["skipped"] += int(result.get("skipped") or 0)
            sofascore["dates"].append({"date": target_date, **result})
        except Exception as exc:
            sofascore["errors"][target_date] = str(exc)

    return {
        "status": "ok",
        "results_fetched": len(results),
        "predictions_graded": graded,
        "predictions_skipped": skipped,
        "sofascore_predictions_graded": sofascore["graded"],
        "sofascore_predictions_skipped": sofascore["skipped"],
        "sofascore": sofascore,
        "matches_archived": archived,
        "signal_stats": _grade_signal_stats(graded + sofascore["graded"]),
    }


@router.get("/predictions/odds-only")
def get_odds_only_predictions():
    """
    Fast predictions using only 1x2 odds — no SofaScore required.
    Runs on every buffered match that has odds, including unenriched ones.
    Useful as a baseline when enrichment hasn't run yet.
    """
    from app.odds_predictor import odds_only_prediction
    from datetime import date

    today = date.today().isoformat()
    docs = get_buffered_matches(today, limit=500)
    predictions = []
    for doc in docs:
        pred = odds_only_prediction(doc)
        if pred:
            predictions.append(pred)

    predictions.sort(
        key=lambda p: (p.get("picks") or [{}])[0].get("confidence", 0),
        reverse=True,
    )
    return {
        "status": "success",
        "date": today,
        "count": len(predictions),
        "predictions": predictions,
    }


@router.get("/analytics/signals")
def get_signal_analytics(
    country: str = "",
    tournament: str = "",
    min_samples: int = 5,
):
    """
    Signal win rate analytics.
    Scope: whole DB (default), filtered by country, or filtered by tournament.
    """
    from app.mongo_store import get_signal_stats, is_configured
    if not is_configured():
        raise HTTPException(status_code=503, detail="MongoDB not configured")
    return {
        "status": "success",
        **get_signal_stats(
            country=country or None,
            tournament=tournament or None,
            min_samples=min_samples,
        ),
    }


@router.get("/analytics/model-explorer")
def get_model_explorer(
    preset: str = Query(default="all"),
    model: str = Query(default="all"),
    min_samples: int = Query(default=1, ge=1, le=100),
    limit: int = Query(default=2000, ge=1, le=5000),
):
    """Prediction history grouped by pick/model so the UI can rank by accuracy."""
    import json
    import sqlite3
    from app.league_memory import DB_PATH, _init_db

    def _pick_family(pick_type: str, selection: str) -> str:
        text = f"{pick_type} {selection}".lower()
        if "under" in text or "btts no" in text or "both teams to score - no" in text:
            return "low_scoring"
        if "over" in text or "both teams to score" in text or "goal" in text:
            return "goals"
        if "draw" in text and ("home" in text or "away" in text):
            return "double_chance"
        if "home" in text:
            return "home"
        if "away" in text:
            return "away"
        if "draw" in text:
            return "draw"
        if "value" in text:
            return "value"
        return pick_type or "other"

    def _normalise_selection(selection: str) -> str:
        text = " ".join(str(selection or "").lower().replace("-", " ").split())
        aliases = {
            "home win": "home",
            "home": "home",
            "1": "home",
            "away win": "away",
            "away": "away",
            "2": "away",
            "draw": "draw",
            "x": "draw",
            "home or draw": "home_or_draw",
            "draw or home": "home_or_draw",
            "1x": "home_or_draw",
            "away or draw": "away_or_draw",
            "draw or away": "away_or_draw",
            "x2": "away_or_draw",
            "home or away": "home_or_away",
            "away or home": "home_or_away",
            "12": "home_or_away",
            "both teams to score": "btts_yes",
            "btts yes": "btts_yes",
            "both teams to score no": "btts_no",
            "both teams to score - no": "btts_no",
            "btts no": "btts_no",
        }
        return aliases.get(text, text)

    def _display_selection(selection: str) -> str:
        normal = _normalise_selection(selection)
        labels = {
            "home": "Home Win",
            "away": "Away Win",
            "draw": "Draw",
            "home_or_draw": "Home or Draw",
            "away_or_draw": "Away or Draw",
            "home_or_away": "Home or Away",
            "btts_yes": "Both Teams To Score",
            "btts_no": "Both Teams To Score - No",
        }
        return labels.get(normal, selection or "Pick")

    def _uses_model(signals: list[dict[str, Any]], model_name: str) -> bool:
        if model_name == "all":
            return True
        names = {str(signal.get("name") or "") for signal in signals}
        aliases = {
            "poisson": {"poisson_model"},
            "dixon_coles": {"dixon_coles_model"},
            "elo": {"elo_model"},
            "ensemble": {"ensemble_model"},
            "database": {"finished_database_memory", "prediction_memory"},
            "odds": {"odds_edge", "odds_progression", "odds_pattern"},
            "rules": {"goal_pressure", "h2h_edge", "league_position_edge", "recent_history_edge", "common_opponent_edge"},
        }
        return bool(names & aliases.get(model_name, {model_name}))

    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select id, match_id, match_name, league_name, country_name, pick_type,
                   selection, confidence, reason, result, graded_at, created_at,
                   signals_json
            from prediction_history
            where pick_type != 'no_bet'
            order by created_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()

    filtered_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    raw_count = 0
    for row in rows:
        signals = json.loads(row["signals_json"] or "[]")
        raw_count += 1
        normal_selection = _normalise_selection(row["selection"] or "")
        display_selection = _display_selection(row["selection"] or "")
        family = _pick_family(row["pick_type"] or "", display_selection)
        if preset != "all" and family != preset:
            continue
        if not _uses_model(signals, model):
            continue
        item = {
            "id": row["id"],
            "match_id": row["match_id"],
            "match_name": row["match_name"],
            "league_name": row["league_name"],
            "country_name": row["country_name"],
            "pick_type": row["pick_type"],
            "selection": display_selection,
            "raw_selection": row["selection"],
            "selection_key": normal_selection,
            "confidence": row["confidence"],
            "reason": row["reason"],
            "result": row["result"],
            "graded": bool(row["graded_at"]),
            "created_at": row["created_at"],
            "family": family,
            "models_used": sorted({str(s.get("name") or "") for s in signals if s.get("name")}),
        }
        dedupe_key = (str(row["match_id"] or ""), str(row["pick_type"] or ""), normal_selection)
        existing = filtered_by_key.get(dedupe_key)
        if not existing:
            filtered_by_key[dedupe_key] = item
        elif item["graded"] and not existing["graded"]:
            filtered_by_key[dedupe_key] = item

    filtered = sorted(filtered_by_key.values(), key=lambda item: item.get("created_at") or "", reverse=True)

    groups: dict[str, dict[str, Any]] = {}
    for item in filtered:
        key = f"{item['family']}::{item['pick_type']}::{item['selection_key']}"
        group = groups.setdefault(key, {
            "family": item["family"],
            "pick_type": item["pick_type"],
            "selection": item["selection"],
            "selection_key": item["selection_key"],
            "total": 0,
            "graded": 0,
            "wins": 0,
            "losses": 0,
            "pending": 0,
            "avg_confidence": 0.0,
            "models_used": set(),
            "recent": [],
        })
        group["total"] += 1
        group["avg_confidence"] += float(item["confidence"] or 0)
        group["models_used"].update(item["models_used"])
        if item["graded"]:
            group["graded"] += 1
            if item["result"] == "win":
                group["wins"] += 1
            elif item["result"] == "loss":
                group["losses"] += 1
        else:
            group["pending"] += 1
        if len(group["recent"]) < 8:
            group["recent"].append(item)

    summary = []
    for group in groups.values():
        group["sample_ready"] = group["graded"] >= min_samples
        group["accuracy"] = round(group["wins"] / group["graded"] * 100, 1) if group["graded"] else None
        group["avg_confidence"] = round(group["avg_confidence"] / group["total"], 1) if group["total"] else 0
        group["models_used"] = sorted(group["models_used"])
        summary.append(group)

    summary.sort(key=lambda g: (g["sample_ready"], g["accuracy"] or -1, g["graded"], g["avg_confidence"]), reverse=True)
    ready = [group for group in summary if group["sample_ready"]]
    best = ready[0] if ready else (summary[0] if summary else None)
    return {
        "status": "success",
        "preset": preset,
        "model": model,
        "min_samples": min_samples,
        "count": len(filtered),
        "summary": {
            "raw_rows": raw_count,
            "unique_picks": len(filtered),
            "duplicates_removed": max(0, raw_count - len(filtered)),
            "groups": len(summary),
            "sample_ready": len(ready),
            "best_accuracy": best.get("accuracy") if best else None,
            "best_selection": best.get("selection") if best else None,
            "graded": sum(group["graded"] for group in summary),
            "pending": sum(group["pending"] for group in summary),
        },
        "groups": summary,
        "recent": filtered[:50],
        "presets": ["all", "low_scoring", "goals", "home", "away", "draw", "double_chance", "value"],
        "models": ["all", "poisson", "dixon_coles", "elo", "ensemble", "database", "odds", "rules"],
    }


@router.post("/matches/{sportybet_id}/enrich")
def enrich_single_match(sportybet_id: str):
    """Force-enrich a single match using saved manual SofaScore match when present."""
    from app.buffer import get_buffered_match, store_enriched
    from app.sofascore_client import fetch_all_scheduled_events, fetch_event_detail, fetch_live_events, is_usable_event_for_mode
    from app.enrichment import _fuzzy_match, _llm_match, _is_junk, FUZZY_THRESHOLD, LLM_FALLBACK_THRESHOLD
    from app.web_context import search_match_context
    from app.market import snapshot_odds
    from app.time_context import match_time_context
    from datetime import date, datetime, timezone

    doc = get_buffered_match(sportybet_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found in buffer")

    sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
    match_date = doc.get("match_date") or date.today().isoformat()
    saved_sofa_id = doc.get("sofascore_id")
    sofa = None
    score = 0.0
    source = "auto"

    if saved_sofa_id:
        sofa = _find_sofascore_event(str(saved_sofa_id), match_date, _is_live_doc(doc), fetch_live_events, fetch_all_scheduled_events)
        if not sofa and isinstance(doc.get("sofascore_event"), dict):
            sofa = doc["sofascore_event"]
        score = _candidate_score(sofa, doc) if sofa else float(doc.get("match_score") or 1.0)
        source = "manual"
    else:
        try:
            sofa_events = fetch_live_events() if _is_live_doc(doc) else fetch_all_scheduled_events(match_date)
            sofa_events = [
                event for event in sofa_events
                if is_usable_event_for_mode(event, live=_is_live_doc(doc))
            ]
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
            _home_team(sporty),
            _away_team(sporty),
            sporty.get("tournament") or "",
        )
    except Exception:
        pass

    now = datetime.now(timezone.utc).isoformat()
    enriched_doc = {
        **doc,
        **sporty,
        "sportybet_id":      sporty.get("id") or sportybet_id,
        "sportybet_name":    sporty.get("sportybet_name") or sporty.get("name"),
        "match_date":        match_date,
        "sofascore_id":      sofa.get("id") if sofa else None,
        "sofascore_name":    sofa.get("name") if sofa else None,
        "sofascore_event":   sofa,
        "sofascore_detail":  detail,
        "web_context":       web_context,
        "match_score":       round(score, 3),
        "match_source":      source,
        "manual_match":      bool(saved_sofa_id),
        "manual_matched_at":  doc.get("manual_matched_at"),
        "raw_sporty":        doc.get("raw_sporty") or sporty,
        "raw_sofascore_event": sofa.get("raw_event") if isinstance(sofa, dict) else None,
        "time_context":      match_time_context({**sporty, "sofascore_event": sofa}),
        "enriched_at":       now,
    }

    snapshot_odds(enriched_doc)
    store_enriched(sportybet_id, enriched_doc)
    try:
        from app.enriched_prediction import predict_enriched_match
        from app.league_memory import record_prediction

        record_prediction(predict_enriched_match(enriched_doc))
    except Exception:
        pass

    return {
        "status": "success",
        "sportybet_id": sportybet_id,
        "matched_sofascore": bool(sofa),
        "sofascore_id": sofa.get("id") if sofa else None,
        "fuzzy_score": round(score, 3),
        "match_source": source,
        "has_detail": bool(detail),
        "has_web_context": bool(web_context.get("snippets")),
        "web_context_query": web_context.get("query"),
        "enriched_at": now,
    }


@router.post("/matches/{sportybet_id}/predict")
def predict_single_match(sportybet_id: str):
    """Run every available model on the richest available match data."""
    from app.buffer import get_buffered_match
    from app.enriched_prediction import predict_enriched_match
    from app.ai_brain import oversee_prediction
    from app.league_memory import record_prediction
    from datetime import date

    doc = get_buffered_match(sportybet_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found")

    if not doc.get("sofascore_detail") and (doc.get("sofascore_id") or not doc.get("enriched_at")):
        try:
            enrich_single_match(sportybet_id)
            doc = get_buffered_match(sportybet_id) or doc
        except Exception:
            pass

    try:
        prediction = predict_enriched_match(doc)
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
            "source": "enriched_ensemble",
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
        "played_seconds": doc.get("played_seconds"),
        "is_live": _is_live_doc(doc),
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
        "statistics": detail.get("statistics") or [],
        "standings": detail.get("standings"),
        "home_last_matches": detail.get("home_last_matches") or [],
        "away_last_matches": detail.get("away_last_matches") or [],
        "lineups": detail.get("lineups"),
        "home_players": detail.get("homeFeaturedPlayers") or detail.get("home_featured_players"),
        "away_players": detail.get("awayFeaturedPlayers") or detail.get("away_featured_players"),
        "incidents": detail.get("incidents"),
        "web_context": {
            "query": web.get("query"),
            "snippets": web.get("snippets", []),
            "articles": web.get("scraped", []) or web.get("articles", []),
            "error": web.get("error"),
            "disabled": web.get("disabled"),
            "diagnostics": web.get("diagnostics"),
        },
        "time_context": doc.get("time_context"),
        "lifecycle": doc.get("lifecycle"),
        "manual_match": bool(doc.get("manual_match")),
        "raw_sporty": doc.get("raw_sporty"),
        "raw_sofascore_event": doc.get("raw_sofascore_event"),
        "odds_movement": get_movement(sportybet_id) if sportybet_id else {"snapshots": 0, "movement": None},
        "prediction": _latest_prediction(sportybet_id),
        "raw": doc,
    }


def _archived_match_detail(match_id: str) -> dict[str, Any] | None:
    """Read a finished match that has already left the hot SQL buffer."""
    archived: dict[str, Any] | None = None
    try:
        from app.mongo_store import get_finished_match
        archived = get_finished_match(match_id)
    except Exception:
        archived = None
    if not archived:
        archived = _local_finished_match(match_id)
    if not archived:
        return None

    detail = archived.get("sofascore_detail") or {}
    score = archived.get("score") or {}
    doc = {
        "sportybet_id": archived.get("sportybet_id") or archived.get("_id") or match_id,
        "id": archived.get("_id") or match_id,
        "sportybet_name": archived.get("name") or archived.get("match_name"),
        "home_team": archived.get("home_team"),
        "away_team": archived.get("away_team"),
        "tournament": archived.get("tournament"),
        "category": archived.get("country") or archived.get("category"),
        "match_date": archived.get("match_date"),
        "period": "Finished",
        "is_finished": True,
        "score": score,
        "sofascore_id": archived.get("sofascore_id") or detail.get("id"),
        "sofascore_detail": detail,
        "sportybet_markets": archived.get("sportybet_markets") or [],
        "web_context": archived.get("web_context") or {},
        "time_context": archived.get("time_context"),
        "lifecycle": {"state": "archived", "stages": {"finished": True, "archived": True}, "missing": []},
        "raw_sporty": archived.get("raw_sporty"),
        "raw_sofascore_event": archived.get("raw_sofascore_event"),
        "archived_at": archived.get("finished_at") or archived.get("archived_at"),
        "raw": archived,
    }
    out = _match_detail(doc)
    out["archive_source"] = archived.get("archive_source") or "finished_store"
    out["is_finished"] = True
    out["period"] = "Finished"
    return out


def _local_finished_match(match_id: str) -> dict[str, Any] | None:
    try:
        import json
        import sqlite3
        from app.league_memory import DB_PATH, _init_db

        _init_db()
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select raw_doc from finished_matches where match_id = ?",
                (str(match_id),),
            ).fetchone()
        if not row or not row["raw_doc"]:
            return None
        doc = json.loads(row["raw_doc"])
        doc["_id"] = str(match_id)
        doc["archive_source"] = "local_sqlite"
        return doc
    except Exception:
        return None


def _find_sofascore_event(
    sofa_id: str,
    match_date: str,
    live: bool,
    fetch_live_events,
    fetch_all_scheduled_events,
) -> dict[str, Any] | None:
    from app.sofascore_client import is_usable_event_for_mode

    if live:
        try:
            match = next((event for event in fetch_live_events() if str(event.get("id")) == sofa_id), None)
            if match and is_usable_event_for_mode(match, live=True):
                return match
        except Exception:
            pass

    dates = []
    for value in (match_date, dt.today().isoformat()):
        if value and value not in dates:
            dates.append(value)
    for target_date in dates:
        try:
            events = fetch_all_scheduled_events(target_date)
        except Exception:
            continue
        match = next((event for event in events if str(event.get("id")) == sofa_id), None)
        if match and is_usable_event_for_mode(match, live=False):
            return match
    return None


def _candidate_summary(event: dict[str, Any], doc: dict[str, Any], match_date: str) -> dict[str, Any]:
    status = event.get("status") or {}
    score = event.get("score") or {}
    return {
        "id": event.get("id"),
        "name": event.get("name"),
        "home_team": (event.get("home_team") or {}).get("name"),
        "away_team": (event.get("away_team") or {}).get("name"),
        "tournament": (event.get("tournament") or {}).get("name"),
        "status": status.get("description") or status.get("type"),
        "status_type": status.get("type"),
        "scoreline": _scoreline(score),
        "start_timestamp": event.get("start_timestamp"),
        "match_date": match_date,
        "score": _candidate_score(event, doc),
        "event": event,
    }


def _candidate_score(event: dict[str, Any], doc: dict[str, Any]) -> float:
    try:
        from app.enrichment import _event_score
        sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
        return _event_score(
            {
                **sporty,
                "name": sporty.get("name") or doc.get("sportybet_name") or doc.get("name"),
                "home_team": sporty.get("home_team") or _home_team(doc),
                "away_team": sporty.get("away_team") or _away_team(doc),
                "tournament": sporty.get("tournament") or doc.get("tournament"),
                "category": sporty.get("category") or doc.get("category"),
                "start_time": sporty.get("start_time") or doc.get("start_time"),
            },
            event,
        )
    except Exception:
        sofa_name = event.get("name") or ""
        sporty_name = doc.get("sportybet_name") or doc.get("name") or ""
        direct = SequenceMatcher(None, sofa_name.lower(), sporty_name.lower()).ratio()
        home = SequenceMatcher(None, ((event.get("home_team") or {}).get("name") or "").lower(), _home_team(doc).lower()).ratio()
        away = SequenceMatcher(None, ((event.get("away_team") or {}).get("name") or "").lower(), _away_team(doc).lower()).ratio()
        return round(max(direct, (home + away) / 2), 3)


def _scoreline(score: dict[str, Any]) -> str | None:
    home = score.get("home")
    away = score.get("away")
    if home is None or away is None:
        return None
    return f"{home}-{away}"


def _is_live_doc(doc: dict[str, Any]) -> bool:
    period = doc.get("period")
    return bool(period and period not in ("Not started", "Not start", "FT", "AET", "Finished", "Ended", ""))


def _is_finished_doc(doc: dict[str, Any]) -> bool:
    period = str(doc.get("period") or "").lower()
    if period in {"ft", "finished", "ended", "aet", "ap", "full time"}:
        return True
    status = doc.get("status") or {}
    if isinstance(status, dict):
        return status.get("type") == "finished" or status.get("code") == 100
    return str(status).lower() in {"finished", "ended"}


def _sort_start(value: Any) -> int:
    try:
        number = int(value)
        return number if number > 1e10 else number * 1000
    except (TypeError, ValueError):
        return 0


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
        "played_seconds": doc.get("played_seconds"),
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
        "has_statistics": bool(detail.get("statistics")),
        "has_lineups": bool(detail.get("lineups")),
        "has_last_matches": bool(detail.get("home_last_matches") or detail.get("away_last_matches")),
        "has_web_context": bool(doc.get("web_context")),
        "lifecycle": doc.get("lifecycle"),
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


def _grade_signal_stats(graded: int) -> dict[str, Any]:
    """Quick signal win rate summary from MongoDB after grading."""
    if graded == 0:
        return {}
    try:
        from app.mongo_store import get_signal_stats, is_configured
        if not is_configured():
            return {}
        stats = get_signal_stats(min_samples=3)
        top = stats.get("signals", [])[:5]
        return {"top_signals": top, "scope": "all"}
    except Exception:
        return {}
