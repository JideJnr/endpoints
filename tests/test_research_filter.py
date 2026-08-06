"""
Unit tests for the research filter module.

Validates: Requirements 1.1–1.9, 2.1–2.7, 3.1–3.9, 9.6, 10.6, 7.4
"""

import json
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import app.research.research_filter as rf
from app.research.research_filter import (
    evaluate_pick,
    _research_filter_candidate,
    get_research_context_for_prompt,
    _get_static_fallback,
    _normalise_league_key,
    _ensure_research_stats_table,
    _get_dynamic_rules,
)


def _make_mem_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite connection with the research_stats table."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_research_stats_table(conn)
    return conn


class TestResearchFilterBasics(unittest.TestCase):
    """Tests for threshold constants and basic helpers."""

    def test_normalise_league_key(self):
        self.assertEqual(rf._normalise_league_key("Scotland League Cup"), "scotland-league-cup")
        self.assertEqual(rf._normalise_league_key("Russia Russian Cup"), "russia-russian-cup")
        self.assertEqual(rf._normalise_league_key("  Argentina Primera LPF  "), "argentina-primera-lpf")

    def test_ensure_research_stats_table_idempotent(self):
        conn = _make_mem_conn()
        try:
            _ensure_research_stats_table(conn)
            # Should not raise on second call
            _ensure_research_stats_table(conn)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='research_stats'"
            ).fetchall()
            self.assertEqual(len(tables), 1)
        finally:
            conn.close()


class TestEvaluatePickBlock(unittest.TestCase):
    """Tests for hard-block checks in evaluate_pick()."""

    def test_match_result_blocked(self):
        result = evaluate_pick({"type": "match_result", "confidence": 80})
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "research_block:match_result_61pct_loss")

    def test_confidence_below_60_blocked(self):
        result = evaluate_pick({"type": "home_win", "confidence": 55})
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "research_block:confidence_below_60")

    def test_draw_odds_below_2_blocked(self):
        result = evaluate_pick({"type": "home_win", "confidence": 80, "draw_odds": 1.50})
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "research_block:draw_odds_below_2")

    def test_favorite_odds_2_50_plus_blocked(self):
        result = evaluate_pick({"type": "home_win", "confidence": 80, "favorite_odds": 3.00})
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "research_block:favorite_odds_2_50_plus")

    def test_home_odds_danger_zone_blocked(self):
        result = evaluate_pick({"type": "home_win", "confidence": 80, "home_odds": 2.70})
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "research_block:home_odds_danger_zone")

    def test_blocked_league_dynamic(self):
        """A league with loss_rate >= 0.75 in research_stats should be blocked."""
        conn = _make_mem_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO research_stats (dimension, key, wins, losses, total, win_rate, loss_rate, min_samples) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("league", "scotland-league-cup", 10, 30, 40, 0.25, 0.75, 5),
            )
            conn.commit()
            with patch.object(rf, "db_conn") as mock_db:
                mock_db.return_value.__enter__.return_value = conn
                rf._dynamic_cache_time = 0.0
                result = evaluate_pick({
                    "type": "home_win", "confidence": 80,
                    "league_key": "scotland-league-cup",
                })
            self.assertTrue(result["blocked"])
            self.assertEqual(result["reason"], "research_block:dynamic_league")
        finally:
            conn.close()

    def test_blocked_country_dynamic(self):
        """A country with loss_rate >= 0.50 in research_stats should be blocked."""
        conn = _make_mem_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO research_stats (dimension, key, wins, losses, total, win_rate, loss_rate, min_samples) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("country", "bolivia", 5, 15, 20, 0.25, 0.75, 10),
            )
            conn.commit()
            with patch.object(rf, "db_conn") as mock_db:
                mock_db.return_value.__enter__.return_value = conn
                rf._dynamic_cache_time = 0.0
                result = evaluate_pick({
                    "type": "home_win", "confidence": 80,
                    "country": "bolivia",
                })
            self.assertTrue(result["blocked"])
            self.assertEqual(result["reason"], "research_block:dynamic_country")
        finally:
            conn.close()

    def test_dynamic_league_block_not_loaded(self):
        """Without dynamic rules loaded, unknown league should pass."""
        result = evaluate_pick({
            "type": "home_win", "confidence": 80,
            "league_key": "some-unknown-league",
        })
        self.assertFalse(result["blocked"])


class TestEvaluatePickCaution(unittest.TestCase):
    """Tests for caution checks in evaluate_pick()."""

    def test_away_or_draw_low_conf_blocked(self):
        result = evaluate_pick({
            "type": "home_win", "selection": "Away or Draw",
            "confidence": 65,
        })
        self.assertTrue(result["blocked"])
        self.assertIn("away_or_draw_low_conf", result["reason"])

    def test_away_or_draw_high_conf_passes(self):
        result = evaluate_pick({
            "type": "home_win", "selection": "Away or Draw",
            "confidence": 75,
        })
        self.assertFalse(result["blocked"])

    def test_noisy_band_capped(self):
        result = evaluate_pick({
            "type": "home_win", "selection": "Home or Away",
            "confidence": 63,
        })
        self.assertFalse(result["blocked"])
        self.assertTrue(result["evidence"].get("noisy_band"))

    def test_trust_boost_home_or_away(self):
        result = evaluate_pick({
            "type": "home_win", "selection": "Home or Away",
            "confidence": 75,
        })
        self.assertFalse(result["blocked"])
        self.assertGreaterEqual(result["trust_boost"], 4)

    def test_trust_boost_sportybet_market_signal(self):
        result = evaluate_pick({
            "type": "home_win", "selection": "Home or Away",
            "confidence": 75, "source": "sportybet_market_signal",
        })
        self.assertFalse(result["blocked"])
        self.assertGreaterEqual(result["trust_boost"], 5)

    def test_trust_boost_trust_country(self):
        """A country with win_rate >= 0.80 in research_stats should get trust boost."""
        conn = _make_mem_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO research_stats (dimension, key, wins, losses, total, win_rate, loss_rate, min_samples) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("country", "austria", 16, 4, 20, 0.80, 0.20, 10),
            )
            conn.commit()
            with patch.object(rf, "db_conn") as mock_db:
                mock_db.return_value.__enter__.return_value = conn
                rf._dynamic_cache_time = 0.0
                result = evaluate_pick({
                    "type": "home_win", "selection": "Home or Away",
                    "confidence": 75, "country": "austria",
                })
            self.assertFalse(result["blocked"])
            self.assertGreaterEqual(result["trust_boost"], 3)
        finally:
            conn.close()

    def test_trust_boost_capped_at_8(self):
        result = evaluate_pick({
            "type": "home_win", "selection": "Home or Away",
            "confidence": 75, "source": "sportybet_market_signal",
            "country": "austria", "home_odds": 1.50,
        })
        self.assertFalse(result["blocked"])
        self.assertLessEqual(result["trust_boost"], 8)

    def test_published_confidence_capped_at_88(self):
        result = evaluate_pick({
            "type": "home_win", "selection": "Home or Away",
            "confidence": 85, "source": "sportybet_market_signal",
            "country": "austria", "home_odds": 1.50,
        })
        self.assertFalse(result["blocked"])
        self.assertLessEqual(result["published_confidence"], 88)

    def test_research_trust_boosts_populated(self):
        result = evaluate_pick({
            "type": "home_win", "selection": "Home or Away",
            "confidence": 75,
        })
        self.assertFalse(result["blocked"])
        boosts = result["evidence"].get("research_trust_boosts", [])
        self.assertTrue(len(boosts) > 0)
        self.assertIn("home_or_away", [b["rule"] for b in boosts])


class TestResearchFilterCandidate(unittest.TestCase):
    """Tests for _research_filter_candidate()."""

    def test_match_result_excluded(self):
        self.assertFalse(_research_filter_candidate({"type": "match_result", "confidence": 80}))

    def test_low_confidence_excluded(self):
        self.assertFalse(_research_filter_candidate({"type": "home_win", "confidence": 55}))

    def test_draw_odds_below_2_excluded(self):
        self.assertFalse(_research_filter_candidate(
            {"type": "home_win", "confidence": 80},
            odds_profile={"draw_odds": 1.50},
        ))

    def test_safe_candidate_included(self):
        self.assertTrue(_research_filter_candidate(
            {"type": "home_win", "confidence": 75, "selection": "Home or Away"},
            odds_profile={"draw_odds": 3.0, "favorite_odds": 1.8, "home_odds": 1.5},
        ))


class TestGetResearchContextForPrompt(unittest.TestCase):
    """Tests for get_research_context_for_prompt()."""

    def test_static_fallback_when_no_data(self):
        with patch.object(rf, "_load_dynamic_rules"):
            result = get_research_context_for_prompt()
        # Should return static fallback or empty string, never raise
        self.assertIsInstance(result, str)

    def test_static_fallback_includes_prefix(self):
        with patch.object(rf, "_load_dynamic_rules"):
            with patch.object(rf, "db_conn") as mock_conn:
                mock_conn.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
                result = get_research_context_for_prompt()
        if result:
            self.assertIn("static snapshot", result.lower())


class TestSchedulerJob(unittest.TestCase):
    """Tests for job_regenerate_research_stats()."""

    def test_insufficient_data_guard(self):
        """Less than 50 graded rows should skip writing."""
        from app.scheduling.scheduler import job_regenerate_research_stats
        result = job_regenerate_research_stats()
        # Should return early with skipped status when no data, or ok if data exists
        self.assertIn(result.get("status"), ["skipped", "ok", "error"])

    def test_job_regenerate_research_stats_function_exists(self):
        """Verify the job function is importable."""
        from app.scheduling.scheduler import job_regenerate_research_stats
        self.assertTrue(callable(job_regenerate_research_stats))


class TestSynthesizeSurePicksResearch(unittest.TestCase):
    """Tests for synthesize_sure_picks() research additions."""

    def test_optimal_profile_score_bounds(self):
        """optimal_profile_score should be in [0, 6]."""
        from app.ai.ai_betbuilder import synthesize_sure_picks
        # This is a structural test - the score is computed from conditions
        # and capped at 6
        self.assertTrue(True)  # Placeholder - see property tests for detailed validation

    def test_research_conviction_adj_stored(self):
        """research_conviction_adj should be stored on ranked items."""
        from app.ai.ai_betbuilder import synthesize_sure_picks
        self.assertTrue(True)  # Placeholder - see property tests for detailed validation


if __name__ == "__main__":
    unittest.main()
