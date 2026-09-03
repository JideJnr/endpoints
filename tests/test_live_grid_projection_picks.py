"""
Tests for the next-goal / no-goal publishing logic in
app.enrichment.enriched_prediction._live_grid_projection_picks.

Before this fix, this function only ever asked "does a team clear 0.40" and
never considered "No More Goals" as a publishable pick in its own right, even
when the grid rated it the most likely of the three outcomes. This is now a
proper three-way read (home scores next / away scores next / no more goals)
gated at the same 0.55 bar every sibling market in this function already
uses.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from app.enrichment.enriched_prediction import _live_grid_projection_picks


class _FakeProjection:
    def __init__(self, next_goal: dict[str, float]):
        self.grid = {(0, 0): 1.0}
        self.diagnostics: dict[str, Any] = {}
        self.home_lambda_remaining = 0.3
        self.away_lambda_remaining = 0.2
        self._next_goal = next_goal

    def match_winner(self) -> dict[str, float]:
        # Deliberately below the 0.55 publish bar so this test only exercises
        # the next-goal/no-goal branch.
        return {"home_win": 0.34, "draw": 0.33, "away_win": 0.33}

    def btts(self) -> float:
        return 0.5

    def over_under(self, line: float) -> dict[str, float]:
        return {"over": 0.5, "under": 0.5}

    def next_goal(self) -> dict[str, float]:
        return self._next_goal


def _doc() -> dict[str, Any]:
    return {
        "home_team": "Home FC",
        "away_team": "Away FC",
        "score": {"home": 1, "away": 0},
        "live_statistics": {"summary": {"home_shots": 5, "away_shots": 3}},
    }


def _run(next_goal: dict[str, float]) -> list[dict[str, Any]]:
    doc = _doc()
    detail: dict[str, Any] = {}
    poisson = {"home_lambda": 1.4, "away_lambda": 1.1}
    dixon = {"home_lambda": 1.4, "away_lambda": 1.1}
    fake = _FakeProjection(next_goal)
    with patch("app.live.live_projection.project_live_match", return_value=fake):
        return _live_grid_projection_picks(doc, detail, poisson, dixon, minute=60)


def test_publishes_no_more_goals_when_it_is_the_most_likely_outcome():
    picks = _run({"no_more_goals": 0.70, "home_scores_next": 0.20, "away_scores_next": 0.10})
    grid_next_goal_picks = [p for p in picks if p["type"] in {"live_no_goal_grid", "live_next_goal_grid"}]
    assert len(grid_next_goal_picks) == 1
    pick = grid_next_goal_picks[0]
    assert pick["type"] == "live_no_goal_grid"
    assert "No More Goals" in pick["selection"]
    assert pick["confidence"] == 70


def test_publishes_team_to_score_next_when_it_is_the_most_likely_outcome():
    picks = _run({"no_more_goals": 0.25, "home_scores_next": 0.60, "away_scores_next": 0.15})
    grid_next_goal_picks = [p for p in picks if p["type"] in {"live_no_goal_grid", "live_next_goal_grid"}]
    assert len(grid_next_goal_picks) == 1
    pick = grid_next_goal_picks[0]
    assert pick["type"] == "live_next_goal_grid"
    assert "Home FC" in pick["selection"]
    assert pick["confidence"] == 60


def test_publishes_nothing_when_no_outcome_clears_the_bar():
    # Old code would have published "Home FC to score next" here since 0.40
    # cleared its old (weaker) bar -- none of these three clears 0.55, so
    # nothing from this market should be published now.
    picks = _run({"no_more_goals": 0.40, "home_scores_next": 0.35, "away_scores_next": 0.25})
    grid_next_goal_picks = [p for p in picks if p["type"] in {"live_no_goal_grid", "live_next_goal_grid"}]
    assert grid_next_goal_picks == []
