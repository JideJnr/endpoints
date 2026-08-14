"""
Tests for _dynamic_bias_multiplier_floor in app/monitoring/self_learner.py

Requirements: R22.2, R22.3, R22.4

The dynamic bias correction floor formula is:
    max(0.72, 1.0 - (loss_rate - 0.50) * 1.4)

Key behaviour:
  - loss_rate = 0.58 → floor ≈ 0.888  (1.0 - 0.08 * 1.4 = 0.888)
  - loss_rate = 0.65 → floor ≈ 0.790  (1.0 - 0.15 * 1.4 = 0.790)
  - loss_rate = 0.71 → 0.72 clamped   (1.0 - 0.21 * 1.4 = 0.706 → clamped to 0.72)
  - loss_rate = 0.80 → 0.72 clamped   (1.0 - 0.30 * 1.4 = 0.580 → clamped to 0.72)
  - Monotonically non-increasing as loss_rate increases.
"""

import pytest
from app.monitoring.self_learner import _dynamic_bias_multiplier_floor


class TestDynamicBiasMultiplierFloor:
    """Unit tests covering R22.2, R22.3, R22.4."""

    def test_loss_rate_0_58_gives_approx_0_888(self):
        """R22.2 — loss_rate = 0.58 → floor ≈ 0.888 (±0.001)."""
        result = _dynamic_bias_multiplier_floor(0.58)
        assert abs(result - 0.888) <= 0.001, f"Expected ≈ 0.888, got {result}"

    def test_loss_rate_0_65_gives_approx_0_790(self):
        """R22.2 — loss_rate = 0.65 → floor ≈ 0.790 (±0.001)."""
        result = _dynamic_bias_multiplier_floor(0.65)
        assert abs(result - 0.790) <= 0.001, f"Expected ≈ 0.790, got {result}"

    def test_loss_rate_0_71_clamped_to_0_72(self):
        """R22.3 — loss_rate = 0.71 → formula gives 0.706, clamped to absolute floor 0.72."""
        result = _dynamic_bias_multiplier_floor(0.71)
        assert result == pytest.approx(0.72, abs=0.001), f"Expected 0.72 (clamped), got {result}"

    def test_loss_rate_0_80_clamped_to_absolute_floor(self):
        """R22.4 — loss_rate = 0.80 → formula gives 0.580, clamped to 0.72 (absolute floor)."""
        result = _dynamic_bias_multiplier_floor(0.80)
        assert result == pytest.approx(0.72, abs=0.001), f"Expected 0.72 (absolute floor), got {result}"

    def test_floor_never_goes_below_0_72(self):
        """R22.4 — absolute floor is always 0.72, regardless of how high loss_rate is."""
        for loss_rate in (0.72, 0.80, 0.90, 0.99, 1.0):
            result = _dynamic_bias_multiplier_floor(loss_rate)
            assert result >= 0.72 - 1e-9, f"Floor dropped below 0.72 at loss_rate={loss_rate}: got {result}"

    def test_monotonicity_increasing_loss_rate_decreases_floor(self):
        """R22.2 — higher loss_rate produces a lower or equal floor (monotonically non-increasing)."""
        loss_rates = [0.58, 0.60, 0.65, 0.70, 0.71, 0.80, 0.90]
        floors = [_dynamic_bias_multiplier_floor(r) for r in loss_rates]
        for i in range(len(floors) - 1):
            assert floors[i] >= floors[i + 1] - 1e-9, (
                f"Monotonicity violated: floor({loss_rates[i]})={floors[i]} < "
                f"floor({loss_rates[i+1]})={floors[i+1]}"
            )

    def test_loss_rate_exactly_0_50_gives_1_0(self):
        """Boundary: at the trigger threshold, formula gives 1.0 - 0 * 1.4 = 1.0."""
        result = _dynamic_bias_multiplier_floor(0.50)
        assert result == pytest.approx(1.0, abs=0.001), f"Expected 1.0 at loss_rate=0.50, got {result}"

    def test_result_is_float(self):
        """Sanity: function always returns a float."""
        result = _dynamic_bias_multiplier_floor(0.65)
        assert isinstance(result, float)
