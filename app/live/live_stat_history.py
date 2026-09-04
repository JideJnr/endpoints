"""
Time-stamped history of live in-play match statistics.

Today `app/match_facts.py::normalize_live_statistics` computes the SAME shape
of stats dict (possession, shots, corners, xG, attacks, dangerous attacks —
see STAT_KEYS below) on every live poll, but it's only ever kept as the
LATEST value on the match doc (`doc["live_statistics"]`) — each poll
overwrites the last one. There is no memory of what the numbers looked like
10 or 40 minutes ago.

This module adds that memory: an append-only table, one row per meaningful
change (mirrors the dedup-on-change design of app/market/market.py's
odds_snapshots, so a 90-minute match produces a handful of rows instead of
~180 near-duplicates). This is the raw material for:
  - first-half vs second-half "who was pressing" tags (diff two snapshots
    that straddle half-time), and
  - in-match xG tracking (SofaScore's own xG figure when it provides one,
    tracked over time instead of only seen as a single current number).

Neither of those is computed here — this module only records the history.
See app/live/live_pressure.py for the half-based read on top of it.
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from app.storage.db import db_conn, _init_db
from app.utils.match_helpers import _played_seconds

# Derive STAT_KEYS from the single authoritative source in match_facts.py so
# a rename or addition there is automatically reflected here without needing
# manual sync.  The tuple order matches the original hand-written list, which
# matches LIVE_STAT_NAMES insertion order (Python 3.7+ dicts are ordered).
from app.match_facts import LIVE_STAT_NAMES as _LIVE_STAT_NAMES
STAT_KEYS: tuple[str, ...] = tuple(_LIVE_STAT_NAMES.keys())


def _ensure_live_stat_tables(conn: sqlite3.Connection) -> None:
    columns_sql = ",\n            ".join(f"home_{key} real,\n            away_{key} real" for key in STAT_KEYS)
    conn.execute(
        f"""
        create table if not exists live_stat_snapshots (
            id integer primary key autoincrement,
            match_id text not null,
            match_name text,
            match_date text,
            minute integer,
            period text,
            provider_period text,
            {columns_sql},
            snapshot_time text not null default current_timestamp
        )
        """
    )
    conn.execute(
        """
        create table if not exists live_stat_snapshot_state (
            match_id text primary key,
            last_sig text,
            updated_at text not null default current_timestamp
        )
        """
    )
    conn.execute("create index if not exists idx_live_stat_match on live_stat_snapshots(match_id)")
    conn.execute("create index if not exists idx_live_stat_match_time on live_stat_snapshots(match_id, snapshot_time)")


def _numeric(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip().rstrip("%")
        try:
            return float(cleaned)
        except ValueError:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_values(summary: dict[str, Any]) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for key in STAT_KEYS:
        item = summary.get(key) or {}
        values[f"home_{key}"] = _numeric(item.get("home"))
        values[f"away_{key}"] = _numeric(item.get("away"))
    return values


def _signature(values: dict[str, float | None]) -> str:
    parts = [f"{values.get(f'home_{key}')}:{values.get(f'away_{key}')}" for key in STAT_KEYS]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _current_minute(doc: dict[str, Any]) -> int | None:
    seconds = _played_seconds(doc.get("played_seconds"))
    if not seconds:
        return None
    return seconds // 60


def snapshot_live_statistics(doc: dict[str, Any]) -> bool:
    """
    Append a row to live_stat_snapshots if the in-play stats moved since the
    last recorded snapshot for this match. No-ops when there is nothing to
    record: prematch docs, or a live match SofaScore has no statistics for
    (see app/storage/buffer.py coverage audit notes — some lower-tier
    leagues simply never get a statistics payload from SofaScore).

    Returns True if a row was written.
    """
    live_stats = doc.get("live_statistics") or {}
    summary = live_stats.get("summary") or {}
    if not summary:
        return False

    match_id = str(doc.get("sportybet_id") or doc.get("id") or doc.get("match_id") or "")
    if not match_id:
        return False

    values = _extract_values(summary)
    if not any(v is not None for v in values.values()):
        return False

    sig = _signature(values)
    match_name = doc.get("sportybet_name") or doc.get("name")
    match_date = doc.get("match_date")
    minute = _current_minute(doc)
    period = doc.get("period")
    # SofaScore sometimes reports separate "1ST"/"2ND" groups directly instead
    # of only a cumulative "ALL" total — when it does, later half-splitting
    # logic can use that directly instead of diffing our own snapshots.
    periods = live_stats.get("periods") or []
    provider_period = periods[-1].get("period") if periods else None

    _init_db()
    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")
        _ensure_live_stat_tables(conn)
        state = conn.execute(
            "select last_sig from live_stat_snapshot_state where match_id = ?",
            (match_id,),
        ).fetchone()
        if state and state[0] == sig:
            conn.commit()
            return False

        columns = ["match_id", "match_name", "match_date", "minute", "period", "provider_period"]
        params: list[Any] = [match_id, match_name, match_date, minute, period, provider_period]
        for key in STAT_KEYS:
            for side in ("home", "away"):
                columns.append(f"{side}_{key}")
                params.append(values.get(f"{side}_{key}"))
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"insert into live_stat_snapshots ({', '.join(columns)}) values ({placeholders})",
            params,
        )
        conn.execute(
            """
            insert into live_stat_snapshot_state (match_id, last_sig, updated_at)
            values (?, ?, current_timestamp)
            on conflict(match_id) do update set
                last_sig = excluded.last_sig,
                updated_at = current_timestamp
            """,
            (match_id, sig),
        )
        conn.commit()
    return True


def get_stat_history(match_id: str) -> list[dict[str, Any]]:
    """Return all recorded live-stat snapshots for a match, oldest first."""
    _init_db()
    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")
        _ensure_live_stat_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select * from live_stat_snapshots where match_id = ? order by snapshot_time asc",
            (str(match_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_history_for_match(match_id: str) -> int:
    """Delete all recorded live-stat snapshots for a match. Returns rows deleted."""
    _init_db()
    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")
        _ensure_live_stat_tables(conn)
        cur = conn.execute("delete from live_stat_snapshots where match_id = ?", (str(match_id),))
        conn.execute("delete from live_stat_snapshot_state where match_id = ?", (str(match_id),))
        conn.commit()
        return cur.rowcount
