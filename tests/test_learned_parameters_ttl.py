"""
Tests for the TTL-based cache in app/monitoring/learned_parameters.py
----------------------------------------------------------------------
R5.5: _graded_rows() must have a TTL mechanism: the cached result is treated
      as stale after 3600 seconds and re-fetched on the next call.
R5.6: When _graded_rows() TTL has elapsed, the module must re-execute the
      graded history SQL query rather than returning the stale cached tuple.
R5.3: When clear_learned_parameter_cache() is called, the _graded_rows()
      cache must be immediately invalidated, forcing a re-fetch on the next
      call regardless of TTL.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import app.monitoring.learned_parameters as lp


# ── helpers ───────────────────────────────────────────────────────────────────

def _reset_module_cache() -> None:
    """Reset the module-level TTL cache variables to a clean slate."""
    lp._GRADED_ROWS_CACHE = None
    lp._GRADED_ROWS_FETCHED_AT = 0.0


@contextmanager
def _mock_db_returning(rows: list[dict]):
    """
    Patch db_conn and _init_db so _graded_rows() returns `rows` without
    touching the filesystem.  Each row dict is surfaced as a plain dict
    (simulating conn.row_factory = sqlite3.Row behaviour after dict()).
    """
    mock_row_list = []
    if rows:
        # Build sqlite3.Row objects from an in-memory DB for realism
        mem = sqlite3.connect(":memory:")
        mem.row_factory = sqlite3.Row
        cols = list(rows[0].keys())
        mem.execute(f"create table t ({', '.join(cols)})")
        for r in rows:
            mem.execute(
                f"insert into t values ({', '.join('?' * len(cols))})",
                [r[c] for c in cols],
            )
        mock_row_list = mem.execute("select * from t").fetchall()

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchall.return_value = mock_row_list
    # row_factory assignment must not raise
    type(mock_conn).row_factory = property(lambda self: None, lambda self, v: None)

    with patch("app.monitoring.learned_parameters.db_conn", return_value=mock_conn), \
         patch("app.monitoring.learned_parameters._init_db"):
        yield mock_conn


# ── R5.5 / R5.6: within-TTL returns cached value without re-querying ─────────

def test_within_ttl_returns_cached_value_without_db_query():
    """
    R5.5 — While the TTL has not elapsed, _graded_rows() must return the
    cached tuple and must NOT call db_conn again.
    """
    _reset_module_cache()

    first_rows = [{"match_id": "m1", "result": "win", "league_name": "PL",
                   "country_name": "England", "pick_type": "match_result",
                   "selection": "home", "confidence": 70.0}]

    # Seed the cache via a first call
    with _mock_db_returning(first_rows) as mock_conn:
        result1 = lp._graded_rows()
        initial_call_count = mock_conn.execute.call_count

    # Second call within TTL — db_conn should NOT be entered again
    with patch("app.monitoring.learned_parameters.db_conn") as patched_db, \
         patch("app.monitoring.learned_parameters._init_db"):
        result2 = lp._graded_rows()
        patched_db.assert_not_called()

    assert result1 == result2, "Cached result must be identical to first result"


# ── R5.5 / R5.6: after TTL elapses, re-fetches from DB ───────────────────────

def test_re_fetches_after_ttl_elapses():
    """
    R5.5 / R5.6 — After the TTL (3600 s) elapses, _graded_rows() must
    execute the graded SQL query again and return fresh data.

    Strategy: mock time.monotonic so the second call appears to be
    3601 seconds after the first.
    """
    _reset_module_cache()

    first_rows = [{"match_id": "old", "result": "win", "league_name": "PL",
                   "country_name": "England", "pick_type": "match_result",
                   "selection": "home", "confidence": 65.0}]
    second_rows = [{"match_id": "new", "result": "loss", "league_name": "PL",
                    "country_name": "England", "pick_type": "match_result",
                    "selection": "away", "confidence": 55.0}]

    # --- First call at t=1000 ---
    with patch("app.monitoring.learned_parameters.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        with _mock_db_returning(first_rows):
            rows_first = lp._graded_rows()

    assert any(r["match_id"] == "old" for r in rows_first), \
        "First call must return the seeded first_rows"

    # --- Second call at t=4602 (1000 + 3602 > TTL of 3600) ---
    with patch("app.monitoring.learned_parameters.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0 + lp._GRADED_ROWS_TTL + 2
        with _mock_db_returning(second_rows) as mock_conn:
            rows_second = lp._graded_rows()
            assert mock_conn.execute.called, \
                "db_conn must be used when TTL has elapsed"

    assert any(r["match_id"] == "new" for r in rows_second), \
        "Second call must return fresh second_rows after TTL expiry"


# ── R5.5 / R5.6: TTL boundary — one second before TTL should NOT re-fetch ─────

def test_one_second_before_ttl_does_not_re_fetch():
    """
    The condition is strict '<', so at (TTL - 1) seconds the cache is still
    valid and no re-fetch should occur.  At exactly TTL seconds the cache is
    expired (not strictly less than), so a re-fetch is triggered.
    """
    _reset_module_cache()

    seed_rows = [{"match_id": "boundary", "result": "win", "league_name": "La Liga",
                  "country_name": "Spain", "pick_type": "match_result",
                  "selection": "home", "confidence": 70.0}]

    with patch("app.monitoring.learned_parameters.time") as mock_time:
        mock_time.monotonic.return_value = 500.0
        with _mock_db_returning(seed_rows):
            lp._graded_rows()

    # One second before TTL: 500 + 3599 = 4099  (3599 < 3600, still valid)
    with patch("app.monitoring.learned_parameters.time") as mock_time:
        mock_time.monotonic.return_value = 500.0 + lp._GRADED_ROWS_TTL - 1
        with patch("app.monitoring.learned_parameters.db_conn") as patched_db, \
             patch("app.monitoring.learned_parameters._init_db"):
            lp._graded_rows()
            patched_db.assert_not_called()


# ── R5.3: clear_learned_parameter_cache() immediately invalidates cache ───────

def test_clear_cache_forces_immediate_re_fetch():
    """
    R5.3 — Calling clear_learned_parameter_cache() must reset
    _GRADED_ROWS_CACHE to None and _GRADED_ROWS_FETCHED_AT to 0.0,
    so the very next call to _graded_rows() re-queries the DB even if
    the TTL has not elapsed.
    """
    _reset_module_cache()

    first_rows = [{"match_id": "before_clear", "result": "win", "league_name": "Bundesliga",
                   "country_name": "Germany", "pick_type": "match_result",
                   "selection": "home", "confidence": 72.0}]

    # Seed the cache
    with patch("app.monitoring.learned_parameters.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        with _mock_db_returning(first_rows):
            lp._graded_rows()

    # Verify cache is populated
    assert lp._GRADED_ROWS_CACHE is not None, "Cache should be populated after first call"
    assert lp._GRADED_ROWS_FETCHED_AT == 1000.0

    # Clear the cache (still well within TTL)
    lp.clear_learned_parameter_cache()

    assert lp._GRADED_ROWS_CACHE is None, \
        "_GRADED_ROWS_CACHE must be None immediately after clear_learned_parameter_cache()"
    assert lp._GRADED_ROWS_FETCHED_AT == 0.0, \
        "_GRADED_ROWS_FETCHED_AT must be reset to 0.0 after clear_learned_parameter_cache()"


def test_clear_cache_causes_db_call_on_next_graded_rows_call():
    """
    R5.3 — After clear_learned_parameter_cache(), the next _graded_rows()
    call must hit the database even if the original TTL has not expired.
    """
    _reset_module_cache()

    seed_rows = [{"match_id": "after_clear", "result": "loss", "league_name": "Serie A",
                  "country_name": "Italy", "pick_type": "match_result",
                  "selection": "away", "confidence": 60.0}]

    # Seed the cache at t=1000
    with patch("app.monitoring.learned_parameters.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        with _mock_db_returning(seed_rows):
            lp._graded_rows()

    # Clear while still within TTL
    lp.clear_learned_parameter_cache()

    # Next call at t=1001 (far within original TTL of 3600s) must still hit DB
    with patch("app.monitoring.learned_parameters.time") as mock_time:
        mock_time.monotonic.return_value = 1001.0
        with _mock_db_returning(seed_rows) as mock_conn:
            lp._graded_rows()
            assert mock_conn.execute.called, \
                "DB must be queried after cache was explicitly cleared, even within TTL"


# ── R5.5: exception during DB fetch sets cache to empty tuple ─────────────────

def test_db_exception_sets_cache_to_empty_tuple():
    """
    R5.5 — When the DB query raises an exception, _graded_rows() must catch
    it, set the cache to an empty tuple, and return that empty tuple rather
    than raising.
    """
    _reset_module_cache()

    with patch("app.monitoring.learned_parameters.db_conn", side_effect=Exception("DB offline")), \
         patch("app.monitoring.learned_parameters._init_db"):
        result = lp._graded_rows()

    assert result == (), \
        "On DB exception, _graded_rows() must return an empty tuple"
    assert lp._GRADED_ROWS_CACHE == (), \
        "_GRADED_ROWS_CACHE must be set to () when a DB exception occurs"


# ── R5.5: TTL constant value ──────────────────────────────────────────────────

def test_ttl_constant_is_3600():
    """
    R5.5 — The TTL must be exactly 3600 seconds (1 hour).
    """
    assert lp._GRADED_ROWS_TTL == 3600, \
        f"_GRADED_ROWS_TTL must be 3600, got {lp._GRADED_ROWS_TTL}"
