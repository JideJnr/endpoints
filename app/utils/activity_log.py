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
    with _lock:
        _events.appendleft(event)
        global _current
        _current = event
    _persist_event(event)
    return event


def mark_idle(message: str = "System is idle") -> None:
    record_activity(message, job="idle", status="idle")


def get_activity(limit: int = 30) -> dict[str, Any]:
    limit = max(1, min(int(limit or 30), _MAX_EVENTS))
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


def _persist_event(event: dict[str, Any]) -> None:
    try:
        _init_db()
        with db_conn(timeout=10) as conn:
            _init_activity_table(conn)
            conn.execute(
                """
                insert into system_activity (
                    ts, job, status, message, match_id, match_name, details_json
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.get("ts"),
                    event.get("job"),
                    event.get("status"),
                    event.get("message"),
                    event.get("match_id"),
                    event.get("match_name"),
                    json.dumps(event.get("details") or {}, default=str),
                ),
            )
            conn.execute(
                """
                delete from system_activity
                where id not in (
                    select id from system_activity order by datetime(ts) desc, id desc limit 500
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
