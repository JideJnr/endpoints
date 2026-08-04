"""
Checkpoint persistence tests for team_watcher_engine.

Tests verify:
1. record_tw_prediction inserts a row for a non-no_bet signal.
2. grade_tw_predictions sets result + graded_at on the inserted row.
3. grade_tw_predictions is idempotent (second call leaves row unchanged).
4. update_tw_weights returns "skipped" when fewer than 10 samples exist.
5. generate_weekly_analysis returns sufficient_data == False with 0 finished matches.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from app.team_watcher_engine import (
    generate_weekly_analysis,
    grade_tw_predictions,
    init_tw_tables,
    record_tw_prediction,
    update_tw_weights,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_mem_conn() -> sqlite3.Connection:
    """In-memory SQLite with the prerequisite tables for init_tw_tables."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_team_watchers (
            team_key             TEXT NOT NULL PRIMARY KEY,
            team_name            TEXT NOT NULL DEFAULT '',
            profile_json         TEXT,
            weekly_analysis_json TEXT DEFAULT '{}',
            weekly_analysis_at   TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_team_watcher_matches (
            team_key      TEXT NOT NULL,
            match_id      TEXT NOT NULL,
            match_date    TEXT,
            goals_for     INTEGER,
            goals_against INTEGER,
            result        TEXT,
            venue         TEXT,
            created_at    TEXT NOT NULL DEFAULT current_timestamp,
            PRIMARY KEY (team_key, match_id)
        )
        """
    )
    conn.commit()
    # Run the engine's schema init so engine tables are present too
    init_tw_tables(conn)
    return conn


@contextmanager
def _mem_db_ctx(conn: sqlite3.Connection, **_kwargs):
    """Context-manager shim that yields the shared in-memory connection."""
    yield conn


def _patch_db(conn: sqlite3.Connection):
    """Return a list of patch context managers that redirect DB calls to conn."""
    return [
        patch("app.team_watcher_engine._init_db"),
        patch("app.team_watcher_engine.db_conn", side_effect=lambda **kw: _mem_db_ctx(conn)),
    ]


def _sample_tw_signal(pick_type: str = "match_result") -> dict:
    return {
        "name": "team_watcher_engine",
        "pick_type": pick_type,
        "selection": "home_win",
        "confidence": 65,
        "models_agree": True,
    }


# ---------------------------------------------------------------------------
# Test 1 — record_tw_prediction inserts a row
# ---------------------------------------------------------------------------

class TestRecordTwPrediction(unittest.TestCase):

    def _run_record(self, conn, team_key, match_id, signal):
        with patch("app.team_watcher_engine._init_db"), \
             patch("app.team_watcher_engine.db_conn", side_effect=lambda **kw: _mem_db_ctx(conn)):
            return record_tw_prediction(team_key, match_id, signal)

    def test_inserts_row_for_non_no_bet(self):
        conn = _make_mem_conn()
        result = self._run_record(conn, "arsenal", "match-001", _sample_tw_signal("match_result"))
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["inserted"])
        row = conn.execute(
            "SELECT * FROM team_watcher_predictions WHERE match_id = ?", ("match-001",)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["team_key"], "arsenal")
        self.assertEqual(row["pick_type"], "match_result")
        self.assertEqual(row["selection"], "home_win")
        self.assertEqual(row["confidence"], 65)
        self.assertEqual(row["sub_model"], "combined")

    def test_skips_no_bet_signal(self):
        conn = _make_mem_conn()
        result = self._run_record(conn, "arsenal", "match-002", _sample_tw_signal("no_bet"))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["inserted"])
        count = conn.execute(
            "SELECT COUNT(*) FROM team_watcher_predictions WHERE match_id = ?", ("match-002",)
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_graded_at_is_null_after_insert(self):
        conn = _make_mem_conn()
        self._run_record(conn, "arsenal", "match-003", _sample_tw_signal("goals"))
        row = conn.execute(
            "SELECT graded_at, result FROM team_watcher_predictions WHERE match_id = ?",
            ("match-003",),
        ).fetchone()
        self.assertIsNone(row["graded_at"])
        self.assertIsNone(row["result"])


# ---------------------------------------------------------------------------
# Test 2 — grade_tw_predictions sets result + graded_at
# ---------------------------------------------------------------------------

class TestGradeTwPredictions(unittest.TestCase):

    def _setup_open_prediction(self, conn, team_key="chelsea", match_id="match-100",
                                pick_type="match_result", selection="home_win", confidence=70):
        conn.execute(
            """
            INSERT INTO team_watcher_predictions
                (team_key, match_id, pick_type, selection, confidence, sub_model)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (team_key, match_id, pick_type, selection, confidence, "combined"),
        )
        conn.commit()

    def _run_grade(self, conn, match_id, result_dict):
        with patch("app.team_watcher_engine._init_db"), \
             patch("app.team_watcher_engine.db_conn", side_effect=lambda **kw: _mem_db_ctx(conn)):
            return grade_tw_predictions(match_id, result_dict)

    def test_grade_sets_result_and_graded_at(self):
        conn = _make_mem_conn()
        self._setup_open_prediction(conn, match_id="match-100", selection="home_win")
        grade_result = self._run_grade(conn, "match-100", {"outcome": "home_win"})
        self.assertEqual(grade_result["status"], "ok")
        self.assertEqual(grade_result["graded"], 1)
        row = conn.execute(
            "SELECT result, graded_at FROM team_watcher_predictions WHERE match_id = ?",
            ("match-100",),
        ).fetchone()
        self.assertEqual(row["result"], "win")
        self.assertIsNotNone(row["graded_at"])

    def test_grade_marks_loss_correctly(self):
        conn = _make_mem_conn()
        self._setup_open_prediction(conn, match_id="match-101", selection="home_win")
        self._run_grade(conn, "match-101", {"outcome": "away_win"})
        row = conn.execute(
            "SELECT result FROM team_watcher_predictions WHERE match_id = ?", ("match-101",)
        ).fetchone()
        self.assertEqual(row["result"], "loss")

    def test_grade_from_scores(self):
        conn = _make_mem_conn()
        self._setup_open_prediction(conn, match_id="match-102", selection="home_win")
        self._run_grade(conn, "match-102", {"home_score": 2, "away_score": 1})
        row = conn.execute(
            "SELECT result FROM team_watcher_predictions WHERE match_id = ?", ("match-102",)
        ).fetchone()
        self.assertEqual(row["result"], "win")


# ---------------------------------------------------------------------------
# Test 3 — grade_tw_predictions is idempotent
# ---------------------------------------------------------------------------

class TestGradeTwPredictionsIdempotent(unittest.TestCase):

    def _run_grade(self, conn, match_id, result_dict):
        with patch("app.team_watcher_engine._init_db"), \
             patch("app.team_watcher_engine.db_conn", side_effect=lambda **kw: _mem_db_ctx(conn)):
            return grade_tw_predictions(match_id, result_dict)

    def test_second_grade_call_leaves_row_unchanged(self):
        conn = _make_mem_conn()
        # Insert an open prediction
        conn.execute(
            """
            INSERT INTO team_watcher_predictions
                (team_key, match_id, pick_type, selection, confidence, sub_model)
            VALUES ('liverpool', 'match-200', 'match_result', 'home_win', 72, 'combined')
            """
        )
        conn.commit()

        # First grade
        self._run_grade(conn, "match-200", {"outcome": "home_win"})
        row_after_first = conn.execute(
            "SELECT result, graded_at FROM team_watcher_predictions WHERE match_id = ?",
            ("match-200",),
        ).fetchone()
        result_first = row_after_first["result"]
        graded_at_first = row_after_first["graded_at"]

        # Second grade — should be a no-op
        second = self._run_grade(conn, "match-200", {"outcome": "away_win"})
        self.assertEqual(second["graded"], 0)  # Nothing to grade — row already graded
        row_after_second = conn.execute(
            "SELECT result, graded_at FROM team_watcher_predictions WHERE match_id = ?",
            ("match-200",),
        ).fetchone()
        # Result and graded_at must be unchanged
        self.assertEqual(row_after_second["result"], result_first)
        self.assertEqual(row_after_second["graded_at"], graded_at_first)


# ---------------------------------------------------------------------------
# Test 4 — update_tw_weights returns "skipped" with < 10 samples
# ---------------------------------------------------------------------------

class TestUpdateTwWeights(unittest.TestCase):

    def _run_update(self, conn, team_key):
        with patch("app.team_watcher_engine._init_db"), \
             patch("app.team_watcher_engine.db_conn", side_effect=lambda **kw: _mem_db_ctx(conn)):
            return update_tw_weights(team_key)

    def test_skipped_with_zero_samples(self):
        conn = _make_mem_conn()
        result = self._run_update(conn, "team-a")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "insufficient_samples")

    def test_skipped_with_9_graded_rows(self):
        conn = _make_mem_conn()
        # Insert 9 graded rows
        for i in range(9):
            conn.execute(
                """
                INSERT INTO team_watcher_predictions
                    (team_key, match_id, pick_type, selection, confidence, sub_model, result, graded_at)
                VALUES ('team-b', ?, 'match_result', 'home_win', 60, 'combined', 'win', '2026-01-01T00:00:00+00:00')
                """,
                (f"match-{i}",),
            )
        conn.commit()
        result = self._run_update(conn, "team-b")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "insufficient_samples")

    def test_ok_with_10_graded_rows(self):
        conn = _make_mem_conn()
        # Insert 10 graded rows (6 wins, 4 losses)
        for i in range(10):
            outcome = "win" if i < 6 else "loss"
            conn.execute(
                """
                INSERT INTO team_watcher_predictions
                    (team_key, match_id, pick_type, selection, confidence, sub_model, result, graded_at)
                VALUES ('team-c', ?, 'match_result', 'home_win', 60, 'combined', ?, '2026-01-01T00:00:00+00:00')
                """,
                (f"match-{i}", outcome),
            )
        conn.commit()
        result = self._run_update(conn, "team-c")
        self.assertEqual(result["status"], "ok")
        updated = result["updated"]
        self.assertTrue(len(updated) >= 1)
        # combined sub_model: 6/10 = 0.6 win_rate → weight_adj = round((0.6-0.5)*2, 3) = 0.2
        combined_entry = next((u for u in updated if u["sub_model"] == "combined"), None)
        self.assertIsNotNone(combined_entry)
        self.assertAlmostEqual(combined_entry["weight_adj"], 0.2, places=3)


# ---------------------------------------------------------------------------
# Test 5 — generate_weekly_analysis returns sufficient_data == False with 0 matches
# ---------------------------------------------------------------------------

class TestGenerateWeeklyAnalysis(unittest.TestCase):

    def _run_generate(self, conn, team_key):
        with patch("app.team_watcher_engine._init_db"), \
             patch("app.team_watcher_engine.db_conn", side_effect=lambda **kw: _mem_db_ctx(conn)):
            return generate_weekly_analysis(team_key)

    def test_insufficient_data_with_no_matches(self):
        conn = _make_mem_conn()
        # Ensure a watcher row exists (so the UPDATE doesn't fail silently)
        conn.execute(
            "INSERT INTO ai_team_watchers (team_key, team_name) VALUES ('no-data-team', 'No Data Team')"
        )
        conn.commit()
        result = self._run_generate(conn, "no-data-team")
        self.assertFalse(result["sufficient_data"])
        self.assertIsNone(result["market_lean_trend"])
        self.assertIsNone(result["trend_summary"])

    def test_insufficient_data_with_4_matches(self):
        conn = _make_mem_conn()
        conn.execute(
            "INSERT INTO ai_team_watchers (team_key, team_name) VALUES ('few-data-team', 'Few Data Team')"
        )
        # Insert 4 finished matches (below the 5-match threshold)
        for i in range(4):
            conn.execute(
                """
                INSERT INTO ai_team_watcher_matches
                    (team_key, match_id, goals_for, goals_against, result, venue)
                VALUES ('few-data-team', ?, 1, 0, 'win', 'home')
                """,
                (f"m-{i}",),
            )
        conn.commit()
        result = self._run_generate(conn, "few-data-team")
        self.assertFalse(result["sufficient_data"])

    def test_sufficient_data_with_5_matches(self):
        conn = _make_mem_conn()
        conn.execute(
            "INSERT INTO ai_team_watchers (team_key, team_name) VALUES ('good-team', 'Good Team')"
        )
        for i in range(5):
            conn.execute(
                """
                INSERT INTO ai_team_watcher_matches
                    (team_key, match_id, goals_for, goals_against, result, venue, match_date)
                VALUES ('good-team', ?, 2, 1, 'win', 'home', '2026-01-01')
                """,
                (f"gm-{i}",),
            )
        conn.commit()
        result = self._run_generate(conn, "good-team")
        self.assertTrue(result["sufficient_data"])
        self.assertIn("rolling_form", result)
        self.assertIn("record", result)
        self.assertIn("venue_split", result)


if __name__ == "__main__":
    unittest.main()
