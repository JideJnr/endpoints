from app.enrichment.enriched_prediction import (
    _apply_publication_filters,
    _blended_model_probabilities,
    _draw_exclusion_fallback,
    _market_selector_picks,
    _model_signals,
    _source_quality_signals,
)


def test_source_quality_signals_are_readiness_not_strength():
    signals = _source_quality_signals({
        "data_sources": {
            "sportybet": {"available": True, "markets": True, "market_count": 20},
            "sofascore": {"detail": True, "history": True, "statistics": True},
        },
    })

    assert signals
    assert all(signal["impact"] == 0 for signal in signals)
    assert all(signal.get("role") == "readiness" for signal in signals)


def test_poisson_and_dixon_count_as_one_correlated_model_family():
    poisson = {"probabilities": {"home_win": 54, "draw": 24, "away_win": 22}}
    dixon = {"probabilities": {"home_win": 56, "draw": 23, "away_win": 21}}

    signals = _model_signals(poisson, dixon, None, {}, {})

    names = [signal["name"] for signal in signals]
    assert "goal_model_family" in names
    assert "poisson_model" not in names
    assert "dixon_coles_model" not in names
    family = next(signal for signal in signals if signal["name"] == "goal_model_family")
    assert family["impact"] == 0
    assert family["role"] == "goal_market_evidence"
    assert family["value"]["applies_to"] == ["goals", "btts"]


def test_goal_models_do_not_push_side_probabilities():
    ensemble = {"probabilities": {"home_win": 40, "draw": 25, "away_win": 35, "over_2_5": 50, "btts": 50}}
    poisson = {"probabilities": {"home_win": 80, "draw": 10, "away_win": 10, "over_2_5": 70, "btts": 66}}
    dixon = {"probabilities": {"home_win": 78, "draw": 12, "away_win": 10, "over_2_5": 72, "btts": 68}}

    blended = _blended_model_probabilities(ensemble, poisson, dixon)

    assert blended["home_win"] == 40
    assert blended["draw"] == 25
    assert blended["away_win"] == 35
    assert blended["over_2_5"] > 70
    assert blended["btts"] > 66


def test_low_draw_probability_prefers_home_or_away_protection_not_static_side():
    picks = _market_selector_picks(
        {},
        {"probabilities": {"home_win": 42, "draw": 16, "away_win": 42, "over_2_5": 50, "btts": 50}},
        {"probabilities": {"home_win": 75, "draw": 10, "away_win": 15, "over_2_5": 50, "btts": 50}},
        {"probabilities": {"home_win": 75, "draw": 10, "away_win": 15, "over_2_5": 50, "btts": 50}},
        {"blended": {}, "scopes": []},
        {"signals": []},
    )

    assert picks[0]["selection"] == "Home or Away"
    assert picks[0]["draw_exclusion"] is True
    assert all(pick["selection"] != "Home Win" for pick in picks)


def test_empty_pick_fallback_uses_home_or_away_when_only_draw_is_weak():
    pick = _draw_exclusion_fallback(
        {"probabilities": {"home_win": 39, "draw": 22, "away_win": 39}}
    )

    assert pick is not None
    assert pick["selection"] == "Home or Away"
    assert pick["source"] == "draw_exclusion_fallback"


def test_away_or_draw_high_confidence_requires_market_recent_common_support():
    prediction = {
        "signals": [
            {"name": "odds_edge", "impact": -3},
            {"name": "recent_history_edge", "impact": 4},
            {"name": "common_opponent_edge", "impact": -3},
        ]
    }
    picks = [{"type": "double_chance", "selection": "Away or Draw", "confidence": 72, "reason": "edge"}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["confidence"] == 64
    assert "away_or_draw_requires_market_recent_common_support" in picks[0]["publication_filter"]["reasons"]


def test_home_win_high_confidence_requires_market_and_recent_or_common_support():
    prediction = {
        "signals": [
            {"name": "odds_edge", "impact": -3},
            {"name": "recent_history_edge", "impact": 5},
            {"name": "common_opponent_edge", "impact": 4},
        ]
    }
    picks = [{"type": "match_result", "selection": "Home Win", "confidence": 73, "reason": "edge"}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["confidence"] == 64
    assert "home_win_requires_market_plus_recent_or_common_support" in picks[0]["publication_filter"]["reasons"]


def test_directional_conflict_caps_high_confidence_pick():
    prediction = {
        "signals": [
            {"name": "h2h_edge", "impact": 3},
            {"name": "odds_edge", "impact": -3},
            {"name": "common_opponent_edge", "impact": 4},
        ]
    }
    picks = [{"type": "match_result", "selection": "Away Win", "confidence": 75, "reason": "edge"}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["confidence"] == 64
    assert any(signal["name"] == "directional_signal_conflict" for signal in prediction["signals"])


def test_double_chance_is_blocked_when_it_excludes_clear_stronger_side():
    prediction = {
        "signals": [
            # Failure shape seen in Porto B/Farense and Lommel/Charleroi:
            # home venue/form supports Home-or-Draw, but stronger side signals
            # point to the away team being the side we are excluding.
            {"name": "venue_form_edge", "impact": 12},
            {"name": "recent_history_edge", "impact": 9},
            {"name": "avg_rating_edge", "impact": -16},
            {"name": "odds_edge", "impact": -5},
            {"name": "league_position_edge", "impact": -4},
        ]
    }
    picks = [{"type": "double_chance", "selection": "Home or Draw", "confidence": 61, "reason": "home avoids defeat"}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["type"] == "no_bet"
    assert picks[0]["selection"] == "No Bet"
    suppressed = picks[0]["suppressed_picks"][0]
    assert suppressed["publication_filter"]["blocked"] is True
    assert "double_chance_excludes_clear_stronger_side" in suppressed["publication_filter"]["reasons"]
