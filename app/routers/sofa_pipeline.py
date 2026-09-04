"""
SofaScore-Only Pipeline Router
-------------------------------
Endpoints for the cloud-safe SofaScore-only enrichment and prediction pipeline.

  GET  /sofa-pipeline/status          — toggle state + buffer summary
  POST /sofa-pipeline/toggle          — enable / disable the pipeline
  POST /sofa-pipeline/run             — manual one-shot cycle
  POST /sofa-pipeline/ingest          — stage 1 only (fetch + buffer)
  POST /sofa-pipeline/enrich          — stage 2+3 only (enrich + predict)
"""
from __future__ import annotations

import logging
from datetime import date as dt
from typing import Any, Optional

from app.storage.db import db_conn
from fastapi import APIRouter, Body, HTTPException, Query

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sofa-pipeline", tags=["sofa-pipeline"])


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
def get_sofa_pipeline_status():
    """Return toggle state + count of SofaScore-source matches in the buffer."""
    from app.data_clients.sofa_pipeline import get_sofa_pipeline_mode
    from app.storage.db import DB_PATH
    from app.storage.league_memory import _init_db
    import sqlite3

    mode = get_sofa_pipeline_mode()

    _init_db()
    counts: dict[str, int] = {}
    try:
        with db_conn(timeout=10) as conn:
            row = conn.execute(
                """
                select
                    count(*) as total,
                    sum(case when is_live = 1 then 1 else 0 end) as live,
                    sum(case when enriched_at is not null then 1 else 0 end) as enriched,
                    sum(case when raw_enriched like '%"prediction"%' then 1 else 0 end) as predicted
                from match_buffer
                where match_id like 'sofa:%'
                  and is_finished = 0
                """
            ).fetchone()
            if row:
                counts = {
                    "total": row[0] or 0,
                    "live": row[1] or 0,
                    "enriched": row[2] or 0,
                    "predicted": row[3] or 0,
                }
    except Exception as exc:
        counts = {"error": str(exc)}

    return {
        "status": "success",
        "mode": mode,
        "buffer": counts,
    }


# ── Toggle ────────────────────────────────────────────────────────────────────

@router.post("/toggle")
def toggle_sofa_pipeline(payload: dict[str, Any] = Body(...)):
    """
    Enable or disable the SofaScore-only pipeline.

    Body: `{"enabled": true}` or `{"enabled": false}`
    """
    from app.data_clients.sofa_pipeline import set_sofa_pipeline_mode
    from app.scheduling.pipeline_registry import activate_pipeline

    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="Body must be {\"enabled\": true|false}")

    if enabled:
        activate_pipeline("sofa_pipeline")
    mode = set_sofa_pipeline_mode(enabled)
    return {
        "status": "success",
        "message": f"SofaScore pipeline {'enabled' if enabled else 'disabled'}",
        "mode": mode,
    }


# ── Manual full cycle ─────────────────────────────────────────────────────────

@router.post("/run")
def run_sofa_pipeline(
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD (defaults to today)"),
    ingest_limit: int = Query(default=300, ge=1, le=1000),
    enrich_batch: int = Query(default=20, ge=1, le=100),
    include_live: bool = Query(default=True),
):
    """
    Run a full SofaScore pipeline cycle: Ingest → Enrich → Predict.

    - `date`: target date (defaults to today)
    - `ingest_limit`: max SofaScore events to fetch per date
    - `enrich_batch`: max matches to enrich+predict per enrichment pass
    - `include_live`: also fetch live events
    """
    from app.data_clients.sofa_pipeline import run_sofa_pipeline_cycle
    from app.scheduling.scheduler import run_job_with_guard

    target_date = date or dt.today().isoformat()
    try:
        result = run_job_with_guard(
            run_sofa_pipeline_cycle,
            match_date=target_date,
            ingest_limit=ingest_limit,
            enrich_batch=enrich_batch,
            include_live=include_live,
            guard_job_id="sofa_pipeline",
        )
        return result
    except Exception as exc:
        _logger.exception("sofa-pipeline/run failed")
        raise HTTPException(status_code=500, detail=f"Pipeline cycle failed: {exc}")


# ── Stage 1 only: Ingest ──────────────────────────────────────────────────────

@router.post("/ingest")
def ingest_sofa(
    date: Optional[str] = Query(default=None),
    limit: int = Query(default=300, ge=1, le=1000),
    include_live: bool = Query(default=True),
):
    """Stage 1: Fetch SofaScore events and write into match_buffer."""
    from app.data_clients.sofa_pipeline import ingest_from_sofascore

    target_date = date or dt.today().isoformat()
    try:
        return ingest_from_sofascore(
            match_date=target_date,
            include_live=include_live,
            limit=limit,
        )
    except Exception as exc:
        _logger.exception("sofa-pipeline/ingest failed")
        raise HTTPException(status_code=500, detail=f"Ingest failed: {exc}")


# ── Stage 2+3 only: Enrich + Predict ─────────────────────────────────────────

@router.post("/enrich")
def enrich_sofa(
    date: Optional[str] = Query(default=None),
    batch_size: int = Query(default=10, ge=1, le=100),
    live_only: bool = Query(default=False),
):
    """Stage 2+3: Enrich buffered SofaScore matches and run predictions."""
    from app.data_clients.sofa_pipeline import enrich_sofa_pipeline
    from app.scheduling.scheduler import run_job_with_guard

    target_date = date or dt.today().isoformat()
    try:
        return run_job_with_guard(
            enrich_sofa_pipeline,
            match_date=target_date,
            batch_size=batch_size,
            live_only=live_only,
            guard_job_id="sofa_pipeline",
        )
    except Exception as exc:
        _logger.exception("sofa-pipeline/enrich failed")
        raise HTTPException(status_code=500, detail=f"Enrich failed: {exc}")
