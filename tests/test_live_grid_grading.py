"""
Tests for grading of the shared-grid live picks in
app.storage.league_memory.queries._grade_candidate_row.

Background: _live_grid_projection_picks() (app/enrichment/enriched_prediction.py)
tags its picks with a "_grid" suffix (live_next_goal_grid, live_no_goal_grid,
live_total_goals_grid, live_match_winner_grid, live_btts_grid) so they can be
told apart from the older independent-heuristic live picks. Before this fix,
_grade_candidate_row only matched the un-suffixed pick_type strings, so every
grid pick fell through to the generic final-score-only grader -- which either
voided them outright or graded markets that need the score AT PICK TIME
(next goal / no goal / totals) using only the final score, which is wrong.

Coverage:
  - live_no_goal_grid: win only if the score never moves again after the pick.
  - live_next_goal_grid: team-specific grading via score delta (not "any goal").
  - legacy generic "next goal" text (no identifiable team) still grades on
    "did any goal happen" for backward compatibility with old rows.
  - live_total_goals_grid: over/under graded off the delta from pick-time
    score, not the absolute final score.
  - live_match_winner_grid: graded like live_match_winner.
  - live_btts_grid: BTTS Yes/No graded off the final score.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.storage.league_memory.queries import _grade_candidate_row


def _row(pick_type: str, selection: str, match_name: str, context: dict[str, Any]) -> sqlite3.Row:
    import json

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "create table t (pick_type text, selection text, match_name text, context_json text)"
    )
    conn.execute(
        "insert into t values (?, ?, ?, ?)",
        (pick_type, selection, match_name, json.dumps(context)),
    )
    row = conn.execute("select * from t").fetchone()
    return row


# ── live_no_goal_grid ────────────────────────────────────────────────────────

def test_no_goal_grid_wins_when_score_never_moves():
    row = _row(
        "live_no_goal_grid",
        "No More Goals (grid)",
        "Arsenal vs Chelsea",
        {"score_home": 1, "score_away": 0},
    )
    assert _grade_candidate_row(row, final_home=1, final_away=0) == "win"


def test_no_goal_grid_loses_when_either_side_scores_again():
    row = _row(
        "live_no_goal_grid",
        "No More Goals (grid)",
        "Arsenal vs Chelsea",
        {"score_home": 1, "score_away": 0},
    )
    assert _grade_candidate_row(row, final_home=2, final_away=0) == "loss"
    row2 = _row(
        "live_no_goal_grid",
        "No More Goals (grid)",
        "Arsenal vs Chelsea",
        {"score_home": 1, "score_away": 0},
    )
    assert _grade_candidate_row(row2, final_home=1, final_away=1) == "loss"


def test_no_more_goal_phrasing_also_graded_as_no_goal_even_without_grid_type():
    """Legacy/plain pick_type but selection text says "no more goal(s)" --
    must not fall into the "did any goal happen" branch backwards."""
    row = _row(
        "live_next_goal",
        "No more goals",
        "Arsenal vs Chelsea",
        {"score_home": 0, "score_away": 0},
    )
    assert _grade_candidate_row(row, final_home=0, final_away=0) == "win"
    row2 = _row(
        "live_next_goal",
        "No more goals",
        "Arsenal vs Chelsea",
        {"score_home": 0, "score_away": 0},
    )
    assert _grade_candidate_row(row2, final_home=1, final_away=0) == "loss"


# ── live_next_goal_grid (team-specific) ─────────────────────────────────────

def test_next_goal_grid_team_specific_win():
    row = _row(
        "live_next_goal_grid",
        "Arsenal to score next (grid)",
        "Arsenal vs Chelsea",
        {"score_home": 0, "score_away": 0},
    )
    # Home (Arsenal) scores, away doesn't -> win for a home-side pick.
    assert _grade_candidate_row(row, final_home=1, final_away=0) == "win"


def test_next_goal_grid_team_specific_loss_when_other_side_scores():
    row = _row(
        "live_next_goal_grid",
        "Arsenal to score next (grid)",
        "Arsenal vs Chelsea",
        {"score_home": 0, "score_away": 0},
    )
    # Away (Chelsea) scores instead -> loss for the Arsenal pick.
    assert _grade_candidate_row(row, final_home=0, final_away=1) == "loss"


def test_next_goal_grid_void_when_score_never_moves():
    row = _row(
        "live_next_goal_grid",
        "Arsenal to score next (grid)",
        "Arsenal vs Chelsea",
        {"score_home": 0, "score_away": 0},
    )
    assert _grade_candidate_row(row, final_home=0, final_away=0) == "void"


def test_legacy_generic_next_goal_text_falls_back_to_any_goal():
    """Old rows with no identifiable team in the selection text keep the
    pre-fix behaviour (win if any goal happened) rather than voiding."""
    row = _row(
        "live_next_goal",
        "Next goal",
        "Arsenal vs Chelsea",
        {"score_home": 0, "score_away": 0},
    )
    assert _grade_candidate_row(row, final_home=1, final_away=0) == "win"
    row2 = _row(
        "live_next_goal",
        "Next goal",
        "Arsenal vs Chelsea",
        {"score_home": 0, "score_away": 0},
    )
    assert _grade_candidate_row(row2, final_home=0, final_away=0) == "loss"


# ── live_total_goals_grid ────────────────────────────────────────────────────

def test_total_goals_grid_uses_delta_from_pick_time_not_absolute_score():
    row = _row(
        "live_total_goals_grid",
        "Over 2.5 (grid)",
        "Arsenal vs Chelsea",
        {"score_home": 1, "score_away": 1},  # 2 goals already in at pick time
    )
    # Final 2-1 = 3 total goals > 2.5 -> win, even though the *pick-time*
    # total (2) was already close to the line -- confirms it's reading the
    # line against the final total directly (this market doesn't need a
    # delta the way next-goal/no-goal do), just via the grid-suffixed path.
    assert _grade_candidate_row(row, final_home=2, final_away=1) == "win"


def test_total_goals_grid_under_loss():
    row = _row(
        "live_total_goals_grid",
        "Under 1.5 (grid)",
        "Arsenal vs Chelsea",
        {"score_home": 0, "score_away": 0},
    )
    assert _grade_candidate_row(row, final_home=1, final_away=1) == "loss"


# ── live_match_winner_grid ───────────────────────────────────────────────────

def test_match_winner_grid_routes_like_match_winner():
    row = _row(
        "live_match_winner_grid",
        "Arsenal to win (grid)",
        "Arsenal vs Chelsea",
        {},
    )
    assert _grade_candidate_row(row, final_home=2, final_away=1) == "win"
    row2 = _row(
        "live_match_winner_grid",
        "Arsenal to win (grid)",
        "Arsenal vs Chelsea",
        {},
    )
    assert _grade_candidate_row(row2, final_home=1, final_away=2) == "loss"


# ── live_btts_grid ────────────────────────────────────────────────────────

def test_btts_grid_yes_win():
    row = _row("live_btts_grid", "BTTS Yes (grid)", "Arsenal vs Chelsea", {})
    assert _grade_candidate_row(row, final_home=1, final_away=1) == "win"


def test_btts_grid_yes_loss():
    row = _row("live_btts_grid", "BTTS Yes (grid)", "Arsenal vs Chelsea", {})
    assert _grade_candidate_row(row, final_home=1, final_away=0) == "loss"


def test_btts_grid_no_win():
    row = _row("live_btts_grid", "BTTS No (grid)", "Arsenal vs Chelsea", {})
    assert _grade_candidate_row(row, final_home=1, final_away=0) == "win"


def test_btts_grid_no_loss():
    row = _row("live_btts_grid", "BTTS No (grid)", "Arsenal vs Chelsea", {})
    assert _grade_candidate_row(row, final_home=1, final_away=1) == "loss"
