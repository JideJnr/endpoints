from fastapi import APIRouter, HTTPException
from app.sofascore_client import (
    fetch_scheduled_events,
    fetch_all_scheduled_events,
    fetch_team_history,
    fetch_standings,
    fetch_h2h,
    fetch_pregame_form,
    fetch_managers,
    fetch_featured_players,
    fetch_odds,
    fetch_odds_featured,
    fetch_event_detail,
)

router = APIRouter(prefix="/sofascore", tags=["sofascore"])


@router.get("/scheduled/{date}")
def get_scheduled_events(date: str, tournament_id: int = 17):
    try:
        events = fetch_scheduled_events(date, tournament_id)
        return {"status": "success", "date": date, "count": len(events), "events": events}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/scheduled/{date}/all")
def get_all_scheduled_events(date: str):
    try:
        events = fetch_all_scheduled_events(date)
        return {"status": "success", "date": date, "count": len(events), "events": events}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/team/{team_id}/history")
def get_team_history(team_id: int, page: int = 0):
    try:
        data = fetch_team_history(team_id, page)
        return {"status": "success", "team_id": team_id, "page": page, **data}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/standings/{tournament_id}/{season_id}")
def get_standings(tournament_id: int, season_id: int):
    try:
        rows = fetch_standings(tournament_id, season_id)
        return {"status": "success", "tournament_id": tournament_id, "season_id": season_id, "standings": rows}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/event/{event_id}/h2h")
def get_h2h(event_id: int):
    try:
        return {"status": "success", "event_id": event_id, **fetch_h2h(event_id)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/event/{event_id}/pregame-form")
def get_pregame_form(event_id: int):
    try:
        return {"status": "success", "event_id": event_id, **fetch_pregame_form(event_id)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/event/{event_id}/managers")
def get_managers(event_id: int):
    try:
        return {"status": "success", "event_id": event_id, **fetch_managers(event_id)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/team/{team_id}/featured-players")
def get_featured_players(team_id: int):
    try:
        players = fetch_featured_players(team_id)
        return {"status": "success", "team_id": team_id, "players": players}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/event/{event_id}/odds")
def get_odds(event_id: int):
    try:
        markets = fetch_odds(event_id)
        return {"status": "success", "event_id": event_id, "markets": markets}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/event/{event_id}/odds/featured")
def get_odds_featured(event_id: int):
    try:
        return {"status": "success", "event_id": event_id, **fetch_odds_featured(event_id)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/event/{event_id}/detail")
def get_event_detail(event_id: int, date: str = None):
    from datetime import date as dt
    try:
        target_date = date or dt.today().isoformat()
        events = fetch_all_scheduled_events(target_date)
        event = next((e for e in events if e["id"] == event_id), None)
        if not event:
            raise HTTPException(status_code=404, detail=f"Event {event_id} not found on {target_date}")
        return {"status": "success", **fetch_event_detail(event)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
