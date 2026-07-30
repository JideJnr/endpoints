from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import db_conn
from app.db import DB_PATH
from app.league_memory import _init_db, get_behavior_weighted_picks, track_user_behavior

router = APIRouter(prefix="/betbuilder", tags=["betbuilder"])


class AutoPlaceRequest(BaseModel):
    model_config = {"populate_by_name": True}
    selections: list[dict[str, Any]]
    stake: float
    share_code: Optional[str] = Field(default=None, alias="shareCode")


@router.get("/auto-suggestions")
def auto_suggestions(max_picks: int = 5, min_confidence: float = 65.0) -> dict[str, Any]:
    """Get auto-bet suggestions based on predictions and user behavior."""
    try:
        _init_db()
        import sqlite3

        with db_conn(timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            # Get top predictions across all active matches
            preds = conn.execute(
                """
                select ph.*
                from prediction_history ph
                where ph.result is null
                  and ph.confidence >= ?
                order by ph.confidence desc
                limit ?
                """,
                (min_confidence, max_picks * 3),  # get more to filter
            ).fetchall()

            if not preds:
                return {"status": "no_suggestions", "reason": "No qualifying predictions found"}

            suggestions = []
            for row in preds:
                pick = dict(row)
                match_id = pick.get("match_id", "")

                # Get user's past behavior for this match
                user_history = get_behavior_weighted_picks(match_id)
                user_actions = {h["user_action"] for h in user_history}

                # Adjust confidence based on user behavior
                boost = 1.0
                if "accepted" in user_actions:
                    boost = 1.05
                if "rejected" in user_actions:
                    boost = 0.95

                adjusted_confidence = round((pick.get("confidence") or 0) * boost, 2)

                # Only include if still above minimum after adjustment
                if adjusted_confidence < min_confidence:
                    continue

                suggestions.append({
                    "match_id": match_id,
                    "match_name": pick.get("match_name"),
                    "pick_type": pick.get("pick_type"),
                    "selection": pick.get("selection"),
                    "confidence": adjusted_confidence,
                    "original_confidence": pick.get("confidence"),
                    "reason": pick.get("reason"),
                    "user_boost": boost,
                })

            # Sort by adjusted confidence and limit
            suggestions.sort(key=lambda x: x["confidence"], reverse=True)
            suggestions = suggestions[:max_picks]

            return {
                "status": "success",
                "suggestions": suggestions,
                "count": len(suggestions),
            }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/auto-place")
def auto_place(body: AutoPlaceRequest) -> dict[str, Any]:
    """Auto-place bets based on selections."""
    try:
        _init_db()
        import sqlite3

        placed = []
        with db_conn(timeout=30) as conn:
            conn.row_factory = sqlite3.Row

            for sel in body.selections:
                match_id = sel.get("match_id", "")
                pick_type = sel.get("pick_type", "")
                selection = sel.get("selection", "")
                confidence = sel.get("confidence", 0)

                # Track the auto-place action
                track_user_behavior(
                    match_id=match_id,
                    user_action="bet_placed",
                    pick_type=pick_type,
                    selection=selection,
                    confidence=confidence,
                    metadata={
                        "auto_placed": True,
                        "stake": body.stake,
                        "share_code": body.share_code,
                    },
                )

                placed.append({
                    "match_id": match_id,
                    "pick_type": pick_type,
                    "selection": selection,
                    "confidence": confidence,
                    "stake": body.stake,
                })

        return {
            "status": "placed",
            "placed_count": len(placed),
            "total_stake": body.stake * len(placed),
            "selections": placed,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))