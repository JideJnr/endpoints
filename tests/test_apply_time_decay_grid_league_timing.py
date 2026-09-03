"""
Tests for the learned per-league goal-timing adjustment reaching the
shared-grid live picks (live_next_goal_grid / live_total_goals_grid /
live_no_goal_grid) in app.ai.prediction_agent._apply_time_decay.

Before this fix, is_goal_timing_pick / the late-goal boost branch only
matched the un-suffixed pick_type strings, so grid picks got the flat clock
decay with no learned league context applied at all -- the exact thing this
function exists to apply once a league has enough resolved match history
(late_goal_memory_signal requires samples >= 2 and smooths toward 0.5, so it
naturally stays near-neutral until there's real history; see
_learned_league_goal_timing_adjustment).

Also covers the sign inversion for "No More Goals": a league whose learned
history favours late goals should make a No More Goals pick LESS confident,
not more (it's the opposite side of the same coin as a "goal still happens"
pick, which should be boosted).
"""
from __future__ import annotations

from unittest.mock import patch

from app.ai.prediction_agent import _apply_time_decay, _time_decay_multiplier


def _picks():
    return [
        {"type": "live_no_goal_grid", "selection": "No More Goals (grid)", "confidence": 80},
        {"type": "live_next_goal_grid", "selection": "Home FC to score next (grid)", "confidence": 80},
        {"type": "live_total_goals_grid", "selection": "Under 2.5 (grid)", "confidence": 80},
        {"type": "match_result", "selection": "Home FC win", "confidence": 80},
    ]


def test_no_league_history_behaves_like_plain_clock_decay():
    """league_adj == 0.0 (no/insufficient history yet) must reproduce the
    exact plain-decay number -- confirms this doesn't change behaviour for
    leagues with no learned signal."""
    with patch("app.ai.prediction_agent._learned_league_goal_timing_adjustment", return_value=0.0):
        result = _apply_time_decay(_picks(), minute=60, is_live=True, late_goal_league=False)
    decay = _time_decay_multiplier(60)
    expected = max(1, min(95, round(80 * decay)))
    by_type = {p["type"]: p["confidence"] for p in result}
    assert by_type["live_no_goal_grid"] == expected
    assert by_type["live_next_goal_grid"] == expected
    assert by_type["live_total_goals_grid"] == expected


def test_late_goal_league_reduces_no_goal_grid_confidence_vs_plain_decay():
    decay = _time_decay_multiplier(60)
    plain = max(1, min(95, round(80 * decay)))
    with patch("app.ai.prediction_agent._learned_league_goal_timing_adjustment", return_value=0.18):
        result = _apply_time_decay(_picks(), minute=60, is_live=True, late_goal_league=False)
    by_type = {p["type"]: p["confidence"] for p in result}
    # Inverted sign: (1 - 0.18) multiplier -- strictly less than plain decay.
    assert by_type["live_no_goal_grid"] < plain
    assert by_type["live_no_goal_grid"] == max(1, min(95, round(80 * decay * (1 - 0.18))))


def test_late_goal_league_raises_next_goal_and_total_goals_grid_confidence():
    decay = _time_decay_multiplier(60)
    plain = max(1, min(95, round(80 * decay)))
    with patch("app.ai.prediction_agent._learned_league_goal_timing_adjustment", return_value=0.18):
        result = _apply_time_decay(_picks(), minute=60, is_live=True, late_goal_league=False)
    by_type = {p["type"]: p["confidence"] for p in result}
    assert by_type["live_next_goal_grid"] > plain
    assert by_type["live_total_goals_grid"] > plain
    assert by_type["live_next_goal_grid"] == max(1, min(95, round(80 * decay * 1.18)))
    assert by_type["live_total_goals_grid"] == max(1, min(95, round(80 * decay * 1.18)))


def test_non_goal_timing_pick_is_untouched_by_league_adjustment():
    with patch("app.ai.prediction_agent._learned_league_goal_timing_adjustment", return_value=0.18):
        result = _apply_time_decay(_picks(), minute=60, is_live=True, late_goal_league=False)
    decay = _time_decay_multiplier(60)
    plain = max(1, min(95, round(80 * decay)))
    by_type = {p["type"]: p["confidence"] for p in result}
    assert by_type["match_result"] == plain
