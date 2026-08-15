"""
Tests for the MIN_COMBINATION_SAMPLES guard in _learn_signal_combinations()
---------------------------------------------------------------------------
R23.1  Combinations with fewer than 12 samples are not written to
       signal_combination_memory.
R23.2  Exactly 12 samples triggers a write (boundary condition).
R23.3  Combinations with > 12 samples write as before.
R23.4  Existing rows for combinations with < 12 samples are NOT deleted
       when the learning cycle runs with insufficient new data.
R23.5  MIN_COMBINATION_SAMPLES is defined at module level as the integer 12,
       distinct from MIN_SAMPLES (15) and MIN_LEAGUE_SAMPLES (5).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.monitoring.self_learner import (
    MIN_COMBINATION_SAMPLES,
    MIN_LEAGUE_SAMPLES,
    MIN_SAMPLES,
    _init_learner_tables,
    _learn_signal_combinations,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    """In-memory SQLite connection with all learner tables created."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_learner_tables(conn)
    return conn


def _make_rows(
    n: int,
    *,
    league_name: str = "Premier League",
    pick_type: str = "match_result",
    selection: str = "home",
    result: str = "win",
    signals: list[dict] | None = None,
    confidence: float = 65.0,
) -> list[sqlite3.Row]:
    """
    Return a list of n sqlite3.Row objects representing graded predictions.
    All rows share the same league / pick_type / selection / signals so that
    build_signal_combination() produces the same combination key every time,
    meaning they all fall into one bucket inside _learn_signal_combinations().
    """
    if signals is None:
        signals = [
            {"name": "home_form", "strength": 0.6},
            {"name": "away_form", "strength": -0.3},
        ]

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        create table rows (
            match_id text,
            league_name text,
            pick_type text,
            selection text,
            result text,
            signals_json text,
            confidence real
        )
    """)
    for i in range(n):
        conn.execute(
            "insert into rows values (?,?,?,?,?,?,?)",
            (
                f"m_{i}",
                league_name,
                pick_type,
                selection,
                result,
                json.dumps(signals),
                confidence,
            ),
        )
    return conn.execute("select * from rows").fetchall()


def _count_combo_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("select count(*) from signal_combination_memory").fetchone()[0]


# ── R23.5 constant value ───────────────────────────────────────────────────────

class TestConstantDefinition:
    """MIN_COMBINATION_SAMPLES is a distinct module-level integer equal to 12."""

    def test_value_is_12(self):
        assert MIN_COMBINATION_SAMPLES == 12

    def test_distinct_from_min_samples(self):
        assert MIN_COMBINATION_SAMPLES != MIN_SAMPLES

    def test_distinct_from_min_league_samples(self):
        assert MIN_COMBINATION_SAMPLES != MIN_LEAGUE_SAMPLES

    def test_is_integer(self):
        assert isinstance(MIN_COMBINATION_SAMPLES, int)


# ── R23.1  fewer than 12 samples → no write ───────────────────────────────────

class TestBelowThreshold:
    """Combinations with 1–11 samples must NOT be written."""

    @pytest.mark.parametrize("n", [1, 5, 10, 11])
    def test_no_write_when_n_below_threshold(self, n):
        conn = _make_conn()
        rows = _make_rows(n)
        written = _learn_signal_combinations(conn, rows)
        assert written == 0, f"Expected 0 writes for {n} samples, got {written}"
        assert _count_combo_rows(conn) == 0

    def test_exactly_11_samples_not_written(self):
        conn = _make_conn()
        rows = _make_rows(11)
        _learn_signal_combinations(conn, rows)
        assert _count_combo_rows(conn) == 0


# ── R23.2  exactly 12 samples → write occurs ──────────────────────────────────

class TestAtThreshold:
    """Exactly 12 samples is the boundary — a write must occur."""

    def test_exactly_12_samples_writes_row(self):
        conn = _make_conn()
        rows = _make_rows(12)
        written = _learn_signal_combinations(conn, rows)
        # Each league produces two buckets: __global__ + league_key
        assert written >= 1
        assert _count_combo_rows(conn) >= 1

    def test_return_value_positive_at_threshold(self):
        conn = _make_conn()
        rows = _make_rows(12)
        written = _learn_signal_combinations(conn, rows)
        assert written > 0

    def test_written_row_has_correct_samples_count(self):
        conn = _make_conn()
        rows = _make_rows(12)
        _learn_signal_combinations(conn, rows)
        row = conn.execute(
            "select samples from signal_combination_memory where league_key = '__global__'"
        ).fetchone()
        assert row is not None
        assert row["samples"] == 12


# ── R23.3  more than 12 samples → write occurs as before ──────────────────────

class TestAboveThreshold:
    """Combinations with 13 or more samples continue to be written normally."""

    @pytest.mark.parametrize("n", [13, 15, 20, 50])
    def test_write_occurs_above_threshold(self, n):
        conn = _make_conn()
        rows = _make_rows(n)
        written = _learn_signal_combinations(conn, rows)
        assert written >= 1
        assert _count_combo_rows(conn) >= 1

    def test_20_samples_writes_correct_win_rate(self):
        conn = _make_conn()
        # 15 wins, 5 losses → win_rate = 0.75
        wins = _make_rows(15, result="win")
        losses = _make_rows(5, result="loss")
        _learn_signal_combinations(conn, wins + losses)

        row = conn.execute(
            "select win_rate, samples from signal_combination_memory "
            "where league_key = '__global__'"
        ).fetchone()
        assert row is not None
        assert row["samples"] == 20
        assert abs(row["win_rate"] - 0.75) < 0.001


# ── R23.4  existing rows for < 12 samples are preserved ───────────────────────

class TestExistingRowsPreserved:
    """
    Rows already in signal_combination_memory for combinations that now have
    fewer than 12 samples are left untouched — no DELETE is issued.
    """

    def test_pre_existing_row_not_deleted_when_new_data_is_insufficient(self):
        conn = _make_conn()

        # Manually insert a row as if it was written by an older cycle
        # when the threshold was lower (e.g. 5).
        conn.execute("""
            insert into signal_combination_memory
                (combination_key, league_key, pick_type, selection,
                 samples, wins, losses, win_rate, avg_confidence, last_updated)
            values ('legacy_key_abc123', '__global__', 'match_result', 'home',
                    7, 5, 2, 0.714, 63.0, '2026-01-01T00:00:00')
        """)
        conn.commit()

        # Now run the learning cycle with only 5 new rows (below threshold).
        rows = _make_rows(5)
        _learn_signal_combinations(conn, rows)

        # The legacy row must still be present.
        legacy = conn.execute(
            "select samples from signal_combination_memory "
            "where combination_key = 'legacy_key_abc123'"
        ).fetchone()
        assert legacy is not None, "Legacy row was deleted — should be preserved"
        assert legacy["samples"] == 7

    def test_pre_existing_row_not_deleted_when_no_new_rows(self):
        conn = _make_conn()

        conn.execute("""
            insert into signal_combination_memory
                (combination_key, league_key, pick_type, selection,
                 samples, wins, losses, win_rate, avg_confidence, last_updated)
            values ('old_combo_key_xyz', 'premier_league', 'match_result', 'away',
                    9, 6, 3, 0.666, 70.0, '2026-02-01T00:00:00')
        """)
        conn.commit()

        # Run with an empty row list.
        _learn_signal_combinations(conn, [])

        row = conn.execute(
            "select samples from signal_combination_memory "
            "where combination_key = 'old_combo_key_xyz'"
        ).fetchone()
        assert row is not None
        assert row["samples"] == 9
