import unittest

from app.deepseek_agent import _analyse_pages_with_deepseek


def _match(home, away, home_goals, away_goals):
    return {
        "status": {"type": "finished"},
        "home_team": {"name": home},
        "away_team": {"name": away},
        "score": {"home": home_goals, "away": away_goals},
    }


class OpponentTableContextTests(unittest.TestCase):
    def test_common_opponent_result_is_weighted_by_table_strength(self):
        signals = []
        edge = _common_opponent_edge(
            "Home",
            "Away",
            [_match("Home", "League Leaders", 2, 0)],
            [_match("League Leaders", "Away", 2, 0)],
            signals,
            [
                {"position": 1, "points": 60, "team": {"name": "League Leaders"}},
                {"position": 2, "points": 55, "team": {"name": "Other"}},
                {"position": 3, "points": 40, "team": {"name": "Third"}},
                {"position": 4, "points": 20, "team": {"name": "Bottom"}},
            ],
        )

        self.assertGreater(edge, 0)
        comparison = signals[0]["value"]["comparisons"][0]
        self.assertEqual(comparison["opponent_table"]["position"], 1)
        self.assertGreater(comparison["table_weight"], 1.0)

    def test_deepseek_reader_is_explicitly_unavailable_without_key(self):
        # No network call is attempted unless a DEEPSEEK_API_KEY is configured.
        import os

        old_key = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            result = _analyse_pages_with_deepseek("Home vs Away", [{"url": "https://example.test", "text": "Preview"}], "Home", "Away")
        finally:
            if old_key is not None:
                os.environ["DEEPSEEK_API_KEY"] = old_key
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
