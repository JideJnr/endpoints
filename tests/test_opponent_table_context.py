import unittest
from unittest.mock import patch

from app.prediction_agent import _common_opponent_edge
from app.web_context import _analyse_pages_with_ai


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

    def test_openrouter_reader_is_explicitly_unavailable_without_key(self):
        # No network call is attempted unless an OPENROUTER_API_KEY is configured.
        mock_settings = unittest.mock.Mock()
        mock_settings.openrouter_api_key = ""
        with patch("app.web_context.get_settings", return_value=mock_settings):
            result = _analyse_pages_with_ai("Home vs Away", [{"url": "https://example.test", "text": "Preview"}], "Home", "Away")
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
