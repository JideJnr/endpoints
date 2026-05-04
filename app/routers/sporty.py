from fastapi import APIRouter, HTTPException
from app.sportybet_client import fetch_live_matches

router = APIRouter(prefix="/sporty", tags=["sporty"])


@router.get("/live")
def get_live_matches():
    try:
        matches = fetch_live_matches()
        return {"status": "success", "count": len(matches), "matches": matches}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
