from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any


from app.utils.primitives import _safe_num

# ── Table size categories ──────────────────────────────────────────────

SMALL_LEAGUE_MAX = 8
MEDIUM_LEAGUE_MAX = 16


def classify_table_size(standings: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify league table size and return awareness metadata.

    Small leagues (<=8 teams) have different competitive dynamics than
    large leagues (20+ teams).  The bottom positions in a 4-team league
    are far less meaningful than in a 24-team league.
    """
    table_size = len(standings or [])
    if table_size <= SMALL_LEAGUE_MAX:
        category = "small"
    elif table_size <= MEDIUM_LEAGUE_MAX:
        category = "medium"
    else:
        category = "large"

    return {
        "table_size": table_size,
        "category": category,
        "is_small_league": category == "small",
        "is_medium_league": category == "medium",
        "is_large_league": category == "large",
        # Bottom N positions that are effectively "relegation zone"
        # scale with league size so small leagues don't over-penalise.
        "bottom_zone_cutoff": _bottom_zone_cutoff(table_size),
        # Top N positions that are "title race" contenders
        "title_race_cutoff": _title_race_cutoff(table_size),
    }


def _bottom_zone_cutoff(table_size: int) -> int:
    if table_size <= 0:
        return 0
    if table_size <= 4:
        return 1  # in a 4-team league, only last place is bottom
    if table_size <= 8:
        return 2
    if table_size <= 16:
        return 3
    return max(2, table_size // 5)  # ~20% of the table


def _title_race_cutoff(table_size: int) -> int:
    if table_size <= 0:
        return 0
    if table_size <= 4:
        return 2  # top half in a 4-team league
    if table_size <= 8:
        return 2
    if table_size <= 16:
        return 4
    return max(3, table_size // 5)  # ~20% of the table


# ── Season stage detection ─────────────────────────────────────────────

def detect_season_stage(
    standings: list[dict[str, Any]],
    match_date: str | date | None = None,
    tournament_name: str = "",
) -> dict[str, Any]:
    """Detect whether a season has started, is beginning, or hasn't started.

    Uses multiple signals from standings data:
    1. If all teams have 0 points and 0 played → season not started
    2. If most teams have very few matches played (<=2) → season beginning
    3. If standings are empty → no data available
    4. Otherwise → season in progress

    Returns metadata that prediction code can use to adjust weighting.
    """
    if match_date is None:
        match_date = date.today()
    if isinstance(match_date, str):
        try:
            match_date = date.fromisoformat(match_date)
        except ValueError:
            match_date = date.today()

    standings = standings or []
    table_size = len(standings)

    if table_size == 0:
        return {
            "stage": "no_data",
            "confidence": "low",
            "reason": "no standings data available",
            "table_size": 0,
            "all_zero_points": False,
            "most_teams_unplayed": False,
            "season_started": False,
            "season_beginning": False,
            "season_not_started": False,
            "standings_meaningful": False,
            "table_context": "no_standings_data",
        }

    # Count teams with 0 played and 0 points
    zero_played = 0
    zero_points = 0
    low_played = 0  # <= 2 matches played
    total_played = 0
    max_position = 0

    for row in standings:
        played = int(_safe_num(row.get("played")) or 0)
        points = int(_safe_num(row.get("points")) or 0)
        position = int(_safe_num(row.get("position")) or 0)
        total_played += played
        max_position = max(max_position, position)
        if played == 0:
            zero_played += 1
        if points == 0:
            zero_points += 1
        if played <= 2:
            low_played += 1

    all_zero_points = (zero_points == table_size)
    all_zero_played = (zero_played == table_size)
    most_teams_unplayed = (low_played >= table_size * 0.7)
    avg_matches = total_played / table_size if table_size > 0 else 0

    # Determine season stage
    if all_zero_played:
        stage = "not_started"
        confidence = "high"
        reason = "all teams have 0 matches played"
    elif all_zero_points and avg_matches > 0 and avg_matches < 1.5:
        # All teams have 0 points but some matches have been played.
        # The season has technically started, but standings are still
        # unreliable because no team has scored.
        stage = "beginning"
        confidence = "high"
        reason = "all teams have 0 points but matches have been played"
    elif all_zero_points and avg_matches < 1.5:
        stage = "not_started"
        confidence = "high"
        reason = "all teams have 0 points and very few matches played"
    elif most_teams_unplayed and avg_matches < 3:
        stage = "beginning"
        confidence = "high"
        reason = f"most teams have played <=2 matches (avg {avg_matches:.1f})"
    elif avg_matches < 5 and low_played >= table_size * 0.5:
        stage = "beginning"
        confidence = "medium"
        reason = f"season early, avg {avg_matches:.1f} matches played"
    else:
        stage = "in_progress"
        confidence = "high" if avg_matches >= 5 else "medium"
        reason = f"season in progress, avg {avg_matches:.1f} matches played"

    # Determine if standings are meaningful for prediction
    # When season hasn't started or is just beginning, standings are
    # unreliable because teams haven't played enough matches.
    standings_meaningful = stage == "in_progress" and avg_matches >= 3

    return {
        "stage": stage,
        "confidence": confidence,
        "reason": reason,
        "table_size": table_size,
        "all_zero_points": all_zero_points,
        "all_zero_played": all_zero_played,
        "most_teams_unplayed": most_teams_unplayed,
        "avg_matches_played": round(avg_matches, 2),
        "zero_played_count": zero_played,
        "zero_points_count": zero_points,
        "low_played_count": low_played,
        "season_started": stage != "not_started",
        "season_beginning": stage == "beginning",
        "season_not_started": stage == "not_started",
        "standings_meaningful": standings_meaningful,
        "table_context": _table_context(stage, avg_matches, table_size),
    }


def _table_context(
    stage: str,
    avg_matches: float,
    table_size: int,
) -> str:
    if stage == "not_started":
        return "season_not_started"
    if stage == "beginning":
        return "season_beginning"
    if avg_matches < 5:
        return "season_early"
    return "season_in_progress"

def season_aware_table_weight(
    position: int,
    table_size: int,
    season_stage: dict[str, Any],
) -> float:
    """Compute a table-weight that accounts for season stage and table size.

    When a season hasn't started or is just beginning, the table position
    is less meaningful, so we reduce the weight.  Small leagues also get
    adjusted weighting because bottom positions are less statistically
    significant.
    """
    if table_size < 2 or position <= 0:
        return 1.0

    base_weight = (table_size - position) / (table_size - 1)

    # Season stage adjustment
    stage_multiplier = 1.0
    stage = season_stage.get("stage", "in_progress")
    if stage == "not_started":
        # Standings are completely meaningless — return the minimum weight
        # so table position doesn't influence the comparison at all.
        return 0.75
    elif stage == "beginning":
        # Standings are unreliable, reduce weight
        avg_matches = season_stage.get("avg_matches_played", 0)
        if avg_matches < 1:
            stage_multiplier = 0.2
        elif avg_matches < 3:
            stage_multiplier = 0.5
        else:
            stage_multiplier = 0.75
    elif stage == "season_early":
        stage_multiplier = 0.85

    # Table size adjustment
    size_multiplier = 1.0
    if table_size <= SMALL_LEAGUE_MAX:
        # Small leagues: bottom positions are less meaningful
        # and the table is more volatile
        if position > table_size * 0.75:
            size_multiplier = 0.7  # reduce weight for bottom teams in small leagues
        elif position <= table_size * 0.25:
            size_multiplier = 1.1  # slightly boost top teams in small leagues
    elif table_size <= MEDIUM_LEAGUE_MAX:
        if position > table_size * 0.8:
            size_multiplier = 0.85

    return round(0.75 + max(0.0, min(1.0, base_weight * stage_multiplier * size_multiplier)) * 0.70, 2)
