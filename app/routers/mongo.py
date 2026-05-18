from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from app.mongo_store import init_mongo, list_finished_matches, mongo_status, save_finished_match, flush_buffer_to_mongo, cleanup_buffer
from app.scheduler import (
    job_ingest_upcoming,
    job_ingest_live,
    job_enrich_worker,
    job_archive_finished,
    job_flush_to_mongo,
    scheduler_status,
    start_scheduler,
    stop_scheduler,
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
        return job_ingest_upcoming(limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan/live")
def post_scan_live(limit: int = Query(default=200, ge=1, le=1000)):
    try:
        return job_ingest_live(limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan/enrich")
def post_scan_enrich():
    try:
        return job_enrich_worker()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan/finished")
def post_scan_finished(match_date: Optional[str] = None, limit: int = Query(default=1000, ge=1, le=2000)):
    try:
        return job_archive_finished(match_date=match_date, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/flush")
def post_flush_to_mongo():
    """Flush live/upcoming enriched buffer rows to MongoDB."""
    try:
        return job_flush_to_mongo()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/purge-junk-predictions")
def post_purge_junk_predictions():
    """
    One-time cleanup: delete all prediction_history rows that are:
    - pick_type = 'no_bet'
    - confidence < 55
    - ungraded and older than today (stale pending that will never resolve)
    """
    import sqlite3
    from app.league_memory import DB_PATH, _init_db
    from datetime import date

    _init_db()
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        r1 = conn.execute("delete from prediction_history where pick_type = 'no_bet'")
        r2 = conn.execute("delete from prediction_history where confidence < 55")
        r3 = conn.execute(
            "delete from prediction_history where graded_at is null and date(created_at) < ?",
            (today,),
        )
        conn.commit()
    return {
        "status": "ok",
        "deleted_no_bet": r1.rowcount,
        "deleted_low_confidence": r2.rowcount,
        "deleted_stale_pending": r3.rowcount,
        "total_deleted": r1.rowcount + r2.rowcount + r3.rowcount,
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
        from app.scheduler import job_prune_mongo
        return job_prune_mongo(keep_days=keep_days)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
