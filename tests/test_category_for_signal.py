"""Unit tests for _category_for_signal() direction-aware fallback (R10).

Covers:
  - R10.1: Exact-match loop still works (signals in SIGNAL_CATEGORIES).
  - R10.2: "away" + "recent_history" → "away_form" (not "home_form").
  - R10.3: "away" + "table"/"standing"/"position"/"league_strength" → "away_table".
  - R10.4: "away" + "goal"/"attack"/"scoring"/"pressure" → "away_goal_pressure".
  - R10.5: "away" + "odds"/"market"/"steam" → "away_odds".
  - R10.6: "home" equivalent mappings → home_* categories.
  - R10.7: Signal with neither "home" nor "away" → existing non-directional fallback.

Key correctness property: "away" is checked before "home", so a name containing
both (which should not occur in practice but must be handled) resolves to "away_*".
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.enrichment.signal_aggregator import _category_for_signal


# ---------------------------------------------------------------------------
# Step 1 — Exact match still takes priority
# ---------------------------------------------------------------------------

class TestExactMatchStep1:
    """Signals present in SIGNAL_CATEGORIES are returned without hitting the fallback."""

    def test_home_form_exact(self):
        assert _category_for_signal("home_form") == "home_form"

    def test_away_form_exact(self):
        assert _category_for_signal("away_form") == "away_form"

    def test_home_table_exact(self):
        assert _category_for_signal("home_table") == "home_table"

    def test_away_table_exact(self):
        assert _category_for_signal("away_table") == "away_table"

    def test_h2h_home_exact(self):
        assert _category_for_signal("h2h_home") == "h2h_home"

    def test_home_goal_pressure_exact(self):
        assert _category_for_signal("home_goal_pressure") == "home_goal_pressure"

    def test_away_goal_pressure_exact(self):
        assert _category_for_signal("away_goal_pressure") == "away_goal_pressure"

    def test_home_defense_exact(self):
        assert _category_for_signal("home_defense") == "home_defense"

    def test_away_defense_exact(self):
        assert _category_for_signal("away_defense") == "away_defense"


# ---------------------------------------------------------------------------
# Step 2 — Direction-aware "away" fallback (R10.2–R10.5)
# ---------------------------------------------------------------------------

class TestAwayDirectionFallback:
    """Signal names containing 'away' but not in SIGNAL_CATEGORIES resolve to away_* categories."""

    # R10.2 — form keywords
    def test_away_recent_history_resolves_to_away_form(self):
        """Spec-mandated example: 'away_recent_history' → 'away_form'."""
        assert _category_for_signal("away_recent_history") == "away_form"

    def test_away_team_watcher_resolves_to_away_form(self):
        assert _category_for_signal("away_team_watcher") == "away_form"

    def test_away_last5_form_resolves_to_away_form(self):
        assert _category_for_signal("away_last5_form") == "away_form"

    def test_away_wd_record_resolves_to_away_form(self):
        assert _category_for_signal("away_wd_record") == "away_form"

    # R10.3 — table/standing/position/league_strength keywords
    def test_away_table_position_resolves_to_away_table(self):
        """Spec-mandated example: 'away_table_position' → 'away_table'."""
        assert _category_for_signal("away_table_position") == "away_table"

    def test_away_league_standing_resolves_to_away_table(self):
        assert _category_for_signal("away_league_standing") == "away_table"

    def test_away_current_position_resolves_to_away_table(self):
        assert _category_for_signal("away_current_position") == "away_table"

    def test_away_league_strength_signal_resolves_to_away_table(self):
        assert _category_for_signal("away_league_strength_signal") == "away_table"

    # R10.4 — goal/attack/scoring/pressure keywords
    def test_away_goal_threat_resolves_to_away_goal_pressure(self):
        assert _category_for_signal("away_goal_threat") == "away_goal_pressure"

    def test_away_attack_strength_resolves_to_away_goal_pressure(self):
        assert _category_for_signal("away_attack_strength") == "away_goal_pressure"

    def test_away_scoring_rate_resolves_to_away_goal_pressure(self):
        assert _category_for_signal("away_scoring_rate") == "away_goal_pressure"

    def test_away_pressure_index_resolves_to_away_goal_pressure(self):
        assert _category_for_signal("away_pressure_index") == "away_goal_pressure"

    # R10.5 — odds/market/steam keywords
    def test_away_odds_movement_resolves_to_away_odds(self):
        assert _category_for_signal("away_odds_movement") == "away_odds"

    def test_away_market_signal_resolves_to_away_odds(self):
        assert _category_for_signal("away_market_signal") == "away_odds"

    def test_away_steam_move_resolves_to_away_odds(self):
        assert _category_for_signal("away_steam_move") == "away_odds"

    # defense keywords
    def test_away_defense_record_resolves_to_away_defense(self):
        assert _category_for_signal("away_defense_record") == "away_defense"

    def test_away_conceding_rate_resolves_to_away_defense(self):
        assert _category_for_signal("away_conceding_rate") == "away_defense"

    def test_away_clean_sheet_chance_resolves_to_away_defense(self):
        assert _category_for_signal("away_clean_sheet_chance") == "away_defense"


# ---------------------------------------------------------------------------
# Step 2 — Direction-aware "home" fallback (R10.6)
# ---------------------------------------------------------------------------

class TestHomeDirectionFallback:
    """Signal names containing 'home' (no 'away') resolve to home_* categories."""

    # form keywords
    def test_home_recent_history_resolves_to_home_form(self):
        assert _category_for_signal("home_recent_history") == "home_form"

    def test_home_team_watcher_resolves_to_home_form(self):
        assert _category_for_signal("home_team_watcher") == "home_form"

    def test_home_wd_record_resolves_to_home_form(self):
        assert _category_for_signal("home_wd_record") == "home_form"

    # table keywords
    def test_home_table_position_resolves_to_home_table(self):
        assert _category_for_signal("home_table_position") == "home_table"

    def test_home_league_standing_resolves_to_home_table(self):
        assert _category_for_signal("home_league_standing") == "home_table"

    def test_home_league_strength_signal_resolves_to_home_table(self):
        assert _category_for_signal("home_league_strength_signal") == "home_table"

    # goal/pressure keywords
    def test_home_goal_pressure_strong_resolves_to_home_goal_pressure(self):
        """Spec-mandated example: 'home_goal_pressure_strong' → 'home_goal_pressure'."""
        assert _category_for_signal("home_goal_pressure_strong") == "home_goal_pressure"

    def test_home_attack_strength_resolves_to_home_goal_pressure(self):
        assert _category_for_signal("home_attack_strength") == "home_goal_pressure"

    def test_home_scoring_rate_resolves_to_home_goal_pressure(self):
        assert _category_for_signal("home_scoring_rate") == "home_goal_pressure"

    # odds keywords
    def test_home_odds_movement_resolves_to_home_odds(self):
        assert _category_for_signal("home_odds_movement") == "home_odds"

    def test_home_market_signal_resolves_to_home_odds(self):
        assert _category_for_signal("home_market_signal") == "home_odds"

    # defense keywords
    def test_home_defense_record_resolves_to_home_defense(self):
        assert _category_for_signal("home_defense_record") == "home_defense"

    def test_home_conceding_rate_resolves_to_home_defense(self):
        assert _category_for_signal("home_conceding_rate") == "home_defense"

    def test_home_clean_sheet_chance_resolves_to_home_defense(self):
        assert _category_for_signal("home_clean_sheet_chance") == "home_defense"


# ---------------------------------------------------------------------------
# Correct direction priority: "away" checked before "home"
# ---------------------------------------------------------------------------

class TestAwayCheckedBeforeHome:
    """A name containing 'away' must never be routed to a home_* category (R10.1)."""

    def test_away_recent_history_is_not_home_form(self):
        result = _category_for_signal("away_recent_history")
        assert result != "home_form", (
            f"'away_recent_history' should resolve to 'away_form', got '{result}'"
        )
        assert result == "away_form"

    def test_away_table_standing_is_not_home_table(self):
        result = _category_for_signal("away_standing")
        assert result != "home_table"
        assert result == "away_table"

    def test_away_goal_pressure_not_home_goal_pressure(self):
        result = _category_for_signal("away_goal_signal")
        assert result != "home_goal_pressure"
        assert result == "away_goal_pressure"


# ---------------------------------------------------------------------------
# Step 3 — Non-directional fallbacks unchanged (R10.7)
# ---------------------------------------------------------------------------

class TestNonDirectionalFallback:
    """Signals with neither 'home' nor 'away' use the original fallback chain."""

    def test_h2h_resolves_to_h2h_home(self):
        # 'h2h' alone is not in SIGNAL_CATEGORIES, falls through to non-directional
        assert _category_for_signal("h2h_strength") == "h2h_home"

    def test_goal_without_direction_resolves_to_home_goal_pressure(self):
        assert _category_for_signal("goal_threat") == "home_goal_pressure"

    def test_form_without_direction_resolves_to_home_form(self):
        assert _category_for_signal("recent_form") == "home_form"

    def test_table_without_direction_resolves_to_home_table(self):
        assert _category_for_signal("league_table") == "home_table"

    def test_odds_without_direction_resolves_to_home_odds(self):
        assert _category_for_signal("market_value") == "home_odds"

    def test_unknown_signal_resolves_to_unknown(self):
        assert _category_for_signal("completely_unrecognised_signal_xyz") == "unknown"

    def test_empty_string_resolves_to_unknown(self):
        assert _category_for_signal("") == "unknown"
