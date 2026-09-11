from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.dependencies import require_admin

# Every endpoint here backs the /scheduler ops page, which is RequireAdmin-gated
# in the frontend — enforce that server-side too, not just in the UI.
router = APIRouter(prefix="/scheduler", tags=["scheduler"], dependencies=[Depends(require_admin)])


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
