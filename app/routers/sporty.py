from fastapi import APIRouter, HTTPException
from app.data_clients.sportybet_client import (
    fetch_match_info,
    fetch_live_and_upcoming_matches_post,
    fetch_upcoming_matches_post,
)
from app.ai.agent_tools import get_live_matches

router = APIRouter(prefix="/sporty", tags=["sporty"])


@router.get("/live")
def get_live_matches_route():
    return get_live_matches()


@router.get("/live/all")
def get_live_matches_all():
    try:
        matches = fetch_live_and_upcoming_matches_post()
        return {"status": "success", "count": len(matches), "matches": matches}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/upcoming")
def get_upcoming_matches():
    try:
        matches = fetch_upcoming_matches_post()
        return {"status": "success", "count": len(matches), "matches": matches}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/match/{sportybet_id}")
def get_sporty_match_info(sportybet_id: str):
    try:
        info = fetch_match_info(sportybet_id)
        if not info.get("found"):
            raise HTTPException(status_code=404, detail=info)
        return {"status": "success", **info}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
