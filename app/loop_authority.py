from __future__ import annotations

import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.db import db_conn
from app.db import DB_PATH
from app.league_memory import _init_db


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
