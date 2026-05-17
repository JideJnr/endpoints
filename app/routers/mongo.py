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
def post_scan_enrich(batch_size: int = Query(default=30, ge=1, le=100)):
    try:
        return job_enrich_worker(batch_size=batch_size)
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


@router.post("/cleanup")
def post_cleanup_buffer():
    """Safety-net: remove any finished rows still in buffer + stale unenriched rows."""
    try:
        return cleanup_buffer()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
