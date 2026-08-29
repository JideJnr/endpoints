from __future__ import annotations

import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db


OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class CorrectionAuthorityBusy(Exception):
    def __init__(self, scope: str, owner: str | None, source: str | None):
        self.scope = scope
        self.owner = owner
        self.source = source
        super().__init__(f"correction scope '{scope}' is held by {source or owner or 'unknown'}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


def _init_authority_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists correction_authority_leases (
            scope text primary key,
            source text not null,
            owner text not null,
            acquired_at text not null,
            heartbeat_at text not null,
            expires_at real not null,
            reason text,
            run_count integer not null default 0
        )
        """
    )
    conn.execute("create index if not exists idx_correction_authority_expires on correction_authority_leases(expires_at)")


@contextmanager
def correction_authority(
    source: str,
    scope: str,
    *,
    ttl_seconds: int = 300,
    reason: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Lease a correction scope so self-healing loops do not fight each other."""
    _init_db()
    token = OWNER
    now = _now_iso()
    expires_at = _now_ts() + max(30, ttl_seconds)
    with db_conn(timeout=30) as conn:
        _init_authority_table(conn)
        conn.execute("begin immediate")
        row = conn.execute(
            "select source, owner, expires_at from correction_authority_leases where scope = ?",
            (scope,),
        ).fetchone()
        if row and float(row[2] or 0) > _now_ts() and row[1] != token:
            conn.rollback()
            raise CorrectionAuthorityBusy(scope, row[1], row[0])
        conn.execute(
            """
            insert into correction_authority_leases (
                scope, source, owner, acquired_at, heartbeat_at, expires_at, reason, run_count
            ) values (?, ?, ?, ?, ?, ?, ?, 1)
            on conflict(scope) do update set
                source = excluded.source,
                owner = excluded.owner,
                acquired_at = excluded.acquired_at,
                heartbeat_at = excluded.heartbeat_at,
                expires_at = excluded.expires_at,
                reason = excluded.reason,
                run_count = correction_authority_leases.run_count + 1
            """,
            (scope, source, token, now, now, expires_at, reason),
        )
        conn.commit()

    try:
        yield {"scope": scope, "source": source, "owner": token, "expires_at": expires_at}
    finally:
        with db_conn(timeout=30) as conn:
            _init_authority_table(conn)
            conn.execute(
                "delete from correction_authority_leases where scope = ? and owner = ?",
                (scope, token),
            )
            conn.commit()


def renew_authority_lease(scope: str, owner: str, *, ttl_seconds: int = 300) -> bool:
    """
    Push a held lease's expiry forward from a live heartbeat.

    Call this periodically while genuinely still working inside a
    `correction_authority(...)` block. Keeping the *initial* ttl_seconds small
    (see run_job_with_guard) and relying on this renewal to keep long jobs
    alive means a crashed/killed process's lease self-expires within one
    ttl window instead of sitting stuck for hours. No-ops (returns False)
    if this owner no longer holds the lease — e.g. it already expired and
    was reclaimed by someone else.
    """
    expires_at = _now_ts() + max(30, ttl_seconds)
    with db_conn(timeout=30) as conn:
        _init_authority_table(conn)
        cur = conn.execute(
            """
            update correction_authority_leases
            set heartbeat_at = ?, expires_at = ?
            where scope = ? and owner = ?
            """,
            (_now_iso(), expires_at, scope, owner),
        )
        conn.commit()
        return cur.rowcount > 0


def clear_all_authority_leases(reason: str = "process boot") -> int:
    """
    Unconditionally drop every correction-authority lease.

    Call this once, early, on process startup only. A fresh process has not
    acquired anything yet, so any row already in the table belongs to a
    process that no longer exists (a previous run of this same app that
    crashed, was force-killed, or was replaced by --reload) — expired or
    not by its own stored expires_at. This is the immediate remedy for a
    lease stuck under the old 6-hour TTL (see run_job_with_guard); the
    renew/short-ttl fix there prevents new leases from getting stuck this
    way going forward, but does not retroactively fix a row already
    written with the old, much longer expiry.
    """
    _init_db()
    with db_conn(timeout=30) as conn:
        _init_authority_table(conn)
        cur = conn.execute("delete from correction_authority_leases")
        conn.commit()
        return cur.rowcount


def recover_abandoned_leases(stale_after_seconds: int = 300) -> dict[str, Any]:
    """
    Delete leases that expired a while ago and were never cleaned up.

    Normal expiry already makes a lease unenforceable (acquire checks
    expires_at > now()), so this is just housekeeping — it keeps the table
    from accumulating dead rows and gives an explicit recovery hook to call
    at startup / on a timer, mirroring job_state.recover_abandoned_jobs().
    """
    _init_db()
    cutoff = _now_ts() - max(30, stale_after_seconds)
    with db_conn(timeout=30) as conn:
        _init_authority_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select scope, owner from correction_authority_leases where expires_at <= ?",
            (cutoff,),
        ).fetchall()
        if rows:
            conn.execute("delete from correction_authority_leases where expires_at <= ?", (cutoff,))
            conn.commit()
    return {"recovered": len(rows), "scopes": [r["scope"] for r in rows]}


def authority_snapshot() -> list[dict[str, Any]]:
    _init_db()
    with db_conn(timeout=30) as conn:
        _init_authority_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select scope, source, owner, acquired_at, heartbeat_at, expires_at, reason, run_count
            from correction_authority_leases
            order by scope
            """
        ).fetchall()
    now = _now_ts()
    return [
        {
            "scope": row["scope"],
            "source": row["source"],
            "owner": row["owner"],
            "acquired_at": row["acquired_at"],
            "heartbeat_at": row["heartbeat_at"],
            "expires_in_seconds": round(float(row["expires_at"] or 0) - now, 1),
            "active": float(row["expires_at"] or 0) > now,
            "reason": row["reason"],
            "run_count": row["run_count"],
        }
        for row in rows
    ]
