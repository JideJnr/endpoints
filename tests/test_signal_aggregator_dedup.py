"""Unit tests for SignalAggregator cross-source signal deduplication (R3).

Covers:
  - Case 1: same category, different sources → only the stronger source's signals are retained.
  - Case 2: same category, same source → all signals are retained.
  - Case 3: dropped_duplicate_count is correctly reflected in calculate_probabilities() return value.
  - Edge: three sources, the winning source keeps all its signals while the other two are dropped.
  - Edge: signals with no category conflict pass through untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Helpers to build minimal signal dicts that add_signals() understands
# ---------------------------------------------------------------------------

def _sig(name: str, impact: float, source: str) -> dict:
    """Build a minimal signal dict in the format expected by add_signals()."""
    return {"name": name, "impact": impact, "source": source}


# ---------------------------------------------------------------------------
# We patch the DB / storage dependencies so the tests are unit-level and
# don't require a real SQLite database.
# ---------------------------------------------------------------------------

def _make_aggregator(league_key: str = "__global__"):
    """Return a SignalAggregator with all DB calls stubbed out."""
    with patch("app.enrichment.signal_aggregator._init_db"), \
         patch("app.storage.db._init_db"), \
         patch("app.enrichment.signal_aggregator.db_conn") as mock_db_conn:
        # Make the context manager return a connection that yields no rows.
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_db_conn.return_value = mock_conn

        from app.enrichment.signal_aggregator import SignalAggregator
        agg = SignalAggregator(league_key=league_key)
    return agg


# ---------------------------------------------------------------------------
# Case 1: same category, different sources → stronger source wins
# ---------------------------------------------------------------------------

class TestCrosSourceDedup:
    """Cross-source signals in the same category: only the stronger survives."""

    def test_weaker_source_is_dropped(self):
        """The signal from the source with the lower abs(strength) is discarded."""
        from app.enrichment.signal_aggregator import SignalAggregator
        agg = SignalAggregator(league_key="__global__")

        # Both map to 'home_form' category.
        # 'source_a' has impact=0.8 (strong), 'source_b' has impact=0.3 (weak).
        # source_a should win.
        signals = [
            _sig("home_form", 0.8, "source_a"),   # strong
            _sig("home_form", 0.3, "source_b"),   # weak — should be dropped
        ]
        agg.add_signals(signals)

        sources_kept = {s.get("source") for s in agg.signals}
        assert "source_a" in sources_kept, "Stronger source should be retained"
        assert "source_b" not in sources_kept, "Weaker source should be dropped"
        assert agg._dropped_duplicates == 1

    def test_stronger_away_source_wins(self):
        """Works symmetrically for away-category signals."""
        from app.enrichment.signal_aggregator import SignalAggregator
        agg = SignalAggregator(league_key="__global__")

        signals = [
            _sig("away_form", 0.2, "weak_src"),
            _sig("away_form", 0.9, "strong_src"),
        ]
        agg.add_signals(signals)

        sources_kept = {s.get("source") for s in agg.signals}
        assert "strong_src" in sources_kept
        assert "weak_src" not in sources_kept
        assert agg._dropped_duplicates == 1

    def test_total_signal_count_is_one_per_winning_source(self):
        """When sources each contribute one signal to a category, exactly one signal survives."""
        from app.enrichment.signal_aggregator import SignalAggregator
        agg = SignalAggregator(league_key="__global__")

        agg.add_signals([
            _sig("home_form", 0.6, "src1"),
            _sig("home_form", 0.4, "src2"),
        ])
        assert len(agg.signals) == 1


# ---------------------------------------------------------------------------
# Case 2: same category, same source → all signals are retained
# ---------------------------------------------------------------------------

class TestSameSourcePreservation:
    """Dedup is cross-source only; same-source signals all pass through (R3.5)."""

    def test_same_source_both_kept(self):
        """Two signals with the same category and same source must both survive."""
        from app.enrichment.signal_aggregator import SignalAggregator
        agg = SignalAggregator(league_key="__global__")

        signals = [
            _sig("home_form", 0.8, "my_source"),
            _sig("home_form", 0.3, "my_source"),
        ]
        agg.add_signals(signals)

        assert len(agg.signals) == 2, "Both same-source signals should be kept"
        assert agg._dropped_duplicates == 0

    def test_same_source_three_signals_all_kept(self):
        """Three same-category same-source signals all survive."""
        from app.enrichment.signal_aggregator import SignalAggregator
        agg = SignalAggregator(league_key="__global__")

        signals = [
            _sig("home_form", 0.9, "only_src"),
            _sig("home_form", 0.5, "only_src"),
            _sig("home_form", 0.1, "only_src"),
        ]
        agg.add_signals(signals)

        assert len(agg.signals) == 3
        assert agg._dropped_duplicates == 0

    def test_same_source_across_categories_all_kept(self):
        """Same source, different categories → all signals kept."""
        from app.enrichment.signal_aggregator import SignalAggregator
        agg = SignalAggregator(league_key="__global__")

        signals = [
            _sig("home_form",  0.7, "src"),
            _sig("away_form",  0.7, "src"),
            _sig("home_table", 0.7, "src"),
        ]
        agg.add_signals(signals)

        assert len(agg.signals) == 3
        assert agg._dropped_duplicates == 0


# ---------------------------------------------------------------------------
# Mixed: winning source keeps ALL its signals; other sources are fully dropped
# ---------------------------------------------------------------------------

class TestWinningSourceKeepsAll:
    """When a source wins, every signal it contributed is retained."""

    def test_winning_source_multiple_signals_all_kept(self):
        """Winning source contributed 2 signals → both are in agg.signals."""
        from app.enrichment.signal_aggregator import SignalAggregator
        agg = SignalAggregator(league_key="__global__")

        # winning_src contributes two home_form signals; loser_src contributes one.
        signals = [
            _sig("home_form", 0.9, "winning_src"),  # strongest
            _sig("home_form", 0.7, "winning_src"),  # second from same source
            _sig("home_form", 0.5, "loser_src"),    # should be dropped
        ]
        agg.add_signals(signals)

        sources_kept = [s.get("source") for s in agg.signals]
        assert sources_kept.count("winning_src") == 2
        assert "loser_src" not in sources_kept
        assert agg._dropped_duplicates == 1

    def test_three_sources_only_strongest_survives(self):
        """Three sources for the same category: only the source with the highest peak strength wins."""
        from app.enrichment.signal_aggregator import SignalAggregator
        agg = SignalAggregator(league_key="__global__")

        signals = [
            _sig("away_form", 0.3, "low"),
            _sig("away_form", 0.9, "high"),
            _sig("away_form", 0.6, "mid"),
        ]
        agg.add_signals(signals)

        sources_kept = {s.get("source") for s in agg.signals}
        assert sources_kept == {"high"}
        assert agg._dropped_duplicates == 2


# ---------------------------------------------------------------------------
# Case 3: dropped_duplicate_count in calculate_probabilities() return value
# ---------------------------------------------------------------------------

class TestDroppedDuplicateCountInResult:
    """calculate_probabilities() must expose dropped_duplicate_count (R3.4)."""

    def _make_agg_with_stubs(self):
        """Create an aggregator with all DB calls mocked out."""
        from unittest.mock import patch, MagicMock
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_conn.execute.return_value.fetchall.return_value = []

        patches = [
            patch("app.enrichment.signal_aggregator.db_conn", return_value=mock_conn),
            patch("app.enrichment.signal_aggregator._init_db"),
            patch("app.storage.league_memory.weighted_signal_combination_memory",
                  return_value={"samples": 0, "win_rate": None, "adjustment": 0,
                                "probability_adjustment": 0.0}),
        ]
        return patches

    def test_count_is_zero_when_no_dedup(self):
        """No deduplication → dropped_duplicate_count == 0."""
        patches = self._make_agg_with_stubs()
        with patches[0], patches[1], patches[2]:
            from app.enrichment.signal_aggregator import SignalAggregator
            agg = SignalAggregator(league_key="__global__")
            agg.add_signals([
                _sig("home_form", 0.8, "src_a"),
                _sig("away_form", 0.7, "src_b"),
            ])
            result = agg.calculate_probabilities()

        assert "dropped_duplicate_count" in result
        assert result["dropped_duplicate_count"] == 0

    def test_count_reflects_dropped_signals(self):
        """One cross-source drop → dropped_duplicate_count == 1 in the result."""
        patches = self._make_agg_with_stubs()
        with patches[0], patches[1], patches[2]:
            from app.enrichment.signal_aggregator import SignalAggregator
            agg = SignalAggregator(league_key="__global__")
            agg.add_signals([
                _sig("home_form", 0.9, "strong_src"),
                _sig("home_form", 0.3, "weak_src"),  # dropped
            ])
            result = agg.calculate_probabilities()

        assert result["dropped_duplicate_count"] == 1

    def test_count_reflects_multiple_drops(self):
        """Two cross-source drops → dropped_duplicate_count == 2 in the result."""
        patches = self._make_agg_with_stubs()
        with patches[0], patches[1], patches[2]:
            from app.enrichment.signal_aggregator import SignalAggregator
            agg = SignalAggregator(league_key="__global__")
            # home_form: 3 sources, 2 are dropped → count +2
            agg.add_signals([
                _sig("home_form", 0.9, "src_a"),
                _sig("home_form", 0.5, "src_b"),   # dropped
                _sig("home_form", 0.3, "src_c"),   # dropped
            ])
            result = agg.calculate_probabilities()

        assert result["dropped_duplicate_count"] == 2

    def test_same_source_dedup_not_counted(self):
        """Same-source signals don't contribute to dropped_duplicate_count."""
        patches = self._make_agg_with_stubs()
        with patches[0], patches[1], patches[2]:
            from app.enrichment.signal_aggregator import SignalAggregator
            agg = SignalAggregator(league_key="__global__")
            agg.add_signals([
                _sig("home_form", 0.8, "only_src"),
                _sig("home_form", 0.4, "only_src"),
            ])
            result = agg.calculate_probabilities()

        assert result["dropped_duplicate_count"] == 0

    def test_key_always_present_even_when_empty(self):
        """dropped_duplicate_count key must exist even when no signals are added."""
        patches = self._make_agg_with_stubs()
        with patches[0], patches[1], patches[2]:
            from app.enrichment.signal_aggregator import SignalAggregator
            agg = SignalAggregator(league_key="__global__")
            agg.add_signals([_sig("home_form", 0.7, "s")])
            result = agg.calculate_probabilities()

        assert "dropped_duplicate_count" in result


# ---------------------------------------------------------------------------
# No-conflict signals pass through untouched
# ---------------------------------------------------------------------------

class TestNoConflictPassThrough:
    """Signals in different categories are never deduplicated against each other."""

    def test_different_categories_all_kept(self):
        """Different categories, different sources → all signals pass through."""
        from app.enrichment.signal_aggregator import SignalAggregator
        agg = SignalAggregator(league_key="__global__")

        signals = [
            _sig("home_form",  0.8, "form_source"),
            _sig("away_table", 0.7, "table_source"),
            _sig("h2h_draw",   0.5, "h2h_source"),
        ]
        agg.add_signals(signals)

        assert len(agg.signals) == 3
        assert agg._dropped_duplicates == 0
