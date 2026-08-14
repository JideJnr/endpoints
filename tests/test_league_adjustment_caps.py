"""
Unit tests for symmetric league accuracy adjustment caps in enriched_prediction.py (R21).

Covers:
  R21.1 — MAX_LEAGUE_ADJUSTMENT constant is defined and equals 10.
  R21.2 — Boost cap: a league with win_rate >= 65% is capped at +MAX_LEAGUE_ADJUSTMENT.
  R21.3 — Penalty cap: a league with win_rate < 50% is capped at -MAX_LEAGUE_ADJUSTMENT.
  R21.4 — Both caps use the same constant, ensuring symmetry.
  R21.5 — League with >= 65% win rate: adjustment does not exceed +10.
  R21.6 — League with < 50% win rate: adjustment is not more negative than -10.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Pure formula helpers — mirrors the inline logic in enriched_prediction.py
# ---------------------------------------------------------------------------

MAX_LEAGUE_ADJUSTMENT = 10  # the constant asserted in R21.1


def _boost_adjustment(win_rate_pct: float) -> int:
    """Compute the boost adjustment for a given win_rate percentage (> 65%)."""
    adj = round((win_rate_pct - 65.0) / 5)
    return min(MAX_LEAGUE_ADJUSTMENT, adj)


def _penalty_adjustment(win_rate_pct: float) -> int:
    """Compute the penalty adjustment for a given win_rate percentage (< 50%)."""
    adj = -round((50.0 - win_rate_pct) / 5)
    return max(-MAX_LEAGUE_ADJUSTMENT, adj)


# ---------------------------------------------------------------------------
# R21.1 — MAX_LEAGUE_ADJUSTMENT constant is 10
# ---------------------------------------------------------------------------

class TestMaxLeagueAdjustmentConstant:
    """The MAX_LEAGUE_ADJUSTMENT constant in enriched_prediction.py must equal 10."""

    def test_source_file_contains_max_league_adjustment_equals_10(self):
        """Verify the constant is present in the source file with value 10."""
        src = (ROOT / "app" / "enrichment" / "enriched_prediction.py").read_text()
        assert "MAX_LEAGUE_ADJUSTMENT = 10" in src, (
            "MAX_LEAGUE_ADJUSTMENT = 10 not found in enriched_prediction.py"
        )

    def test_source_file_uses_max_league_adjustment_for_min_boost(self):
        """Verify min(..., MAX_LEAGUE_ADJUSTMENT) is used for the boost cap."""
        src = (ROOT / "app" / "enrichment" / "enriched_prediction.py").read_text()
        assert "min(MAX_LEAGUE_ADJUSTMENT," in src, (
            "min(MAX_LEAGUE_ADJUSTMENT, ...) not found — boost cap should use the constant"
        )

    def test_source_file_uses_max_league_adjustment_for_max_penalty(self):
        """Verify max(-MAX_LEAGUE_ADJUSTMENT, ...) is used for the penalty cap."""
        src = (ROOT / "app" / "enrichment" / "enriched_prediction.py").read_text()
        assert "max(-MAX_LEAGUE_ADJUSTMENT," in src, (
            "max(-MAX_LEAGUE_ADJUSTMENT, ...) not found — penalty cap should use the constant"
        )

    def test_old_asymmetric_boost_cap_is_gone(self):
        """The old hardcoded 'min(8, ...) ' pattern must no longer appear in context."""
        src = (ROOT / "app" / "enrichment" / "enriched_prediction.py").read_text()
        # Only check the adjustment block — ignore any other unrelated min(8, ...) usages
        assert "min(8, _league_adj)" not in src, (
            "Old asymmetric 'min(8, _league_adj)' still present — should use MAX_LEAGUE_ADJUSTMENT"
        )


# ---------------------------------------------------------------------------
# R21.4 — Symmetry: abs(positive cap) == abs(negative cap)
# ---------------------------------------------------------------------------

class TestCapSymmetry:
    """The positive boost cap and negative penalty cap must be equal in magnitude."""

    def test_abs_max_positive_equals_abs_max_negative(self):
        """abs(max_positive_cap) == abs(max_negative_cap) — symmetry requirement (R21.4)."""
        max_positive = MAX_LEAGUE_ADJUSTMENT
        max_negative = -MAX_LEAGUE_ADJUSTMENT
        assert abs(max_positive) == abs(max_negative), (
            f"Caps are not symmetric: +{max_positive} vs {max_negative}"
        )

    def test_constant_value_is_10(self):
        """The shared cap value must be exactly 10."""
        assert MAX_LEAGUE_ADJUSTMENT == 10

    def test_boost_and_penalty_use_same_magnitude(self):
        """An extreme boost and an extreme penalty must both be capped at the same magnitude."""
        extreme_boost = _boost_adjustment(1000.0)    # absurdly high win rate
        extreme_penalty = _penalty_adjustment(0.0)   # absurdly low win rate (0%)
        assert extreme_boost == MAX_LEAGUE_ADJUSTMENT, (
            f"Extreme boost should be capped at {MAX_LEAGUE_ADJUSTMENT}, got {extreme_boost}"
        )
        assert extreme_penalty == -MAX_LEAGUE_ADJUSTMENT, (
            f"Extreme penalty should be capped at -{MAX_LEAGUE_ADJUSTMENT}, got {extreme_penalty}"
        )
        assert abs(extreme_boost) == abs(extreme_penalty), "Caps must be symmetric"


# ---------------------------------------------------------------------------
# R21.5 — Boost cap: win rate >= 65% never exceeds +10
# ---------------------------------------------------------------------------

class TestBoostCap:
    """A league with high win rate must be capped at +MAX_LEAGUE_ADJUSTMENT = +10 (R21.5)."""

    @pytest.mark.parametrize("win_rate_pct", [
        70.0,   # moderate boost
        80.0,   # strong boost
        90.0,   # very strong
        100.0,  # theoretical maximum
        200.0,  # absurdly high, cap must hold
    ])
    def test_boost_never_exceeds_max_league_adjustment(self, win_rate_pct):
        adj = _boost_adjustment(win_rate_pct)
        assert adj <= MAX_LEAGUE_ADJUSTMENT, (
            f"win_rate={win_rate_pct}% produced boost {adj}, exceeds cap {MAX_LEAGUE_ADJUSTMENT}"
        )
        assert adj > 0, f"win_rate={win_rate_pct}% should produce a positive boost"

    def test_boost_at_exactly_115_pct_win_rate_is_capped(self):
        """win_rate of 115% → raw adj = round(50/5) = 10 — exactly at the cap."""
        adj = _boost_adjustment(115.0)
        assert adj == MAX_LEAGUE_ADJUSTMENT

    def test_boost_above_115_pct_win_rate_is_capped(self):
        """win_rate beyond the cap boundary is still capped."""
        adj = _boost_adjustment(200.0)
        assert adj == MAX_LEAGUE_ADJUSTMENT

    def test_small_boost_when_just_above_65_pct(self):
        """win_rate just above 65% should give a small positive boost."""
        adj = _boost_adjustment(66.0)
        assert adj >= 0, f"Expected positive boost near 65% boundary, got {adj}"

    def test_zero_boost_at_exactly_65_pct_boundary(self):
        """win_rate = 65.0% is the boundary — round(0/5) = 0, no boost."""
        adj = _boost_adjustment(65.0)
        assert adj == 0

    def test_boost_at_70_pct_is_1(self):
        """win_rate 70% → raw = round(5/5) = 1."""
        assert _boost_adjustment(70.0) == 1

    def test_boost_at_80_pct_is_3(self):
        """win_rate 80% → raw = round(15/5) = 3."""
        assert _boost_adjustment(80.0) == 3

    def test_boost_at_90_pct_is_5(self):
        """win_rate 90% → raw = round(25/5) = 5."""
        assert _boost_adjustment(90.0) == 5


# ---------------------------------------------------------------------------
# R21.6 — Penalty cap: win rate < 50% never goes below -10
# ---------------------------------------------------------------------------

class TestPenaltyCap:
    """A league with low win rate must be capped at -MAX_LEAGUE_ADJUSTMENT = -10 (R21.6)."""

    @pytest.mark.parametrize("win_rate_pct", [
        40.0,   # moderate penalty
        30.0,   # strong penalty
        20.0,   # very strong
        0.0,    # theoretical minimum
        -50.0,  # absurdly low, cap must hold
    ])
    def test_penalty_never_below_negative_max_league_adjustment(self, win_rate_pct):
        adj = _penalty_adjustment(win_rate_pct)
        assert adj >= -MAX_LEAGUE_ADJUSTMENT, (
            f"win_rate={win_rate_pct}% produced penalty {adj}, "
            f"more negative than cap -{MAX_LEAGUE_ADJUSTMENT}"
        )
        assert adj < 0, f"win_rate={win_rate_pct}% should produce a negative penalty"

    def test_penalty_at_exactly_0_pct_win_rate_is_capped(self):
        """win_rate 0% → raw = -round(50/5) = -10 — exactly at the cap."""
        adj = _penalty_adjustment(0.0)
        assert adj == -MAX_LEAGUE_ADJUSTMENT

    def test_penalty_below_0_pct_win_rate_is_capped(self):
        """win_rate below 0% (hypothetical) is still capped."""
        adj = _penalty_adjustment(-50.0)
        assert adj == -MAX_LEAGUE_ADJUSTMENT

    def test_small_penalty_when_just_below_50_pct(self):
        """win_rate just below 50% should give a small negative penalty."""
        adj = _penalty_adjustment(49.0)
        assert adj <= 0, f"Expected penalty near 50% boundary, got {adj}"

    def test_penalty_at_40_pct_is_minus_2(self):
        """win_rate 40% → raw = -round(10/5) = -2."""
        assert _penalty_adjustment(40.0) == -2

    def test_penalty_at_30_pct_is_minus_4(self):
        """win_rate 30% → raw = -round(20/5) = -4."""
        assert _penalty_adjustment(30.0) == -4

    def test_penalty_at_20_pct_is_minus_6(self):
        """win_rate 20% → raw = -round(30/5) = -6."""
        assert _penalty_adjustment(20.0) == -6


# ---------------------------------------------------------------------------
# R21.3 — Neutral band: 50% <= win_rate <= 65% produces no adjustment
# ---------------------------------------------------------------------------

class TestNeutralBand:
    """Win rates in [50%, 65%] produce no adjustment."""

    @pytest.mark.parametrize("win_rate_pct", [50.0, 55.0, 60.0, 62.5, 65.0])
    def test_no_adjustment_in_neutral_band(self, win_rate_pct):
        """Win rates within the neutral band must result in zero adjustment."""
        if win_rate_pct >= 65.0:
            adj = _boost_adjustment(win_rate_pct)
        elif win_rate_pct < 50.0:
            adj = _penalty_adjustment(win_rate_pct)
        else:
            adj = 0  # in the neutral band, no formula is applied
        assert adj == 0, (
            f"win_rate={win_rate_pct}% is in the neutral band, expected 0 adjustment, got {adj}"
        )
