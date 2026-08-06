from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


class IntervalPatch(BaseModel):
    intervals: dict[str, int] = Field(default_factory=dict)


@router.get("/intervals")
def get_intervals(active_only: bool = True) -> dict[str, Any]:
    try:
        from app.scheduling.scheduler import scheduler_intervals

        return scheduler_intervals(active_only=active_only)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.patch("/intervals")
def patch_intervals(payload: IntervalPatch) -> dict[str, Any]:
    try:
        from app.scheduling.scheduler import patch_scheduler_intervals

        return patch_scheduler_intervals(payload.intervals)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/intervals/reset")
def reset_intervals() -> dict[str, Any]:
    try:
        from app.scheduling.scheduler import reset_scheduler_intervals

        return reset_scheduler_intervals()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
