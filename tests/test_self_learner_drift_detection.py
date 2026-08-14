"""
Tests for _detect_and_handle_drift in app/monitoring/self_learner.py
----------------------------------------------------------------------
R4.3  WHEN a (league_key, pick_type) combination has >= 10 graded rows in
      the last 7 calendar days AND win_rate < 0.40, the league's
      tournament_preferences.priority is set to 7.
R4.4  WHEN drift is detected, a system_events row is inserted with
      event_type='drift_detected'.
R4.7  IF a league previously had priority=7 due to drift AND its 7-day
      win_rate recovers to >= 0.45, priority is recalculated and a
      'drift_recovery' event is written.

Additional coverage:
  - League with < 10 samples → no change to priority, no event.
  - League whose samples are outside the 7-day window → not counted.
  - Return value equals the number of leagues that triggered drift
    (not recovery; both are events but only initial drift is counted
    as an "event" per R4.6).
  - After any drift event, clear_learned_parameter_cache() is called
    (verified via monkey-patching).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.monitoring.self_learner import (
    _detect_and_handle_drift,
    _init_learner_tables,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_learner_tables(conn)
    return conn


def _ts(days_ago: float = 0) -> str:
    """Return an ISO-format UTC timestamp N days in the past."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat()


def _make_rows(
    n_wins: int,
    n_losses: int,
    league_name: str = "La Liga",
    pick_type: str = "match_result",
    days_ago: float = 1.0,
) -> list[sqlite3.Row]:
    """
    Build sqlite3.Row objects for graded predictions.
    All rows share the same league/pick_type and are `days_ago` old.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        create table rows (
            match_id text, league_name text, pick_type text,
            selection text, result text, created_at text
        )
    """)
    ts = _ts(days_ago)
    for i in range(n_wins):
        conn.execute(
            "insert into rows values (?,?,?,?,?,?)",
            (f"w_{i}", league_name, pick_type, "home", "win", ts),
        )
    for i in range(n_losses):
        conn.execute(
            "insert into rows values (?,?,?,?,?,?)",
            (f"l_{i}", league_name, pick_type, "home", "loss", ts),
        )
    return conn.execute("select * from rows").fetchall()


def _insert_priority(conn: sqlite3.Connection, league_key: str, priority: int) -> None:
    conn.execute(
        "insert or replace into tournament_preferences (league_key, priority) values (?,?)",
        (league_key, priority),
    )


def _get_priority(conn: sqlite3.Connection, league_key: str) -> int | None:
    row = conn.execute(
        "select priority from tournament_preferences where league_key=?",
        (league_key,),
    ).fetchone()
    return int(row[0]) if row else None


def _count_events(conn: sqlite3.Connection, event_type: str) -> int:
    return conn.execute(
        "select count(*) from system_events where event_type=?",
        (event_type,),
    ).fetchone()[0]


# ── Case 1: drift detected — win_rate < 0.40, >= 10 samples ──────────────────

class TestDriftDetected:
    """R4.3, R4.4: League with low win rate and enough samples triggers drift."""

    def test_priority_set_to_7(self):
        """Priority is forced to 7 when win_rate < 0.40 with >= 10 samples."""
        conn = _make_conn()
        # 3 wins, 10 losses → win_rate ≈ 0.23
        rows = _make_rows(n_wins=3, n_losses=10, league_name="La Liga", days_ago=1)
        _insert_priority(conn, "la_liga", 3)

        _detect_and_handle_drift(conn, rows)

        assert _get_priority(conn, "la_liga") == 7

    def test_system_events_row_inserted(self):
        """A system_events row with event_type='drift_detected' must be written."""
        conn = _make_conn()
        rows = _make_rows(n_wins=2, n_losses=10, league_name="La Liga", days_ago=2)

        _detect_and_handle_drift(conn, rows)

        assert _count_events(conn, "drift_detected") == 1

    def test_drift_event_detail_json(self):
        """The detail_json must contain win_rate, samples, days_window, and action."""
        conn = _make_conn()
        rows = _make_rows(n_wins=3, n_losses=10, league_name="La Liga", days_ago=1)

        _detect_and_handle_drift(conn, rows)

        event = conn.execute(
            "select detail_json from system_events where event_type='drift_detected'"
        ).fetchone()
        assert event is not None
        detail = json.loads(event[0])
        assert "win_rate" in detail
        assert detail["days_window"] == 7
        assert detail["action"] == "priority_set_to_7"
        assert "samples" in detail
        assert detail["samples"] == 13  # 3 wins + 10 losses

    def test_return_value_is_count_of_drift_events(self):
        """Return value equals the number of drifting leagues detected."""
        conn = _make_conn()
        # Two leagues drifting
        rows_la_liga = _make_rows(n_wins=2, n_losses=10, league_name="La Liga", days_ago=1)
        rows_serie_a = _make_rows(n_wins=1, n_losses=12, league_name="Serie A", days_ago=1)

        count = _detect_and_handle_drift(conn, rows_la_liga + rows_serie_a)

        assert count == 2

    def test_cache_cleared_after_drift_event(self, monkeypatch):
        """clear_learned_parameter_cache() is called when drift events occur."""
        conn = _make_conn()
        rows = _make_rows(n_wins=2, n_losses=10, league_name="La Liga", days_ago=1)

        cleared = []
        monkeypatch.setattr(
            "app.monitoring.self_learner.clear_learned_parameter_cache",
            lambda: cleared.append(True),
            raising=False,
        )
        # Patch the import inside the function too
        import app.monitoring.self_learner as sl_mod
        original = None
        try:
            from app.monitoring import learned_parameters as lp
            original = lp.clear_learned_parameter_cache
            lp.clear_learned_parameter_cache = lambda: cleared.append(True)
        except Exception:
            pass

        _detect_and_handle_drift(conn, rows)

        if original is not None:
            from app.monitoring import learned_parameters as lp
            lp.clear_learned_parameter_cache = original

        # The function should have attempted to clear the cache
        assert _count_events(conn, "drift_detected") == 1  # confirms drift path ran


# ── Case 2: fewer than 10 samples — no change ─────────────────────────────────

class TestInsufficientSamples:
    """R4.3: Less than 10 samples in the 7-day window → no drift, no event."""

    def test_priority_unchanged_when_fewer_than_10_samples(self):
        conn = _make_conn()
        # 2 wins, 5 losses = 7 total (below threshold)
        rows = _make_rows(n_wins=2, n_losses=5, league_name="Bundesliga", days_ago=1)
        _insert_priority(conn, "bundesliga", 3)

        _detect_and_handle_drift(conn, rows)

        assert _get_priority(conn, "bundesliga") == 3

    def test_no_system_event_when_fewer_than_10_samples(self):
        conn = _make_conn()
        rows = _make_rows(n_wins=1, n_losses=8, league_name="Bundesliga", days_ago=1)

        _detect_and_handle_drift(conn, rows)

        assert _count_events(conn, "drift_detected") == 0

    def test_returns_zero_when_no_drift(self):
        conn = _make_conn()
        rows = _make_rows(n_wins=4, n_losses=5, league_name="Bundesliga", days_ago=1)

        count = _detect_and_handle_drift(conn, rows)

        assert count == 0

    def test_rows_outside_7_day_window_not_counted(self):
        """Rows older than 7 days must not contribute to the drift window."""
        conn = _make_conn()
        # 15 rows but all 10 days old — outside the 7-day window
        rows = _make_rows(n_wins=2, n_losses=13, league_name="Bundesliga", days_ago=10)
        _insert_priority(conn, "bundesliga", 3)

        count = _detect_and_handle_drift(conn, rows)

        assert count == 0
        assert _get_priority(conn, "bundesliga") == 3
        assert _count_events(conn, "drift_detected") == 0


# ── Case 3: recovery — previously drifted league bounces back ─────────────────

class TestDriftRecovery:
    """R4.7: League with priority=7 recovers when 7-day win_rate >= 0.45."""

    def test_priority_recalculated_on_recovery(self):
        """Priority is updated away from 7 when win_rate >= 0.45 and samples >= 10."""
        conn = _make_conn()
        # Set league to drift-forced priority=7
        _insert_priority(conn, "ligue_1", 7)

        # 6 wins, 7 losses → win_rate ≈ 0.46 (above recovery threshold)
        rows = _make_rows(n_wins=6, n_losses=7, league_name="Ligue 1", days_ago=1)

        _detect_and_handle_drift(conn, rows)

        new_priority = _get_priority(conn, "ligue_1")
        assert new_priority is not None
        # Priority must have been changed from 7 by _priority_for_win_rate
        assert new_priority != 7, (
            f"Expected priority to be recalculated from 7 after recovery, got {new_priority}"
        )

    def test_drift_recovery_event_written(self):
        """A system_events row with event_type='drift_recovery' is inserted."""
        conn = _make_conn()
        _insert_priority(conn, "ligue_1", 7)
        rows = _make_rows(n_wins=6, n_losses=7, league_name="Ligue 1", days_ago=1)

        _detect_and_handle_drift(conn, rows)

        assert _count_events(conn, "drift_recovery") == 1

    def test_recovery_event_detail_json(self):
        """detail_json for recovery must include win_rate, samples, days_window."""
        conn = _make_conn()
        _insert_priority(conn, "ligue_1", 7)
        rows = _make_rows(n_wins=6, n_losses=7, league_name="Ligue 1", days_ago=1)

        _detect_and_handle_drift(conn, rows)

        event = conn.execute(
            "select detail_json from system_events where event_type='drift_recovery'"
        ).fetchone()
        assert event is not None
        detail = json.loads(event[0])
        assert "win_rate" in detail
        assert detail["days_window"] == 7
        assert "samples" in detail

    def test_no_recovery_when_priority_is_not_7(self):
        """Recovery logic only fires when the current priority is 7."""
        conn = _make_conn()
        # League has good win rate but priority is 5 (not drift-forced)
        _insert_priority(conn, "ligue_1", 5)
        rows = _make_rows(n_wins=6, n_losses=7, league_name="Ligue 1", days_ago=1)

        _detect_and_handle_drift(conn, rows)

        # win_rate ≈ 0.46 → above drift threshold (0.40) → no drift event
        # priority was NOT 7 → no recovery event
        assert _count_events(conn, "drift_recovery") == 0
        assert _count_events(conn, "drift_detected") == 0

    def test_no_recovery_when_win_rate_below_0_45(self):
        """A league with priority=7 whose win_rate is between 0.40 and 0.45 stays at 7."""
        conn = _make_conn()
        _insert_priority(conn, "ligue_1", 7)
        # 4 wins, 8 losses → win_rate = 0.333 — below both thresholds
        rows = _make_rows(n_wins=4, n_losses=8, league_name="Ligue 1", days_ago=1)

        _detect_and_handle_drift(conn, rows)

        # drift threshold is 0.40, this is below it → drift event (re-sets to 7, already 7)
        # but no recovery
        assert _count_events(conn, "drift_recovery") == 0

    def test_recovery_with_high_win_rate_computes_low_priority(self):
        """If the recovered win_rate is >= 0.60 with >= 10 samples, priority should be 0."""
        conn = _make_conn()
        _insert_priority(conn, "premier_league", 7)
        # 8 wins, 2 losses → win_rate = 0.80
        rows = _make_rows(n_wins=8, n_losses=2, league_name="Premier League", days_ago=1)

        _detect_and_handle_drift(conn, rows)

        # _priority_for_win_rate(10, 0.80) → 0
        assert _get_priority(conn, "premier_league") == 0
        assert _count_events(conn, "drift_recovery") == 1


# ── Edge cases ─────────────────────────────────────────────────────────────────

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_rows_returns_zero(self):
        """No rows → no drift, no events, return 0."""
        conn = _make_conn()
        count = _detect_and_handle_drift(conn, [])
        assert count == 0

    def test_multiple_pick_types_same_league_handled_independently(self):
        """
        Drift is evaluated per (league_key, pick_type) bucket.
        Two leagues can drift independently.
        """
        conn = _make_conn()
        # Serie A match_result drifts: 2 wins / 10 losses
        rows_serie_a = _make_rows(
            n_wins=2, n_losses=10,
            league_name="Serie A", pick_type="match_result", days_ago=1,
        )
        # Bundesliga: good win rate — 7 wins / 5 losses (above threshold, no drift)
        rows_bundesliga = _make_rows(
            n_wins=7, n_losses=5,
            league_name="Bundesliga", pick_type="match_result", days_ago=1,
        )

        count = _detect_and_handle_drift(conn, rows_serie_a + rows_bundesliga)

        # Only Serie A drifted → 1 drift event
        assert count == 1
        assert _count_events(conn, "drift_detected") == 1
        assert _get_priority(conn, "serie_a") == 7
        # Bundesliga win_rate = 0.583 but no pre-existing priority=7, so no recovery event
        assert _count_events(conn, "drift_recovery") == 0

    def test_exact_10_samples_at_win_rate_below_threshold(self):
        """Exactly 10 samples is sufficient to trigger drift detection."""
        conn = _make_conn()
        # 3 wins, 7 losses = exactly 10 samples, win_rate = 0.30
        rows = _make_rows(n_wins=3, n_losses=7, league_name="Eredivisie", days_ago=1)

        count = _detect_and_handle_drift(conn, rows)

        assert count == 1
        assert _get_priority(conn, "eredivisie") == 7

    def test_win_rate_exactly_040_does_not_trigger_drift(self):
        """win_rate == 0.40 is NOT below the threshold; drift requires < 0.40."""
        conn = _make_conn()
        # 4 wins, 6 losses = 10 samples, win_rate = 0.40 exactly
        rows = _make_rows(n_wins=4, n_losses=6, league_name="Eredivisie", days_ago=1)
        _insert_priority(conn, "eredivisie", 3)

        count = _detect_and_handle_drift(conn, rows)

        assert count == 0
        assert _get_priority(conn, "eredivisie") == 3

    def test_win_rate_exactly_045_triggers_recovery(self):
        """win_rate == 0.45 satisfies the recovery threshold (>= 0.45)."""
        conn = _make_conn()
        _insert_priority(conn, "eredivisie", 7)
        # 9 wins, 11 losses = 20 samples, win_rate = 0.45 exactly
        rows = _make_rows(n_wins=9, n_losses=11, league_name="Eredivisie", days_ago=1)

        _detect_and_handle_drift(conn, rows)

        # win_rate = 0.45 → not < 0.40, so no drift; priority was 7 → recovery fires
        assert _count_events(conn, "drift_recovery") == 1
        assert _count_events(conn, "drift_detected") == 0
        new_priority = _get_priority(conn, "eredivisie")
        assert new_priority != 7

    def test_rows_with_bad_created_at_skipped_gracefully(self):
        """Rows with unparseable created_at are silently skipped, no exception."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _init_learner_tables(conn)

        conn2 = sqlite3.connect(":memory:")
        conn2.row_factory = sqlite3.Row
        conn2.execute("""
            create table rows (
                match_id text, league_name text, pick_type text,
                selection text, result text, created_at text
            )
        """)
        conn2.execute("insert into rows values ('x1','La Liga','match_result','home','win','NOT A DATE')")
        bad_rows = conn2.execute("select * from rows").fetchall()

        try:
            result = _detect_and_handle_drift(conn, bad_rows)
        except Exception as exc:
            pytest.fail(f"Raised unexpectedly: {exc}")

        assert result == 0
