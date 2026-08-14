"""Unit tests for _get_base_probs() and its integration with calculate_probabilities() (R7).

Covers:
  - League row with known rates in league_outcome_distribution → returns those rates and
    base_probs_source == 'learned'.
  - No league-specific row but global rows present → global fallback path and
    base_probs_source == 'global_fallback'.
  - No rows at all → static fallback and base_probs_source == 'static_fallback'.
  - calculate_probabilities() mixed-signal branch exposes 'base_probs_source' in the return dict.
  - DB exception → static fallback without raising.

Requirements: R7.1, R7.2, R7.3, R7.4, R7.5
"""
from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
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
    # Make truthiness work: a row object is always truthy (mirrors sqlite3.Row)
    row.__bool__ = MagicMock(return_value=True)
    return row


def _db_conn_returning(league_row, global_row):
    """
    Build a db_conn context-manager mock whose execute().fetchone() returns
    `league_row` on the first call and `global_row` on the second call.
    This mirrors the two-query lookup inside _get_base_probs().
    """
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    # Use side_effect list so first call → league_row, second → global_row
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


def _sig(name: str, impact: float, source: str = "src") -> dict:
    return {"name": name, "impact": impact, "source": source}


def _make_agg_with_db(db_mock, league_key: str = "test_league"):
    """Create a SignalAggregator using the supplied db_conn mock."""
    with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
         patch("app.enrichment.signal_aggregator._init_db"), \
         patch("app.storage.db._init_db"):
        from app.enrichment.signal_aggregator import SignalAggregator
        return SignalAggregator(league_key=league_key)


# ---------------------------------------------------------------------------
# Tests: _get_base_probs() directly
# ---------------------------------------------------------------------------

class TestGetBaseProbs:
    """Direct unit tests for the _get_base_probs() module-level function."""

    def test_learned_path_returns_league_rates(self):
        """League row present with samples >= 20 → returns that row's rates and 'learned'."""
        league_row = _make_row({"home_rate": 0.38, "draw_rate": 0.32, "away_rate": 0.30})
        db_mock = _db_conn_returning(league_row, None)

        with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
             patch("app.enrichment.signal_aggregator._init_db"):
            from app.enrichment.signal_aggregator import _get_base_probs
            home, draw, away, source = _get_base_probs("test_league")

        assert abs(home - 0.38) < 1e-9, f"Expected home_rate=0.38, got {home}"
        assert abs(draw - 0.32) < 1e-9, f"Expected draw_rate=0.32, got {draw}"
        assert abs(away - 0.30) < 1e-9, f"Expected away_rate=0.30, got {away}"
        assert source == "learned", f"Expected source='learned', got {source!r}"

    def test_global_fallback_when_no_league_row(self):
        """No league-specific row but global rows present → 'global_fallback'."""
        # First query (league-specific) → None; second query (global avg) → a row
        global_row = _make_row({"home_rate": 0.44, "draw_rate": 0.28, "away_rate": 0.28})
        db_mock = _db_conn_returning(None, global_row)

        with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
             patch("app.enrichment.signal_aggregator._init_db"):
            from app.enrichment.signal_aggregator import _get_base_probs
            home, draw, away, source = _get_base_probs("unknown_league")

        assert abs(home - 0.44) < 1e-9, f"Expected home_rate=0.44, got {home}"
        assert abs(draw - 0.28) < 1e-9, f"Expected draw_rate=0.28, got {draw}"
        assert abs(away - 0.28) < 1e-9, f"Expected away_rate=0.28, got {away}"
        assert source == "global_fallback", f"Expected 'global_fallback', got {source!r}"

    def test_static_fallback_when_no_rows_at_all(self):
        """No rows in league_outcome_distribution → static constants and 'static_fallback'."""
        # global_row has home_rate = None (empty table → AVG returns NULL)
        global_row = _make_row({"home_rate": None, "draw_rate": None, "away_rate": None})
        db_mock = _db_conn_returning(None, global_row)

        with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
             patch("app.enrichment.signal_aggregator._init_db"):
            from app.enrichment.signal_aggregator import _get_base_probs
            home, draw, away, source = _get_base_probs("no_data_league")

        assert abs(home - 0.45) < 1e-9, f"Expected static home=0.45, got {home}"
        assert abs(draw - 0.30) < 1e-9, f"Expected static draw=0.30, got {draw}"
        assert abs(away - 0.25) < 1e-9, f"Expected static away=0.25, got {away}"
        assert source == "static_fallback", f"Expected 'static_fallback', got {source!r}"

    def test_static_fallback_when_global_query_returns_none(self):
        """Both queries return None → static fallback."""
        db_mock = _db_conn_returning(None, None)

        with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
             patch("app.enrichment.signal_aggregator._init_db"):
            from app.enrichment.signal_aggregator import _get_base_probs
            home, draw, away, source = _get_base_probs("empty_league")

        assert source == "static_fallback"
        assert abs(home - 0.45) < 1e-9
        assert abs(draw - 0.30) < 1e-9
        assert abs(away - 0.25) < 1e-9

    def test_db_exception_falls_back_to_static(self):
        """OperationalError → static fallback, no exception raised."""
        db_mock = _db_conn_raising()

        with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
             patch("app.enrichment.signal_aggregator._init_db"):
            from app.enrichment.signal_aggregator import _get_base_probs
            home, draw, away, source = _get_base_probs("broken_league")

        assert source == "static_fallback"
        assert abs(home - 0.45) < 1e-9
        assert abs(away - 0.25) < 1e-9

    def test_return_type_is_tuple_of_three_floats_and_str(self):
        """Return value must be (float, float, float, str) in all cases."""
        db_mock = _db_conn_returning(None, None)

        with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
             patch("app.enrichment.signal_aggregator._init_db"):
            from app.enrichment.signal_aggregator import _get_base_probs
            result = _get_base_probs("any_league")

        assert len(result) == 4
        home, draw, away, source = result
        assert isinstance(home, float)
        assert isinstance(draw, float)
        assert isinstance(away, float)
        assert isinstance(source, str)

    def test_learned_rates_are_exact_float_conversion(self):
        """Values stored as non-standard floats are returned unchanged."""
        league_row = _make_row({"home_rate": 0.4123456789, "draw_rate": 0.3000000001, "away_rate": 0.2876543210})
        db_mock = _db_conn_returning(league_row, None)

        with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
             patch("app.enrichment.signal_aggregator._init_db"):
            from app.enrichment.signal_aggregator import _get_base_probs
            home, draw, away, source = _get_base_probs("precise_league")

        assert abs(home - 0.4123456789) < 1e-9
        assert source == "learned"


# ---------------------------------------------------------------------------
# Tests: base_probs_source key in calculate_probabilities() return dict
# ---------------------------------------------------------------------------

class TestBaseProbsSourceInCalculateProbabilities:
    """calculate_probabilities() must always include 'base_probs_source' in its return dict (R7.5)."""

    def _combo_memory_mock(self):
        return {"samples": 0, "win_rate": None, "adjustment": 0, "probability_adjustment": 0.0}

    def _run_calculate_probs(self, signals, db_mock, league_key="test_league"):
        """Run calculate_probabilities() with the given signals and db mock."""
        with patch("app.enrichment.signal_aggregator.db_conn", db_mock), \
             patch("app.enrichment.signal_aggregator._init_db"), \
             patch("app.storage.db._init_db"), \
             patch("app.storage.league_memory.weighted_signal_combination_memory",
                   return_value=self._combo_memory_mock()):
            from app.enrichment.signal_aggregator import SignalAggregator
            agg = SignalAggregator(league_key=league_key)
            agg.add_signals(signals)
            return agg.calculate_probabilities()

    def test_key_always_present_in_result(self):
        """'base_probs_source' must be in the result dict regardless of signal mix."""
        # Mixed signals (one home, one away) → triggers the else branch
        signals = [
            _sig("home_form", 0.6, "src_a"),
            _sig("away_form", 0.4, "src_b"),
        ]
        db_mock = _db_conn_returning(None, None)  # static fallback path
        result = self._run_calculate_probs(signals, db_mock)

        assert "base_probs_source" in result, "'base_probs_source' key must always be present"

    def test_learned_source_when_league_row_present(self):
        """Mixed-signal branch uses learned rates when league row is available."""
        signals = [
            _sig("home_form", 0.5, "src_a"),
            _sig("away_form", 0.5, "src_b"),
        ]
        league_row = _make_row({"home_rate": 0.40, "draw_rate": 0.33, "away_rate": 0.27})
        db_mock = _db_conn_returning(league_row, None)

        result = self._run_calculate_probs(signals, db_mock)

        assert result.get("base_probs_source") == "learned", (
            f"Expected 'learned', got {result.get('base_probs_source')!r}"
        )

    def test_global_fallback_source_in_result(self):
        """Mixed-signal branch records 'global_fallback' when using global averages."""
        signals = [
            _sig("home_form", 0.5, "src_a"),
            _sig("away_form", 0.5, "src_b"),
        ]
        global_row = _make_row({"home_rate": 0.44, "draw_rate": 0.29, "away_rate": 0.27})
        db_mock = _db_conn_returning(None, global_row)

        result = self._run_calculate_probs(signals, db_mock)

        assert result.get("base_probs_source") == "global_fallback", (
            f"Expected 'global_fallback', got {result.get('base_probs_source')!r}"
        )

    def test_static_fallback_source_in_result(self):
        """Mixed-signal branch records 'static_fallback' when no DB rows exist."""
        signals = [
            _sig("home_form", 0.5, "src_a"),
            _sig("away_form", 0.5, "src_b"),
        ]
        db_mock = _db_conn_returning(None, None)

        result = self._run_calculate_probs(signals, db_mock)

        assert result.get("base_probs_source") == "static_fallback", (
            f"Expected 'static_fallback', got {result.get('base_probs_source')!r}"
        )

    def test_learned_base_probs_influence_probabilities(self):
        """When learned rates differ from static defaults, the output probabilities must differ."""
        signals = [
            _sig("home_form", 0.4, "src_a"),
            _sig("away_form", 0.6, "src_b"),
        ]

        # Static fallback run
        static_db = _db_conn_returning(None, None)
        static_result = self._run_calculate_probs(signals, static_db)

        # Learned run — use very different rates to make the difference detectable
        league_row = _make_row({"home_rate": 0.55, "draw_rate": 0.20, "away_rate": 0.25})
        learned_db = _db_conn_returning(league_row, None)
        learned_result = self._run_calculate_probs(signals, learned_db)

        assert learned_result["home_prob"] != static_result["home_prob"] or \
               learned_result["draw_prob"] != static_result["draw_prob"] or \
               learned_result["away_prob"] != static_result["away_prob"], (
            "Learned base probs should produce different output than static defaults"
        )

    def test_all_favor_away_branch_also_has_base_probs_source(self):
        """Even when all-favor-away branch is taken, base_probs_source is present."""
        # All signals favor away
        signals = [
            _sig("away_form", 0.8, "src_a"),
            _sig("away_form", 0.7, "src_b"),
        ]
        db_mock = _db_conn_returning(None, None)

        result = self._run_calculate_probs(signals, db_mock)

        assert "base_probs_source" in result

    def test_all_favor_home_branch_also_has_base_probs_source(self):
        """Even when all-favor-home branch is taken, base_probs_source is present."""
        signals = [
            _sig("home_form", 0.8, "src_a"),
            _sig("home_form", 0.7, "src_b"),
        ]
        db_mock = _db_conn_returning(None, None)

        result = self._run_calculate_probs(signals, db_mock)

        assert "base_probs_source" in result

    def test_base_probs_source_valid_value(self):
        """base_probs_source must be one of the three valid string values."""
        valid_sources = {"learned", "global_fallback", "static_fallback"}
        signals = [
            _sig("home_form", 0.5, "src_a"),
            _sig("away_form", 0.5, "src_b"),
        ]
        db_mock = _db_conn_returning(None, None)

        result = self._run_calculate_probs(signals, db_mock)

        assert result.get("base_probs_source") in valid_sources, (
            f"base_probs_source must be one of {valid_sources}, "
            f"got {result.get('base_probs_source')!r}"
        )
