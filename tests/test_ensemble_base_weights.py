"""Unit tests for _BASE_WEIGHTS and _get_weights() fallback in ensemble.py (R9).

Covers:
  - R9.1: _BASE_WEIGHTS has exactly the required keys, correct values, and sums to 1.00.
  - R9.2: _get_weights() returns _BASE_WEIGHTS when get_learned_weights() returns {}.
  - R9.3: ensemble_prediction() returns max(probs.values()) > 34.0 with non-trivial model
          output and no learned weights.
  - R9.4: total_weight == 0.0 path still returns 33/33/33 with limited_signal=True when no
          model produces valid output (even with non-empty _BASE_WEIGHTS).
  - R9.5: When learned weights are available, _get_weights() returns them in preference to
          _BASE_WEIGHTS.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Helper: force cache reset between tests so each test starts fresh
# ---------------------------------------------------------------------------

def _reset_weights_cache():
    """Clear ensemble module-level weight cache so _get_weights() re-evaluates."""
    import app.models.ensemble as ens
    ens._cached_weights = None
    ens._cache_hits = 0


# _get_weights() uses a local `from app.monitoring.self_learner import get_learned_weights`
# inside a try/except, so we patch the function on its home module.
_PATCH_TARGET = "app.monitoring.self_learner.get_learned_weights"


# ---------------------------------------------------------------------------
# R9.1 — _BASE_WEIGHTS shape, values, and sum
# ---------------------------------------------------------------------------

class TestBaseWeightsShape:
    """_BASE_WEIGHTS must be non-empty, contain exactly the required model keys,
    have the specified individual values, and sum to 1.00 (R9.1)."""

    def test_base_weights_is_non_empty(self):
        from app.models.ensemble import _BASE_WEIGHTS
        assert _BASE_WEIGHTS, "_BASE_WEIGHTS must not be empty"

    def test_base_weights_has_exactly_required_keys(self):
        from app.models.ensemble import _BASE_WEIGHTS
        required = {"dixon_coles", "elo", "poisson", "rules", "llm"}
        assert set(_BASE_WEIGHTS.keys()) == required, (
            f"_BASE_WEIGHTS keys mismatch: expected {required}, got {set(_BASE_WEIGHTS.keys())}"
        )

    def test_base_weights_individual_values(self):
        from app.models.ensemble import _BASE_WEIGHTS
        assert _BASE_WEIGHTS["dixon_coles"] == pytest.approx(0.30, abs=1e-9)
        assert _BASE_WEIGHTS["elo"]         == pytest.approx(0.25, abs=1e-9)
        assert _BASE_WEIGHTS["poisson"]     == pytest.approx(0.15, abs=1e-9)
        assert _BASE_WEIGHTS["rules"]       == pytest.approx(0.20, abs=1e-9)
        assert _BASE_WEIGHTS["llm"]         == pytest.approx(0.10, abs=1e-9)

    def test_base_weights_sum_to_one(self):
        from app.models.ensemble import _BASE_WEIGHTS
        total = sum(_BASE_WEIGHTS.values())
        assert total == pytest.approx(1.00, abs=1e-9), (
            f"_BASE_WEIGHTS values must sum to 1.00, got {total}"
        )


# ---------------------------------------------------------------------------
# R9.2 — _get_weights() returns _BASE_WEIGHTS when learned weights are empty
# ---------------------------------------------------------------------------

class TestGetWeightsFallback:
    """_get_weights() must fall back to _BASE_WEIGHTS when learned weights are unavailable."""

    def test_returns_base_weights_when_learned_is_empty_dict(self):
        """get_learned_weights() → {} means _BASE_WEIGHTS should be returned."""
        import app.models.ensemble as ens
        from app.models.ensemble import _BASE_WEIGHTS
        _reset_weights_cache()

        with patch(_PATCH_TARGET, return_value={}):
            weights = ens._get_weights()

        assert weights == _BASE_WEIGHTS, (
            f"Expected _BASE_WEIGHTS as fallback when learned is empty, got {weights}"
        )

    def test_returns_base_weights_when_self_learner_raises(self):
        """When the self_learner import or call raises, _BASE_WEIGHTS is still returned."""
        import app.models.ensemble as ens
        from app.models.ensemble import _BASE_WEIGHTS
        _reset_weights_cache()

        with patch(_PATCH_TARGET, side_effect=RuntimeError("db unavailable")):
            weights = ens._get_weights()

        assert weights == _BASE_WEIGHTS

    def test_weights_are_learned_false_when_using_base(self):
        """_weights_are_learned must be False when _BASE_WEIGHTS fallback is used."""
        import app.models.ensemble as ens
        _reset_weights_cache()

        with patch(_PATCH_TARGET, return_value={}):
            ens._get_weights()

        assert ens._weights_are_learned is False

    def test_returned_dict_is_copy_not_same_object(self):
        """Modifying the returned dict must not corrupt _BASE_WEIGHTS."""
        import app.models.ensemble as ens
        from app.models.ensemble import _BASE_WEIGHTS
        _reset_weights_cache()

        with patch(_PATCH_TARGET, return_value={}):
            weights = ens._get_weights()

        weights["dixon_coles"] = 999.0  # mutate the returned dict
        assert _BASE_WEIGHTS["dixon_coles"] == pytest.approx(0.30, abs=1e-9), (
            "_BASE_WEIGHTS must not be mutated by callers of _get_weights()"
        )


# ---------------------------------------------------------------------------
# R9.3 — ensemble_prediction() produces max prob > 34.0 with non-trivial input
#         and no learned weights
# ---------------------------------------------------------------------------

class TestEnsemblePredictionWithBaseWeights:
    """With _BASE_WEIGHTS active and real model output, the top probability must exceed 34.0."""

    def _predict(self, dixon=None, elo=None, poisson=None,
                 rules_confidence=70, rules_pick="home win", llm=None):
        """Call ensemble_prediction with learned weights stubbed to return {}."""
        import app.models.ensemble as ens
        _reset_weights_cache()

        with patch(_PATCH_TARGET, return_value={}):
            result = ens.ensemble_prediction(
                dixon=dixon,
                elo=elo,
                poisson=poisson,
                rules_confidence=rules_confidence,
                rules_pick=rules_pick,
                llm=llm,
            )
        return result

    def test_rules_only_exceeds_34_percent(self):
        """Rules-only at 70% confidence → top probability > 34.0."""
        result = self._predict(rules_confidence=70, rules_pick="home win")
        best = max(result["probabilities"].values())
        assert best > 34.0, f"Expected best probability > 34.0, got {best}"

    def test_dixon_only_exceeds_34_percent(self):
        """Dixon-Coles-only strong signal → top probability > 34.0."""
        dixon = {"probabilities": {"home_win": 55.0, "draw": 25.0, "away_win": 20.0}}
        result = self._predict(dixon=dixon, rules_confidence=0, rules_pick="")
        best = max(result["probabilities"].values())
        assert best > 34.0, f"Expected best probability > 34.0, got {best}"

    def test_multi_model_exceeds_34_percent(self):
        """Full multi-model run → top probability comfortably above 34.0."""
        dixon   = {"probabilities": {"home_win": 55.0, "draw": 25.0, "away_win": 20.0}}
        elo     = {"home_win_probability": 60.0, "away_win_probability": 25.0}
        poisson = {"probabilities": {"home_win": 52.0, "draw": 28.0, "away_win": 20.0}}

        result = self._predict(
            dixon=dixon, elo=elo, poisson=poisson,
            rules_confidence=68, rules_pick="home win",
        )
        best = max(result["probabilities"].values())
        assert best > 34.0, f"Expected best probability > 34.0, got {best}"

    def test_limited_signal_not_set_on_meaningful_output(self):
        """A meaningful prediction must not carry limited_signal=True."""
        dixon = {"probabilities": {"home_win": 55.0, "draw": 25.0, "away_win": 20.0}}
        result = self._predict(dixon=dixon, rules_confidence=65, rules_pick="home win")
        assert not result.get("limited_signal", False), (
            "limited_signal should not be True when models produce valid output"
        )

    def test_weights_source_is_set(self):
        """weights_source must be a non-empty string when using _BASE_WEIGHTS."""
        result = self._predict(rules_confidence=70, rules_pick="home win")
        assert result.get("weights_source"), "weights_source must be present and non-empty"


# ---------------------------------------------------------------------------
# R9.4 — total_weight == 0.0 still triggers 33/33/33 with limited_signal=True
# ---------------------------------------------------------------------------

class TestNeutralFallbackPreserved:
    """Even with non-empty _BASE_WEIGHTS, if no model produces valid output the
    neutral 33/33/33 fallback with limited_signal=True is preserved (R9.4)."""

    def _predict_no_models(self):
        import app.models.ensemble as ens
        _reset_weights_cache()

        with patch(_PATCH_TARGET, return_value={}):
            # Pass nothing useful — rules_pick='' means neither home/away/draw
            # branch is taken, so rules weight won't be applied.
            result = ens.ensemble_prediction(
                dixon=None, elo=None, poisson=None,
                rules_confidence=0, rules_pick="", llm=None,
            )
        return result

    def test_limited_signal_true_with_no_model_output(self):
        result = self._predict_no_models()
        assert result.get("limited_signal") is True, (
            "limited_signal must be True when no model produces valid output"
        )

    def test_neutral_probabilities_approx_33(self):
        result = self._predict_no_models()
        probs = result["probabilities"]
        assert abs(probs["home_win"] - 33.3) < 1.0
        assert abs(probs["away_win"] - 33.3) < 1.0

    def test_prediction_is_draw_in_neutral_case(self):
        result = self._predict_no_models()
        assert result["prediction"] == "Draw"


# ---------------------------------------------------------------------------
# R9.5 — Learned weights take precedence over _BASE_WEIGHTS
# ---------------------------------------------------------------------------

class TestLearnedWeightsPrecedence:
    """When learned weights are available, _get_weights() returns them, not _BASE_WEIGHTS."""

    def test_learned_weights_override_base(self):
        import app.models.ensemble as ens
        from app.models.ensemble import _BASE_WEIGHTS
        _reset_weights_cache()

        learned = {
            "dixon_coles": 0.40, "elo": 0.30, "poisson": 0.10,
            "rules": 0.15, "llm": 0.05,
        }

        with patch(_PATCH_TARGET, return_value=learned):
            weights = ens._get_weights()

        assert weights == learned, "Learned weights must override _BASE_WEIGHTS"
        assert weights != _BASE_WEIGHTS

    def test_weights_are_learned_true_when_using_learned(self):
        import app.models.ensemble as ens
        _reset_weights_cache()

        learned = {
            "dixon_coles": 0.40, "elo": 0.30, "poisson": 0.10,
            "rules": 0.15, "llm": 0.05,
        }

        with patch(_PATCH_TARGET, return_value=learned):
            ens._get_weights()

        assert ens._weights_are_learned is True
