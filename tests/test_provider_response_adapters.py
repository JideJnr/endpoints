import unittest

from app.sofascore_client import _events_from_response, _parse_event
from app.sportybet_client import parse_events_response


class ProviderResponseAdapterTests(unittest.TestCase):
    def test_sportybet_accepts_compact_event_list(self):
        matches = parse_events_response({
            "data": {
                "eventList": [{
                    "id": "new-event-id",
                    "homeTeam": {"name": "Home"},
                    "awayTeam": {"name": "Away"},
                    "score": {"home": 2, "away": 1},
                    "marketList": [{"id": "1", "desc": "1X2", "selections": []}],
                }],
            },
        })

        self.assertEqual(matches[0]["id"], "new-event-id")
        self.assertEqual(matches[0]["score"], {"home": 2, "away": 1})
        self.assertEqual(matches[0]["markets"][0]["name"], "1X2")

    def test_sofascore_accepts_wrapped_event_lists_and_event_state(self):
        raw_events = _events_from_response({"data": {"items": [{"id": 1}]}})
        self.assertEqual(raw_events, [{"id": 1}])

        event = _parse_event({
            "id": 42,
            "homeTeam": {"id": 1, "name": "Home"},
            "awayTeam": {"id": 2, "name": "Away"},
            "homeScore": {"display": 3},
            "awayScore": {"display": 2},
            "eventState": {"status": {"type": "inprogress", "description": "2nd half"}},
            "tournament": {"uniqueTournamentId": 17, "tournamentId": 1, "uniqueTournament": {"name": "Premier League"}},
            "seasonInfo": {"id": 9, "name": "2026/27"},
        })

        self.assertEqual(event["score"], {"home": 3, "away": 2, "home_ht": None, "away_ht": None})
        self.assertEqual(event["status"]["type"], "inprogress")
        self.assertEqual(event["tournament"]["id"], 17)
        self.assertEqual(event["season_id"], 9)


if __name__ == "__main__":
    unittest.main()
