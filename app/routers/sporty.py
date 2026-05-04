from fastapi import APIRouter, HTTPException
from app.sportybet_client import fetch_live_matches, fetch_live_matches_post

router = APIRouter(prefix="/sporty", tags=["sporty"])


@router.get("/live")
def get_live_matches():
    try:
        matches = fetch_live_matches()
        return {"status": "success", "count": len(matches), "matches": matches}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/live/all")
def get_live_matches_all():
    try:
        matches = fetch_live_matches_post()
        return {"status": "success", "count": len(matches), "matches": matches}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
