import json
import unittest
from unittest.mock import patch, Mock

from app.web_context import (
    _clamp_probability,
    _analyse_pages_with_ai,
    search_league_sentiment,
)


def _mock_settings(openrouter_key="test-key", web_search=True):
    """Create a mock settings object with OpenRouter credentials."""
    settings = Mock()
    settings.openrouter_api_key = openrouter_key
    settings.openrouter_model = "openrouter/free"
    settings.openrouter_base_url = "https://openrouter.ai/api/v1"
    settings.web_search_enabled = web_search
    settings.web_search_max_results = 3
    settings.web_scrape_timeout_seconds = 6
    settings.web_scrape_max_chars = 1500
    settings.web_search_timeout_seconds = 4
    return settings


class ClampProbabilityTests(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_clamp_probability(None))

    def test_valid_percentage(self):
        self.assertEqual(_clamp_probability(55.5), 55.5)

    def test_clamps_above_100(self):
        self.assertEqual(_clamp_probability(150), 100.0)

    def test_clamps_below_0(self):
        self.assertEqual(_clamp_probability(-10), 0.0)

    def test_invalid_string_returns_none(self):
        self.assertIsNone(_clamp_probability("not_a_number"))

    def test_zero(self):
        self.assertEqual(_clamp_probability(0), 0.0)

    def test_hundred(self):
        self.assertEqual(_clamp_probability(100), 100.0)


class AnalysePagesWithAITests(unittest.TestCase):
    def test_no_scraped_returns_skipped(self):
        result = _analyse_pages_with_ai("query", [], "Home", "Away")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_pages_read")
        self.assertEqual(result["sentiment"], {})
        self.assertEqual(result["probability"], {})

    def test_no_api_key_returns_unavailable(self):
        with patch("app.web_context.get_settings", return_value=_mock_settings(openrouter_key="")):
            result = _analyse_pages_with_ai("query", [{"url": "http://x", "text": "content"}], "Home", "Away")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["sentiment"], {})
        self.assertEqual(result["probability"], {})

    def test_empty_page_text_returns_skipped(self):
        with patch("app.web_context.get_settings", return_value=_mock_settings()):
            result = _analyse_pages_with_ai("query", [{"url": "http://x", "text": ""}], "Home", "Away")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "empty_page_text")

    def test_ai_error_returns_error_with_empty_sentiment_probability(self):
        with patch("app.web_context.get_settings", return_value=_mock_settings()):
            with patch("requests.post", side_effect=Exception("connection error")):
                result = _analyse_pages_with_ai("query", [{"url": "http://x", "text": "some content"}], "Home", "Away")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["sentiment"], {})
        self.assertEqual(result["probability"], {})

    def test_ai_returns_sentiment_and_probability(self):
        mock_response = Mock()
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "Test summary",
                                "evidence": [{"claim": "Home team is in good form", "source_url": "http://x", "relevance": "high"}],
                                "uncertainty": "low",
                                "sentiment": {
                                    "home_sentiment": "positive",
                                    "away_sentiment": "negative",
                                    "overall_sentiment": "positive",
                                    "confidence": 75,
                                },
                                "probability": {
                                    "implied_home_win": 55.0,
                                    "implied_draw": 25.0,
                                    "implied_away_win": 20.0,
                                },
                            }
                        )
                    }
                }
            ]
        }
        mock_response.raise_for_status = Mock()

        with patch("app.web_context.get_settings", return_value=_mock_settings()):
            with patch("requests.post", return_value=mock_response):
                result = _analyse_pages_with_ai("query", [{"url": "http://x", "text": "some content"}], "Home", "Away")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sentiment"]["home_sentiment"], "positive")
        self.assertEqual(result["sentiment"]["away_sentiment"], "negative")
        self.assertEqual(result["sentiment"]["overall_sentiment"], "positive")
        self.assertEqual(result["sentiment"]["confidence"], 75)
        self.assertEqual(result["probability"]["implied_home_win"], 55.0)
        self.assertEqual(result["probability"]["implied_draw"], 25.0)
        self.assertEqual(result["probability"]["implied_away_win"], 20.0)

    def test_ai_returns_null_probability_when_not_in_source(self):
        mock_response = Mock()
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "No probability mentioned",
                                "evidence": [],
                                "uncertainty": "high",
                                "sentiment": {
                                    "home_sentiment": "neutral",
                                    "away_sentiment": "neutral",
                                    "overall_sentiment": "neutral",
                                    "confidence": 50,
                                },
                                "probability": {},
                            }
                        )
                    }
                }
            ]
        }
        mock_response.raise_for_status = Mock()

        with patch("app.web_context.get_settings", return_value=_mock_settings()):
            with patch("requests.post", return_value=mock_response):
                result = _analyse_pages_with_ai("query", [{"url": "http://x", "text": "some content"}], "Home", "Away")

        self.assertIsNone(result["probability"]["implied_home_win"])
        self.assertIsNone(result["probability"]["implied_draw"])
        self.assertIsNone(result["probability"]["implied_away_win"])


class SearchLeagueSentimentTests(unittest.TestCase):
    def setUp(self):
        from app.config import invalidate_settings_cache
        invalidate_settings_cache()

    def test_disabled_returns_disabled(self):
        with patch("app.web_context.get_settings", return_value=_mock_settings(web_search=False)):
            result = search_league_sentiment("Premier League")
        self.assertTrue(result.get("disabled"))

    def test_no_results_returns_error(self):
        with patch("app.web_context.get_settings", return_value=_mock_settings()):
            with patch("app.web_context._search", return_value=([], [])):
                result = search_league_sentiment("Premier League")
        self.assertEqual(result.get("error"), "no_results")

    def test_league_sentiment_returns_sentiment_and_probability(self):
        mock_response = Mock()
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "League sentiment summary",
                                "sentiment": {
                                    "overall_league_sentiment": "positive",
                                    "confidence": 65,
                                },
                                "probability": {
                                    "implied_home_win": 45.0,
                                    "implied_draw": 30.0,
                                    "implied_away_win": 25.0,
                                },
                            }
                        )
                    }
                }
            ]
        }
        mock_response.raise_for_status = Mock()

        with patch("app.web_context.get_settings", return_value=_mock_settings()):
            with patch("app.web_context._search", return_value=([{"title": "Guide", "body": "Summary", "href": "http://x"}], [])):
                with patch("app.web_context._scrape_parallel", return_value=[{"url": "http://x", "text": "League content"}]):
                    with patch("requests.post", return_value=mock_response):
                        result = search_league_sentiment("Premier League")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sentiment"]["overall_league_sentiment"], "positive")
        self.assertEqual(result["sentiment"]["confidence"], 65)
        self.assertEqual(result["probability"]["implied_home_win"], 45.0)

    def test_league_sentiment_handles_ai_error(self):
        with patch("app.web_context.get_settings", return_value=_mock_settings()):
            with patch("app.web_context._search", return_value=([{"title": "Guide", "body": "Summary", "href": "http://x"}], [])):
                with patch("app.web_context._scrape_parallel", return_value=[{"url": "http://x", "text": "League content"}]):
                    with patch("requests.post", side_effect=Exception("API error")):
                        result = search_league_sentiment("Premier League")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["sentiment"], {})
        self.assertEqual(result["probability"], {})


class WebContextSignalIntegrationTests(unittest.TestCase):
    """Test that sentiment and probability signals are correctly built in enriched_prediction."""

    def _make_doc(self, grok_analysis):
        return {
            "web_context": {
                "query": "test query",
                "snippets": [{"title": "Source", "snippet": "Snippet text", "url": "http://x"}],
                "scraped": [{"url": "http://x", "text": "Content"}],
                "grok_analysis": grok_analysis,
            }
        }

    def test_sentiment_signal_positive_home(self):
        from app.enriched_prediction import _model_signals

        doc = self._make_doc(
            {
                "status": "ok",
                "sentiment": {
                    "home_sentiment": "positive",
                    "away_sentiment": "negative",
                    "overall_sentiment": "positive",
                    "confidence": 80,
                },
                "probability": {},
            }
        )
        signals = _model_signals(None, None, None, {}, doc)
        sentiment_signals = [s for s in signals if s["name"] == "web_sentiment"]
        self.assertEqual(len(sentiment_signals), 1)
        self.assertEqual(sentiment_signals[0]["value"]["home_sentiment"], "positive")
        self.assertEqual(sentiment_signals[0]["value"]["away_sentiment"], "negative")
        # positive home - negative away = net positive direction
        self.assertGreater(sentiment_signals[0]["impact"], 0)

    def test_sentiment_signal_negative_home(self):
        from app.enriched_prediction import _model_signals

        doc = self._make_doc(
            {
                "status": "ok",
                "sentiment": {
                    "home_sentiment": "negative",
                    "away_sentiment": "positive",
                    "overall_sentiment": "negative",
                    "confidence": 70,
                },
                "probability": {},
            }
        )
        signals = _model_signals(None, None, None, {}, doc)
        sentiment_signals = [s for s in signals if s["name"] == "web_sentiment"]
        self.assertEqual(len(sentiment_signals), 1)
        # negative home - positive away = net negative direction
        self.assertLess(sentiment_signals[0]["impact"], 0)

    def test_no_sentiment_when_grok_failed(self):
        from app.enriched_prediction import _model_signals

        doc = self._make_doc(
            {
                "status": "error",
                "sentiment": {},
                "probability": {},
            }
        )
        signals = _model_signals(None, None, None, {}, doc)
        sentiment_signals = [s for s in signals if s["name"] == "web_sentiment"]
        self.assertEqual(len(sentiment_signals), 0)

    def test_probability_signal_when_present(self):
        from app.enriched_prediction import _model_signals

        doc = self._make_doc(
            {
                "status": "ok",
                "sentiment": {},
                "probability": {
                    "implied_home_win": 60.0,
                    "implied_draw": 25.0,
                    "implied_away_win": 15.0,
                },
            }
        )
        signals = _model_signals(None, None, None, {}, doc)
        prob_signals = [s for s in signals if s["name"] == "web_probability"]
        self.assertEqual(len(prob_signals), 1)
        self.assertEqual(prob_signals[0]["value"]["implied_home_win"], 60.0)
        self.assertEqual(prob_signals[0]["value"]["implied_draw"], 25.0)
        self.assertEqual(prob_signals[0]["value"]["implied_away_win"], 15.0)

    def test_no_probability_when_not_present(self):
        from app.enriched_prediction import _model_signals

        doc = self._make_doc(
            {
                "status": "ok",
                "sentiment": {},
                "probability": {},
            }
        )
        signals = _model_signals(None, None, None, {}, doc)
        prob_signals = [s for s in signals if s["name"] == "web_probability"]
        self.assertEqual(len(prob_signals), 0)


class WebTextExtractionTests(unittest.TestCase):
    """Test that _web_text includes sentiment and probability data."""

    def test_web_text_includes_sentiment(self):
        from app.contextual_intelligence import _web_text

        doc = {
            "web_context": {
                "grok_analysis": {
                    "status": "ok",
                    "summary": "Test summary",
                    "evidence": [],
                    "sentiment": {
                        "home_sentiment": "positive",
                        "away_sentiment": "negative",
                        "overall_sentiment": "positive",
                        "confidence": 75,
                    },
                    "probability": {
                        "implied_home_win": 55.0,
                        "implied_draw": 25.0,
                        "implied_away_win": 20.0,
                    },
                }
            }
        }
        text = _web_text(doc)
        self.assertIn("home_sentiment: positive", text)
        self.assertIn("away_sentiment: negative", text)
        self.assertIn("sentiment_confidence: 75", text)
        self.assertIn("implied_home_win: 55.0%", text)
        self.assertIn("implied_draw: 25.0%", text)
        self.assertIn("implied_away_win: 20.0%", text)

    def test_web_text_without_sentiment(self):
        from app.contextual_intelligence import _web_text

        doc = {
            "web_context": {
                "grok_analysis": {
                    "status": "ok",
                    "summary": "Test summary",
                    "evidence": [],
                }
            }
        }
        text = _web_text(doc)
        self.assertNotIn("home_sentiment", text)
        self.assertNotIn("implied_home_win", text)


if __name__ == "__main__":
    unittest.main()
