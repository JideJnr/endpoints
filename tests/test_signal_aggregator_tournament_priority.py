"""Unit tests for SignalAggregator tournament priority confidence modifier (R15).

Covers:
  - Priority 0 → confidence increases by 0.05.
  - Priority 1 → confidence increases by 0.05.
  - Priority 7 → confidence decreases by 0.10.
  - Priority 6 → confidence decreases by 0.10.
  - Priority 4 → confidence unchanged.
  - Priority 2, 3, 5 → confidence unchanged.
  - Missing tournament_preferences table (DB error) → no error, confidence unchanged.
  - No row for league_key → no modification, treated as priority 4.
  - Confidence is clamped to [0.1, 0.95] after modification.

Requirements: R15.1, R15.2, R15.3, R15.4, R15.5, R15.6
"""
from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sig(name: str, impact: float, source: str = "src") -> dict:
    """Build a minimal signal dict accepted by add_signals()."""
    return {"name": name, "impact": impact, "source": source}


def _make_db_conn_mock(priority_row=None, raise_on_tournament_query=False):
    """
    Return a context-manager mock for db_conn.

    Each call to conn.execute().fetchone() is set up to return `priority_row`
    for the tournament_preferences query and None for everything else.

    If raise_on_tournament_query is True, the execute() call raises an
    OperationalError (simulating a missing table).
    """
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    if raise_on_tournament_query:
        mock_conn.execute.side_effect = sqlite3.OperationalError("no such table: tournament_preferences")
    else:
        # fetchone() always returns priority_row regardless of query; this is
        # fine because our modifier block is isolated in its own try/except.
        mock_conn.execute.return_value.fetchone.return_value = priority_row
        mock_conn.execute.return_value.fetchall.return_value = []

    return mock_conn


def _agg_with_priority(priority: int | None, league_key: str = "test_league"):
    """
    Create a SignalAggregator whose tournament_preferences query returns the
    given priority (or None to simulate no row).

    Returns (aggregator, base_confidence_without_modifier) so tests can check
    the delta independently.
    """
    # Build a row-like object that supports row[0] access
    if priority is not None:
        row_obj = MagicMock()
        row_obj.__getitem__ = MagicMock(return_value=priority)
        # also support row[0] via side_effect
        row_obj.__getitem__.side_effect = lambda i: priority if i == 0 else None
    else:
        row_obj = None

    mock_conn = _make_db_conn_mock(priority_row=row_obj)
    db_mock = MagicMock(return_value=mock_conn)

    with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
         patch("app.enrichment.signal_aggregator._init_db"), \
         patch("app.storage.db._init_db"), \
         patch("app.storage.league_memory.weighted_signal_combination_memory",
               return_value={"samples": 0, "win_rate": None, "adjustment": 0,
                             "probability_adjustment": 0.0}):
        from app.enrichment.signal_aggregator import SignalAggregator
        agg = SignalAggregator(league_key=league_key)

    return agg, db_mock


def _call_calculate_confidence(agg, db_mock, signals=None):
    """
    Call _calculate_confidence() with the supplied signals loaded and the
    db_conn mock active, returning the resulting confidence float.
    """
    if signals is None:
        # Provide a minimal set of signals to avoid the early-return 0.0 path
        signals = [_sig("home_form", 0.7, "s")]

    with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
         patch("app.enrichment.signal_aggregator._init_db"), \
         patch("app.storage.db._init_db"):
        for sig in signals:
            agg.add_signal(sig["name"], sig["impact"], sig.get("source", "s"))
        confidence = agg._calculate_confidence({}, {})

    return confidence


# ---------------------------------------------------------------------------
# Baseline helper — confidence without any tournament-priority modification.
#
# We derive the "no-modifier" baseline by running with priority=None (no row),
# which should produce the unmodified confidence value.
# ---------------------------------------------------------------------------

def _baseline_confidence(signals=None):
    """Return confidence for a standard set of signals with no priority row."""
    agg, db_mock = _agg_with_priority(None)
    return _call_calculate_confidence(agg, db_mock, signals)


# ---------------------------------------------------------------------------
# Tests: priority 0 and 1 → +0.05 boost
# ---------------------------------------------------------------------------

class TestHighPriorityBoost:
    """Leagues with priority 0 or 1 receive a +0.05 confidence boost (R15.2)."""

    def test_priority_0_adds_0_05(self):
        """Priority 0 → confidence should be base + 0.05 (clamped to 0.95)."""
        signals = [_sig("home_form", 0.7, "s")]
        base = _baseline_confidence(signals)

        agg, db_mock = _agg_with_priority(0)
        result = _call_calculate_confidence(agg, db_mock, signals)

        expected = min(0.95, base + 0.05)
        assert abs(result - expected) < 1e-9, (
            f"Priority 0 should add 0.05; base={base:.4f}, got={result:.4f}, expected={expected:.4f}"
        )

    def test_priority_1_adds_0_05(self):
        """Priority 1 → same +0.05 boost as priority 0."""
        signals = [_sig("home_form", 0.7, "s")]
        base = _baseline_confidence(signals)

        agg, db_mock = _agg_with_priority(1)
        result = _call_calculate_confidence(agg, db_mock, signals)

        expected = min(0.95, base + 0.05)
        assert abs(result - expected) < 1e-9, (
            f"Priority 1 should add 0.05; base={base:.4f}, got={result:.4f}, expected={expected:.4f}"
        )

    def test_boost_clamped_at_0_95(self):
        """If base confidence + 0.05 > 0.95, result is clamped to 0.95."""
        # Use many strongly agreeing signals to push raw confidence close to 0.95
        signals = [_sig("home_form", 1.0, f"s{i}") for i in range(15)]
        agg, db_mock = _agg_with_priority(0)
        result = _call_calculate_confidence(agg, db_mock, signals)
        assert result <= 0.95, f"Confidence must be clamped to 0.95, got {result}"


# ---------------------------------------------------------------------------
# Tests: priority 6 and 7 → -0.10 penalty
# ---------------------------------------------------------------------------

class TestLowPriorityPenalty:
    """Leagues with priority 6 or 7 receive a -0.10 confidence penalty (R15.3)."""

    def test_priority_7_subtracts_0_10(self):
        """Priority 7 → confidence should be base - 0.10 (clamped to 0.1)."""
        signals = [_sig("home_form", 0.7, "s")]
        base = _baseline_confidence(signals)

        agg, db_mock = _agg_with_priority(7)
        result = _call_calculate_confidence(agg, db_mock, signals)

        expected = max(0.1, base - 0.10)
        assert abs(result - expected) < 1e-9, (
            f"Priority 7 should subtract 0.10; base={base:.4f}, got={result:.4f}, expected={expected:.4f}"
        )

    def test_priority_6_subtracts_0_10(self):
        """Priority 6 → same -0.10 penalty as priority 7."""
        signals = [_sig("home_form", 0.7, "s")]
        base = _baseline_confidence(signals)

        agg, db_mock = _agg_with_priority(6)
        result = _call_calculate_confidence(agg, db_mock, signals)

        expected = max(0.1, base - 0.10)
        assert abs(result - expected) < 1e-9, (
            f"Priority 6 should subtract 0.10; base={base:.4f}, got={result:.4f}, expected={expected:.4f}"
        )

    def test_penalty_clamped_at_0_1(self):
        """If base confidence - 0.10 < 0.1, result is clamped to 0.1."""
        # Use a single weak signal with no agreement to produce very low base confidence
        signals = [_sig("home_form", 0.01, "s")]
        agg, db_mock = _agg_with_priority(7)
        result = _call_calculate_confidence(agg, db_mock, signals)
        assert result >= 0.1, f"Confidence must be clamped to 0.1, got {result}"


# ---------------------------------------------------------------------------
# Tests: priority 2–5 → no modification (R15.4)
# ---------------------------------------------------------------------------

class TestNeutralPriorities:
    """Priorities 2–5 must not change confidence (R15.4)."""

    def _check_neutral(self, priority: int):
        signals = [_sig("home_form", 0.7, "s")]
        base = _baseline_confidence(signals)

        agg, db_mock = _agg_with_priority(priority)
        result = _call_calculate_confidence(agg, db_mock, signals)

        assert abs(result - base) < 1e-9, (
            f"Priority {priority} should leave confidence unchanged; "
            f"base={base:.4f}, got={result:.4f}"
        )

    def test_priority_2_no_change(self):
        self._check_neutral(2)

    def test_priority_3_no_change(self):
        self._check_neutral(3)

    def test_priority_4_no_change(self):
        """Priority 4 (default/neutral) must leave confidence unchanged."""
        self._check_neutral(4)

    def test_priority_5_no_change(self):
        self._check_neutral(5)


# ---------------------------------------------------------------------------
# Tests: no row for league_key → no modification (R15.5)
# ---------------------------------------------------------------------------

class TestNoRowForLeague:
    """When tournament_preferences has no row for the league, confidence is unchanged."""

    def test_no_row_treated_as_neutral(self):
        """Missing league row → no modifier applied (treated as priority 4)."""
        signals = [_sig("home_form", 0.7, "s")]
        base = _baseline_confidence(signals)

        agg, db_mock = _agg_with_priority(None)  # None = fetchone returns None
        result = _call_calculate_confidence(agg, db_mock, signals)

        assert abs(result - base) < 1e-9, (
            f"Missing row should not change confidence; base={base:.4f}, got={result:.4f}"
        )


# ---------------------------------------------------------------------------
# Tests: DB error (missing table or any exception) → no error, confidence unchanged
# ---------------------------------------------------------------------------

class TestDatabaseErrorHandling:
    """Any exception during the tournament_preferences query must be silently swallowed (R15.5)."""

    def test_missing_table_no_exception(self):
        """OperationalError from missing table → method does not raise."""
        signals = [_sig("home_form", 0.7, "s")]

        mock_conn = _make_db_conn_mock(raise_on_tournament_query=True)
        db_mock = MagicMock(return_value=mock_conn)

        with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
             patch("app.enrichment.signal_aggregator._init_db"), \
             patch("app.storage.db._init_db"):
            from app.enrichment.signal_aggregator import SignalAggregator
            agg = SignalAggregator(league_key="missing_league")
            for sig in signals:
                agg.add_signal(sig["name"], sig["impact"], sig.get("source", "s"))
            # Must not raise
            result = agg._calculate_confidence({}, {})

        assert isinstance(result, float), "Should return a float even on DB error"

    def test_missing_table_confidence_unchanged(self):
        """OperationalError → confidence equals the no-modifier baseline."""
        signals = [_sig("home_form", 0.7, "s")]
        base = _baseline_confidence(signals)

        mock_conn = _make_db_conn_mock(raise_on_tournament_query=True)
        db_mock = MagicMock(return_value=mock_conn)

        with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
             patch("app.enrichment.signal_aggregator._init_db"), \
             patch("app.storage.db._init_db"):
            from app.enrichment.signal_aggregator import SignalAggregator
            agg = SignalAggregator(league_key="missing_league")
            for sig in signals:
                agg.add_signal(sig["name"], sig["impact"], sig.get("source", "s"))
            result = agg._calculate_confidence({}, {})

        assert abs(result - base) < 1e-9, (
            f"DB error should not change confidence; base={base:.4f}, got={result:.4f}"
        )

    def test_generic_exception_no_raise(self):
        """Any non-DB exception during priority lookup is also swallowed."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = RuntimeError("unexpected error")
        db_mock = MagicMock(return_value=mock_conn)

        with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
             patch("app.enrichment.signal_aggregator._init_db"), \
             patch("app.storage.db._init_db"):
            from app.enrichment.signal_aggregator import SignalAggregator
            agg = SignalAggregator(league_key="err_league")
            agg.add_signal("home_form", 0.7, "s")
            # Must not raise
            result = agg._calculate_confidence({}, {})

        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# Integration-style: verify modifier values are exactly ±0.05/0.10
# ---------------------------------------------------------------------------

class TestModifierMagnitude:
    """Verify the exact modifier magnitudes as specified in R15.2 and R15.3."""

    def test_boost_delta_is_exactly_0_05(self):
        """Priority 0 boost must be exactly 0.05 (before clamping)."""
        signals = [_sig("home_form", 0.5, "s")]
        base = _baseline_confidence(signals)

        agg, db_mock = _agg_with_priority(0)
        result = _call_calculate_confidence(agg, db_mock, signals)

        # Only check delta when clamping doesn't interfere
        if base + 0.05 <= 0.95:
            assert abs(result - (base + 0.05)) < 1e-9

    def test_penalty_delta_is_exactly_0_10(self):
        """Priority 7 penalty must be exactly 0.10 (before clamping)."""
        signals = [_sig("home_form", 0.5, "s")]
        base = _baseline_confidence(signals)

        agg, db_mock = _agg_with_priority(7)
        result = _call_calculate_confidence(agg, db_mock, signals)

        # Only check delta when clamping doesn't interfere
        if base - 0.10 >= 0.1:
            assert abs(result - (base - 0.10)) < 1e-9

    def test_boost_and_penalty_are_not_swapped(self):
        """Priority 0 must produce a higher confidence than priority 7."""
        signals = [_sig("home_form", 0.6, "s")]

        agg0, db_mock0 = _agg_with_priority(0)
        result0 = _call_calculate_confidence(agg0, db_mock0, signals)

        agg7, db_mock7 = _agg_with_priority(7)
        result7 = _call_calculate_confidence(agg7, db_mock7, signals)

        assert result0 > result7, (
            f"Priority 0 should yield higher confidence than priority 7; "
            f"got {result0:.4f} vs {result7:.4f}"
        )
