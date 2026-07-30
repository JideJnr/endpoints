from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import get_settings


DB_PATH: Path = get_settings().database_path
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_BUSY_TIMEOUT_MS = 30000
_local = threading.local()


def configure_connection(conn: sqlite3.Connection, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Apply the app-wide SQLite connection policy."""
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma synchronous = normal")
    conn.execute(f"pragma busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("pragma cache_size = -8000")
    return conn


def connect_db(*, timeout: int = DEFAULT_TIMEOUT_SECONDS, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a SQLite connection using the shared app database settings."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=timeout, check_same_thread=check_same_thread)
    return configure_connection(conn)


def connect_readonly_db(*, timeout: int = 2) -> sqlite3.Connection:
    """Open the shared database in read-only mode with the same row handling."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout = 30000")
    return conn


def get_db() -> sqlite3.Connection:
    """Return a thread-local persistent connection, creating it on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect_db(timeout=DEFAULT_TIMEOUT_SECONDS, check_same_thread=False)
        _local.conn = conn
    return conn


def close_db() -> None:
    """Close and discard the current thread's persistent connection."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


@contextmanager
def db_conn(timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Iterator[sqlite3.Connection]:
    """Yield a short-lived connection with consistent WAL and busy-timeout pragmas."""
    with connect_db(timeout=timeout) as conn:
        yield conn


@contextmanager
def _conn(timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Iterator[sqlite3.Connection]:
    """Backward-compatible alias for modules that already use app-level DB contexts."""
    with db_conn(timeout=timeout) as conn:
        yield conn


def is_sqlite_lock(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message
