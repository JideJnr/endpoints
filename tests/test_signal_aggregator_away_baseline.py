"""Unit tests for _get_away_baseline() in signal_aggregator.py (R6).

Covers:
  - League present in league_outcome_distribution with away_rate = 0.38 → returns ≈ 0.38.
  - No league row but global rows present → returns global average away_rate.
  - No rows at all → returns the hardcoded constant 0.54.
  - DB exception → returns 0.54 without raising.
  - Return type is always float.

Requirements: R6.2, R6.3, R6.4
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(data: dict):
    """Return a MagicMock that behaves like a sqlite3.Row for the given dict."""
    row = MagicMock()
    row.__getitem__ = MagicMock(side_effect=lambda k: data[k])
    row.__bool__ = MagicMock(return_value=True)
    return row


def _db_conn_returning(league_row, global_row):
    """
    Build a db_conn context-manager mock whose execute().fetchone() returns
    `league_row` on the first call and `global_row` on the second call.
    This mirrors the two-query lookup inside _get_away_baseline().
    """
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchone.side_effect = [league_row, global_row]
    mock_conn.execute.return_value.fetchall.return_value = []
    return MagicMock(return_value=mock_conn)


def _db_conn_raising():
    """Build a db_conn mock that raises OperationalError on execute()."""
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.side_effect = sqlite3.OperationalError("no such table")
    return MagicMock(return_value=mock_conn)


def _call_get_away_baseline(league_key: str, db_mock) -> float:
    """Invoke _get_away_baseline() with the supplied db_conn mock."""
    with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
         patch("app.enrichment.signal_aggregator._init_db"):
        from app.enrichment.signal_aggregator import _get_away_baseline
        return _get_away_baseline(league_key)


# ---------------------------------------------------------------------------
# Tests: league-specific row present
# ---------------------------------------------------------------------------

class TestGetAwayBaselineLeagueRow:
    """League row exists in league_outcome_distribution (R6.2)."""

    def test_returns_league_away_rate(self):
        """League present with away_rate = 0.38 → _get_away_baseline returns ≈ 0.38."""
        league_row = _make_row({"away_rate": 0.38})
        db_mock = _db_conn_returning(league_row, None)

        result = _call_get_away_baseline("test_league", db_mock)

        assert abs(result - 0.38) < 1e-9, f"Expected ≈ 0.38, got {result}"

    def test_returns_exact_float_conversion(self):
        """away_rate stored with many decimals is returned with full precision."""
        league_row = _make_row({"away_rate": 0.3812345678})
        db_mock = _db_conn_returning(league_row, None)

        result = _call_get_away_baseline("precise_league", db_mock)

        assert abs(result - 0.3812345678) < 1e-9

    def test_league_row_takes_priority_over_global(self):
        """League row exists → global query is not needed; league rate is returned."""
        league_row = _make_row({"away_rate": 0.30})
        global_row = _make_row({"avg_away": 0.45})  # should NOT be used
        db_mock = _db_conn_returning(league_row, global_row)

        result = _call_get_away_baseline("my_league", db_mock)

        assert abs(result - 0.30) < 1e-9, (
            "League-specific rate must take priority over global average"
        )

    def test_away_rate_zero_point_five(self):
        """League away_rate = 0.50 is returned correctly (no clamping applied)."""
        league_row = _make_row({"away_rate": 0.50})
        db_mock = _db_conn_returning(league_row, None)

        result = _call_get_away_baseline("high_away_league", db_mock)

        assert abs(result - 0.50) < 1e-9

    def test_return_type_is_float(self):
        """Return value must be a Python float."""
        league_row = _make_row({"away_rate": 0.38})
        db_mock = _db_conn_returning(league_row, None)

        result = _call_get_away_baseline("type_check_league", db_mock)

        assert isinstance(result, float), f"Expected float, got {type(result)}"


# ---------------------------------------------------------------------------
# Tests: no league row → global fallback
# ---------------------------------------------------------------------------

class TestGetAwayBaselineGlobalFallback:
    """No league-specific row but global rows present → returns global average (R6.3)."""

    def test_returns_global_average_when_no_league_row(self):
        """League row absent → returns global AVG(away_rate)."""
        global_row = _make_row({"avg_away": 0.42})
        db_mock = _db_conn_returning(None, global_row)

        result = _call_get_away_baseline("unknown_league", db_mock)

        assert abs(result - 0.42) < 1e-9, f"Expected global average 0.42, got {result}"

    def test_global_average_with_different_value(self):
        """Global average of 0.29 is returned as-is."""
        global_row = _make_row({"avg_away": 0.29})
        db_mock = _db_conn_returning(None, global_row)

        result = _call_get_away_baseline("another_league", db_mock)

        assert abs(result - 0.29) < 1e-9

    def test_global_fallback_return_type_is_float(self):
        """Global fallback return must be a Python float."""
        global_row = _make_row({"avg_away": 0.42})
        db_mock = _db_conn_returning(None, global_row)

        result = _call_get_away_baseline("type_league", db_mock)

        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# Tests: no rows at all → hardcoded default 0.54
# ---------------------------------------------------------------------------

class TestGetAwayBaselineHardcodedDefault:
    """No rows at all in league_outcome_distribution → returns 0.54 (R6.4)."""

    def test_returns_054_when_no_rows(self):
        """Both queries return None → hardcoded default 0.54 is returned."""
        db_mock = _db_conn_returning(None, None)

        result = _call_get_away_baseline("empty_db_league", db_mock)

        assert abs(result - 0.54) < 1e-9, f"Expected 0.54, got {result}"

    def test_returns_054_when_global_avg_is_none(self):
        """Global query returns a row but avg_away is NULL (empty table) → 0.54."""
        global_row = _make_row({"avg_away": None})
        db_mock = _db_conn_returning(None, global_row)

        result = _call_get_away_baseline("no_data_league", db_mock)

        assert abs(result - 0.54) < 1e-9, (
            "NULL avg_away (empty table) must fall back to 0.54"
        )

    def test_hardcoded_default_return_type_is_float(self):
        """0.54 default return must be a Python float."""
        db_mock = _db_conn_returning(None, None)

        result = _call_get_away_baseline("type_check", db_mock)

        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# Tests: DB exception handling
# ---------------------------------------------------------------------------

class TestGetAwayBaselineDbException:
    """DB errors are caught and the hardcoded fallback is returned (R6.4)."""

    def test_operational_error_returns_054(self):
        """OperationalError (e.g. missing table) → returns 0.54, no exception raised."""
        db_mock = _db_conn_raising()

        result = _call_get_away_baseline("broken_league", db_mock)

        assert abs(result - 0.54) < 1e-9, f"Expected 0.54 on error, got {result}"

    def test_general_exception_returns_054(self):
        """Any unexpected Exception → returns 0.54, no exception propagates."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = Exception("unexpected DB failure")
        db_mock = MagicMock(return_value=mock_conn)

        result = _call_get_away_baseline("exception_league", db_mock)

        assert abs(result - 0.54) < 1e-9

    def test_no_exception_raised_on_db_error(self):
        """_get_away_baseline() must not raise even when the DB explodes."""
        db_mock = _db_conn_raising()

        try:
            _call_get_away_baseline("safe_league", db_mock)
        except Exception as exc:
            raise AssertionError(
                f"_get_away_baseline() raised an unexpected exception: {exc}"
            ) from exc
