from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.league_memory import DB_PATH, _init_db


OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class JobBusy(Exception):
    def __init__(self, job_id: str, owner: str | None):
        self.job_id = job_id
        self.owner = owner
        super().__init__(f"{job_id} is already running")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_job_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists job_runs (
            job_id text primary key,
            status text,
            owner text,
            started_at text,
            heartbeat_at text,
            finished_at text,
            last_result_json text,
            last_error text,
            run_count integer default 0,
            fail_count integer default 0
        )
        """
    )


@contextmanager
def job_guard(job_id: str, *, stale_after_seconds: int = 900) -> Iterator[dict[str, Any]]:
    """
    Cross-process SQLite guard for scheduler and manual jobs.

    The DB connection is closed before yielding so the write lock is not held
    for the entire job duration — that was causing all concurrent jobs to see
    'database is locked'.
    """
    _init_db()
    token = OWNER
    start = _now()
    stale_before = time.time() - max(30, stale_after_seconds)

    # Acquire the guard in a short-lived connection — close before yielding.
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_job_table(conn)
        conn.execute("begin immediate")
        row = conn.execute(
            "select status, owner, heartbeat_at from job_runs where job_id = ?",
            (job_id,),
        ).fetchone()
        if row:
            status, owner, heartbeat_at = row
            heartbeat_ts = _parse_ts(heartbeat_at)
            if status == "running" and heartbeat_ts and heartbeat_ts > stale_before:
                conn.rollback()
                raise JobBusy(job_id, owner)
            conn.execute(
                """
                update job_runs
                set status = 'running',
                    owner = ?,
                    started_at = ?,
                    heartbeat_at = ?,
                    finished_at = null,
                    last_error = null,
                    run_count = coalesce(run_count, 0) + 1
                where job_id = ?
                """,
                (token, start, start, job_id),
            )
        else:
            conn.execute(
                """
                insert into job_runs (
                    job_id, status, owner, started_at, heartbeat_at, run_count
                ) values (?, 'running', ?, ?, ?, 1)
                """,
                (job_id, token, start, start),
            )
        conn.commit()
    # Connection is now closed — DB lock released before the job runs.

    state = {"job_id": job_id, "owner": token, "started_at": start}
    try:
        yield state
    except Exception as exc:
        finish_job(job_id, status="error", owner=token, error=str(exc))
        raise


def finish_job(
    job_id: str,
    *,
    status: str,
    owner: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_job_table(conn)
        conn.execute(
            """
            update job_runs
            set status = ?,
                heartbeat_at = ?,
                finished_at = ?,
                last_result_json = ?,
                last_error = ?,
                fail_count = coalesce(fail_count, 0) + ?
            where job_id = ? and owner = ?
            """,
            (
                status,
                _now(),
                _now(),
                json.dumps(result or {}, default=str),
                error,
                1 if status == "error" else 0,
                job_id,
                owner,
            ),
        )
        conn.commit()


def heartbeat(job_id: str, *, owner: str) -> None:
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        _init_job_table(conn)
        conn.execute(
            "update job_runs set heartbeat_at = ? where job_id = ? and owner = ?",
            (_now(), job_id, owner),
        )
        conn.commit()


def list_job_states() -> list[dict[str, Any]]:
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_job_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select job_id, status, owner, started_at, heartbeat_at, finished_at,
                   last_result_json, last_error, run_count, fail_count
            from job_runs
            order by coalesce(heartbeat_at, started_at) desc
            """
        ).fetchall()
    out = []
    for row in rows:
        result = {}
        try:
            result = json.loads(row["last_result_json"] or "{}")
        except Exception:
            result = {}
        out.append({
            "job_id": row["job_id"],
            "status": row["status"],
            "owner": row["owner"],
            "started_at": row["started_at"],
            "heartbeat_at": row["heartbeat_at"],
            "finished_at": row["finished_at"],
            "last_result": result,
            "last_error": row["last_error"],
            "run_count": row["run_count"],
            "fail_count": row["fail_count"],
        })
    return out


def recover_abandoned_jobs(stale_after_seconds: int = 300) -> dict[str, Any]:
    """Mark stale running jobs from crashed/reloaded processes as recovered."""
    _init_db()
    cutoff = time.time() - max(30, stale_after_seconds)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_job_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select job_id, heartbeat_at from job_runs where status = 'running'"
        ).fetchall()
        stale = [row["job_id"] for row in rows if (_parse_ts(row["heartbeat_at"]) or 0) <= cutoff]
        if stale:
            placeholders = ",".join("?" for _ in stale)
            conn.execute(
                f"""
                update job_runs
                set status = 'recovered',
                    finished_at = ?,
                    last_error = 'Recovered after process restart or missed heartbeat'
                where job_id in ({placeholders})
                """,
                (_now(), *stale),
            )
        conn.commit()
    return {"recovered": len(stale), "jobs": stale}


def _parse_ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None
