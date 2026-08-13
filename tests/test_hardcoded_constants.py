"""
Task 1.3 — Hardcoded constants externalised to Settings.
Tests verify:
  1. Each new field exists on the Settings dataclass.
  2. With no env overrides, each field equals its original hardcoded default.
  3. Setting the env var and calling invalidate_settings_cache() picks up the new value.
"""
from __future__ import annotations

import os
import pytest


def _fresh_settings():
    from app.config.config import invalidate_settings_cache, get_settings
    invalidate_settings_cache()
    return get_settings()


# ── Field existence ───────────────────────────────────────────────────────────

NEW_FIELDS = [
    "ensemble_weight_dixon_coles",
    "ensemble_weight_elo",
    "ensemble_weight_poisson",
    "ensemble_weight_rules",
    "ensemble_weight_llm",
    "regime_tier1_min_confidence",
    "regime_tier2_min_confidence",
    "regime_tier3_min_confidence",
    "regime_tier4_min_confidence",
    "pick_generator_fallback_away_confidence",
    "pick_generator_fallback_home_confidence",
    "calibrator_moderate_threshold",
    "calibrator_severe_threshold",
    "poisson_league_avg_goals",
]


@pytest.mark.parametrize("field", NEW_FIELDS)
def test_settings_field_exists(field: str) -> None:
    settings = _fresh_settings()
    assert hasattr(settings, field), f"Settings is missing field '{field}'"


# ── Default values match original hardcoded literals ─────────────────────────

DEFAULTS = {
    "pick_generator_fallback_away_confidence": 0.54,
    "pick_generator_fallback_home_confidence": 0.50,
    "calibrator_moderate_threshold": 10.0,
    "calibrator_severe_threshold": 20.0,
    "poisson_league_avg_goals": 1.3,
    "ensemble_weight_dixon_coles": 0.30,
    "ensemble_weight_elo": 0.25,
    "ensemble_weight_poisson": 0.15,
    "ensemble_weight_rules": 0.20,
    "ensemble_weight_llm": 0.10,
    "regime_tier1_min_confidence": 78,
    "regime_tier2_min_confidence": 72,
    "regime_tier3_min_confidence": 68,
    "regime_tier4_min_confidence": 82,
}


@pytest.mark.parametrize("field,expected", DEFAULTS.items())
def test_settings_default_value(field: str, expected) -> None:
    # Clear any stale env vars that might interfere
    env_key = f"PREDICTX_{field.upper()}"
    os.environ.pop(env_key, None)
    settings = _fresh_settings()
    if not hasattr(settings, field):
        pytest.skip(f"Field '{field}' not yet added to Settings")
    actual = getattr(settings, field)
    assert actual == pytest.approx(expected), (
        f"Settings.{field} default is {actual!r}, expected {expected!r}"
    )


# ── Env-override round-trip ───────────────────────────────────────────────────

@pytest.mark.parametrize("field,env_key,override,expected_type", [
    ("poisson_league_avg_goals",              "PREDICTX_POISSON_LEAGUE_AVG_GOALS",              "1.5",  float),
    ("pick_generator_fallback_away_confidence","PREDICTX_PICK_GENERATOR_FALLBACK_AWAY_CONFIDENCE","0.60", float),
    ("calibrator_moderate_threshold",         "PREDICTX_CALIBRATOR_MODERATE_THRESHOLD",         "12.0", float),
    ("ensemble_weight_dixon_coles",           "PREDICTX_ENSEMBLE_WEIGHT_DIXON_COLES",           "0.40", float),
])
def test_settings_env_override(field: str, env_key: str, override: str, expected_type) -> None:
    if not hasattr(_fresh_settings(), field):
        pytest.skip(f"Field '{field}' not yet added to Settings")
    os.environ[env_key] = override
    try:
        settings = _fresh_settings()
        actual = getattr(settings, field)
        assert actual == pytest.approx(expected_type(override)), (
            f"Env override {env_key}={override} not reflected in Settings.{field}"
        )
    finally:
        os.environ.pop(env_key, None)
        _fresh_settings()  # restore defaults
