from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db


VALID_SOURCES = {"sportybet", "sofascore"}
EXPIRE_MINUTES = 120


def ensure_live_retry_queue(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists live_retry_queue (
            match_id text not null,
            source text not null,
            sportybet_id text,
            sofascore_id text,
            first_seen_at text not null default current_timestamp,
            last_seen_at text not null default current_timestamp,
            resolved_at text,
            expired_at text,
            reason text,
            attempts integer not null default 0,
            primary key (match_id, source)
        )
        """
    )
    conn.execute("create index if not exists idx_live_retry_active on live_retry_queue(resolved_at, expired_at, last_seen_at)")


def mark_pending(
    *,
    match_id: str,
    source: str,
    sportybet_id: str | None = None,
    sofascore_id: str | None = None,
    reason: str | None = None,
) -> None:
    if not match_id or source not in VALID_SOURCES:
        return
    _init_db()
    with db_conn(timeout=30) as conn:
        ensure_live_retry_queue(conn)
        conn.execute(
            """
            insert into live_retry_queue (
                match_id, source, sportybet_id, sofascore_id, first_seen_at,
                last_seen_at, reason, attempts
            )
            values (?, ?, ?, ?, current_timestamp, current_timestamp, ?, 1)
            on conflict(match_id, source) do update set
                sportybet_id = coalesce(excluded.sportybet_id, live_retry_queue.sportybet_id),
                sofascore_id = coalesce(excluded.sofascore_id, live_retry_queue.sofascore_id),
                last_seen_at = current_timestamp,
                reason = excluded.reason,
                attempts = live_retry_queue.attempts + 1
            where live_retry_queue.resolved_at is null
              and live_retry_queue.expired_at is null
            """,
            (str(match_id), source, sportybet_id, sofascore_id, reason),
        )
        conn.commit()


def mark_resolved(match_id: str, source: str) -> None:
    if not match_id or source not in VALID_SOURCES:
        return
    _init_db()
    with db_conn(timeout=30) as conn:
        ensure_live_retry_queue(conn)
        conn.execute(
            """
            update live_retry_queue
            set resolved_at = current_timestamp
            where match_id = ?
              and source = ?
              and resolved_at is null
              and expired_at is null
            """,
            (str(match_id), source),
        )
        conn.commit()


def expire_stale_entries() -> int:
    _init_db()
    with db_conn(timeout=30) as conn:
        ensure_live_retry_queue(conn)
        cur = conn.execute(
            """
            update live_retry_queue
            set expired_at = current_timestamp
            where resolved_at is null
              and expired_at is null
              and datetime(first_seen_at) < datetime('now', ?)
            """,
            (f"-{EXPIRE_MINUTES} minutes",),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def active_pending_count() -> int:
    _init_db()
    with db_conn(timeout=30) as conn:
        ensure_live_retry_queue(conn)
        row = conn.execute(
            """
            select count(*)
            from live_retry_queue
            where resolved_at is null
              and expired_at is null
            """
        ).fetchone()
        return int(row[0] if row else 0)


def list_active(limit: int = 200) -> list[dict[str, Any]]:
    _init_db()
    with db_conn(timeout=30) as conn:
        ensure_live_retry_queue(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select *
            from live_retry_queue
            where resolved_at is null
              and expired_at is null
            order by datetime(last_seen_at) asc
            limit ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
