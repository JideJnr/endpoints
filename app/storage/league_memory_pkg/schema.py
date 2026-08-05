"""
league_memory.schema
~~~~~~~~~~~~~~~~~~~~
Schema-ensure helpers specific to league_memory concerns.

These are small "ensure table exists" helpers that league_memory operations
call before doing I/O on tables that may not have been created by the main
``_init_db()`` (which lives in ``app.db``).

The main ``_init_db`` is re-exported from here so that sub-modules in this
package can import it from one place:

    from app.storage.league_memory.schema import _init_db, _ensure_signal_outcomes_table
"""
from __future__ import annotations

import sqlite3

# Re-export the primary DB initialiser so callers within the package
# (and external shims) can import it from here.
from app.db import _init_db as _init_db  # noqa: F401, PLC0414


def _ensure_signal_outcomes_table(conn: sqlite3.Connection) -> None:
    """Create the ``signal_outcomes`` table and indexes if they don't exist."""
    conn.execute(
        """
        create table if not exists signal_outcomes (
            id integer primary key autoincrement,
            match_id text not null,
            match_name text,
            tournament text,
            country text,
            match_date text,
            signal_name text not null,
            signal_value_json text not null default '{}',
            signal_impact real,
            result text not null,
            pick_type text,
            selection text,
            confidence integer,
            recorded_at text not null default current_timestamp,
            unique (match_id, pick_type, selection, signal_name)
        )
        """
    )
    conn.execute("create index if not exists idx_signal_outcomes_signal on signal_outcomes(signal_name)")
    conn.execute("create index if not exists idx_signal_outcomes_scope on signal_outcomes(country, tournament, result)")


def _ensure_buffer_tables(conn: sqlite3.Connection) -> None:
    """Ensure buffer-related tables exist by delegating to ``app.buffer``."""
    try:
        from app.buffer import _init_buffer_table

        _init_buffer_table(conn)
    except Exception:
        pass
