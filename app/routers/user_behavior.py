from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import DB_PATH
from app.league_memory import _init_db, track_user_behavior, get_user_behavior_summary, get_behavior_weighted_picks

router = APIRouter(prefix="/user-behavior", tags=["user-behavior"])


class BehaviorTrack(BaseModel):
    match_id: str
    action: str
    pick_type: Optional[str] = None
    selection: Optional[str] = None
    confidence: Optional[float] = None
    metadata: Optional[dict[str, Any]] = None


@router.post("/track")
def track_behavior(body: BehaviorTrack) -> dict[str, Any]:
    """Track a user interaction with a prediction for self-learning."""
    try:
        _init_db()
        track_user_behavior(
            match_id=body.match_id,
            user_action=body.action,
            pick_type=body.pick_type,
            selection=body.selection,
            confidence=body.confidence,
            metadata=body.metadata,
        )
        return {"status": "success", "message": "Behavior tracked"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/summary")
def behavior_summary(match_id: Optional[str] = None, days_back: int = 30) -> dict[str, Any]:
    """Get aggregated user behavior summary for self-learning."""
    try:
        _init_db()
        return get_user_behavior_summary(match_id=match_id, days=days_back)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/history/{match_id}")
def behavior_history(match_id: str) -> dict[str, Any]:
    """Get user behavior history for a specific match."""
    try:
        _init_db()
        summary = get_user_behavior_summary(match_id=match_id, days=90)
        weighted_picks = get_behavior_weighted_picks(match_id)
        return {"status": "success", "summary": summary, "weighted_picks": weighted_picks}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))