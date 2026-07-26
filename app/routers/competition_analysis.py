"""Competition Analysis API endpoints.

GET  /{competition_key}/analysis/latest   — most recent analysis row
GET  /{competition_key}/analysis/history  — paginated history
POST /{competition_key}/analysis/trigger  — trigger on-demand analysis
"""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.competition_special import DEFAULT_WORLD_CUP, TOP_30_COMPETITIONS
from app.league_memory import DB_PATH, _init_db

router = APIRouter(prefix="/competition", tags=["competition_analysis"])

_VALID_KEYS = frozenset(entry["key"] for entry in TOP_30_COMPETITIONS) | {DEFAULT_WORLD_CUP["key"]}


def _guard_key(competition_key: str) -> None:
    if competition_key not in _VALID_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown competition key: {competition_key}")


@router.get("/{competition_key}/analysis/latest")
def get_latest_analysis_endpoint(competition_key: str) -> dict[str, Any]:
    _guard_key(competition_key)
    from app.competition_analyser import get_latest_analysis, init_competition_analysis_table

    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        init_competition_analysis_table(conn)
        row = get_latest_analysis(competition_key, conn)

    if row is None:
        return {"status": "not_found"}
    return {
        "status": "ok",
        "competition_key": row.get("competition_key"),
        "round_name": row.get("round_name"),
        "analysis_text": row.get("analysis_text"),
        "model_used": row.get("model_used"),
        "generated_at": row.get("generated_at"),
        "match_count": row.get("match_count"),
        "matchday_date": row.get("matchday_date"),
    }


@router.get("/{competition_key}/analysis/history")
def get_analysis_history_endpoint(
    competition_key: str,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    _guard_key(competition_key)
    from app.competition_analyser import get_analysis_history, init_competition_analysis_table

    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        init_competition_analysis_table(conn)
        rows = get_analysis_history(competition_key, limit, conn)

    return {"status": "ok", "competition_key": competition_key, "count": len(rows), "history": rows}


@router.post("/{competition_key}/analysis/trigger")
def trigger_analysis_endpoint(competition_key: str) -> dict[str, Any]:
    _guard_key(competition_key)
    from app.competition_analyser import run_competition_analysis

    result = run_competition_analysis(competition_key)
    return result
