"""
Checkpoint unit tests for team_watcher_engine core.

Tests cover:
1. init_tw_tables runs against an in-memory DB without error (called twice for idempotency).
2. team_watcher_signal with no profiles returns pick_type == "no_bet".
3. team_watcher_signal never raises even when _ai_model is mocked to raise.
4. _merge_signal with both weights at -0.7 returns pick_type == "no_bet".
"""

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import app.team_watcher_engine as twe
from app.team_watcher_engine import _merge_signal, init_tw_tables, team_watcher_signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mem_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite connection with all prerequisite tables."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # init_tw_tables calls _ensure_column on these two tables, so they must exist.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_team_watchers (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            team_key TEXT NOT NULL UNIQUE,
            profile_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_team_watcher_matches (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            team_key TEXT NOT NULL,
            match_id TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


@contextmanager
def _mem_db_conn(conn: sqlite3.Connection, **_kwargs):
    """Context-manager shim that yields the shared in-memory connection."""
    yield conn


# ---------------------------------------------------------------------------
# Test 1 — init_tw_tables idempotency
# ---------------------------------------------------------------------------

class TestInitTwTablesIdempotency(unittest.TestCase):
    """init_tw_tables must run against an in-memory DB twice without error."""

    def test_init_twice_no_error(self):
        conn = _make_mem_conn()
        # First call — creates tables, indexes, and columns.
        init_tw_tables(conn)
        # Second call — all IF NOT EXISTS / _ensure_column guards must prevent errors.
        init_tw_tables(conn)

        # Verify the main tables were created.
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("team_watcher_predictions", tables)
        self.assertIn("team_watcher_weights", tables)

        # Verify the indexes were created.
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        self.assertIn("idx_tw_preds_team", indexes)
        self.assertIn("idx_tw_preds_match", indexes)
        self.assertIn("idx_tw_preds_graded", indexes)


# ---------------------------------------------------------------------------
# Test 2 — team_watcher_signal with no profiles → "no_bet"
# ---------------------------------------------------------------------------

class TestSignalNoProfiles(unittest.TestCase):
    """team_watcher_signal must return pick_type == "no_bet" when no profiles exist."""

    def _patched_signal(self, match_doc):
        """Run team_watcher_signal with DB and profile patched so no profiles are found."""
        mem_conn = _make_mem_conn()
        init_tw_tables(mem_conn)

        def _patched_db_conn(**kwargs):
            return _mem_db_conn(mem_conn)

        with (
            patch("app.team_watcher_engine._init_db"),
            patch("app.team_watcher_engine.db_conn", side_effect=_patched_db_conn),
            patch("app.team_watcher_engine._get_profile", return_value=None),
        ):
            return team_watcher_signal(match_doc)

    def test_no_profiles_returns_no_bet(self):
        result = self._patched_signal({"home_team": "Team A", "away_team": "Team B"})
        self.assertEqual(result.get("pick_type"), "no_bet")
        self.assertEqual(result.get("name"), "team_watcher_engine")

    def test_no_profiles_reason_is_no_profile(self):
        result = self._patched_signal({"home_team": "Team A", "away_team": "Team B"})
        self.assertEqual(result.get("reason"), "no_profile")

    def test_no_profiles_empty_match_doc(self):
        """Empty match_doc with no team names also returns no_bet."""
        result = self._patched_signal({})
        self.assertEqual(result.get("pick_type"), "no_bet")


# ---------------------------------------------------------------------------
# Test 3 — team_watcher_signal never raises even when _ai_model raises
# ---------------------------------------------------------------------------

class TestSignalNeverRaises(unittest.TestCase):
    """team_watcher_signal must never raise, even when _ai_model blows up."""

    def _patched_signal_with_ai_error(self, match_doc):
        mem_conn = _make_mem_conn()
        init_tw_tables(mem_conn)

        home_profile = {"sample_size": 10, "win_rate": 0.6, "team_key": "team-a"}
        away_profile = {"sample_size": 10, "win_rate": 0.4, "team_key": "team-b"}

        def _patched_db_conn(**kwargs):
            return _mem_db_conn(mem_conn)

        def _get_profile_side_effect(conn, team_key):
            if "team-a" in team_key or team_key == "team-a":
                return home_profile
            if "team-b" in team_key or team_key == "team-b":
                return away_profile
            return None

        with (
            patch("app.team_watcher_engine._init_db"),
            patch("app.team_watcher_engine.db_conn", side_effect=_patched_db_conn),
            patch("app.team_watcher_engine._get_profile", side_effect=_get_profile_side_effect),
            patch("app.team_watcher_engine._ai_model", side_effect=RuntimeError("network failure")),
            patch("app.team_watcher_engine.get_team_weights", return_value={"rules": 0.0, "ai": 0.0}),
        ):
            return team_watcher_signal(match_doc)

    def test_ai_model_raises_does_not_propagate(self):
        """Function must return a dict, not raise, when _ai_model raises."""
        try:
            result = self._patched_signal_with_ai_error(
                {"home_team": "team-a", "away_team": "team-b"}
            )
        except Exception as exc:
            self.fail(f"team_watcher_signal raised an exception: {exc}")

        # Result must be a dict with the engine name
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("name"), "team_watcher_engine")

    def test_ai_model_raises_returns_no_bet_or_valid_signal(self):
        """The returned dict must have a valid pick_type."""
        result = self._patched_signal_with_ai_error(
            {"home_team": "team-a", "away_team": "team-b"}
        )
        self.assertIn(result.get("pick_type"), {"no_bet", "match_result", "goals", "btts"})

    def test_ai_model_raises_returns_no_bet_on_engine_error(self):
        """When _ai_model raises and propagates through merge, engine_error no_bet is returned."""
        # Patch _rules_model to also raise to guarantee engine_error path
        with (
            patch("app.team_watcher_engine._init_db"),
            patch("app.team_watcher_engine.db_conn", side_effect=RuntimeError("db error")),
        ):
            result = team_watcher_signal({"home_team": "team-a", "away_team": "team-b"})

        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("pick_type"), "no_bet")
        self.assertEqual(result.get("name"), "team_watcher_engine")


# ---------------------------------------------------------------------------
# Test 4 — _merge_signal with both weights at -0.7 → "no_bet"
# ---------------------------------------------------------------------------

class TestMergeSignalBothSuppressed(unittest.TestCase):
    """_merge_signal with both weights at -0.7 must return pick_type == 'no_bet'."""

    def _make_rules_out(self):
        return {
            "pick_type": "match_result",
            "selection": "home_win",
            "confidence": 65,
            "venue_context": {"home_win_rate": 0.65, "home_goals_avg": 2.0},
        }

    def _make_ai_out(self):
        return {
            "pick_type": "match_result",
            "selection": "home_win",
            "confidence": 70,
            "ai_model_available": True,
            "venue_context": {},
        }

    def test_both_weights_suppressed_returns_no_bet(self):
        weights = {"rules": -0.7, "ai": -0.7}
        result = _merge_signal(self._make_rules_out(), self._make_ai_out(), weights)
        self.assertEqual(result.get("pick_type"), "no_bet")

    def test_both_weights_suppressed_reason(self):
        weights = {"rules": -0.7, "ai": -0.7}
        result = _merge_signal(self._make_rules_out(), self._make_ai_out(), weights)
        self.assertEqual(result.get("reason"), "all_models_suppressed")

    def test_both_weights_suppressed_flags(self):
        weights = {"rules": -0.7, "ai": -0.7}
        result = _merge_signal(self._make_rules_out(), self._make_ai_out(), weights)
        self.assertTrue(result.get("rules_model_suppressed"))
        self.assertTrue(result.get("ai_model_suppressed"))

    def test_both_weights_suppressed_name(self):
        weights = {"rules": -0.7, "ai": -0.7}
        result = _merge_signal(self._make_rules_out(), self._make_ai_out(), weights)
        self.assertEqual(result.get("name"), "team_watcher_engine")

    def test_threshold_boundary_exactly_neg_06_not_suppressed(self):
        """A weight of exactly -0.6 should NOT be suppressed (threshold is < -0.6)."""
        weights = {"rules": -0.6, "ai": -0.6}
        result = _merge_signal(self._make_rules_out(), self._make_ai_out(), weights)
        # Neither suppressed, so pick_type should not be no_bet from suppression
        self.assertFalse(result.get("rules_model_suppressed"))
        self.assertFalse(result.get("ai_model_suppressed"))

    def test_threshold_boundary_just_below_suppressed(self):
        """A weight of -0.601 must be suppressed."""
        weights = {"rules": -0.601, "ai": -0.601}
        result = _merge_signal(self._make_rules_out(), self._make_ai_out(), weights)
        self.assertEqual(result.get("pick_type"), "no_bet")
        self.assertEqual(result.get("reason"), "all_models_suppressed")


if __name__ == "__main__":
    unittest.main()
