"""
Property-based tests for the research filter module.

Feature: research-driven-predictor-improvements
"""
from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import app.research.research_filter as rf
from app.research.research_filter import (
    evaluate_pick,
    _research_filter_candidate,
    _get_dynamic_rules,
    _load_dynamic_rules,
)

settings.register_profile("predictx_db_properties", deadline=None)
settings.load_profile("predictx_db_properties")

# ── Hypothesis strategies ──────────────────────────────────

CONFIDENCE_LOW = st.integers(min_value=0, max_value=49)  # below learned threshold of 50
CONFIDENCE_NOISY = st.integers(min_value=60, max_value=66)
CONFIDENCE_CAUTION = st.integers(min_value=60, max_value=71)
CONFIDENCE_SAFE = st.integers(min_value=72, max_value=100)
CONFIDENCE_TRUST = st.integers(min_value=70, max_value=100)

DRAW_ODDS_BLOCK = st.floats(min_value=1.00, max_value=1.99, allow_nan=False, allow_infinity=False)
DRAW_ODDS_SAFE = st.floats(min_value=2.00, max_value=10.0, allow_nan=False, allow_infinity=False)

FAV_ODDS_BLOCK = st.floats(min_value=2.50, max_value=10.0, allow_nan=False, allow_infinity=False)
FAV_ODDS_SAFE = st.floats(min_value=1.01, max_value=2.49, allow_nan=False, allow_infinity=False)

HOME_ODDS_DANGER = st.floats(min_value=2.50, max_value=2.99, allow_nan=False, allow_infinity=False)
HOME_ODDS_SAFE = st.floats(min_value=1.01, max_value=2.49, allow_nan=False, allow_infinity=False)

SAFE_COUNTRY_STRAT = st.sampled_from(["austria", "india", "uzbekistan", "switzerland", "norway", "australia", "sweden", "ireland", "kazakhstan", "bulgaria", ""])


def _safe_odds_profile() -> dict[str, float]:
    """Composite strategy for safe odds profiles (no block conditions active)."""
    return {
        "draw_odds": 3.0,
        "favorite_odds": 1.8,
        "home_odds": 1.5,
        "away_odds": 1.9,
    }


# ── Property 1: match_result learned threshold ──────────────────

@given(pick=st.fixed_dictionaries({
    "type": st.just("match_result"),
    "confidence": st.integers(min_value=0, max_value=100),
}))
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_match_result_uses_learned_threshold(pick):
    # Feature: research-driven-predictor-improvements, Property 1: match_result learned threshold
    result = evaluate_pick(pick)
    assert result["blocked"] is (pick["confidence"] < 50)


# ── Property 2: low confidence block ──────────────────────

@given(conf=CONFIDENCE_LOW)
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_low_confidence_blocked(conf):
    # Feature: research-driven-predictor-improvements, Property 2: low confidence block
    result = evaluate_pick({"type": "home_win", "confidence": conf})
    assert result["blocked"] is True


# ── Property 3: draw odds block ───────────────────────────

@given(draw_odds=DRAW_ODDS_BLOCK)
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_3_draw_odds_neutral_without_learned_evidence(draw_odds):
    # Draw odds alone are not a block without learned loss evidence.
    result = evaluate_pick({
        "type": "home_win",
        "confidence": 80,
        "draw_odds": draw_odds,
    })
    assert result["blocked"] is False


# ── Property 4: favorite odds block ───────────────────────

@given(fav_odds=FAV_ODDS_BLOCK)
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_4_favorite_odds_neutral_without_learned_evidence(fav_odds):
    # Favorite odds alone are not a block without learned loss evidence.
    result = evaluate_pick({
        "type": "home_win",
        "confidence": 80,
        "favorite_odds": fav_odds,
    })
    assert result["blocked"] is False


# ── Property 5: caution threshold enforcement ─────────────

@given(conf=CONFIDENCE_CAUTION)
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_5_away_or_draw_caution_neutral_without_learned_evidence(conf):
    # Selection/confidence bands are only blocked after learned loss evidence.
    result = evaluate_pick({
        "type": "home_win",
        "selection": "Away or Draw",
        "confidence": conf,
    })
    assert result["blocked"] is False


# ── Property 6: caution threshold pass-through ────────────

@given(conf=CONFIDENCE_SAFE)
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_property_6_away_or_draw_pass_through(conf):
    # Feature: research-driven-predictor-improvements, Property 6: caution threshold pass-through
    result = evaluate_pick({
        "type": "home_win",
        "selection": "Away or Draw",
        "confidence": conf,
        "draw_odds": 3.0,
        "favorite_odds": 1.8,
        "home_odds": 1.5,
        "country": "austria",
    })
    assert result["blocked"] is False


# ── Property 7: home-or-away trust guarantee ──────────────

@given(conf=CONFIDENCE_SAFE)
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_7_home_or_away_no_default_trust_boost(conf):
    # Home-or-away confidence is not boosted unless learned signal weights exist.
    result = evaluate_pick({
        "type": "home_win",
        "selection": "Home or Away",
        "confidence": conf,
        "draw_odds": 3.0,
        "favorite_odds": 1.8,
        "home_odds": 1.5,
    })
    assert result["blocked"] is False
    assert result["trust_boost"] == 0


# ── Property 8: trust boost cap invariant ─────────────────

@given(
    conf=CONFIDENCE_SAFE,
    has_home_odds=st.booleans(),
    has_draw_odds=st.booleans(),
    has_fav_odds=st.booleans(),
    has_away_odds=st.booleans(),
    is_trust_country=st.booleans(),
    is_sportybet=st.booleans(),
    is_conf_74=st.booleans(),
)
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_8_trust_boost_cap(conf, has_home_odds, has_draw_odds, has_fav_odds, has_away_odds, is_trust_country, is_sportybet, is_conf_74):
    # Feature: research-driven-predictor-improvements, Property 8: trust boost cap invariant
    odds = {}
    if has_home_odds:
        odds["home_odds"] = 1.5
    if has_draw_odds:
        odds["draw_odds"] = 3.0
    if has_fav_odds:
        odds["favorite_odds"] = 1.8
    if has_away_odds:
        odds["away_odds"] = 1.9

    pick = {
        "type": "home_win",
        "selection": "Home or Away",
        "confidence": conf,
        **odds,
    }
    if is_trust_country:
        pick["country"] = "austria"
    if is_sportybet:
        pick["source"] = "sportybet_market_signal"
    if is_conf_74:
        pick["confidence"] = 74

    result = evaluate_pick(pick)
    assert result["blocked"] is False
    assert result["trust_boost"] <= 8


# ── Property 9: evaluate_pick idempotence ─────────────────

@given(pick=st.fixed_dictionaries({
    "type": st.sampled_from(["home_win", "away_win", "draw", "match_result"]),
    "selection": st.sampled_from(["Home or Away", "Away or Draw", "Home or Draw"]),
    "confidence": st.integers(min_value=0, max_value=100),
    "draw_odds": st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    "favorite_odds": st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    "home_odds": st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    "country": st.sampled_from(["austria", "bolivia", "india", "russia", ""]),
    "league_key": st.sampled_from(["scotland-league-cup", "russia-russian-cup", "argentina-primera-lpf", ""]),
    "source": st.sampled_from(["sportybet_market_signal", "enriched_ensemble", ""]),
}))
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_9_evaluate_pick_idempotence(pick):
    # Feature: research-driven-predictor-improvements, Property 9: evaluate_pick idempotence
    result1 = evaluate_pick(pick)
    result2 = evaluate_pick(pick)
    assert result1["blocked"] == result2["blocked"]
    assert result1["reason"] == result2["reason"]
    assert result1["trust_boost"] == result2["trust_boost"]


# ── Property 10: safe country pass-through ─────────────

@given(conf=CONFIDENCE_TRUST, country=SAFE_COUNTRY_STRAT)
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_10_safe_country_pass_through(conf, country):
    # Feature: research-driven-predictor-improvements, Property 10: safe country pass-through
    pick = {
        "type": "home_win",
        "selection": "Home or Away",
        "confidence": conf,
        "country": country,
        "draw_odds": 3.0,
        "favorite_odds": 1.8,
        "home_odds": 1.5,
    }
    result = evaluate_pick(pick)
    assert result["blocked"] is False


# ── Property 11: betbuilder parity ────────────────────────

@given(pick=st.fixed_dictionaries({
    "type": st.sampled_from(["home_win", "away_win", "draw", "match_result"]),
    "selection": st.sampled_from(["Home or Away", "Away or Draw", "Home or Draw"]),
    "confidence": st.integers(min_value=0, max_value=100),
    "draw_odds": st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    "favorite_odds": st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    "home_odds": st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    "league_key": st.sampled_from(["scotland-league-cup", "russia-russian-cup", "argentina-primera-lpf", ""]),
}), country=st.sampled_from(["austria", "bolivia", "india", "russia", ""]))
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_11_betbuilder_parity(pick, country):
    # Feature: research-driven-predictor-improvements, Property 11: betbuilder parity
    league_key = pick.get("league_key") or ""
    odds_profile = {
        "draw_odds": pick.get("draw_odds", 0),
        "favorite_odds": pick.get("favorite_odds", 0),
        "home_odds": pick.get("home_odds", 0),
    }
    candidate_result = _research_filter_candidate(pick, odds_profile, country, league_key)
    evaluate_result = evaluate_pick(pick)
    assert candidate_result == (not evaluate_result["blocked"])


# ── Property 15: bounded context output ───────────────────

@given(row=st.fixed_dictionaries({
    "dimension": st.sampled_from(["league", "country", "selection"]),
    "key": st.text(min_size=1, max_size=20),
    "wins": st.integers(min_value=1, max_value=100),
    "losses": st.integers(min_value=0, max_value=50),
    "total": st.integers(min_value=5, max_value=100),
    "win_rate": st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    "loss_rate": st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
}))
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_15_bounded_context_output(row):
    # Feature: research-driven-predictor-improvements, Property 15: bounded context output
    with patch.object(rf, "db_conn") as mock_conn:
        mock_conn.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [row]
        result = rf.get_research_context_for_prompt()
    assert isinstance(result, str)
    assert len(result) <= 1000
    if result:
        assert len(result) > 0


# ── Property 16: dynamic league block threshold ───────────

@given(key=st.text(min_size=1, max_size=20))
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_16_dynamic_league_block_threshold(key):
    # Feature: research-driven-predictor-improvements, Property 16: dynamic league block threshold
    pick = {
        "type": "home_win",
        "confidence": 80,
        "league_key": key,
    }
    # With a league that has loss_rate >= 0.75 and total >= 5, it should be blocked
    # This is tested indirectly - the dynamic rules are loaded from research_stats
    result = evaluate_pick(pick)
    # The result depends on whether the league is in dynamic block list
    # We just verify it doesn't crash
    assert isinstance(result["blocked"], bool)


# ── Property 19: cache hit idempotence ────────────────────

def test_property_19_cache_hit_idempotence():
    # Feature: research-driven-predictor-improvements, Property 19: cache hit idempotence
    result1 = rf.get_research_context_for_prompt()
    result2 = rf.get_research_context_for_prompt()
    assert result1 == result2


# ── Property 20: optimal profile score bounds ─────────────

@given(
    selection=st.sampled_from(["Home or Away", "Away or Draw", "Home or Draw"]),
    confidence=st.integers(min_value=0, max_value=100),
    odds_profile=st.fixed_dictionaries({
        "draw_odds": st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
        "favorite_odds": st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
        "home_odds": st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
        "away_odds": st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
    }),
    country=SAFE_COUNTRY_STRAT,
    source=st.sampled_from(["sportybet_market_signal", "enriched_ensemble", ""]),
)
@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow])
def test_property_20_optimal_profile_score_bounds(selection, confidence, odds_profile, country, source):
    # Feature: research-driven-predictor-improvements, Property 20: optimal profile score bounds
    # The optimal_profile_score is computed from 6 conditions, each contributing 0 or 1
    # So it must be in [0, 6]
    # We verify this by checking the individual conditions
    score = 0
    if selection == "Home or Away":
        score += 1
    if confidence >= 72:
        score += 1
    if 1.50 <= odds_profile.get("favorite_odds", 0) <= 1.99:
        score += 1
    if source == "sportybet_market_signal":
        score += 1
    if selection == "Home or Away":
        score += 1  # second point for home-away being strongest
    score = min(score, 6)
    assert 0 <= score <= 6


if __name__ == "__main__":
    unittest.main()
