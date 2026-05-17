"""
Scheduler
---------
  every 30 sec  — INGEST live matches + patch scores/periods (fast)
                  └ finished matches archived to MongoDB + deleted from buffer immediately
  every  2 min  — INGEST upcoming matches from SportyBet into buffer (fast)
  every 30 sec  — ENRICH worker: picks up unenriched/stale matches, auto-predicts
  every  2 min  — FLUSH live/upcoming enriched rows to MongoDB + safety-net cleanup
  every 15 min  — ARCHIVE SofaScore scheduled events to MongoDB
  every  6 hrs  — GRADE yesterday's predictions + update ELO
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
    from app.market import snapshot_odds

    today = dt.today().isoformat()
    matches = fetch_upcoming_matches_post()[:limit]
    ingested = ingest_matches(matches, today)
    snapped = 0
    for m in matches:
        if snapshot_odds({
            "sportybet_id":      m.get("id"),
            "sportybet_name":    m.get("name"),
            "match_date":        today,
            "sportybet_markets": m.get("markets", []),
        }):
            snapped += 1
    print(f"[scheduler] ingest_upcoming: {ingested} matches buffered for {today} | {snapped} odds snapped")
    return {"status": "ok", "job": "ingest_upcoming", "date": today, "ingested": ingested, "odds_snapshots": snapped}


def job_ingest_live(limit: int = 200) -> dict[str, Any]:
    """Fast: fetch live matches, add new ones to buffer, patch scores on existing ones."""
    from app.league_memory import observe_matches
    from app.market import snapshot_odds

    today = dt.today().isoformat()
    matches = fetch_live_matches_post()[:limit]

    # add brand-new live matches not yet in buffer
    new_count = ingest_matches(matches, today)

    # patch scores/periods + archive finished ones
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

    observe_matches("sportybet", matches)

    print(f"[scheduler] ingest_live: {len(matches)} from api | {new_count} new | {patched} patched | {snapped} odds snapped")
    return {
        "status": "ok",
        "job": "ingest_live",
        "live_count": len(matches),
        "new": new_count,
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


def job_flush_to_mongo() -> dict[str, Any]:
    """Flush live/upcoming enriched buffer rows to MongoDB, then run safety-net cleanup."""
    from app.mongo_store import flush_buffer_to_mongo, cleanup_buffer

    flush_result = flush_buffer_to_mongo()
    cleanup_result = cleanup_buffer()
    print(
        f"[scheduler] flush_to_mongo: flushed={flush_result.get('flushed')} "
        f"errors={flush_result.get('errors')} "
        f"cleaned_finished={cleanup_result.get('deleted_finished')} "
        f"cleaned_stale={cleanup_result.get('deleted_stale_unenriched')}"
    )
    return {"flush": flush_result, "cleanup": cleanup_result}

def job_grade_predictions() -> dict[str, Any]:
    """Auto-grade yesterday's predictions against finished SofaScore results."""
    from datetime import date, timedelta

    from app.elo import record_match_result_once
    from app.league_memory import get_grading_metrics, grade_predictions_for_date
    from app.sofascore_client import fetch_all_scheduled_events

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        events = fetch_all_scheduled_events(yesterday)
        result = grade_predictions_for_date(yesterday, events)
        finished = [event for event in events if (event.get("status") or {}).get("type") == "finished"]
        elo_updated = 0
        for event in finished:
            elo_result = record_match_result_once("sofascore", event)
            if elo_result.get("updated"):
                elo_updated += 1
        metrics = get_grading_metrics()
        print(
            f"[scheduler] grade_predictions: graded={result.get('graded')} "
            f"elo_updated={elo_updated} win_rate={metrics.get('win_percent')}%"
        )
        return {**result, "metrics": metrics, "elo_updated": elo_updated}
    except Exception as exc:
        print(f"[scheduler] grade_predictions failed: {exc}")
        return {"status": "error", "error": str(exc)}


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

    # ingest live — every 30 sec
    scheduler.add_job(
        _safe(job_ingest_live),
        IntervalTrigger(seconds=30),
        id="ingest_live",
        name="Ingest live matches + patch scores",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=30,
    )

    # ingest upcoming — every 2 min
    scheduler.add_job(
        _safe(job_ingest_upcoming),
        IntervalTrigger(minutes=2),
        id="ingest_upcoming",
        name="Ingest upcoming matches from SportyBet",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )

    # enrichment worker — every 90 sec, processes a batch
    scheduler.add_job(
        _safe(job_enrich_worker),
        IntervalTrigger(seconds=90),
        id="enrich_worker",
        name="Enrichment + prediction worker",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=120,
    )

    # flush to mongo — every 2 min
    scheduler.add_job(
        _safe(job_flush_to_mongo),
        IntervalTrigger(minutes=2),
        id="flush_to_mongo",
        name="Flush buffer to MongoDB + cleanup",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=120,
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

    scheduler.add_job(
        _safe(job_grade_predictions),
        IntervalTrigger(hours=6),
        id="grade_predictions",
        name="Auto-grade predictions + ELO update",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=900,
    )

    scheduler.start()
    _scheduler = scheduler
    print("[scheduler] started — running forever, no frontend required")
    print("[scheduler]   ingest_live      every 30 sec  (scores + periods)")
    print("[scheduler]   ingest_upcoming  every  2 min  (new matches)")
    print("[scheduler]   enrich_worker    every 30 sec  (SofaScore + web + predict)")
    print("[scheduler]   flush_to_mongo   every  2 min  (buffer → MongoDB)")
    print("[scheduler]   archive_finished every 15 min  (finished → MongoDB)")
    print("[scheduler]   grade_predictions every  6 hrs  (analytics + ELO)")
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
