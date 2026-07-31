import unittest

from app.season_stage import (
    classify_table_size,
    detect_season_stage,
    season_aware_table_weight,
)


def _standing(position, points, played, team_name, team_id=None):
    return {
        "position": position,
        "points": points,
        "played": played,
        "goals_for": 0,
        "goals_against": 0,
        "team": {"name": team_name, "id": team_id} if team_id else {"name": team_name},
    }


class DetectSeasonStageTests(unittest.TestCase):
    def test_empty_standings_returns_no_data(self):
        result = detect_season_stage([])
        self.assertEqual(result["stage"], "no_data")
        self.assertFalse(result["season_started"])
        self.assertFalse(result["standings_meaningful"])

    def test_all_zero_points_and_zero_played_is_not_started(self):
        standings = [
            _standing(1, 0, 0, "Team A"),
            _standing(2, 0, 0, "Team B"),
            _standing(3, 0, 0, "Team C"),
            _standing(4, 0, 0, "Team D"),
        ]
        result = detect_season_stage(standings)
        self.assertEqual(result["stage"], "not_started")
        self.assertTrue(result["season_not_started"])
        self.assertFalse(result["standings_meaningful"])

    def test_all_zero_points_but_some_played_is_beginning(self):
        standings = [
            _standing(1, 0, 1, "Team A"),
            _standing(2, 0, 1, "Team B"),
            _standing(3, 0, 1, "Team C"),
            _standing(4, 0, 1, "Team D"),
        ]
        result = detect_season_stage(standings)
        self.assertEqual(result["stage"], "beginning")
        self.assertTrue(result["season_beginning"])
        self.assertFalse(result["standings_meaningful"])

    def test_most_teams_unplayed_is_beginning(self):
        standings = [
            _standing(1, 6, 2, "Team A"),
            _standing(2, 3, 2, "Team B"),
            _standing(3, 0, 0, "Team C"),
            _standing(4, 0, 0, "Team D"),
            _standing(5, 0, 0, "Team E"),
            _standing(6, 0, 0, "Team F"),
            _standing(7, 0, 0, "Team G"),
            _standing(8, 0, 0, "Team H"),
            _standing(9, 0, 0, "Team I"),
            _standing(10, 0, 0, "Team J"),
        ]
        result = detect_season_stage(standings)
        self.assertEqual(result["stage"], "beginning")
        self.assertTrue(result["season_beginning"])

    def test_normal_season_is_in_progress(self):
        standings = [
            _standing(1, 60, 20, "Team A"),
            _standing(2, 55, 20, "Team B"),
            _standing(3, 40, 20, "Team C"),
            _standing(4, 20, 20, "Team D"),
        ]
        result = detect_season_stage(standings)
        self.assertEqual(result["stage"], "in_progress")
        self.assertTrue(result["season_started"])
        self.assertTrue(result["standings_meaningful"])

    def test_table_size_is_reported(self):
        standings = [_standing(i, 0, 0, f"Team {i}") for i in range(1, 21)]
        result = detect_season_stage(standings)
        self.assertEqual(result["table_size"], 20)


class ClassifyTableSizeTests(unittest.TestCase):
    def test_small_league(self):
        standings = [_standing(i, 0, 0, f"Team {i}") for i in range(1, 5)]
        result = classify_table_size(standings)
        self.assertEqual(result["category"], "small")
        self.assertTrue(result["is_small_league"])
        self.assertEqual(result["table_size"], 4)

    def test_medium_league(self):
        standings = [_standing(i, 0, 0, f"Team {i}") for i in range(1, 13)]
        result = classify_table_size(standings)
        self.assertEqual(result["category"], "medium")
        self.assertTrue(result["is_medium_league"])
        self.assertEqual(result["table_size"], 12)

    def test_large_league(self):
        standings = [_standing(i, 0, 0, f"Team {i}") for i in range(1, 21)]
        result = classify_table_size(standings)
        self.assertEqual(result["category"], "large")
        self.assertTrue(result["is_large_league"])
        self.assertEqual(result["table_size"], 20)

    def test_24_team_league(self):
        standings = [_standing(i, 0, 0, f"Team {i}") for i in range(1, 25)]
        result = classify_table_size(standings)
        self.assertEqual(result["category"], "large")
        self.assertEqual(result["table_size"], 24)

    def test_bottom_zone_cutoff_scales_with_size(self):
        # 4-team league: only 1 bottom
        result = classify_table_size([_standing(i, 0, 0, f"T{i}") for i in range(1, 5)])
        self.assertEqual(result["bottom_zone_cutoff"], 1)
        # 20-team league: ~4 bottom
        result = classify_table_size([_standing(i, 0, 0, f"T{i}") for i in range(1, 21)])
        self.assertEqual(result["bottom_zone_cutoff"], 4)

    def test_title_race_cutoff_scales_with_size(self):
        # 4-team league: top 2
        result = classify_table_size([_standing(i, 0, 0, f"T{i}") for i in range(1, 5)])
        self.assertEqual(result["title_race_cutoff"], 2)
        # 20-team league: top 4
        result = classify_table_size([_standing(i, 0, 0, f"T{i}") for i in range(1, 21)])
        self.assertEqual(result["title_race_cutoff"], 4)


class SeasonAwareTableWeightTests(unittest.TestCase):
    def test_not_started_season_heavily_discounts_weight(self):
        standings = [_standing(i, 0, 0, f"Team {i}") for i in range(1, 21)]
        stage = detect_season_stage(standings)
        # Top team in a 20-team league, season not started
        weight = season_aware_table_weight(1, 20, stage)
        # Should be heavily discounted (close to 0.75, the minimum)
        self.assertLess(weight, 0.85)

    def test_in_progress_season_normal_weight(self):
        standings = [_standing(i, 60 - i * 3, 20, f"Team {i}") for i in range(1, 21)]
        stage = detect_season_stage(standings)
        # Top team should get high weight
        weight = season_aware_table_weight(1, 20, stage)
        self.assertGreater(weight, 1.0)
        # Bottom team should get low weight
        weight = season_aware_table_weight(20, 20, stage)
        self.assertLess(weight, 0.85)

    def test_small_league_bottom_discounted(self):
        standings = [_standing(i, 0, 0, f"Team {i}") for i in range(1, 5)]
        stage = detect_season_stage(standings)
        # Bottom team in a 4-team league should be discounted
        weight = season_aware_table_weight(4, 4, stage)
        self.assertLess(weight, 0.85)

    def test_small_league_top_boosted(self):
        standings = [_standing(i, 60 - i * 3, 20, f"Team {i}") for i in range(1, 5)]
        stage = detect_season_stage(standings)
        # Top team in a 4-team league should be boosted
        weight = season_aware_table_weight(1, 4, stage)
        self.assertGreater(weight, 1.0)

    def test_beginning_season_partial_discount(self):
        standings = [_standing(i, 0, 1, f"Team {i}") for i in range(1, 21)]
        stage = detect_season_stage(standings)
        # Season beginning should partially discount
        weight = season_aware_table_weight(1, 20, stage)
        self.assertLess(weight, 1.2)  # less than full weight


class PredictionAgentTableWeightTests(unittest.TestCase):
    def test_common_opponent_edge_with_not_started_season(self):
        from app.prediction_agent import _common_opponent_edge

        def _match(home, away, hg, ag):
            return {
                "status": {"type": "finished"},
                "home_team": {"name": home, "id": 1 if home == "Home" else 2},
                "away_team": {"name": away, "id": 3 if away == "League Leaders" else 4},
                "score": {"home": hg, "away": ag},
            }

        signals = []
        # Standings where all teams have 0 points and 0 played
        standings = [
            {"position": 1, "points": 0, "played": 0, "team": {"name": "League Leaders", "id": 3}},
            {"position": 2, "points": 0, "played": 0, "team": {"name": "Other", "id": 5}},
            {"position": 3, "points": 0, "played": 0, "team": {"name": "Third", "id": 6}},
            {"position": 4, "points": 0, "played": 0, "team": {"name": "Bottom", "id": 4}},
        ]
        edge = _common_opponent_edge(
            "Home", "Away",
            [_match("Home", "League Leaders", 2, 0)],
            [_match("League Leaders", "Away", 2, 0)],
            signals,
            standings,
        )
        # Edge should be small because standings are unreliable
        self.assertLess(abs(edge), 5.0)
        # Signal should include season stage info
        if signals:
            sig = signals[0]["value"]
            self.assertEqual(sig["season_stage"], "not_started")
            self.assertTrue(sig["season_not_started"])
            self.assertFalse(sig["standings_meaningful"])

    def test_common_opponent_edge_with_in_progress_season(self):
        from app.prediction_agent import _common_opponent_edge

        def _match(home, away, hg, ag):
            return {
                "status": {"type": "finished"},
                "home_team": {"name": home, "id": 1 if home == "Home" else 2},
                "away_team": {"name": away, "id": 3 if away == "League Leaders" else 4},
                "score": {"home": hg, "away": ag},
            }

        signals = []
        # Standings where teams have played many matches
        standings = [
            {"position": 1, "points": 60, "played": 20, "team": {"name": "League Leaders", "id": 3}},
            {"position": 2, "points": 55, "played": 20, "team": {"name": "Other", "id": 5}},
            {"position": 3, "points": 40, "played": 20, "team": {"name": "Third", "id": 6}},
            {"position": 4, "points": 20, "played": 20, "team": {"name": "Bottom", "id": 4}},
        ]
        edge = _common_opponent_edge(
            "Home", "Away",
            [_match("Home", "League Leaders", 2, 0)],
            [_match("League Leaders", "Away", 2, 0)],
            signals,
            standings,
        )
        # Edge should be larger because standings are meaningful
        self.assertGreater(abs(edge), 0)
        if signals:
            sig = signals[0]["value"]
            self.assertEqual(sig["season_stage"], "in_progress")
            self.assertTrue(sig["standings_meaningful"])


class CompetitionSpecialTableContextTests(unittest.TestCase):
    def test_standing_summary_includes_season_stage(self):
        from app.competition_special import _standing_summary

        row = {
            "position": 1,
            "points": 0,
            "played": 0,
            "goals_for": 0,
            "goals_against": 0,
            "team": {"name": "Test Team"},
        }
        season_stage = detect_season_stage([
            {"position": 1, "points": 0, "played": 0, "team": {"name": "Test Team"}},
            {"position": 2, "points": 0, "played": 0, "team": {"name": "Other Team"}},
        ])
        summary = _standing_summary(row, season_stage)
        self.assertEqual(summary["season_stage"], "not_started")
        self.assertFalse(summary["standings_meaningful"])
        self.assertFalse(summary["ppg_reliable"])

    def test_standing_summary_in_progress_season(self):
        from app.competition_special import _standing_summary

        row = {
            "position": 1,
            "points": 45,
            "played": 20,
            "goals_for": 40,
            "goals_against": 20,
            "team": {"name": "Test Team"},
        }
        season_stage = detect_season_stage([
            {"position": 1, "points": 45, "played": 20, "team": {"name": "Test Team"}},
            {"position": 2, "points": 30, "played": 20, "team": {"name": "Other Team"}},
        ])
        summary = _standing_summary(row, season_stage)
        self.assertEqual(summary["season_stage"], "in_progress")
        self.assertTrue(summary["standings_meaningful"])
        self.assertTrue(summary["ppg_reliable"])

    def test_competition_readiness_notes_season_aware(self):
        from app.competition_special import _competition_readiness_notes

        home_table = {"position": 1, "points": 0, "played": 0, "points_per_game": 0}
        away_table = {"position": 2, "points": 0, "played": 0, "points_per_game": 0}
        home_strength = {"sample_size": 5, "strength_score": 50}
        away_strength = {"sample_size": 5, "strength_score": 50}
        season_stage = detect_season_stage([
            {"position": 1, "points": 0, "played": 0, "team": {"name": "Home"}},
            {"position": 2, "points": 0, "played": 0, "team": {"name": "Away"}},
        ])
        notes = _competition_readiness_notes(home_table, away_table, home_strength, away_strength, season_stage)
        self.assertIn("season_not_started_standings_unreliable", notes)

    def test_competition_readiness_notes_beginning_season(self):
        from app.competition_special import _competition_readiness_notes

        home_table = {"position": 1, "points": 3, "played": 1, "points_per_game": 3.0}
        away_table = {"position": 2, "points": 0, "played": 1, "points_per_game": 0}
        home_strength = {"sample_size": 5, "strength_score": 50}
        away_strength = {"sample_size": 5, "strength_score": 50}
        season_stage = detect_season_stage([
            {"position": 1, "points": 3, "played": 1, "team": {"name": "Home"}},
            {"position": 2, "points": 0, "played": 1, "team": {"name": "Away"}},
        ])
        notes = _competition_readiness_notes(home_table, away_table, home_strength, away_strength, season_stage)
        self.assertIn("season_beginning_standings_unreliable", notes)


class FormTrajectorySignalTests(unittest.TestCase):
    def test_form_trajectory_with_not_started_season(self):
        from app.prediction_agent import form_trajectory_signal

        # Standings where all teams have 0 points and 0 played
        standings = [
            {"position": 1, "points": 0, "played": 0, "team": {"name": "Opponent A", "id": 10}},
            {"position": 2, "points": 0, "played": 0, "team": {"name": "Opponent B", "id": 11}},
        ]
        history = [
            {
                "status": {"type": "finished"},
                "home_team": {"name": "Team", "id": 1},
                "away_team": {"name": "Opponent A", "id": 10},
                "score": {"home": 2, "away": 1},
            },
            {
                "status": {"type": "finished"},
                "home_team": {"name": "Team", "id": 1},
                "away_team": {"name": "Opponent B", "id": 11},
                "score": {"home": 1, "away": 2},
            },
            {
                "status": {"type": "finished"},
                "home_team": {"name": "Team", "id": 1},
                "away_team": {"name": "Opponent A", "id": 10},
                "score": {"home": 3, "away": 0},
            },
            {
                "status": {"type": "finished"},
                "home_team": {"name": "Team", "id": 1},
                "away_team": {"name": "Opponent B", "id": 11},
                "score": {"home": 0, "away": 1},
            },
        ]
        result = form_trajectory_signal(1, history, standings, side="home")
        # Should still be available but with reduced opponent weighting
        self.assertTrue(result["available"])
        # The trajectory should be computed but with reduced opponent quality weighting
        self.assertIn("trajectory", result)


class TableEdgeSeasonAwarenessTests(unittest.TestCase):
    def test_table_edge_not_started_season_is_discounted(self):
        from app.prediction_agent import _table_edge

        event = {
            "pregame_form": {
                "home_team": {"name": "Home", "id": 1, "position": 1},
                "away_team": {"name": "Away", "id": 2, "position": 4},
            },
        }
        signals = []
        # Standings where all teams have 0 points and 0 played
        standings = [
            {"position": 1, "points": 0, "played": 0, "team": {"name": "Home", "id": 1}},
            {"position": 2, "points": 0, "played": 0, "team": {"name": "Other", "id": 3}},
            {"position": 3, "points": 0, "played": 0, "team": {"name": "Third", "id": 4}},
            {"position": 4, "points": 0, "played": 0, "team": {"name": "Away", "id": 2}},
        ]
        edge = _table_edge(event, signals, standings)
        # Edge should be heavily discounted (weight=0.1) because season hasn't started
        # Normal edge would be (4-1)*1.5 = 4.5, discounted to 0.45
        self.assertLess(abs(edge), 1.0)
        self.assertTrue(signals)
        sig = signals[0]["value"]
        self.assertEqual(sig["season_stage"], "not_started")
        self.assertFalse(sig["standings_meaningful"])

    def test_table_edge_in_progress_season_normal(self):
        from app.prediction_agent import _table_edge

        event = {
            "pregame_form": {
                "home_team": {"name": "Home", "id": 1, "position": 1},
                "away_team": {"name": "Away", "id": 2, "position": 4},
            },
        }
        signals = []
        # Standings where teams have played many matches
        standings = [
            {"position": 1, "points": 60, "played": 20, "team": {"name": "Home", "id": 1}},
            {"position": 2, "points": 55, "played": 20, "team": {"name": "Other", "id": 3}},
            {"position": 3, "points": 40, "played": 20, "team": {"name": "Third", "id": 4}},
            {"position": 4, "points": 20, "played": 20, "team": {"name": "Away", "id": 2}},
        ]
        edge = _table_edge(event, signals, standings)
        # Edge should be normal (weight=1.0): (4-1)*1.5 = 4.5
        self.assertAlmostEqual(edge, 4.5, places=1)
        self.assertTrue(signals)
        sig = signals[0]["value"]
        self.assertEqual(sig["season_stage"], "in_progress")
        self.assertTrue(sig["standings_meaningful"])

    def test_table_edge_beginning_season_partially_discounted(self):
        from app.prediction_agent import _table_edge

        event = {
            "pregame_form": {
                "home_team": {"name": "Home", "id": 1, "position": 1},
                "away_team": {"name": "Away", "id": 2, "position": 4},
            },
        }
        signals = []
        # Standings where most teams have played <=2 matches
        standings = [
            {"position": 1, "points": 3, "played": 1, "team": {"name": "Home", "id": 1}},
            {"position": 2, "points": 1, "played": 1, "team": {"name": "Other", "id": 3}},
            {"position": 3, "points": 0, "played": 1, "team": {"name": "Third", "id": 4}},
            {"position": 4, "points": 0, "played": 1, "team": {"name": "Away", "id": 2}},
        ]
        edge = _table_edge(event, signals, standings)
        # Edge should be partially discounted (weight=0.3): (4-1)*1.5*0.3 = 1.35
        self.assertLess(abs(edge), 2.0)
        self.assertTrue(signals)
        sig = signals[0]["value"]
        self.assertEqual(sig["season_stage"], "beginning")
        self.assertFalse(sig["standings_meaningful"])


if __name__ == "__main__":
    unittest.main()
