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

import atexit
import logging
import sys
import threading
from datetime import date as dt, datetime, timedelta, timezone
from typing import Any

from app.sportybet_client import fetch_live_matches_post, fetch_upcoming_matches_post
from app.buffer import (
    ingest_matches,
    patch_live_scores,
    run_enrichment_worker,
    get_buffer_stats,
    purge_ghost_matches,
)
from app.activity_log import record_activity


_scheduler = None
_shutting_down = False
_live_priority_lock = threading.Lock()
LIVE_PRIORITY_ENGINE_ID = "live_priority_mode"
_JOB_STALE_SECONDS = {
    "ingest_live": 180,
    "ingest_upcoming": 300,
    "enrich_worker": 600,
    "live_priority_toggle": 600,
    "flush_to_mongo": 300,
    "archive_finished": 900,
    "grade_overdue_predictions": 1200,
    "grade_predictions": 3600,
}


class _ApschedulerShutdownNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "Error submitting job" not in message:
            return True
        if _shutting_down or sys.is_finalizing():
            return False
        exc = record.exc_info[1] if record.exc_info else None
        if exc and _is_shutdown_runtime_error(exc):
            return False
        return True


logging.getLogger("apscheduler.scheduler").addFilter(_ApschedulerShutdownNoiseFilter())


# ── Job functions ─────────────────────────────────────────────────────────────

def job_ingest_upcoming(limit: int = 500) -> dict[str, Any]:
    """Fast: fetch upcoming matches from SportyBet and dump into buffer."""
    from app.market import snapshot_odds
    from app.buffer import purge_ghost_matches

    if is_shutting_down():
        return {"status": "shutdown", "job": "ingest_upcoming"}
    record_activity("Fetching upcoming SportyBet matches", job="ingest_upcoming", status="running")
    matches = fetch_upcoming_matches_post()[:limit]
    groups = _group_matches_by_local_date(matches)
    ingested = 0
    for match_date, dated_matches in groups.items():
        ingested += ingest_matches(dated_matches, match_date)
    snapped = 0
    for m in matches:
        match_date = _match_local_date(m)
        if snapshot_odds({
            "sportybet_id":      m.get("id"),
            "sportybet_name":    m.get("name"),
            "match_date":        match_date,
            "sportybet_markets": m.get("markets", []),
            "time_context":      _match_time(m),
        }):
            snapped += 1
    purged = purge_ghost_matches()
    dates = sorted(groups)
    print(f"[scheduler] ingest_upcoming: {ingested} matches buffered across {len(dates)} date(s) | {snapped} odds snapped | {purged} ghosts purged")
    record_activity(
        f"Upcoming ingest finished: {ingested} new, {snapped} odds snapshots",
        job="ingest_upcoming",
        status="ok",
        details={"dates": dates, "ingested": ingested, "odds_snapshots": snapped, "purged": purged},
    )
    return {"status": "ok", "job": "ingest_upcoming", "dates": dates, "ingested": ingested, "odds_snapshots": snapped, "purged": purged}


def job_ingest_live(limit: int = 200) -> dict[str, Any]:
    """Fast: fetch live matches, add new ones to buffer, patch scores on existing ones."""
    from app.league_memory import observe_matches
    from app.market import snapshot_odds

    if is_shutting_down():
        return {"status": "shutdown", "job": "ingest_live"}
    record_activity("Fetching live SportyBet matches", job="ingest_live", status="running")
    matches = fetch_live_matches_post()[:limit]

    # add brand-new live matches not yet in buffer
    groups = _group_matches_by_local_date(matches)
    new_count = 0
    for match_date, dated_matches in groups.items():
        new_count += ingest_matches(dated_matches, match_date)

    # patch scores/periods + archive finished ones
    patched = patch_live_scores(matches)

    # snapshot odds for movement tracking
    snapped = 0
    for m in matches:
        match_date = _match_local_date(m)
        if snapshot_odds({
            "sportybet_id":      m.get("id"),
            "sportybet_name":    m.get("name"),
            "match_date":        match_date,
            "sportybet_markets": m.get("markets", []),
            "time_context":      _match_time(m),
        }):
            snapped += 1

    observe_matches("sportybet", matches)

    print(f"[scheduler] ingest_live: {len(matches)} from api | {new_count} new | {patched} patched | {snapped} odds snapped")
    record_activity(
        f"Live ingest finished: {len(matches)} from API, {patched} patched",
        job="ingest_live",
        status="ok",
        details={"live_count": len(matches), "new": new_count, "patched": patched, "odds_snapshots": snapped},
    )
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
    Runs every 30 sec — processes today + live matches first, then tomorrow, then future.
    """
    if is_shutting_down():
        return {"status": "shutdown", "job": "enrich_worker"}
    record_activity("Looking for the next match to enrich", job="enrich_worker", status="running")
    stats = get_buffer_stats()
    if int(stats.get("live") or 0):
        result = run_enrichment_worker(batch_size=8, live_only=True, fetch_web_context=False)
        result["priority"] = "live"
        if result.get("status") == "idle" and int(stats.get("hot_pending_enrichment") or 0):
            result = run_enrichment_worker(batch_size=2)
            result["priority"] = "normal_after_live_idle"
    else:
        result = run_enrichment_worker(batch_size=2)
        result["priority"] = "normal"
    if result.get("status") == "idle":
        print("[scheduler] enrich_worker: nothing to enrich")
        record_activity("Enrichment worker idle: no pending match", job="enrich_worker", status="idle")
    else:
        print(
            f"[scheduler] enrich_worker: "
            f"processed={result.get('batch')} "
            f"matched={result.get('matched')} "
            f"stored={result.get('stored')} "
            f"predicted={result.get('predicted')} "
            f"llm={result.get('llm_fallback')}"
        )
        record_activity(
            f"Enrichment worker finished: {result.get('matched')} matched, {result.get('predicted')} predicted",
            job="enrich_worker",
            status="ok",
            details={k: result.get(k) for k in ("batch", "matched", "unmatched", "stored", "predicted", "llm_fallback")},
        )
    return result


def job_live_priority(count: int = 30, limit: int = 500) -> dict[str, Any]:
    """Manual/urgent live lane: refresh Sporty live, then enrich and predict live rows first."""
    if is_shutting_down():
        return {"status": "shutdown", "job": "live_priority"}
    if not _live_priority_lock.acquire(blocking=False):
        return {"status": "busy", "job": "live_priority", "reason": "previous live priority run still active"}
    try:
        record_activity("Live priority run started", job="live_priority", status="running")
        ingest = run_job_with_guard(job_ingest_live, limit=limit)
        if is_shutting_down():
            return {"status": "shutdown", "job": "live_priority", "ingest": ingest}
        enrich = run_job_with_guard(
            _job_live_priority_enrich,
            guard_job_id="enrich_worker",
            count=count,
        )
        stats = get_buffer_stats()
        record_activity(
            f"Live priority finished: {enrich.get('stored')} enriched, {enrich.get('predicted')} predicted",
            job="live_priority",
            status="ok",
            details={"ingest": ingest, "enrich": enrich, "live": stats.get("live")},
        )
        return {"status": "ok", "job": "live_priority", "ingest": ingest, "enrich": enrich, "buffer": stats}
    finally:
        _live_priority_lock.release()


def get_live_priority_mode() -> dict[str, Any]:
    """Persistent setting used by the Settings toggle and scheduler."""
    try:
        from app.league_memory import get_engine_states
        enabled = get_engine_states().get(LIVE_PRIORITY_ENGINE_ID) == "active"
    except Exception:
        enabled = False
    return {
        "enabled": enabled,
        "engine_id": LIVE_PRIORITY_ENGINE_ID,
        "mode": "continuous" if enabled else "normal",
        "interval_seconds": 60,
    }


def set_live_priority_mode(enabled: bool) -> dict[str, Any]:
    from app.league_memory import set_engine_status

    status = "active" if enabled else "paused"
    set_engine_status(LIVE_PRIORITY_ENGINE_ID, status)
    record_activity(
        f"Live priority {'enabled' if enabled else 'disabled'}",
        job="live_priority",
        status="ok",
        details={"enabled": enabled},
    )
    return get_live_priority_mode()


def job_live_priority_toggle() -> dict[str, Any]:
    """Continuous live lane. Does work only when the Settings toggle is enabled."""
    if is_shutting_down():
        return {"status": "shutdown", "job": "live_priority_toggle"}
    mode = get_live_priority_mode()
    if not mode.get("enabled"):
        return {"status": "idle", "job": "live_priority_toggle", "enabled": False}
    result = job_live_priority(count=8, limit=200)
    result["enabled"] = True
    result["continuous"] = True
    return result


def _job_live_priority_enrich(count: int = 30) -> dict[str, Any]:
    return run_enrichment_worker(batch_size=count, live_only=True, force_live_retry=True, fetch_web_context=False)


def job_enrich_future() -> dict[str, Any]:
    """
    Future-match enrichment: once per hour, enrich upcoming fixtures beyond
    tomorrow so SofaScore H2H, standings, and team form are pre-populated
    before kick-off. Uses a larger batch since these are low-urgency.
    """
    if is_shutting_down():
        return {"status": "shutdown", "job": "enrich_future"}
    from app.league_memory import DB_PATH, _init_db
    from app.buffer import _init_buffer_table
    import sqlite3 as _sqlite3

    _init_db()
    with _sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_buffer_table(conn)
        pending = conn.execute(
            """
            select count(*) from future_match_buffer
            where is_finished = 0
              and (enriched_at is null or sofascore_id is null or sofascore_id = '')
              and (
                json_extract(raw_enriched, '$.sofascore_match_status') is null
                or json_extract(raw_enriched, '$.sofascore_match_status') != 'no_match'
                or coalesce(cast(json_extract(raw_enriched, '$.sofascore_retry_after_ts') as real), 0) <= strftime('%s','now')
              )
            """,
            (),
        ).fetchone()[0]

    if not pending:
        return {"status": "idle", "future_pending": 0}

    record_activity("Future enrichment checking next pending fixture", job="enrich_future", status="running")
    result = run_enrichment_worker(batch_size=3, future_only=True)
    print(
        f"[scheduler] enrich_future: future_pending={pending} "
        f"processed={result.get('batch')} matched={result.get('matched')} "
        f"stored={result.get('stored')}"
    )
    record_activity(
        f"Future enrichment finished: {result.get('matched')} matched",
        job="enrich_future",
        status="ok",
        details={"future_pending": pending, **{k: result.get(k) for k in ("batch", "matched", "stored", "predicted")}},
    )
    return {**result, "future_pending": pending}


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
    ghost_deleted = purge_ghost_matches()
    print(
        f"[scheduler] flush_to_mongo: flushed={flush_result.get('flushed')} "
        f"errors={flush_result.get('errors')} "
        f"cleaned_finished={cleanup_result.get('deleted_finished')} "
        f"cleaned_stale={cleanup_result.get('deleted_stale_unenriched')} "
        f"ghost_purged={ghost_deleted}"
    )
    return {"flush": flush_result, "cleanup": cleanup_result, "ghost_purged": ghost_deleted}

def job_grade_predictions() -> dict[str, Any]:
    """Auto-grade recent predictions and refresh learning tables."""
    from datetime import date, timedelta

    from app.elo import record_match_result_once
    from app.league_memory import get_grading_metrics, grade_betbuilder_history, grade_overdue_predictions, grade_predictions_for_date
    from app.sofascore_client import fetch_all_scheduled_events

    target_dates = [(date.today() - timedelta(days=days)).isoformat() for days in range(0, 4)]
    try:
        by_date = []
        total_graded = 0
        total_skipped = 0
        elo_updated = 0
        for target_date in target_dates:
            events = fetch_all_scheduled_events(target_date)
            result = grade_predictions_for_date(target_date, events)
            finished = [event for event in events if (event.get("status") or {}).get("type") == "finished"]
            for event in finished:
                elo_result = record_match_result_once("sofascore", event)
                if elo_result.get("updated"):
                    elo_updated += 1
            total_graded += int(result.get("graded") or 0)
            total_skipped += int(result.get("skipped") or 0)
            by_date.append({"date": target_date, **result, "finished": len(finished)})
        overdue_result = grade_overdue_predictions(hours_after_kickoff=2, limit=500)
        total_graded += int(overdue_result.get("graded") or 0)
        total_skipped += int(overdue_result.get("skipped") or 0)
        betbuilder_result = grade_betbuilder_history(limit=500)
        metrics = get_grading_metrics()

        # Rebuild confidence calibration from updated win/loss history
        try:
            from app.confidence_calibrator import rebuild_calibration
            cal_result = rebuild_calibration()
        except Exception as exc:
            cal_result = {"error": str(exc)}

        # Run self-learning cycle and optimise ensemble weights.
        try:
            from app.weight_optimiser import optimise_ensemble_weights
            learn_result = optimise_ensemble_weights()
        except Exception as exc:
            learn_result = {"error": str(exc)}

        # Grade odds patterns for yesterday
        try:
            from app.odds_pattern import grade_patterns_for_date
            pattern_result = {"by_date": [{"date": d, **grade_patterns_for_date(d)} for d in target_dates]}
        except Exception as exc:
            pattern_result = {"error": str(exc)}

        # Compute CLV for yesterday — fill closing odds + attach results
        try:
            from app.clv import compute_clv_for_date
            clv_result = {"by_date": [{"date": d, **compute_clv_for_date(d)} for d in target_dates]}
        except Exception as exc:
            clv_result = {"error": str(exc)}

        try:
            from app.mongo_store import cleanup_buffer
            cleanup_result = cleanup_buffer()
        except Exception as exc:
            cleanup_result = {"error": str(exc)}

        print(
            f"[scheduler] grade_predictions: graded={total_graded} skipped={total_skipped} "
            f"elo_updated={elo_updated} win_rate={metrics.get('win_percent')}% "
            f"calibration_bands={cal_result.get('bands_updated', 0)} "
            f"signals_learned={(learn_result.get('learning') or {}).get('signal_updates', 0)} "
            f"model_weights={(learn_result.get('learning') or {}).get('model_weight_updates', 0)} "
            f"overdue={overdue_result.get('graded', 0)} "
            f"cleanup_finished={cleanup_result.get('deleted_finished')}"
        )
        return {"status": "success", "dates": by_date, "graded": total_graded, "skipped": total_skipped,
                "metrics": metrics, "elo_updated": elo_updated,
                "calibration": cal_result, "patterns": pattern_result,
                "clv": clv_result, "learning": learn_result, "cleanup": cleanup_result,
                "overdue": overdue_result, "betbuilder": betbuilder_result}
    except Exception as exc:
        print(f"[scheduler] grade_predictions failed: {exc}")
        return {"status": "error", "error": str(exc)}


def job_grade_overdue_predictions() -> dict[str, Any]:
    """Frequent safety-net grader using SportyBet results first, SofaScore fallback second."""
    try:
        from app.league_memory import grade_betbuilder_history, grade_overdue_predictions

        record_activity("Checking overdue match results", job="grade_overdue", status="running")
        result = grade_overdue_predictions(hours_after_kickoff=2, limit=500)
        graded = int(result.get("graded") or 0) + int(result.get("candidate_graded") or 0)
        if graded:
            try:
                from app.confidence_calibrator import rebuild_calibration
                from app.weight_optimiser import optimise_ensemble_weights

                result["calibration"] = rebuild_calibration()
                result["learning"] = optimise_ensemble_weights()
            except Exception as exc:
                result["learning_error"] = str(exc)
        result["betbuilder"] = grade_betbuilder_history(limit=300)
        record_activity(
            f"Overdue grading finished: {result.get('graded', 0)} primary, {result.get('candidate_graded', 0)} candidates",
            job="grade_overdue",
            status="ok",
            details=result,
        )
        print(
            f"[scheduler] grade_overdue: checked={result.get('checked')} "
            f"graded={result.get('graded')} candidates={result.get('candidate_graded')} "
            f"live={result.get('still_live')} not_found={result.get('not_found')}"
        )
        return result
    except Exception as exc:
        record_activity(f"Overdue grading failed: {exc}", job="grade_overdue", status="error")
        print(f"[scheduler] grade_overdue failed: {exc}")
        return {"status": "error", "error": str(exc)}


def job_prune_mongo(keep_days: int = 90) -> dict[str, Any]:
    """Prune MongoDB finished_matches older than keep_days to control storage."""
    try:
        from app.mongo_store import prune_old_finished_matches, is_configured
        if not is_configured():
            return {"status": "skipped", "reason": "mongodb not configured"}
        deleted = prune_old_finished_matches(keep_days=keep_days)
        print(f"[scheduler] prune_mongo: deleted {deleted} finished matches older than {keep_days} days")
        return {"status": "ok", "deleted": deleted, "keep_days": keep_days}
    except Exception as exc:
        print(f"[scheduler] prune_mongo failed: {exc}")
        return {"status": "error", "error": str(exc)}


def job_keep_alive() -> dict[str, Any]:
    """
    Ping our own /health endpoint every 10 minutes to prevent Render free tier
    from spinning down the server. Without this, the dyno sleeps after 15 min
    of inactivity and the next request gets a cold-start 405/timeout.
    """
    import os
    from urllib import request as urllib_request, error as urllib_error

    # Self-URL: set RENDER_EXTERNAL_URL in Render env vars (auto-set by Render)
    # or fall back to the known endpoint.
    base = (
        os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("PREDICTX_SELF_URL")
        or "https://endpoints-dtfx.onrender.com"
    ).rstrip("/")

    url = f"{base}/health"
    try:
        req = urllib_request.Request(url, method="GET")
        with urllib_request.urlopen(req, timeout=10) as resp:
            status = resp.status
        return {"status": "ok", "pinged": url, "response": status}
    except urllib_error.URLError as exc:
        return {"status": "error", "pinged": url, "error": str(exc)}


# ── Scheduler setup ───────────────────────────────────────────────────────────

def start_scheduler():
    global _scheduler, _shutting_down
    if _scheduler:
        return _scheduler
    _shutting_down = False

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except Exception as exc:
        print(f"[scheduler] disabled - apscheduler not installed: {exc}")
        return None

    scheduler = BackgroundScheduler(timezone="UTC")
    now = datetime.now(timezone.utc)

    # ingest live — every 30 sec
    scheduler.add_job(
        _safe(job_ingest_live),
        IntervalTrigger(seconds=30),
        id="ingest_live",
        name="Ingest live matches + patch scores",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
        next_run_time=now + timedelta(seconds=5),
    )

    # ingest upcoming — every 2 min
    scheduler.add_job(
        _safe(job_ingest_upcoming),
        IntervalTrigger(minutes=2),
        id="ingest_upcoming",
        name="Ingest upcoming matches from SportyBet",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        next_run_time=now + timedelta(seconds=10),
    )

    # enrichment worker — every 30 sec, processes today/live first
    scheduler.add_job(
        _safe(job_enrich_worker),
        IntervalTrigger(seconds=30),
        id="enrich_worker",
        name="Enrichment + prediction worker (live + today)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=now + timedelta(seconds=45),
    )

    # future enrichment — every 1 hour, enriches fixtures beyond tomorrow
    scheduler.add_job(
        _safe(job_live_priority_toggle),
        IntervalTrigger(seconds=60),
        id="live_priority_toggle",
        name="Continuous live priority lane",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )

    scheduler.add_job(
        _safe(job_enrich_future),
        IntervalTrigger(hours=1),
        id="enrich_future",
        name="Future match enrichment (SofaScore pre-match data)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # flush to mongo — every 2 min
    scheduler.add_job(
        _safe(job_flush_to_mongo),
        IntervalTrigger(minutes=2),
        id="flush_to_mongo",
        name="Flush buffer to MongoDB + cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
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
        coalesce=True,
        misfire_grace_time=300,
    )

    scheduler.add_job(
        _safe(job_grade_overdue_predictions),
        IntervalTrigger(minutes=30),
        id="grade_overdue_predictions",
        name="Grade matches 2h after kickoff",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    scheduler.add_job(
        _safe(job_grade_predictions),
        IntervalTrigger(hours=6),
        id="grade_predictions",
        name="Auto-grade predictions + ELO update",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )

    # prune old MongoDB finished matches — weekly
    scheduler.add_job(
        _safe(job_prune_mongo),
        IntervalTrigger(days=7),
        id="prune_mongo",
        name="Prune old finished matches from MongoDB",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # keep-alive ping — every 10 min to prevent Render free tier sleep
    scheduler.add_job(
        _safe(job_keep_alive),
        IntervalTrigger(minutes=10),
        id="keep_alive",
        name="Self-ping to prevent Render cold starts",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    scheduler.start()
    _scheduler = scheduler
    print("[scheduler] started - running forever, no frontend required")
    print("[scheduler]   ingest_live      every 30 sec  (scores + periods)")
    print("[scheduler]   ingest_upcoming  every  2 min  (new matches)")
    print("[scheduler]   enrich_worker    every 30 sec  (live + today - SofaScore + predict)")
    print("[scheduler]   enrich_future    every  1 hr   (future fixtures - pre-match data)")
    print("[scheduler]   flush_to_mongo   every  2 min  (buffer -> MongoDB)")
    print("[scheduler]   archive_finished every 15 min  (finished -> MongoDB)")
    print("[scheduler]   grade_overdue    every 30 min  (Sporty results + Sofa fallback)")
    print("[scheduler]   grade_predictions every  6 hrs  (analytics + ELO)")
    print("[scheduler]   prune_mongo      every  7 days (remove matches >90 days old)")
    print("[scheduler]   keep_alive       every 10 min  (prevent Render cold start)")
    return scheduler


def stop_scheduler(wait: bool = False) -> bool:
    global _scheduler, _shutting_down
    _shutting_down = True
    scheduler = _scheduler
    if not scheduler:
        return False
    try:
        if getattr(scheduler, "running", False):
            try:
                scheduler.remove_all_jobs()
            except Exception:
                pass
            scheduler.shutdown(wait=wait)
    except Exception as exc:
        message = str(exc).lower()
        if "not running" not in message and "shutdown" not in message:
            print(f"[scheduler] shutdown warning: {exc}")
    _scheduler = None
    return True


def scheduler_status() -> dict[str, Any]:
    try:
        from app.job_state import list_job_states
        job_states = list_job_states()
    except Exception:
        job_states = []
    if not _scheduler:
        return {"running": False, "jobs": [], "job_states": job_states}
    if _shutting_down:
        return {"running": False, "shutting_down": True, "jobs": [], "job_states": job_states}
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
        "job_states": job_states,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_shutting_down() -> bool:
    return _shutting_down or sys.is_finalizing()


def _safe(fn):
    """Wrap a job function so exceptions are logged but don't crash the scheduler."""
    def wrapper(*args, **kwargs):
        if is_shutting_down():
            return {"status": "shutdown", "job": fn.__name__}
        try:
            return run_job_with_guard(fn, *args, **kwargs)
        except RuntimeError as exc:
            if _is_shutdown_runtime_error(exc):
                return {"status": "shutdown", "job": fn.__name__}
            print(f"[scheduler] {fn.__name__} failed: {exc}")
            record_activity(f"{fn.__name__} failed: {exc}", job=fn.__name__, status="error")
        except Exception as exc:
            print(f"[scheduler] {fn.__name__} failed: {exc}")
            record_activity(f"{fn.__name__} failed: {exc}", job=fn.__name__, status="error")
    return wrapper


def run_job_with_guard(fn, *args, guard_job_id: str | None = None, **kwargs):
    """Run a scheduler job through the persistent cross-process job ledger."""
    from app.job_state import JobBusy, finish_job, job_guard

    job_id = guard_job_id or (fn.__name__[4:] if fn.__name__.startswith("job_") else fn.__name__)
    stale_after = _JOB_STALE_SECONDS.get(job_id, 900)
    try:
        with job_guard(job_id, stale_after_seconds=stale_after) as state:
            result = fn(*args, **kwargs)
            status = "ok" if not isinstance(result, dict) else str(result.get("status") or "ok")
            if status == "busy":
                final_status = "busy"
            elif status in {"error", "failed"}:
                final_status = "error"
            else:
                final_status = "ok"
            finish_job(job_id, status=final_status, owner=state["owner"], result=result if isinstance(result, dict) else {"result": result})
            return result
    except JobBusy as exc:
        record_activity(
            f"{job_id} skipped: previous run still active",
            job=job_id,
            status="busy",
            details={"owner": exc.owner},
        )
        return {"status": "busy", "job": job_id, "reason": "previous run still active", "owner": exc.owner}


def _is_shutdown_runtime_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "interpreter shutdown" in message or "cannot schedule new futures" in message


def _atexit_stop_scheduler() -> None:
    global _scheduler, _shutting_down
    _shutting_down = True
    scheduler = _scheduler
    if not scheduler:
        return
    try:
        scheduler.remove_all_jobs()
    except Exception:
        pass
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass
    _scheduler = None


atexit.register(_atexit_stop_scheduler)


def _match_time(match: dict[str, Any]) -> dict[str, Any]:
    from app.time_context import match_time_context

    return match_time_context(match)


def _match_local_date(match: dict[str, Any]) -> str:
    context = _match_time(match)
    return context.get("local_date") or context.get("utc_date") or dt.today().isoformat()


def _group_matches_by_local_date(matches: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for match in matches:
        groups.setdefault(_match_local_date(match), []).append(match)
    return groups
