from __future__ import annotations

from datetime import date as dt
from typing import Any

from app.enrichment import run_enrichment
from app.league_memory import observe_matches
from app.market import snapshot_odds
from app.mongo_store import store_scheduled_matches
from app.sofascore_client import fetch_all_scheduled_events
from app.sportybet_client import fetch_live_matches_post, fetch_upcoming_matches_post


_scheduler = None


def scan_upcoming_buffer(limit: int = 500) -> dict[str, Any]:
    target_date = dt.today().isoformat()
    matches = fetch_upcoming_matches_post()[:limit]
    observed = observe_matches("sportybet", matches)
    enriched = run_enrichment(match_date=target_date, force=False, limit=limit)
    return {
        "status": "success",
        "job": "upcoming_buffer",
        "date": target_date,
        "sporty_count": len(matches),
        "observed": observed,
        "enriched": enriched,
    }


def scan_live_buffer(limit: int = 500) -> dict[str, Any]:
    matches = fetch_live_matches_post()[:limit]
    observed = observe_matches("sportybet", matches)
    snapped = 0
    for match in matches:
        if snapshot_odds({
            "sportybet_id": match.get("id"),
            "sportybet_name": match.get("name"),
            "match_date": dt.today().isoformat(),
            "sportybet_markets": match.get("markets", []),
        }):
            snapped += 1
    return {
        "status": "success",
        "job": "live_buffer",
        "sporty_count": len(matches),
        "observed": observed,
        "odds_snapshots": snapped,
    }


def scan_finished_matches(match_date: str | None = None, limit: int = 1000) -> dict[str, Any]:
    target_date = match_date or dt.today().isoformat()
    events = fetch_all_scheduled_events(target_date)[:limit]
    store_scheduled_matches(events, match_date=target_date)
    finished = [event for event in events if (event.get("status") or {}).get("type") == "finished"]
    observed = observe_matches("sofascore", finished)
    return {
        "status": "success",
        "job": "finished_archive",
        "date": target_date,
        "event_count": len(events),
        "finished_count": len(finished),
        "observed": observed,
    }


def start_scheduler():
    global _scheduler
    if _scheduler:
        return _scheduler

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except Exception as exc:
        print(f"[scheduler] disabled: {exc}")
        return None

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _safe_job,
        IntervalTrigger(minutes=15),
        args=["upcoming_buffer", scan_upcoming_buffer],
        id="upcoming_buffer",
        name="SportyBet upcoming buffer + SofaScore enrichment",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=180,
    )
    scheduler.add_job(
        _safe_job,
        IntervalTrigger(minutes=5),
        args=["live_buffer", scan_live_buffer],
        id="live_buffer",
        name="SportyBet live buffer + odds movement",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        _safe_job,
        IntervalTrigger(minutes=15),
        args=["finished_archive", scan_finished_matches],
        id="finished_archive",
        name="Finished match Mongo archive",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=180,
    )
    scheduler.start()
    _scheduler = scheduler
    print("[scheduler] started: upcoming=15m live=5m finished=15m")
    return scheduler


def stop_scheduler() -> bool:
    global _scheduler
    if not _scheduler:
        return False
    _scheduler.shutdown(wait=False)
    _scheduler = None
    return True


def scheduler_status() -> dict[str, Any]:
    if not _scheduler:
        return {"running": False, "jobs": []}
    return {
        "running": _scheduler.running,
        "jobs": [
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in _scheduler.get_jobs()
        ],
    }


def _safe_job(name: str, fn):
    try:
        result = fn()
        print(f"[scheduler] {name}: {result}")
    except Exception as exc:
        print(f"[scheduler] {name} failed: {exc}")
