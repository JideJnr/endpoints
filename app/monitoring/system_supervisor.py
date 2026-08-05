from __future__ import annotations

import time
import sqlite3
import json
from typing import Any

from app.db import db_conn
from app.activity_log import record_activity
from app.buffer import get_buffer_stats, purge_ghost_matches
from app.job_state import list_job_states, recover_abandoned_jobs
from app.db import DB_PATH
from app.league_memory import _init_db
from app.mongo_store import cleanup_buffer


SNAPSHOT_KEEP_ROWS = 1000


def run_system_supervisor(*, auto_correct: bool = True, deep_audit: bool = False) -> dict[str, Any]:
    """Observe the production pipeline and apply safe operational corrections.

    This layer deliberately avoids model math, model weights, prediction
    strategy, and historical grading truth. It only repairs operational state
    that can be derived from existing provider/buffer data.
    """
    started = time.time()
    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    authority: dict[str, Any] = {"source": "system_supervisor", "scope": "operational", "active": False}

    audit = _safe_call("audit", lambda: _supervisor_audit(deep=deep_audit), errors)
    buffer_before = audit.get("buffer") or _safe_call("buffer_stats", get_buffer_stats, errors)
    issues = audit.get("issues") or {}

    if auto_correct:
        from app.loop_authority import CorrectionAuthorityBusy, correction_authority

        try:
            with correction_authority("system_supervisor", "operational", reason="safe operational repair") as lease:
                authority = {**lease, "active": True}
                recovered = _safe_call("recover_abandoned_jobs", lambda: recover_abandoned_jobs(stale_after_seconds=300), errors)
                if int(recovered.get("recovered") or 0):
                    actions.append({"action": "recover_abandoned_jobs", **recovered})

                ghost_deleted = _safe_call("purge_ghost_matches", purge_ghost_matches, errors)
                if isinstance(ghost_deleted, int) and ghost_deleted:
                    actions.append({"action": "purge_ghost_matches", "deleted": ghost_deleted})

                cleanup = _safe_call("cleanup_buffer", cleanup_buffer, errors)
                cleanup_deleted = sum(
                    int(cleanup.get(key) or 0)
                    for key in ("deleted_finished", "deleted_90_plus", "deleted_ghost", "deleted_stale_unenriched")
                ) if isinstance(cleanup, dict) else 0
                if cleanup_deleted:
                    actions.append({"action": "cleanup_buffer", **cleanup})
        except CorrectionAuthorityBusy as exc:
            authority = {
                "source": "system_supervisor",
                "scope": exc.scope,
                "active": False,
                "blocked_by": exc.source or exc.owner,
            }
            errors.append(f"correction_authority_busy: {exc}")

    buffer_after = _safe_call("buffer_stats_after", get_buffer_stats, errors)
    result = {
        "status": "ok" if not errors else "degraded",
        "mode": "auto_correct" if auto_correct else "observe",
        "audit_depth": "deep" if deep_audit else "light",
        "duration_seconds": round(time.time() - started, 2),
        "actions": actions,
        "errors": errors[:8],
        "authority": authority,
        "issues": issues,
        "buffer_before": buffer_before,
        "buffer_after": buffer_after,
        "principle": "single-authority operational correction only; prediction models, weights, strategy, and historical results untouched",
    }
    _persist_supervisor_snapshot(result)
    _record_supervisor_activity(result)
    return result


def latest_supervisor_snapshots(limit: int = 50) -> dict[str, Any]:
    """Return recent supervisor observations for review after being away."""
    _init_db()
    limit = max(1, min(int(limit or 50), 300))
    with db_conn(timeout=20) as conn:
        _init_supervisor_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select created_at, status, mode, audit_depth, duration_seconds,
                   actions_json, errors_json, issues_json, buffer_json
            from system_supervisor_snapshots
            order by datetime(created_at) desc, id desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    snapshots = [_snapshot_row(row) for row in rows]
    return {
        "status": "success",
        "count": len(snapshots),
        "snapshots": snapshots,
        "summary": _snapshot_summary(snapshots),
    }


def _supervisor_audit(*, deep: bool = False) -> dict[str, Any]:
    if deep:
        from app.monitoring.system_audit import prediction_system_audit

        return prediction_system_audit(limit=80)

    _init_db()
    with db_conn(timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        duplicate_buffer_rows = conn.execute(
            """
            select count(*)
            from match_buffer mb
            join future_match_buffer fb on fb.match_id = mb.match_id
            """
        ).fetchone()[0]
        pending_predictions = conn.execute(
            """
            select count(distinct match_id)
            from prediction_history
            where graded_at is null and pick_type != 'no_bet'
            """
        ).fetchone()[0]
        pending_candidates = conn.execute(
            """
            select count(distinct match_id)
            from prediction_candidate_history
            where graded_at is null
            """
        ).fetchone()[0]
        pending_decisions = conn.execute(
            """
            select count(distinct match_id)
            from prediction_decision_log
            where graded_at is null
            """
        ).fetchone()[0]
        expired_no_match_rows = conn.execute(
            """
            select count(*)
            from match_buffer
            where is_finished = 0
              and json_extract(raw_enriched, '$.sofascore_match_status') = 'no_match'
              and coalesce(cast(json_extract(raw_enriched, '$.sofascore_retry_after_ts') as real), 0) <= strftime('%s','now')
            """
        ).fetchone()[0]

    jobs = list_job_states()
    stuck_jobs = [
        job for job in jobs
        if job.get("status") == "running" and _age_seconds(job.get("heartbeat_at")) > 900
    ]
    job_by_id = {str(job.get("job_id") or ""): job for job in jobs}
    core_stale_limits = {
        "ingest_live": 180,
        "enrich_worker": 180,
        "autopilot_guardian": 600,
    }
    stale_core_jobs = [
        {
            "job_id": job_id,
            "age_seconds": round(_age_seconds((job_by_id.get(job_id) or {}).get("heartbeat_at")), 1),
            "status": (job_by_id.get(job_id) or {}).get("status"),
            "heartbeat_at": (job_by_id.get(job_id) or {}).get("heartbeat_at"),
        }
        for job_id, limit in core_stale_limits.items()
        if _age_seconds((job_by_id.get(job_id) or {}).get("heartbeat_at")) > limit
    ]
    return {
        "status": "success",
        "buffer": get_buffer_stats(),
        "jobs": jobs,
        "issues": {
            "duplicate_buffer_rows": duplicate_buffer_rows,
            "pending_prediction_matches": pending_predictions,
            "pending_candidate_matches": pending_candidates,
            "pending_decision_matches": pending_decisions,
            "expired_no_match_rows": expired_no_match_rows,
            "stuck_jobs": len(stuck_jobs),
            "stale_core_jobs": len(stale_core_jobs),
        },
        "samples": {"stuck_jobs": stuck_jobs[:20], "stale_core_jobs": stale_core_jobs[:20]},
    }


def _safe_call(name: str, fn, errors: list[str]) -> Any:
    try:
        return fn()
    except Exception as exc:
        errors.append(f"{name}: {exc}")
        return {}


def _persist_supervisor_snapshot(result: dict[str, Any]) -> None:
    try:
        _init_db()
        with db_conn(timeout=20) as conn:
            _init_supervisor_table(conn)
            conn.execute(
                """
                insert into system_supervisor_snapshots (
                    created_at, status, mode, audit_depth, duration_seconds,
                    actions_json, errors_json, issues_json, buffer_json
                ) values (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.get("status"),
                    result.get("mode"),
                    result.get("audit_depth"),
                    result.get("duration_seconds"),
                    json.dumps(result.get("actions") or [], default=str),
                    json.dumps(result.get("errors") or [], default=str),
                    json.dumps(result.get("issues") or {}, default=str),
                    json.dumps(result.get("buffer_after") or {}, default=str),
                ),
            )
            conn.execute(
                """
                delete from system_supervisor_snapshots
                where id not in (
                    select id
                    from system_supervisor_snapshots
                    order by datetime(created_at) desc, id desc
                    limit ?
                )
                """,
                (SNAPSHOT_KEEP_ROWS,),
            )
            conn.commit()
    except Exception:
        pass


def _init_supervisor_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists system_supervisor_snapshots (
            id integer primary key autoincrement,
            created_at text not null,
            status text,
            mode text,
            audit_depth text,
            duration_seconds real,
            actions_json text not null default '[]',
            errors_json text not null default '[]',
            issues_json text not null default '{}',
            buffer_json text not null default '{}'
        )
        """
    )
    conn.execute("create index if not exists idx_supervisor_snapshots_created on system_supervisor_snapshots(created_at)")


def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "created_at": row["created_at"],
        "status": row["status"],
        "mode": row["mode"],
        "audit_depth": row["audit_depth"],
        "duration_seconds": row["duration_seconds"],
        "actions": _loads(row["actions_json"], []),
        "errors": _loads(row["errors_json"], []),
        "issues": _loads(row["issues_json"], {}),
        "buffer": _loads(row["buffer_json"], {}),
    }


def _snapshot_summary(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    if not snapshots:
        return {}
    latest = snapshots[0]
    degraded = sum(1 for item in snapshots if item.get("status") != "ok")
    actions = sum(len(item.get("actions") or []) for item in snapshots)
    errors = sum(len(item.get("errors") or []) for item in snapshots)
    return {
        "latest_status": latest.get("status"),
        "latest_created_at": latest.get("created_at"),
        "degraded_count": degraded,
        "actions_recorded": actions,
        "errors_recorded": errors,
        "latest_buffer": latest.get("buffer") or {},
        "latest_issues": latest.get("issues") or {},
    }


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _age_seconds(value: Any) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime

        return max(0.0, time.time() - datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0.0


def _record_supervisor_activity(result: dict[str, Any]) -> None:
    action_count = len(result.get("actions") or [])
    error_count = len(result.get("errors") or [])
    status = "ok" if not error_count else "error"
    try:
        record_activity(
            f"System supervisor pass: {action_count} correction(s), {error_count} error(s)",
            job="system_supervisor",
            status=status,
            details={
                "actions": result.get("actions") or [],
                "errors": result.get("errors") or [],
                "issues": result.get("issues") or {},
            },
        )
    except Exception:
        pass
