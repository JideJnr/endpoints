"""
Social post ledger
-------------------
Tracks every booking-code thread the system has generated for X (Twitter),
whether it actually went out (dry_run=0) or was only logged because
X_POSTING_ENABLED / credentials weren't configured (dry_run=1).

Two jobs read this table:
  - the poster job itself, to avoid re-posting a thread built from the same
    match set within a short dedup window (see `recent_match_id_sets`)
  - the public site / status endpoints, to show "latest booking code" pages
    without re-deriving them from betbuilder_history each time.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.storage.db import db_conn
from app.storage.league_memory import _init_db


def record_social_post(
    *,
    platform: str = "x",
    betbuilder_id: int | None,
    match_ids: list[str],
    share_code: str | None,
    thread: list[str],
    status: str,
    dry_run: bool,
    post_ids: list[str] | None = None,
    error: str | None = None,
) -> int:
    _init_db()
    with db_conn(timeout=20) as conn:
        cur = conn.execute(
            """
            insert into social_posts
                (platform, betbuilder_id, match_ids_json, share_code, thread_json,
                 post_ids_json, status, dry_run, error, posted_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, case when ? then current_timestamp else null end)
            """,
            (
                platform,
                betbuilder_id,
                json.dumps(match_ids),
                share_code,
                json.dumps(thread),
                json.dumps(post_ids or []),
                status,
                1 if dry_run else 0,
                error,
                status == "posted",
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def recent_match_id_sets(hours: int = 6, limit: int = 20) -> list[set[str]]:
    """Match-id sets from recently created social posts, newest first --
    used to skip posting a near-duplicate thread for a slip that covers
    mostly the same fixtures as one already posted this window."""
    _init_db()
    with db_conn(timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select match_ids_json from social_posts
            where created_at >= datetime('now', ?)
            order by created_at desc
            limit ?
            """,
            (f"-{int(hours)} hours", int(limit)),
        ).fetchall()
    out: list[set[str]] = []
    for row in rows:
        try:
            ids = json.loads(row["match_ids_json"] or "[]")
        except Exception:
            ids = []
        out.append({str(i) for i in ids})
    return out


def list_recent_posts(limit: int = 20, *, only_posted: bool = False) -> list[dict[str, Any]]:
    _init_db()
    with db_conn(timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        clause = "where status = 'posted'" if only_posted else ""
        rows = conn.execute(
            f"""
            select * from social_posts
            {clause}
            order by created_at desc
            limit ?
            """,
            (int(limit),),
        ).fetchall()
    posts = []
    for row in rows:
        doc = dict(row)
        for key in ("match_ids_json", "thread_json", "post_ids_json"):
            try:
                doc[key.replace("_json", "")] = json.loads(doc.pop(key) or "[]")
            except Exception:
                doc[key.replace("_json", "")] = []
        posts.append(doc)
    return posts
