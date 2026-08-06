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
import os

_logger = logging.getLogger(__name__)
import sqlite3
import sys
import threading
from datetime import date as dt, datetime, timedelta, timezone
from typing import Any

from app.db import db_conn
from app.sportybet_client import fetch_live_matches_post, fetch_upcoming_matches_post
from app.buffer import (
    ingest_matches,
    patch_live_scores,
    run_enrichment_worker,
    get_buffer_stats,
    purge_ghost_matches,
)
from app.activity_log import record_activity
from app.ai_prediction_pipeline import job_ai_prediction_queue
from app.competition_analyser import job_competition_analysis
from app.season_stage import detect_season_stage


_scheduler = None
_shutting_down = False
_live_priority_lock = threading.Lock()
_watchdog_thread: threading.Thread | None = None
_watchdog_stop = threading.Event()
LIVE_PRIORITY_ENGINE_ID = "live_priority_mode"
# A full SofaScore detail fetch fans out into several provider calls. Keep each
# scheduled pass comfortably below its five-minute cadence, then advance the
# priority queue on the next pass.
UNIFIED_UPCOMING_BATCH_SIZE = 12
SCHEDULER_INTERVAL_DEFAULTS: dict[str, dict[str, Any]] = {
    "ai_prediction_queue": {"label": "AI prediction queue", "default": 300, "min": 60, "max": 600, "pipeline_id": "ai_prediction_queue"},
    "ingest_live": {"label": "Live ingest", "default": 180, "min": 15, "max": 600, "pipeline_id": "sportybet_ingest_live"},
    "ingest_upcoming": {"label": "Upcoming ingest", "default": 120, "min": 60, "max": 600, "pipeline_id": "sportybet_ingest_upcoming"},
    "enrich_worker": {"label": "Live enrichment", "default": 30, "min": 30, "max": 300, "pipeline_id": "sportybet_enrich_live"},
    "enrich_future": {"label": "Prematch enrichment", "default": 1800, "min": 60, "max": 1800, "pipeline_id": "sportybet_enrich_prematch"},
    "sofa_pipeline": {"label": "SofaScore pipeline", "default": 300, "min": 60, "max": 600, "pipeline_id": "sofa_pipeline"},
    "live_priority_toggle": {"label": "Live priority lane", "default": 60, "min": 30, "max": 300, "pipeline_id": "live_priority_mode"},
    "unified_upcoming": {"label": "Unified upcoming pipeline", "default": 300, "min": 60, "max": 600, "pipeline_id": "unified_upcoming"},
    "unified_live": {"label": "Unified live pipeline", "default": 60, "min": 30, "max": 300, "pipeline_id": "unified_live"},
    "regenerate_research_stats": {"label": "Research stats regeneration", "default": 86400, "min": 3600, "max": 86400 * 7, "pipeline_id": None},
}
_DB_WRITE_JOB_IDS = {
    "ai_prediction_queue",
    "ingest_live",
    "ingest_upcoming",
    "enrich_worker",
    "live_priority_toggle",
    "live_priority",
    "flush_to_mongo",
    "system_supervisor",
    "prediction_monitor",
    "autopilot_guardian",
    "archive_finished",
    "grade_overdue_predictions",
    "grade_predictions",
    "enrich_future",
    "competition_special",
    "competition_analysis",
    "sofa_pipeline",
    "unified_upcoming",
    "unified_live",
    "regenerate_research_stats",
}
_JOB_STALE_SECONDS = {
    "ai_prediction_queue": 600,
    "ingest_live": 90,
    "ingest_upcoming": 120,
    "enrich_worker": 120,
    "live_priority_toggle": 120,
    "live_priority": 120,
    "flush_to_mongo": 120,
    "system_supervisor": 240,
    "prediction_monitor": 1800,
    "archive_finished": 600,
    "grade_overdue_predictions": 900,
    "grade_predictions": 3600,
    "autopilot_guardian": 900,
    "competition_special": 1800,
    "competition_analysis": 90000,
    "sofa_pipeline": 600,
    "unified_upcoming": 480,
    "unified_live": 120,
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
    from app.market.market import snapshot_odds
    from app.buffer import purge_ghost_matches

    if is_shutting_down():
        return {"status": "shutdown", "job": "ingest_upcoming"}
    # ── Pipeline toggle check ──────────────────────────────────────────────────
    try:
        from app.pipeline_registry import is_pipeline_enabled
        if not is_pipeline_enabled("sportybet_ingest_upcoming"):
            return {"status": "skipped", "job": "ingest_upcoming", "reason": "pipeline_disabled"}
    except Exception:
        pass
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
    from app.market.market import snapshot_odds

    if is_shutting_down():
        return {"status": "shutdown", "job": "ingest_live"}
    # ── Pipeline toggle check ──────────────────────────────────────────────────
    try:
        from app.pipeline_registry import is_pipeline_enabled
        if not is_pipeline_enabled("sportybet_ingest_live"):
            return {"status": "skipped", "job": "ingest_live", "reason": "pipeline_disabled"}
    except Exception:
        pass
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

    # ── Pipeline toggle check ──────────────────────────────────────────────────
    # Determine which sub-modes are enabled from the registry
    try:
        from app.pipeline_registry import get_enrich_worker_mode
        enrich_mode = get_enrich_worker_mode()
    except Exception:
        enrich_mode = {"disabled": False, "live_only": False, "exclude_live": False, "both": True}

    if enrich_mode.get("disabled"):
        return {"status": "skipped", "job": "enrich_worker", "reason": "pipeline_disabled"}

    record_activity("Looking for the next match to enrich", job="enrich_worker", status="running")
    try:
        from app.live_retry_queue import active_pending_count, expire_stale_entries

        expired = expire_stale_entries()
        pending_retries = active_pending_count()
        if pending_retries:
            print(f"[scheduler] live_retry_queue: pending={pending_retries} expired={expired}")
            record_activity(
                f"Live retry queue pending: {pending_retries}",
                job="enrich_worker",
                status="waiting",
                details={"pending_live_retries": pending_retries, "expired_live_retries": expired},
            )
    except Exception:
        pass
    stats = get_buffer_stats()
    mode = get_live_priority_mode()
    live_priority_enabled = bool(mode.get("enabled"))

    if live_priority_enabled and int(stats.get("live") or 0):
        result = run_enrichment_worker(batch_size=8, live_only=True, fetch_web_context=False)
        result["priority"] = "live"
        if result.get("status") == "idle" and int(stats.get("hot_pending_enrichment") or 0):
            result = run_enrichment_worker(batch_size=2, exclude_live=True)
            result["priority"] = "upcoming_after_live_idle"
    elif enrich_mode.get("live_only"):
        # Live enrichment toggle on, prematch off
        result = run_enrichment_worker(batch_size=4, live_only=True, fetch_web_context=False)
        result["priority"] = "live_only"
    elif enrich_mode.get("exclude_live"):
        # Prematch only
        result = run_enrichment_worker(batch_size=4, exclude_live=True)
        result["priority"] = "upcoming"
    else:
        # Both on (normal mode) — live first, then fall through to prematch
        live_result = run_enrichment_worker(batch_size=4, live_only=True, fetch_web_context=False)
        if live_result.get("status") == "idle":
            result = run_enrichment_worker(batch_size=4, exclude_live=True)
            result["priority"] = "upcoming"
        else:
            result = live_result
            result["priority"] = "live_normal_mode"
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
        "interval_seconds": _scheduler_interval("live_priority_toggle"),
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


def _ensure_scheduler_interval_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists scheduler_intervals (
            job_id text primary key,
            interval_seconds integer not null,
            updated_at text not null default current_timestamp
        )
        """
    )


def _scheduler_interval(job_id: str) -> int:
    meta = SCHEDULER_INTERVAL_DEFAULTS.get(job_id)
    if not meta:
        return 60
    try:
        from app.db import DB_PATH
        from app.league_memory import _init_db
        _init_db()
        with db_conn(timeout=10) as conn:
            _ensure_scheduler_interval_table(conn)
            row = conn.execute("select interval_seconds from scheduler_intervals where job_id = ?", (job_id,)).fetchone()
            if row:
                value = int(row[0])
                return max(int(meta["min"]), min(int(meta["max"]), value))
    except Exception:
        pass
    return int(meta["default"])


def scheduler_intervals(active_only: bool = True) -> dict[str, Any]:
    from app.pipeline_registry import is_pipeline_enabled

    status = scheduler_status()
    jobs_by_id = {job.get("id"): job for job in status.get("jobs") or []}
    states_by_id = {job.get("job_id"): job for job in status.get("job_states") or []}
    items: list[dict[str, Any]] = []
    for job_id, meta in SCHEDULER_INTERVAL_DEFAULTS.items():
        pipeline_id = meta.get("pipeline_id")
        enabled = True
        try:
            enabled = is_pipeline_enabled(str(pipeline_id)) if pipeline_id else True
        except Exception:
            enabled = True
        if active_only and not enabled:
            continue
        job = jobs_by_id.get(job_id) or {}
        state = states_by_id.get(job_id) or {}
        items.append({
            "job_id": job_id,
            "engine_id": pipeline_id,
            "label": meta["label"],
            "enabled": enabled,
            "interval_seconds": _scheduler_interval(job_id),
            "default_seconds": meta["default"],
            "min_seconds": meta["min"],
            "max_seconds": meta["max"],
            "last_run_at": state.get("finished_at") or state.get("heartbeat_at"),
            "next_run_at": job.get("next_run_time"),
        })
    return {"status": "success", "jobs": items, "running": status.get("running", False)}


def patch_scheduler_intervals(intervals: dict[str, int]) -> dict[str, Any]:
    from apscheduler.triggers.interval import IntervalTrigger
    from app.db import DB_PATH
    from app.league_memory import _init_db

    _init_db()
    applied: list[dict[str, Any]] = []
    with db_conn(timeout=30) as conn:
        _ensure_scheduler_interval_table(conn)
        for job_id, raw_value in intervals.items():
            if job_id not in SCHEDULER_INTERVAL_DEFAULTS:
                continue
            meta = SCHEDULER_INTERVAL_DEFAULTS[job_id]
            seconds = max(int(meta["min"]), min(int(meta["max"]), int(raw_value)))
            conn.execute(
                """
                insert into scheduler_intervals (job_id, interval_seconds, updated_at)
                values (?, ?, current_timestamp)
                on conflict(job_id) do update set
                    interval_seconds = excluded.interval_seconds,
                    updated_at = current_timestamp
                """,
                (job_id, seconds),
            )
            if _scheduler and _scheduler.get_job(job_id):
                _scheduler.reschedule_job(job_id, trigger=IntervalTrigger(seconds=seconds))
            applied.append({"job_id": job_id, "interval_seconds": seconds})
        conn.commit()
    return {"status": "success", "applied": applied, **scheduler_intervals(active_only=True)}


def reset_scheduler_intervals() -> dict[str, Any]:
    defaults = {job_id: int(meta["default"]) for job_id, meta in SCHEDULER_INTERVAL_DEFAULTS.items()}
    return patch_scheduler_intervals(defaults)


def job_sofa_pipeline() -> dict[str, Any]:
    """
    SofaScore-only pipeline: Ingest → Enrich → Predict using SofaScore as the
    sole data source.  Runs every 5 minutes when the toggle is enabled.
    Safe on Render/cloud where SportyBet blocks datacenter IPs.
    """
    if is_shutting_down():
        return {"status": "shutdown", "job": "sofa_pipeline"}
    try:
        from app.sofa_pipeline import get_sofa_pipeline_mode, run_sofa_pipeline_cycle
        mode = get_sofa_pipeline_mode()
        if not mode.get("enabled"):
            return {"status": "idle", "job": "sofa_pipeline", "enabled": False}
        result = run_sofa_pipeline_cycle(enrich_batch=12, include_live=True)
        result["job"] = "sofa_pipeline"
        record_activity(
            f"SofaScore pipeline: enriched={result.get('enrich', {}).get('enriched', 0)} "
            f"predicted={result.get('enrich', {}).get('predicted', 0)}",
            job="sofa_pipeline",
            status="ok",
            details=result,
        )
        return result
    except Exception as exc:
        record_activity(f"SofaScore pipeline failed: {exc}", job="sofa_pipeline", status="error")
        return {"status": "error", "job": "sofa_pipeline", "error": str(exc)}


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
    from app.db import DB_PATH
    from app.league_memory import _init_db
    from app.buffer import _init_buffer_table
    import sqlite3 as _sqlite3

    _init_db()
    with db_conn(timeout=30) as conn:
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


def job_unified_upcoming() -> dict[str, Any]:
    """
    Unified Upcoming Pipeline:
    1. Fetch upcoming matches from SportyBet → ingest all into buffer
    2. Fetch SofaScore scheduled events for today + tomorrow
    3. Match ALL unmatched buffer matches against SofaScore in one pass
    4. Run predictions on matched ones
    """
    if is_shutting_down():
        return {"status": "shutdown", "job": "unified_upcoming"}
    try:
        from app.pipeline_registry import is_pipeline_enabled
        if not is_pipeline_enabled("unified_upcoming"):
            return {"status": "skipped", "job": "unified_upcoming", "reason": "pipeline_disabled"}
    except Exception:
        pass

    from app.sportybet_client import fetch_upcoming_matches_post
    from app.sofascore_client import fetch_all_scheduled_events, is_usable_event_for_mode, search_events
    from app.buffer import ingest_matches, get_unenriched_batch, store_enriched, get_buffered_match
    from app.storage.buffer import _extract_1x2
    from app.enrichment import _fuzzy_match, _llm_match, _is_junk, FUZZY_THRESHOLD, LLM_FALLBACK_THRESHOLD
    from app.sofascore_client import fetch_event_detail
    from app.market.market import snapshot_odds
    from app.time_context import match_time_context
    from app.match_state import classify_match_state
    from app.prediction_flow import apply_prediction_state
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import date as date_cls, timedelta, datetime, timezone

    record_activity("Unified upcoming pipeline starting", job="unified_upcoming", status="running")

    # ── Step 1: Ingest all SportyBet upcoming ─────────────────────────────────
    try:
        matches = fetch_upcoming_matches_post()
    except Exception as exc:
        record_activity(f"Unified upcoming: SportyBet fetch failed: {exc}", job="unified_upcoming", status="error")
        return {"status": "error", "job": "unified_upcoming", "error": str(exc)}

    groups = _group_matches_by_local_date(matches)
    ingested_new = 0
    for match_date, dated_matches in groups.items():
        ingested_new += ingest_matches(dated_matches, match_date)
    for m in matches:
        try:
            snapshot_odds({"sportybet_id": m.get("id"), "sportybet_name": m.get("name"),
                           "match_date": _match_local_date(m), "sportybet_markets": m.get("markets", []),
                           "time_context": _match_time(m)})
        except Exception:
            pass

    # ── Step 2: Fetch SofaScore events for today + tomorrow ───────────────────
    today = date_cls.today().isoformat()
    tomorrow = (date_cls.today() + timedelta(days=1)).isoformat()
    sofa_cache: dict[str, list] = {}
    for d in [today, tomorrow]:
        try:
            sofa_cache[d] = fetch_all_scheduled_events(d)
        except Exception as exc:
            _logger.warning("unified_upcoming: sofa fetch failed for %s: %s", d, exc)
            sofa_cache[d] = []
    all_sofa_events = [e for events in sofa_cache.values() for e in events]

    # ── Step 3: Match ALL unmatched buffer matches against SofaScore ──────────
    # Work through a small, time-bounded queue.  One enriched fixture fans out
    # into multiple SofaScore detail calls; processing the whole backlog here
    # leaves the scheduler job running indefinitely and blocks later cycles.
    eligible_dates = {today, tomorrow}
    pending = [
        item for item in get_unenriched_batch(
            limit=UNIFIED_UPCOMING_BATCH_SIZE,
            exclude_live=True,
        )
        if item.get("match_date") in eligible_dates
    ]
    record_activity(
        f"Unified upcoming matching {len(pending)} next fixtures",
        job="unified_upcoming",
        status="running",
        details={"batch_size": UNIFIED_UPCOMING_BATCH_SIZE, "sofa_candidates": len(all_sofa_events)},
    )

    now = datetime.now(timezone.utc).isoformat()
    matched_count = unmatched_count = predicted_count = deferred_count = stored_count = search_matched_count = 0

    def _fetch_detail_safe(sofa_event):
        try:
            return fetch_event_detail(sofa_event)
        except Exception:
            return None

    def _search_match_safe(sporty_event: dict) -> tuple[dict | None, float]:
        """Use SofaScore's team search only when the daily feed misses a real fixture."""
        if _is_junk(sporty_event.get("name") or ""):
            return None, 0.0
        query = " ".join(filter(None, [
            str(sporty_event.get("home_team") or ""),
            str(sporty_event.get("away_team") or ""),
        ])).strip()
        if not query:
            return None, 0.0
        try:
            candidates = [
                event for event in search_events(query, limit=12)
                if is_usable_event_for_mode(event, live=False)
            ]
            sofa_event, candidate_score = _fuzzy_match(sporty_event, candidates)
            # Search results are already team-specific. Allow minor provider-name
            # differences (e.g. FC/Hana Citizen) when kickoff also agrees.
            return (sofa_event, candidate_score) if candidate_score >= 0.70 else (None, candidate_score)
        except Exception:
            return None, 0.0

    # Match all in parallel detail fetches
    pairs: list[tuple[dict, Any, float]] = []
    for item in pending:
        sporty = item["sporty"]
        existing = item.get("existing") or {}

        # ── SRL / simulated match guard ───────────────────────────────────────
        # Skip SofaScore matching entirely for SRL/virtual fixtures.
        match_name = sporty.get("name") or item.get("match_id") or ""
        if _is_junk(match_name):
            from app.buffer import store_enriched as _store_enriched
            srl_doc = {**(existing or {}), "sofascore_match_status": "srl_skip", "data_source": "sportybet"}
            _store_enriched(item["match_id"], srl_doc)
            unmatched_count += 1
            continue

        # Use the same resolution logic as the manual endpoint so auto-matching
        # benefits from team-bound IDs, saved IDs, multi-query search, and
        # live-aware thresholds.
        try:
            from app.match_enrichment import _resolve_sofascore_match

            match_date = item.get("match_date") or date_cls.today().isoformat()
            sofa, score, source = _resolve_sofascore_match(existing, sporty, match_date)
            if source == "team_watcher_exact":
                matched_count += 1
            elif source == "team_watcher_reversed":
                matched_count += 1
            elif source == "team_watcher_partial":
                matched_count += 1
            elif source == "saved":
                matched_count += 1
            elif source == "search":
                matched_count += 1
                search_matched_count += 1
            elif source == "llm":
                matched_count += 1
            elif source == "auto":
                matched_count += 1
            elif source == "no_match":
                unmatched_count += 1
            elif source == "team_watcher_no_exact_match":
                unmatched_count += 1
            elif source == "team_watcher_no_sofascore_id":
                unmatched_count += 1
            elif source == "team_watcher_unavailable":
                unmatched_count += 1
            else:
                if sofa:
                    matched_count += 1
                else:
                    unmatched_count += 1
        except Exception:
            sofa, score = _fuzzy_match(sporty, all_sofa_events)
            threshold = FUZZY_THRESHOLD
            if score < threshold:
                searched_sofa, searched_score = _search_match_safe(sporty)
                if searched_sofa:
                    sofa = searched_sofa
                    score = searched_score
                    matched_count += 1
                    search_matched_count += 1
                    pairs.append((item, sofa, score))
                    continue
                if score >= LLM_FALLBACK_THRESHOLD and not _is_junk(sporty.get("name") or ""):
                    llm_sofa = _llm_match(sporty, all_sofa_events)
                    if llm_sofa:
                        sofa = llm_sofa
                        matched_count += 1
                    else:
                        sofa = None
                        unmatched_count += 1
                else:
                    sofa = None
                    unmatched_count += 1
            else:
                matched_count += 1
        pairs.append((item, sofa, score))

    # Fetch SofaScore detail in parallel for all matched
    # Skip detail fetch for already-enriched prematch matches — reuse saved detail
    needs_detail = [
        (i, sofa, item) for i, (item, sofa, _) in enumerate(pairs)
        if sofa and not (
            (item.get("existing") or {}).get("sofascore_detail")
            and (item.get("existing") or {}).get("sofascore_id")
        )
    ]
    details: dict[int, dict | None] = {}
    # Pre-populate with saved detail for already-enriched matches
    for i, (item, sofa, _) in enumerate(pairs):
        existing = item.get("existing") or {}
        if sofa and existing.get("sofascore_detail") and existing.get("sofascore_id"):
            details[i] = existing["sofascore_detail"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_detail_safe, sofa): i for i, sofa, item in needs_detail}
        for future in as_completed(futures):
            details[futures[future]] = future.result()

    # Store enriched + predict
    for i, (item, sofa, score) in enumerate(pairs):
        sporty = item["sporty"]
        existing = item.get("existing") or {}
        detail = details.get(i)
        match_state = classify_match_state(sporty)
        time_ctx = match_time_context({**sporty, "sofascore_event": sofa})
        from datetime import timedelta as _td
        retry_after_ts = (datetime.now(timezone.utc) + _td(minutes=180)).timestamp()

        doc = {
            **existing,
            "data_source":       "both" if sofa else "sportybet",
            "sportybet_id":      sporty.get("id"),
            "match_id":          item.get("match_id"),
            "name":              sporty.get("name"),
            "sportybet_name":    sporty.get("name"),
            "match_date":        time_ctx.get("local_date") or item["match_date"],
            "tournament":        sporty.get("tournament"),
            "category":          sporty.get("category"),
            "start_time":        sporty.get("start_time"),
            "period":            sporty.get("period"),
            "score":             sporty.get("score"),
            "sportybet_markets": sporty.get("markets", []),
            "markets":           sporty.get("markets", []),
            "odds_1x2":          _extract_1x2(sporty.get("markets") or []),
            "sofascore_id":      sofa.get("id") if sofa else existing.get("sofascore_id"),
            "sofascore_event":   sofa,
            "sofascore_detail":  detail,
            "home_last_matches": (detail or {}).get("home_last_matches") or [],
            "away_last_matches": (detail or {}).get("away_last_matches") or [],
            "standings":         (detail or {}).get("standings") or [],
            "league_table":      (detail or {}).get("standings") or [],
            "season_stage":      detect_season_stage((detail or {}).get("standings") or []),
            "match_score":       round(score, 3),
            "sofascore_match_status": "matched" if sofa else "no_match",
            "sofascore_retry_after_ts": None if sofa else retry_after_ts,
            "raw_sporty":        sporty,
            "time_context":      time_ctx,
            "match_state":       match_state,
            "enriched_at":       now,
            "is_live":           False,
            "is_finished":       False,
        }
        snapshot_odds(doc)
        store_enriched(item["match_id"], doc)
        stored_count += 1

        # Predict for ALL stored matches, not just SofaScore-matched ones.
        # Matches with SportyBet markets but no SofaScore ID can still get
        # a degraded market-signal prediction.
        try:
            fresh = get_buffered_match(item["match_id"]) or doc
            result = apply_prediction_state(fresh, match_id=str(item["match_id"]))
            if result.get("status") == "predicted":
                predicted_count += 1
            elif result.get("status") == "deferred":
                deferred_count += 1
        except Exception as exc:
            _logger.debug("unified_upcoming: prediction failed for %s: %s", item["match_id"], exc)

    record_activity(
        f"Unified upcoming done: {len(matches)} fetched, {ingested_new} new, "
        f"{matched_count}/{len(pending)} matched ({search_matched_count} via search), "
        f"{predicted_count} predicted, {deferred_count} deferred",
        job="unified_upcoming", status="ok",
        details={"fetched": len(matches), "ingested_new": ingested_new,
                 "pending_processed": len(pending), "matched": matched_count,
                 "unmatched": unmatched_count, "stored": stored_count,
                 "predicted": predicted_count, "deferred": deferred_count,
                 "search_matched": search_matched_count},
    )
    return {
        "status": "ok", "job": "unified_upcoming",
        "fetched": len(matches), "ingested_new": ingested_new,
        "pending_processed": len(pending), "matched": matched_count,
        "unmatched": unmatched_count, "stored": stored_count,
        "predicted": predicted_count, "deferred": deferred_count,
        "search_matched": search_matched_count,
    }


def job_unified_live() -> dict[str, Any]:
    """Unified Live Pipeline:
    1. Fetch live matches from SportyBet → ingest + patch scores + snapshot odds
    2. Fetch Sofa live events once
    3a. Already-matched live (have sofascore_id + detail) → lightweight live refresh
    3b. Unmatched live → fuzzy match against live events → full fetch_event_detail
    4. Build doc with fresh Sporty markets + odds_1x2 → store_enriched → apply_prediction_state
    """
    if is_shutting_down():
        return {"status": "shutdown", "job": "unified_live"}
    try:
        from app.pipeline_registry import is_pipeline_enabled
        if not is_pipeline_enabled("unified_live"):
            return {"status": "skipped", "job": "unified_live", "reason": "pipeline_disabled"}
    except Exception:
        pass

    from app.sportybet_client import fetch_live_matches_post
    from app.buffer import (
        ingest_matches, patch_live_scores, get_unenriched_batch,
        store_enriched, get_buffered_match,
    )
    from app.storage.buffer import _extract_1x2
    from app.sofascore_client import fetch_live_events, fetch_event_detail, fetch_event_detail_live_refresh
    from app.enrichment import _fuzzy_match, _is_junk, FUZZY_THRESHOLD
    from app.league_memory import observe_matches
    from app.market.market import snapshot_odds
    from app.match_state import classify_match_state
    from app.time_context import match_time_context
    from app.prediction_flow import apply_prediction_state
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime, timezone

    record_activity("Unified live pipeline starting", job="unified_live", status="running")
    try:
        matches = fetch_live_matches_post()
    except Exception as exc:
        record_activity(f"Unified live: SportyBet fetch failed: {exc}", job="unified_live", status="error")
        return {"status": "error", "job": "unified_live", "error": str(exc)}

    # ── Step 1: Ingest + patch + snapshot ─────────────────────────────────────
    groups = _group_matches_by_local_date(matches)
    ingested = 0
    for match_date, dated_matches in groups.items():
        ingested += ingest_matches(dated_matches, match_date)
    patched = patch_live_scores(matches)
    for m in matches:
        try:
            snapshot_odds({"sportybet_id": m.get("id"), "sportybet_name": m.get("name"),
                           "match_date": _match_local_date(m), "sportybet_markets": m.get("markets", []),
                           "time_context": _match_time(m)})
        except Exception:
            pass
    observe_matches("sportybet", matches)

    # ── Step 2: Fetch Sofa live events once ───────────────────────────────────
    try:
        live_sofa_events = fetch_live_events()
    except Exception:
        live_sofa_events = []

    # ── Step 3: Get live buffer items needing enrichment ──────────────────────
    pending = get_unenriched_batch(limit=8, live_only=True)

    # Build a sportybet_id → raw sporty map for fast lookup
    sporty_by_id = {str(m.get("id")): m for m in matches if m.get("id")}

    now = datetime.now(timezone.utc).isoformat()
    matched_count = unmatched_count = stored_count = predicted_count = 0

    def _fetch_detail_safe(sofa_id: int | str, existing_detail: dict | None) -> dict | None:
        try:
            if existing_detail:
                return fetch_event_detail_live_refresh(int(sofa_id), existing_detail)
            return fetch_event_detail({"id": sofa_id})
        except Exception:
            return None

    # Split into fast-path (already matched) and match-needed
    pairs: list[tuple[dict, str | None, dict | None]] = []  # (item, sofa_id, existing_detail)
    for item in pending:
        existing = item.get("existing") or {}
        saved_sofa_id = existing.get("sofascore_id")
        if saved_sofa_id and existing.get("sofascore_detail"):
            pairs.append((item, str(saved_sofa_id), existing.get("sofascore_detail")))
            matched_count += 1
        else:
            sporty = item["sporty"]
            sofa, score = _fuzzy_match(sporty, live_sofa_events)
            threshold = 0.62  # lower threshold for live
            if sofa and score >= threshold:
                pairs.append((item, str(sofa.get("id")), None))
                matched_count += 1
            else:
                pairs.append((item, None, None))
                unmatched_count += 1

    # Fetch details in parallel
    details: dict[int, dict | None] = {}
    needs_detail = [(i, sofa_id, existing_detail) for i, (item, sofa_id, existing_detail) in enumerate(pairs) if sofa_id]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_detail_safe, sofa_id, existing_detail): i
                   for i, sofa_id, existing_detail in needs_detail}
        for future in as_completed(futures):
            details[futures[future]] = future.result()

    # ── Step 4: Build doc + store + predict ───────────────────────────────────
    for i, (item, sofa_id, _) in enumerate(pairs):
        existing = item.get("existing") or {}
        # Use fresh sporty data from this cycle if available
        sporty = sporty_by_id.get(str(item["sporty"].get("id") or "")) or item["sporty"]
        detail = details.get(i)
        match_state = classify_match_state(sporty)
        time_ctx = match_time_context({**sporty, "sofascore_event": existing.get("sofascore_event")})

        doc = {
            **existing,
            "data_source":       "both" if sofa_id else "sportybet",
            "sportybet_id":      sporty.get("id"),
            "match_id":          item.get("match_id"),
            "name":              sporty.get("name"),
            "sportybet_name":    sporty.get("name"),
            "match_date":        time_ctx.get("local_date") or item["match_date"],
            "tournament":        sporty.get("tournament"),
            "category":          sporty.get("category"),
            "start_time":        sporty.get("start_time"),
            "period":            sporty.get("period"),
            "played_seconds":    sporty.get("played_seconds"),
            "score":             sporty.get("score"),
            "sportybet_markets": sporty.get("markets", []),
            "markets":           sporty.get("markets", []),
            "odds_1x2":          _extract_1x2(sporty.get("markets") or []),
            "sofascore_id":      sofa_id or existing.get("sofascore_id"),
            "sofascore_detail":  detail or existing.get("sofascore_detail"),
            "home_last_matches": (detail or existing.get("sofascore_detail") or {}).get("home_last_matches") or [],
            "away_last_matches": (detail or existing.get("sofascore_detail") or {}).get("away_last_matches") or [],
            "standings":         (detail or existing.get("sofascore_detail") or {}).get("standings") or [],
            "season_stage":      detect_season_stage((detail or existing.get("sofascore_detail") or {}).get("standings") or []),
            "sofascore_match_status": "matched" if sofa_id else existing.get("sofascore_match_status", "no_match"),
            "raw_sporty":        sporty,
            "time_context":      time_ctx,
            "match_state":       match_state,
            "enriched_at":       now,
            "is_live":           True,
            "is_finished":       False,
        }
        store_enriched(item["match_id"], doc)
        stored_count += 1

        if sofa_id:
            try:
                fresh = get_buffered_match(item["match_id"]) or doc
                result = apply_prediction_state(fresh, match_id=str(item["match_id"]))
                if result.get("status") == "predicted":
                    predicted_count += 1
            except Exception as exc:
                _logger.debug("unified_live: prediction failed for %s: %s", item["match_id"], exc)

    record_activity(
        f"Unified live done: {len(matches)} live, {patched} patched, "
        f"{matched_count}/{len(pending)} matched, {predicted_count} predicted",
        job="unified_live", status="ok",
        details={"live_count": len(matches), "new": ingested, "patched": patched,
                 "pending": len(pending), "matched": matched_count, "unmatched": unmatched_count,
                 "stored": stored_count, "predicted": predicted_count},
    )
    return {
        "status": "ok", "job": "unified_live",
        "live_count": len(matches), "new": ingested, "patched": patched,
        "pending": len(pending), "matched": matched_count, "unmatched": unmatched_count,
        "stored": stored_count, "predicted": predicted_count,
    }


def job_competition_special() -> dict[str, Any]:
    """Run the dedicated SofaScore lane for all enabled top-30 competitions."""
    if is_shutting_down():
        return {"status": "shutdown", "job": "competition_special"}
    # ── Pipeline toggle check ──────────────────────────────────────────────────
    try:
        from app.pipeline_registry import is_pipeline_enabled
        if not is_pipeline_enabled("competition_special"):
            return {"status": "skipped", "job": "competition_special", "reason": "pipeline_disabled"}
    except Exception:
        pass
    from app.competition_special import run_enabled_competition_cycles

    record_activity("Competition special cycle checking enabled top-30 buffers", job="competition_special", status="running")
    result = run_enabled_competition_cycles()
    record_activity(
        f"Competition special cycle {result.get('status')}",
        job="competition_special",
        status="ok" if result.get("status") != "error" else "error",
        details=result,
    )
    return {"job": "competition_special", **result}


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

        # Grade orphaned predictions (matches removed from buffer before grading)
        try:
            from app.league_memory import grade_orphaned_predictions
            orphaned_result = grade_orphaned_predictions(limit=1000)
            total_graded += int(orphaned_result.get("graded") or 0)
        except Exception as _oe:
            orphaned_result = {"status": "error", "reason": str(_oe), "graded": 0}
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
            from app.self_learner import run_learning_cycle
            from app.weight_optimiser import optimise_ensemble_weights
            self_learn_result = run_learning_cycle()
            learn_result = optimise_ensemble_weights()
            learn_result["self_learner"] = self_learn_result
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
            f"signals_learned={(learn_result.get('self_learner') or {}).get('signal_updates', learn_result.get('signal_updates', 0))} "
            f"model_weights={(learn_result.get('self_learner') or {}).get('model_weight_updates', 0)} "
            f"league_profiles={(learn_result.get('self_learner') or {}).get('league_updates', 0)} "
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


def job_regenerate_research_stats() -> dict[str, Any]:
    """Regenerate research_stats table from prediction_history.

    Aggregates win/loss data across multiple dimensions and writes
    summary rows with minimum sample gates.  Guarded by a total
    wins+losses threshold of 50.
    """
    import time as _time

    from app.research.research_filter import (
        MIN_LEAGUE_SAMPLES,
        MIN_COUNTRY_SAMPLES,
        MIN_PICK_TYPE_SAMPLES,
    )

    start = _time.time()
    try:
        from app.db import db_conn

        with db_conn() as conn:
            total = conn.execute(
                "SELECT count(*) as cnt FROM prediction_history WHERE result IN ('win', 'loss')"
            ).fetchone()
            total_graded = int(total[0]) if total else 0

        if total_graded < 50:
            print(f"[scheduler] research_stats_job:insufficient_data total_graded={total_graded}")
            return {"status": "skipped", "reason": "insufficient_data", "total_graded": total_graded}

        dimensions = [
            "pick_type",
            "selection",
            "confidence_band",
            "country",
            "league",
            "source",
            "odds_bucket_home",
            "odds_bucket_draw",
            "odds_bucket_away",
            "odds_bucket_fav",
            "favorite_side",
        ]

        rows_written = 0
        rows_skipped = 0

        with db_conn() as conn:
            for dim in dimensions:
                min_samples = MIN_LEAGUE_SAMPLES if dim == "league" else MIN_COUNTRY_SAMPLES if dim == "country" else MIN_PICK_TYPE_SAMPLES
                rows = conn.execute(
                    f"""
                    SELECT
                        {dim} as key,
                        sum(case when result = 'win' then 1 else 0 end) as wins,
                        sum(case when result = 'loss' then 1 else 0 end) as losses,
                        count(*) as total
                    FROM prediction_history
                    WHERE result IN ('win', 'loss')
                      AND {dim} IS NOT NULL
                      AND {dim} != ''
                    GROUP BY {dim}
                    HAVING count(*) >= ?
                    """,
                    (min_samples,),
                ).fetchall()

                for row in rows:
                    key = str(row["key"] or "")
                    wins = int(row["wins"] or 0)
                    losses = int(row["losses"] or 0)
                    total = int(row["total"] or 0)
                    win_rate = wins / total if total > 0 else 0.0
                    loss_rate = losses / total if total > 0 else 0.0
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO research_stats
                            (dimension, key, wins, losses, total, win_rate, loss_rate, min_samples, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
                        """,
                        (dim, key, wins, losses, total, round(win_rate, 6), round(loss_rate, 6), min_samples),
                    )
                    rows_written += 1

                rows_skipped += len([r for r in rows if int(r["total"]) < min_samples])

            conn.commit()

        elapsed = round(_time.time() - start, 3)
        print(
            f"[scheduler] regenerate_research_stats: rows_written={rows_written} "
            f"rows_skipped={rows_skipped} duration={elapsed}s"
        )
        return {
            "status": "ok",
            "job": "regenerate_research_stats",
            "rows_written": rows_written,
            "rows_skipped": rows_skipped,
            "total_graded": total_graded,
            "duration_seconds": elapsed,
        }
    except Exception as exc:
        print(f"[scheduler] regenerate_research_stats failed: {exc}")
        return {"status": "error", "error": str(exc)}


def job_grade_overdue_predictions() -> dict[str, Any]:
    """Frequent safety-net grader using SportyBet results first, SofaScore fallback second."""
    try:
        from app.league_memory import grade_betbuilder_history, grade_orphaned_predictions, grade_overdue_predictions

        record_activity("Checking overdue match results", job="grade_overdue", status="running")
        result = grade_overdue_predictions(hours_after_kickoff=2, limit=500)

        # Also grade orphaned predictions on every run
        try:
            orphaned = grade_orphaned_predictions(limit=500)
            result["orphaned"] = orphaned
            result["graded"] = int(result.get("graded") or 0) + int(orphaned.get("graded") or 0)
        except Exception as _oe:
            result["orphaned"] = {"status": "error", "reason": str(_oe)}

        graded = int(result.get("graded") or 0) + int(result.get("candidate_graded") or 0)
        if graded:
            try:
                from app.confidence_calibrator import rebuild_calibration
                from app.self_learner import run_learning_cycle
                from app.weight_optimiser import optimise_ensemble_weights

                result["calibration"] = rebuild_calibration()
                # Rebuild signal weights from fresh graded data
                learn_cycle = run_learning_cycle()
                opt_result = optimise_ensemble_weights()
                result["learning"] = {**opt_result, "self_learner": learn_cycle}
            except Exception as exc:
                result["learning_error"] = str(exc)
        result["betbuilder"] = grade_betbuilder_history(limit=300)
        record_activity(
            f"Overdue grading finished: {result.get('graded', 0)} primary, {result.get('candidate_graded', 0)} candidates, {int((result.get('orphaned') or {}).get('graded') or 0)} orphaned",
            job="grade_overdue",
            status="ok",
            details=result,
        )
        print(
            f"[scheduler] grade_overdue: checked={result.get('checked')} "
            f"graded={result.get('graded')} candidates={result.get('candidate_graded')} "
            f"orphaned={int((result.get('orphaned') or {}).get('graded') or 0)} "
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


def job_system_supervisor() -> dict[str, Any]:
    """Operational supervisor: observe pipeline health and apply safe repairs."""
    if is_shutting_down():
        return {"status": "shutdown", "job": "system_supervisor"}
    from app.system_supervisor import run_system_supervisor

    return run_system_supervisor(auto_correct=True)


def job_prediction_monitor() -> dict[str, Any]:
    """Hourly prediction-quality loop: grade truth, diagnose misses, refresh learning."""
    if is_shutting_down():
        return {"status": "shutdown", "job": "prediction_monitor"}
    from app.prediction_monitor import run_prediction_monitor

    return run_prediction_monitor(auto_correct=True)


def job_autopilot_guardian() -> dict[str, Any]:
    """Self-healing coordinator for unattended operation.

    The normal scheduler jobs do the regular work. This guardian watches their
    outputs and makes conservative catch-up calls when truth, learning, or flow
    has gone stale. It is intentionally orchestration-only: it uses the same
    grading, monitor, supervisor, ingest, and enrichment jobs as the rest of the
    app instead of inventing another prediction path.
    """
    if is_shutting_down():
        return {"status": "shutdown", "job": "autopilot_guardian"}
    if not _autopilot_enabled():
        return {"status": "idle", "job": "autopilot_guardian", "enabled": False}

    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    record_activity("Autopilot guardian checking system health", job="autopilot_guardian", status="running")

    def guarded(label: str, fn, *args, **kwargs) -> Any:
        try:
            result = fn(*args, **kwargs)
            actions.append({"action": label, "result": _summarise_guardian_result(result)})
            return result
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            return {"status": "error", "error": str(exc)}

    guarded("recover_abandoned_jobs", _recover_jobs)
    stats = guarded("buffer_stats", get_buffer_stats)

    # Flow healing: if live work or enrichment backlog is visible, nudge the
    # same workers the scheduler uses. Guard rows prevent duplicate work.
    if get_live_priority_mode().get("enabled") and _has_live_pressure(stats):
        guarded("live_priority", lambda: run_job_with_guard(job_live_priority, count=12, limit=300))
    elif _needs_enrichment_nudge(stats):
        from app.pipeline_registry import is_pipeline_enabled
        enrich_on = is_pipeline_enabled("sportybet_enrich_live") or is_pipeline_enabled("sportybet_enrich_prematch")
        if enrich_on:
            guarded("enrich_worker", lambda: run_job_with_guard(job_enrich_worker))

    if _job_stale("system_supervisor", 5 * 60):
        actions.append({
            "action": "system_supervisor_stale",
            "result": {"status": "observed", "reason": "guardian does not own operational correction authority"},
        })

    if _job_stale("grade_overdue_predictions", 15 * 60):
        guarded("grade_overdue_predictions", lambda: run_job_with_guard(job_grade_overdue_predictions))

    if _job_stale("prediction_monitor", 50 * 60):
        actions.append({
            "action": "prediction_monitor_stale",
            "result": {"status": "observed", "reason": "guardian does not own learning correction authority"},
        })

    if _job_stale("grade_predictions", 4 * 60 * 60):
        guarded("grade_predictions", lambda: run_job_with_guard(job_grade_predictions))

    if _deep_audit_due():
        guarded("deep_system_supervisor_observe", lambda: _run_deep_supervisor(auto_correct=False))

    final_stats = guarded("buffer_stats_after", get_buffer_stats)
    status = "ok" if not errors else "degraded"
    record_activity(
        f"Autopilot guardian pass: {len(actions)} action(s), {len(errors)} error(s)",
        job="autopilot_guardian",
        status=status,
        details={"actions": actions[-10:], "errors": errors[:8], "buffer": final_stats},
    )
    return {
        "status": status,
        "enabled": True,
        "actions": actions,
        "errors": errors[:8],
        "buffer_before": stats,
        "buffer_after": final_stats,
        "principle": "unattended operation: heal flow and settle truth; monitor/supervisor corrections stay behind explicit authority leases",
    }


# ── Scheduler setup ───────────────────────────────────────────────────────────

def start_scheduler():
    global _scheduler, _shutting_down
    if _scheduler:
        _start_scheduler_watchdog()
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
    # misfire_grace_time=60: with 8s timeout + 2 retries, worst case ~25s.
    # 60s grace means a job fired up to 60s late is still executed (not dropped).
    scheduler.add_job(
        _safe(job_ingest_live),
        IntervalTrigger(seconds=_scheduler_interval("ingest_live")),
        id="ingest_live",
        name="Ingest live matches + patch scores",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        next_run_time=now + timedelta(seconds=5),
    )

    # ingest upcoming — every 2 min
    scheduler.add_job(
        _safe(job_ingest_upcoming),
        IntervalTrigger(seconds=_scheduler_interval("ingest_upcoming")),
        id="ingest_upcoming",
        name="Ingest upcoming matches from SportyBet",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=now + timedelta(seconds=10),
    )

    # enrichment worker — every 30 sec, processes today/live first.
    # Single instance to avoid concurrent SQLite write contention.
    scheduler.add_job(
        _safe_no_guard(job_enrich_worker),
        IntervalTrigger(seconds=_scheduler_interval("enrich_worker")),
        id="enrich_worker",
        name="Enrichment + prediction worker (live + today)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=now + timedelta(seconds=45),
    )

    scheduler.add_job(
        _safe(job_ai_prediction_queue),
        IntervalTrigger(seconds=_scheduler_interval("ai_prediction_queue")),
        id="ai_prediction_queue",
        name="Evidence-first AI prediction queue",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    # future enrichment — every 1 hour, enriches fixtures beyond tomorrow
    scheduler.add_job(
        _safe(job_live_priority_toggle),
        IntervalTrigger(seconds=_scheduler_interval("live_priority_toggle")),
        id="live_priority_toggle",
        name="Continuous live priority lane",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )

    # SofaScore-only pipeline — every 5 min, only runs when toggle is enabled
    scheduler.add_job(
        _safe(job_sofa_pipeline),
        IntervalTrigger(seconds=_scheduler_interval("sofa_pipeline")),
        id="sofa_pipeline",
        name="SofaScore-only enrichment + prediction (cloud mode)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )

    scheduler.add_job(
        _safe(job_enrich_future),
        IntervalTrigger(seconds=_scheduler_interval("enrich_future")),
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
        _safe(job_system_supervisor),
        IntervalTrigger(minutes=3),
        id="system_supervisor",
        name="System intelligence supervisor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=now + timedelta(seconds=75),
    )

    scheduler.add_job(
        _safe(job_prediction_monitor),
        IntervalTrigger(hours=1),
        id="prediction_monitor",
        name="Hourly prediction performance monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
        next_run_time=now + timedelta(minutes=10),
    )

    scheduler.add_job(
        _safe(job_autopilot_guardian),
        IntervalTrigger(minutes=5),
        id="autopilot_guardian",
        name="Autopilot guardian: self-heal + learning coordinator",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=180,
        next_run_time=now + timedelta(seconds=120),
    )

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
        _safe(job_competition_special),
        IntervalTrigger(minutes=5),
        id="competition_special",
        name="Continuous competition special lane (World Cup)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=180,
        next_run_time=now + timedelta(minutes=3),
    )

    scheduler.add_job(
        _safe(job_competition_analysis),
        IntervalTrigger(seconds=86400),
        id="competition_analysis",
        name="Post-matchday competition analysis (Ollama)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        _safe(job_unified_upcoming),
        IntervalTrigger(seconds=_scheduler_interval("unified_upcoming")),
        id="unified_upcoming",
        name="Unified Upcoming Pipeline (Sporty + Sofa → match → predict)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=240,
    )

    scheduler.add_job(
        _safe(job_unified_live),
        IntervalTrigger(seconds=_scheduler_interval("unified_live")),
        id="unified_live",
        name="Unified Live Pipeline (Sporty + Sofa live → match → predict)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=90,
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

    # regenerate research_stats — daily at 03:00 server time
    scheduler.add_job(
        _safe(job_regenerate_research_stats),
        IntervalTrigger(days=1),
        id="regenerate_research_stats",
        name="Research stats regeneration",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
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
    print("[scheduler]   competition_special every  5 min  (enabled top-30 SofaScore special lanes)")
    print("[scheduler]   flush_to_mongo   every  2 min  (buffer -> MongoDB)")
    print("[scheduler]   supervisor       every  3 min  (audit + safe self-correction)")
    print("[scheduler]   pred_monitor     every  1 hr   (grade + mismatch/trend learning)")
    print("[scheduler]   autopilot        every  5 min  (self-heal + catch-up learning)")
    print("[scheduler]   archive_finished every 15 min  (finished -> MongoDB)")
    print("[scheduler]   grade_overdue    every 30 min  (Sporty results + Sofa fallback)")
    print("[scheduler]   grade_predictions every  6 hrs  (analytics + ELO)")
    print("[scheduler]   prune_mongo      every  7 days (remove matches >90 days old)")
    print("[scheduler]   keep_alive       every 10 min  (prevent Render cold start)")
    _start_scheduler_watchdog()
    return scheduler


def stop_scheduler(wait: bool = False) -> bool:
    global _scheduler, _shutting_down
    _shutting_down = True
    _watchdog_stop.set()
    scheduler = _scheduler
    _scheduler = None  # clear reference before shutdown so new process doesn't inherit it
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

def _autopilot_enabled() -> bool:
    return os.getenv("PREDICTX_AUTOPILOT_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def _start_scheduler_watchdog() -> None:
    """Start an out-of-band flow watchdog that is not run by APScheduler."""
    global _watchdog_thread
    if _watchdog_thread and _watchdog_thread.is_alive():
        return
    _watchdog_stop.clear()
    _watchdog_thread = threading.Thread(
        target=_scheduler_watchdog_loop,
        name="predictx_scheduler_watchdog",
        daemon=True,
    )
    _watchdog_thread.start()


def _scheduler_watchdog_loop() -> None:
    # Give the normal scheduler first chance after startup/reload.
    if _watchdog_stop.wait(90):
        return
    while not _watchdog_stop.is_set() and not is_shutting_down():
        try:
            _scheduler_watchdog_tick()
        except Exception as exc:
            try:
                record_activity(
                    f"Scheduler watchdog failed: {exc}",
                    job="scheduler_watchdog",
                    status="error",
                )
            except Exception:
                pass
        _watchdog_stop.wait(60)


def _scheduler_watchdog_tick() -> dict[str, Any]:
    """Heal flow when APScheduler goes silent but the process is still alive."""
    if not _autopilot_enabled():
        return {"status": "idle", "enabled": False}
    stats = get_buffer_stats()
    actions: list[dict[str, Any]] = []

    ingest_stale = _job_stale("ingest_live", 2 * 60)
    enrich_stale = _job_stale("enrich_worker", 2 * 60)
    guardian_stale = _job_stale("autopilot_guardian", 7 * 60)
    has_pressure = _has_live_pressure(stats) or _needs_enrichment_nudge(stats)

    if not (has_pressure and (ingest_stale or enrich_stale or guardian_stale)):
        return {"status": "ok", "actions": actions}

    record_activity(
        "Scheduler watchdog detected stalled flow",
        job="scheduler_watchdog",
        status="running",
        details={
            "ingest_stale": ingest_stale,
            "enrich_stale": enrich_stale,
            "guardian_stale": guardian_stale,
            "live": stats.get("live"),
            "hot_pending_enrichment": stats.get("hot_pending_enrichment"),
            "last_ingested_at": stats.get("last_ingested_at"),
            "last_enriched_at": stats.get("last_enriched_at"),
        },
    )

    from app.pipeline_registry import is_pipeline_enabled
    if ingest_stale and int(stats.get("live") or 0) and is_pipeline_enabled("sportybet_ingest_live"):
        result = run_job_with_guard(job_ingest_live, limit=150)
        actions.append({"action": "ingest_live", "result": _summarise_guardian_result(result)})
    enrich_on = is_pipeline_enabled("sportybet_enrich_live") or is_pipeline_enabled("sportybet_enrich_prematch")
    if enrich_stale and has_pressure and enrich_on:
        result = run_job_with_guard(job_enrich_worker)
        actions.append({"action": "enrich_worker", "result": _summarise_guardian_result(result)})
    if guardian_stale:
        actions.append({"action": "autopilot_guardian_stale", "result": {"status": "observed"}})

    final_stats = get_buffer_stats()
    record_activity(
        f"Scheduler watchdog healed stalled flow: {len(actions)} action(s)",
        job="scheduler_watchdog",
        status="ok",
        details={"actions": actions, "buffer": final_stats},
    )
    return {"status": "ok", "actions": actions, "buffer": final_stats}


def _recover_jobs() -> dict[str, Any]:
    from app.job_state import recover_abandoned_jobs

    return recover_abandoned_jobs(stale_after_seconds=120)


def _has_live_pressure(stats: Any) -> bool:
    if not isinstance(stats, dict):
        return False
    return (
        int(stats.get("live") or 0) > 0
        or int(stats.get("stale_live") or 0) > 0
        or int(stats.get("hot_pending_enrichment") or 0) >= 50
    )


def _needs_enrichment_nudge(stats: Any) -> bool:
    if not isinstance(stats, dict):
        return False
    pending = int(stats.get("hot_pending_enrichment") or 0)
    if pending <= 0:
        return False
    last_enriched = _parse_datetime(stats.get("last_enriched_at"))
    if last_enriched is None:
        return True
    return (datetime.now(timezone.utc) - last_enriched).total_seconds() > 10 * 60


def _job_stale(job_id: str, max_age_seconds: int) -> bool:
    age = _seconds_since_job_finished(job_id)
    return age is None or age >= max_age_seconds


def _deep_audit_due() -> bool:
    age = _seconds_since_supervisor_deep_audit()
    return age is None or age >= 60 * 60


def _run_deep_supervisor(*, auto_correct: bool = True) -> dict[str, Any]:
    from app.system_supervisor import run_system_supervisor

    return run_system_supervisor(auto_correct=auto_correct, deep_audit=True)


def _seconds_since_job_finished(job_id: str) -> float | None:
    try:
        from app.job_state import _init_job_table
        from app.db import DB_PATH
        from app.league_memory import _init_db

        _init_db()
        with db_conn(timeout=10) as conn:
            _init_job_table(conn)
            row = conn.execute(
                "select finished_at, heartbeat_at from job_runs where job_id = ?",
                (job_id,),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    when = _parse_datetime(row[0] or row[1])
    if not when:
        return None
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds())


def _seconds_since_supervisor_deep_audit() -> float | None:
    try:
        from app.db import DB_PATH
        from app.league_memory import _init_db
        from app.system_supervisor import _init_supervisor_table

        _init_db()
        with db_conn(timeout=10) as conn:
            _init_supervisor_table(conn)
            row = conn.execute(
                """
                select created_at from system_supervisor_snapshots
                where audit_depth = 'deep'
                order by datetime(created_at) desc, id desc
                limit 1
                """
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    when = _parse_datetime(row[0])
    if not when:
        return None
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds())


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _summarise_guardian_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"value": result}
    keys = (
        "status",
        "job",
        "graded",
        "candidate_graded",
        "checked",
        "live_count",
        "new",
        "patched",
        "batch",
        "matched",
        "stored",
        "predicted",
        "enabled",
        "duration_seconds",
    )
    summary = {key: result.get(key) for key in keys if key in result}
    if "buffer" in result and isinstance(result["buffer"], dict):
        summary["buffer"] = {
            key: result["buffer"].get(key)
            for key in ("live", "hot_pending_enrichment", "ready", "deferred", "stale_live")
            if key in result["buffer"]
        }
    return summary or {"status": result.get("status")}

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


def _safe_no_guard(fn):
    """Wrap a scheduler job without the persistent single-run guard."""
    def wrapper(*args, **kwargs):
        if is_shutting_down():
            return {"status": "shutdown", "job": fn.__name__}
        try:
            return fn(*args, **kwargs)
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
    job_id = guard_job_id or (fn.__name__[4:] if fn.__name__.startswith("job_") else fn.__name__)
    return _run_job_with_guard_locked(fn, *args, guard_job_id=job_id, **kwargs)


def _run_job_with_guard_locked(fn, *args, guard_job_id: str | None = None, **kwargs):
    from app.job_state import JobBusy, finish_job, heartbeat, job_guard

    job_id = guard_job_id or (fn.__name__[4:] if fn.__name__.startswith("job_") else fn.__name__)
    stale_after = _JOB_STALE_SECONDS.get(job_id, 900)
    try:
        with job_guard(job_id, stale_after_seconds=stale_after) as state:
            stop_heartbeat = threading.Event()
            interval = max(15, min(60, stale_after // 3))

            def _beat() -> None:
                while not stop_heartbeat.wait(interval):
                    try:
                        heartbeat(job_id, owner=state["owner"])
                    except Exception:
                        pass

            heartbeat_thread = threading.Thread(target=_beat, name=f"{job_id}_heartbeat", daemon=True)
            heartbeat_thread.start()
            try:
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
            finally:
                stop_heartbeat.set()
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
