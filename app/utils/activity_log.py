from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import sqlite3
from threading import Lock
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db


_MAX_EVENTS = 120
_events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
_lock = Lock()
_current: dict[str, Any] = {
    "status": "idle",
    "message": "System is idle",
    "job": "idle",
    "updated_at": datetime.now(timezone.utc).isoformat(),
}

# ── Write-batching ────────────────────────────────────────────────────────────
# record_activity() is called 4-6x per match during enrichment.  The old
# implementation opened a DB connection and ran INSERT + DELETE-subquery on
# every single call (40-60 serial connections for a 10-match batch).
# Now events queue in memory and flush in one executemany + one purge, either
# when _FLUSH_BATCH_SIZE events have accumulated or immediately before any
# read so get_activity() always returns current data.
_FLUSH_BATCH_SIZE = 10
_pending: list[dict[str, Any]] = []


def record_activity(
    message: str,
    *,
    job: str = "system",
    status: str = "info",
    match_id: str | None = None,
    match_name: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a tiny in-process activity event for the settings dashboard."""
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "job": job,
        "status": status,
        "message": message,
        "match_id": match_id,
        "match_name": match_name,
        "details": details or {},
    }
    to_flush: list[dict[str, Any]] | None = None
    with _lock:
        _events.appendleft(event)
        global _current
        _current = event
        _pending.append(event)
        if len(_pending) >= _FLUSH_BATCH_SIZE:
            to_flush, _pending[:] = list(_pending), []
    if to_flush:
        _persist_events(to_flush)
    return event


def mark_idle(message: str = "System is idle") -> None:
    record_activity(message, job="idle", status="idle")


def _flush_pending() -> None:
    """Force any queued events out to the DB now. Called before every read
    so get_activity() never returns data staler than the caller's own last
    record_activity() call, regardless of the write-batch size."""
    with _lock:
        if not _pending:
            return
        to_flush, _pending[:] = list(_pending), []
    _persist_events(to_flush)


def get_activity(limit: int = 30) -> dict[str, Any]:
    limit = max(1, min(int(limit or 30), _MAX_EVENTS))
    _flush_pending()
    with _lock:
        events = list(_events)[:limit]
        current = dict(_current)
    persisted = _load_events(limit)
    if persisted:
        events = persisted
        current = persisted[0]
    return {
        "current": current,
        "events": events,
    }


def _persist_events(events: list[dict[str, Any]]) -> None:
    if not events:
        return
    try:
        _init_db()
        with db_conn(timeout=10) as conn:
            _init_activity_table(conn)
            conn.executemany(
                """
                insert into system_activity (
                    ts, job, status, message, match_id, match_name, details_json
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        event.get("ts"),
                        event.get("job"),
                        event.get("status"),
                        event.get("message"),
                        event.get("match_id"),
                        event.get("match_name"),
                        json.dumps(event.get("details") or {}, default=str),
                    )
                    for event in events
                ],
            )
            # Keep only the most recent 500 rows. The old version used
            # `WHERE id NOT IN (SELECT id ... ORDER BY ts DESC, id DESC LIMIT 500)`,
            # an expensive anti-pattern in SQLite (materializes the whole
            # subquery result to test every row's membership). id is an
            # autoincrement PK assigned in the same insertion order as ts,
            # so ordering the boundary lookup by id instead of ts is
            # equivalent here and lets SQLite use the primary-key index
            # directly instead of a NOT IN scan.
            conn.execute(
                """
                delete from system_activity
                where id < (
                    select id from system_activity order by id desc limit 1 offset 499
                )
                """
            )
            conn.commit()
    except Exception:
        pass


def _load_events(limit: int) -> list[dict[str, Any]]:
    try:
        _init_db()
        with db_conn(timeout=10) as conn:
            _init_activity_table(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select ts, job, status, message, match_id, match_name, details_json
                from system_activity
                order by datetime(ts) desc, id desc
                limit ?
                """,
                (limit,),
            ).fetchall()
    except Exception:
        return []
    events = []
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except Exception:
            details = {}
        events.append({
            "ts": row["ts"],
            "job": row["job"],
            "status": row["status"],
            "message": row["message"],
            "match_id": row["match_id"],
            "match_name": row["match_name"],
            "details": details,
        })
    return events


def _init_activity_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists system_activity (
            id integer primary key autoincrement,
            ts text,
            job text,
            status text,
            message text,
            match_id text,
            match_name text,
            details_json text
        )
        """
    )
    conn.execute("create index if not exists idx_system_activity_ts on system_activity(ts)")
