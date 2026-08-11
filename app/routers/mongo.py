from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from app.storage.db import db_conn
from app.storage.mongo_store import init_mongo, list_finished_matches, mongo_status, save_finished_match, flush_buffer_to_mongo, cleanup_buffer
from app.scheduling.scheduler import (
    job_ingest_upcoming,
    job_ingest_live,
    job_enrich_worker,
    job_archive_finished,
    job_flush_to_mongo,
    job_live_priority,
    job_competition_special,
    reset_deferred_predictions_and_repredict,
    get_live_priority_mode,
    set_live_priority_mode,
    scheduler_status,
    start_scheduler,
    stop_scheduler,
    run_job_with_guard,
)


router = APIRouter(prefix="/mongo", tags=["mongo"])


@router.get("/status")
def get_mongo_status():
    try:
        return {"status": "success", **mongo_status()}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/init")
def post_mongo_init():
    try:
        return {"status": "success", **init_mongo()}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/finished-matches")
def get_finished_matches(match_date: Optional[str] = None, limit: int = Query(default=200, ge=1, le=1000)):
    try:
        matches = list_finished_matches(match_date=match_date, limit=limit)
        return {"status": "success", "count": len(matches), "matches": matches}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/finished-matches")
def post_finished_match(source: str = "manual", match: dict = Body(...)):
    try:
        archived = save_finished_match(source, match)
        return {"status": "success", "archived": archived}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/scheduler")
def get_scheduler():
    return {"status": "success", **scheduler_status()}


@router.post("/scheduler/start")
def post_scheduler_start():
    scheduler = start_scheduler()
    return {"status": "success", "started": bool(scheduler), **scheduler_status()}


@router.post("/scheduler/stop")
def post_scheduler_stop():
    stopped = stop_scheduler()
    return {"status": "success", "stopped": stopped, **scheduler_status()}


@router.post("/scan/upcoming")
def post_scan_upcoming(limit: int = Query(default=500, ge=1, le=1000)):
    try:
        return run_job_with_guard(job_ingest_upcoming, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan/live")
def post_scan_live(limit: int = Query(default=200, ge=1, le=1000)):
    try:
        return run_job_with_guard(job_ingest_live, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan/refresh-odds")
def post_refresh_buffer_odds(limit: int = Query(default=500, ge=1, le=1000)):
    """Refresh live and upcoming SportyBet state, update buffer odds, and snapshot movement."""
    try:
        from app.storage.buffer import refresh_sporty_buffer_scope

        live = refresh_sporty_buffer_scope("live", limit=min(limit, 1000))
        upcoming = refresh_sporty_buffer_scope("upcoming", limit=min(limit, 1000))
        return {
            "status": "success",
            "live": live,
            "upcoming": upcoming,
            "fetched": int(live.get("fetched") or 0) + int(upcoming.get("fetched") or 0),
            "patched": int(live.get("patched") or 0) + int(upcoming.get("patched") or 0),
            "ingested": int(live.get("ingested") or 0) + int(upcoming.get("ingested") or 0),
            "odds_snapshotted": int(live.get("odds_snapshotted") or 0) + int(upcoming.get("odds_snapshotted") or 0),
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan/enrich")
def post_scan_enrich():
    try:
        return run_job_with_guard(job_enrich_worker)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan/live-priority")
def post_scan_live_priority(
    count: int = Query(default=30, ge=1, le=80),
    limit: int = Query(default=500, ge=1, le=1000),
):
    try:
        return run_job_with_guard(job_live_priority, count=count, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan/world-cup-special")
def post_scan_world_cup_special():
    """Manual run for the dedicated World Cup competition lane."""
    try:
        return run_job_with_guard(job_competition_special)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/live-priority")
def get_live_priority():
    try:
        return {"status": "success", **get_live_priority_mode()}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/live-priority")
def post_live_priority(payload: dict = Body(...)):
    try:
        enabled = bool(payload.get("enabled"))
        return {"status": "success", **set_live_priority_mode(enabled)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan/match-and-enrich")
def post_match_and_enrich(count: int = Query(default=12, ge=1, le=50)):
    """Date-aware manual run for the upcoming analytics page."""
    try:
        from app.storage.buffer import run_date_aware_enrichment

        ingest = run_job_with_guard(job_ingest_upcoming, limit=500)
        enrich = run_date_aware_enrichment(count=count)
        return {"status": "success", "ingest": ingest, "enrich": enrich}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan/reset-deferred")
def post_reset_deferred_predictions():
    """
    Clear all deferred predictions (insufficient data) and reset match state
    so the system can rematch, renrich, and repredict.

    This endpoint:
    1. Finds all matches in the buffer with deferred predictions
       (``prediction_error`` set in ``raw_enriched``) or with a
       ``sofascore_match_status`` of ``no_match`` / ``srl_skip``.
    2. Resets their enrichment state — clears ``sofascore_match_status``,
       ``sofascore_id``, ``sofascore_detail``, ``enriched_at``, and all
       prediction-related fields from the ``raw_enriched`` JSON.
    3. Runs ``job_unified_upcoming`` so the pipeline immediately re-ingests,
       re-matches, re-enriches, and re-predicts the affected fixtures.
    """
    try:
        return reset_deferred_predictions_and_repredict()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan/finished")
def post_scan_finished(match_date: Optional[str] = None, limit: int = Query(default=1000, ge=1, le=2000)):
    try:
        return run_job_with_guard(job_archive_finished, match_date=match_date, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/flush")
def post_flush_to_mongo():
    """Flush live/upcoming enriched buffer rows to MongoDB."""
    try:
        return run_job_with_guard(job_flush_to_mongo)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/purge-junk-predictions")
def post_purge_junk_predictions(confirm: bool = False):
    """
    Inspect junk prediction rows by default. Pass confirm=true to delete rows that are:
    - pick_type = 'no_bet'
    - confidence < 55
    - ungraded and older than today (stale pending that will never resolve)

    Prediction history is learning data, so this endpoint is intentionally
    non-destructive unless the caller explicitly confirms the purge.
    """
    import sqlite3
    from app.storage.db import DB_PATH
    from app.storage.league_memory import _init_db
    from datetime import date

    _init_db()
    today = date.today().isoformat()
    with db_conn(timeout=30) as conn:
        no_bet = conn.execute("select count(*) from prediction_history where pick_type = 'no_bet'").fetchone()[0]
        low_confidence = conn.execute("select count(*) from prediction_history where confidence < 55").fetchone()[0]
        stale_pending = conn.execute(
            "select count(*) from prediction_history where graded_at is null and date(created_at) < ?",
            (today,),
        ).fetchone()[0]
        deleted_no_bet = deleted_low_confidence = deleted_stale_pending = 0
        if confirm:
            deleted_no_bet = conn.execute("delete from prediction_history where pick_type = 'no_bet'").rowcount
            deleted_low_confidence = conn.execute("delete from prediction_history where confidence < 55").rowcount
            deleted_stale_pending = conn.execute(
                "delete from prediction_history where graded_at is null and date(created_at) < ?",
                (today,),
            ).rowcount
            conn.commit()
    return {
        "status": "ok",
        "confirmed": confirm,
        "candidate_no_bet": no_bet,
        "candidate_low_confidence": low_confidence,
        "candidate_stale_pending": stale_pending,
        "deleted_no_bet": deleted_no_bet,
        "deleted_low_confidence": deleted_low_confidence,
        "deleted_stale_pending": deleted_stale_pending,
        "total_deleted": deleted_no_bet + deleted_low_confidence + deleted_stale_pending,
    }


@router.post("/cleanup")
def post_cleanup_buffer():
    """Safety-net: remove any finished rows still in buffer + stale unenriched rows."""
    try:
        return cleanup_buffer()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/prune")
def post_prune_mongo(keep_days: int = Query(default=90, ge=7, le=365)):
    """Prune finished_matches from MongoDB older than keep_days. Default 90 days."""
    try:
        from app.scheduling.scheduler import job_prune_mongo
        return job_prune_mongo(keep_days=keep_days)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
