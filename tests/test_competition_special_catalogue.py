import unittest

from app.competition_special import TOP_30_COMPETITIONS, _catalogue_default, _special_markets, apply_known_competition_context
from app.sportybet_client import _parse_event


class CompetitionSpecialCatalogueTests(unittest.TestCase):
    def test_catalogue_has_thirty_unique_sofascore_competitions(self):
        self.assertEqual(len(TOP_30_COMPETITIONS), 30)
        self.assertEqual(len({item["key"] for item in TOP_30_COMPETITIONS}), 30)
        self.assertEqual(len({item["unique_tournament_id"] for item in TOP_30_COMPETITIONS}), 30)
        self.assertTrue(all(item["unique_tournament_id"] > 0 for item in TOP_30_COMPETITIONS))

    def test_league_defaults_do_not_inherit_world_cup_dates(self):
        premier_league = _catalogue_default("premier-league")
        self.assertEqual(premier_league["unique_tournament_id"], 17)
        self.assertEqual(premier_league["start_date"], "")
        self.assertEqual(premier_league["end_date"], "")

    def test_featured_sofascore_odds_are_converted_to_decimal_market(self):
        markets = _special_markets({
            "odds_featured": {"full_time": {"market_name": "1X2", "choices": [
                {"name": "Home", "fractional_value": "5/4"},
                {"name": "Draw", "fractional_value": "2/1"},
                {"name": "Away", "fractional_value": "11/5"},
            ]}}
        })
        self.assertEqual(markets[0]["id"], "sofascore_featured_1x2")
        self.assertEqual([item["odds"] for item in markets[0]["selections"]], [2.25, 3.0, 3.2])

    def test_known_competition_context_is_attached_to_manual_match(self):
        doc = {
            "tournament": "Premier League",
            "sofascore_event": {"tournament": {"tournament_id": 17, "name": "Premier League"}},
            "sofascore_detail": {"standings": []},
        }
        apply_known_competition_context(doc)
        self.assertTrue(doc["known_competition"]["known"])
        self.assertEqual(doc["known_competition"]["key"], "premier-league")
        self.assertIn("team_strength", doc["competition_intelligence"])

    def test_sporty_event_keeps_extended_market_and_event_details(self):
        event = _parse_event({
            "eventId": "sr:match:1", "homeTeamName": "Home", "awayTeamName": "Away",
            "homeTeamId": "home-id", "awayTeamId": "away-id", "commentsNum": 7,
            "matchTrackerNotAllowed": False, "markets": [{
                "id": "1", "desc": "1X2", "group": "Main", "marketGuide": "Winner",
                "lastOddsChangeTime": 123, "outcomes": [{"id": "1", "desc": "Home", "odds": "2.0", "voidProbability": "0"}],
            }],
        }, {"name": "Premier League", "categoryName": "England"})
        self.assertEqual(event["team_ids"]["home"], "home-id")
        self.assertEqual(event["sporty_metadata"]["comments_count"], 7)
        self.assertEqual(event["markets"][0]["guide"], "Winner")
        self.assertEqual(event["markets"][0]["selections"][0]["void_probability"], "0")

if __name__ == "__main__":
    unittest.main()
