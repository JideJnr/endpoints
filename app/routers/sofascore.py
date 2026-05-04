from fastapi import APIRouter, HTTPException
from app.sofascore_client import fetch_scheduled_events

router = APIRouter(prefix="/sofascore", tags=["sofascore"])


@router.get("/scheduled/{date}")
def get_scheduled_events(date: str, tournament_id: int = 17):
    try:
        events = fetch_scheduled_events(date, tournament_id)
        return {"status": "success", "date": date, "count": len(events), "events": events}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
