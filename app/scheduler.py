"""
Scheduler
---------
Three background jobs:

  every 15 min  — INGEST upcoming matches from SportyBet into buffer (fast, no enrichment)
  every 5  min  — INGEST live matches + patch scores/periods (fast)
  every 2  min  — ENRICH worker: picks up unenriched/stale matches and enriches them
  every 15 min  — ARCHIVE finished matches to MongoDB

The ingest jobs are intentionally fast — they just dump raw data into the buffer.
The enrich worker runs continuously and processes matches in small batches.
This means the frontend always has data (even unenriched) and enrichment
catches up in the background without blocking anything.
"""
from __future__ import annotations

from datetime import date as dt
from typing import Any

from app.sportybet_client import fetch_live_matches_post, fetch_upcoming_matches_post
from app.buffer import (
    ingest_matches,
    patch_live_scores,
    run_enrichment_worker,
    get_buffer_stats,
)


_scheduler = None


# ── Job functions ─────────────────────────────────────────────────────────────

def job_ingest_upcoming(limit: int = 500) -> dict[str, Any]:
    """Fast: fetch upcoming matches from SportyBet and dump into buffer."""
    today = dt.today().isoformat()
    matches = fetch_upcoming_matches_post()[:limit]
    ingested = ingest_matches(matches, today)
    print(f"[scheduler] ingest_upcoming: {ingested} matches buffered for {today}")
    return {"status": "ok", "job": "ingest_upcoming", "date": today, "ingested": ingested}


def job_ingest_live(limit: int = 200) -> dict[str, Any]:
    """Fast: fetch live matches, update scores/periods in buffer, snapshot odds."""
    from app.league_memory import observe_matches
    from app.market import snapshot_odds

    today = dt.today().isoformat()
    matches = fetch_live_matches_post()[:limit]

    # ingest any new live matches not yet in buffer
    ingested = ingest_matches(matches, today)

    # patch scores/periods on already-buffered matches
    patched = patch_live_scores(matches)

    # snapshot odds for movement tracking
    snapped = 0
    for m in matches:
        if snapshot_odds({
            "sportybet_id":      m.get("id"),
            "sportybet_name":    m.get("name"),
            "match_date":        today,
            "sportybet_markets": m.get("markets", []),
        }):
            snapped += 1

    # record in league memory for late-goal stats
    observe_matches("sportybet", matches)

    print(f"[scheduler] ingest_live: {len(matches)} live | {ingested} new | {patched} patched | {snapped} odds snapped")
    return {
        "status": "ok",
        "job": "ingest_live",
        "live_count": len(matches),
        "ingested": ingested,
        "patched": patched,
        "odds_snapshots": snapped,
    }


def job_enrich_worker() -> dict[str, Any]:
    """
    Enrichment worker: picks up a batch of unenriched/stale matches and enriches them.
    Runs every 2 minutes — processes ENRICH_BATCH_SIZE matches per run.
    Prioritises: live matches first, then never-enriched, then stale upcoming.
    """
    result = run_enrichment_worker()
    if result.get("status") == "idle":
        print("[scheduler] enrich_worker: nothing to enrich")
    else:
        print(
            f"[scheduler] enrich_worker: "
            f"batch={result.get('batch')} "
            f"matched={result.get('matched')} "
            f"stored={result.get('stored')} "
            f"llm={result.get('llm_fallback')}"
        )
    return result


def job_archive_finished(match_date: str | None = None, limit: int = 1000) -> dict[str, Any]:
    """Archive finished matches to MongoDB via SofaScore."""
    from app.mongo_store import store_scheduled_matches
    from app.sofascore_client import fetch_all_scheduled_events
    from app.league_memory import observe_matches

    target_date = match_date or dt.today().isoformat()
    try:
        events = fetch_all_scheduled_events(target_date)[:limit]
        store_scheduled_matches(events, match_date=target_date)
        finished = [e for e in events if (e.get("status") or {}).get("type") == "finished"]
        observe_matches("sofascore", finished)
        print(f"[scheduler] archive_finished: {len(finished)}/{len(events)} finished on {target_date}")
        return {"status": "ok", "job": "archive_finished", "date": target_date,
                "total": len(events), "finished": len(finished)}
    except Exception as exc:
        print(f"[scheduler] archive_finished failed: {exc}")
        return {"status": "error", "job": "archive_finished", "error": str(exc)}


# ── Scheduler setup ───────────────────────────────────────────────────────────

def start_scheduler():
    global _scheduler
    if _scheduler:
        return _scheduler

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except Exception as exc:
        print(f"[scheduler] disabled — apscheduler not installed: {exc}")
        return None

    scheduler = BackgroundScheduler(timezone="UTC")

    # ingest upcoming — every 15 min, fast
    scheduler.add_job(
        _safe(job_ingest_upcoming),
        IntervalTrigger(minutes=15),
        id="ingest_upcoming",
        name="Ingest upcoming matches from SportyBet",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=120,
    )

    # ingest live — every 5 min, fast
    scheduler.add_job(
        _safe(job_ingest_live),
        IntervalTrigger(minutes=5),
        id="ingest_live",
        name="Ingest live matches + patch scores",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )

    # enrichment worker — every 2 min, processes a batch
    # misfire_grace_time=600 because enrichment can take a few minutes per batch
    scheduler.add_job(
        _safe(job_enrich_worker),
        IntervalTrigger(minutes=2),
        id="enrich_worker",
        name="Enrichment worker (SofaScore + web context)",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )

    # archive finished — every 15 min
    scheduler.add_job(
        _safe(job_archive_finished),
        IntervalTrigger(minutes=15),
        id="archive_finished",
        name="Archive finished matches to MongoDB",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    scheduler.start()
    _scheduler = scheduler
    print("[scheduler] started")
    print("[scheduler]   ingest_upcoming  every 15 min  (fast)")
    print("[scheduler]   ingest_live      every  5 min  (fast)")
    print("[scheduler]   enrich_worker    every  2 min  (batch enrichment)")
    print("[scheduler]   archive_finished every 15 min  (MongoDB)")
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(fn):
    """Wrap a job function so exceptions are logged but don't crash the scheduler."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            print(f"[scheduler] {fn.__name__} failed: {exc}")
    return wrapper
