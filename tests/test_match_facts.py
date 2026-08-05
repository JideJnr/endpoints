import unittest

from app.match_facts import enrich_match_facts


class MatchFactsTests(unittest.TestCase):
    def test_enriches_half_time_goal_timing_and_live_stats_from_sofascore(self):
        doc = {
            "score": {"home": 3, "away": 1, "home_ht": 1, "away_ht": 0},
            "sofascore_detail": {
                "graph": {"points": [{"minute": 10, "value": 42}]},
                "statistics": [
                    {
                        "period": "ALL",
                        "groups": [
                            {
                                "statisticsItems": [
                                    {"name": "Shots on target", "home": "7", "away": "3"},
                                    {"name": "Corner kicks", "home": "6", "away": "2"},
                                ]
                            }
                        ],
                    }
                ],
                "incidents": [
                    {"incidentType": "goal", "time": 12, "isHome": True, "homeScore": 1, "awayScore": 0},
                    {"incidentType": "goal", "time": 51, "isHome": False, "homeScore": 1, "awayScore": 1},
                    {"incidentType": "goal", "time": 82, "isHome": True, "homeScore": 2, "awayScore": 1},
                ],
            },
            "data_sources": {"sofascore": {}, "sportybet": {}},
        }

        result = enrich_match_facts(doc)

        self.assertEqual(result["half_time_score"], {"home": 1, "away": 0, "source": "score_period1"})
        self.assertEqual(result["goal_timing"]["goal_minutes"], [12.0, 51.0, 82.0])
        self.assertEqual(result["goal_timing"]["average_interval_minutes"], 35.0)
        self.assertEqual(result["live_statistics"]["summary"]["shots_on_target"]["home"], "7")
        self.assertEqual(result["live_statistics"]["summary"]["corner_kicks"]["away"], "2")
        self.assertTrue(result["provider_live_capabilities"]["sofascore"]["live_statistics"])
        self.assertTrue(result["provider_live_capabilities"]["sofascore"]["goal_incidents"])
        self.assertTrue(result["provider_live_capabilities"]["sofascore"]["goal_momentum"])

    def test_derives_half_time_from_incident_scores_when_period_score_missing(self):
        doc = {
            "score": {"home": 2, "away": 1},
            "sofascore_detail": {
                "incidents": [
                    {"incidentType": "goal", "time": 44, "isHome": True, "homeScore": 1, "awayScore": 0},
                    {"incidentType": "goal", "time": 63, "isHome": False, "homeScore": 1, "awayScore": 1},
                ],
            },
        }

        result = enrich_match_facts(doc)

        self.assertEqual(result["half_time_score"], {"home": 1, "away": 0, "source": "goal_incidents_score"})

    def test_detects_sporty_raw_tracker_capabilities(self):
        doc = {
            "played_seconds": 3600,
            "raw_sporty": {
                "raw_event": {
                    "matchStatistics": [{"name": "Shots on target"}],
                    "goalMomentum": [{"minute": 12, "value": 68}],
                },
                "sporty_metadata": {"match_tracker_available": True},
            },
        }

        result = enrich_match_facts(doc)

        self.assertTrue(result["provider_live_capabilities"]["sportybet"]["live_clock"])
        self.assertTrue(result["provider_live_capabilities"]["sportybet"]["live_statistics"])
        self.assertTrue(result["provider_live_capabilities"]["sportybet"]["goal_momentum"])
        self.assertTrue(result["provider_live_capabilities"]["sportybet"]["match_tracker_available"])


if __name__ == "__main__":
    unittest.main()
