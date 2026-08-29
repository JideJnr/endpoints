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
    # odds_edge agrees (away), but neither recent_history_edge nor
    # common_opponent_edge corroborates (both favor home here, no h2h_edge
    # present) -- the hedge itself lacks support and gets capped.
    prediction = {
        "signals": [
            {"name": "odds_edge", "impact": -3},
            {"name": "recent_history_edge", "impact": 4},
            {"name": "common_opponent_edge", "impact": 3},
        ]
    }
    picks = [{"type": "double_chance", "selection": "Away or Draw", "confidence": 72, "reason": "edge"}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["confidence"] == 64
    assert "away_or_draw_requires_market_recent_common_support" in picks[0]["publication_filter"]["reasons"]


def test_away_or_draw_accepts_h2h_or_common_opponent_as_corroboration():
    # Same as above but common_opponent_edge now agrees with away -- one
    # corroborator alongside the core odds_edge signal is enough, so this
    # should NOT be capped (h2h_edge counts the same way if present instead).
    prediction = {
        "signals": [
            {"name": "odds_edge", "impact": -3},
            {"name": "recent_history_edge", "impact": 4},
            {"name": "common_opponent_edge", "impact": -3},
        ]
    }
    picks = [{"type": "double_chance", "selection": "Away or Draw", "confidence": 72, "reason": "edge"}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["confidence"] == 72
    assert picks[0].get("publication_filter") is None


def test_home_win_downgrades_to_home_or_draw_when_any_signal_dissents():
    # Every side-evidence signal gets a vote now: odds_edge is the only one
    # arguing away, but that's enough to downgrade the SELECTION itself
    # rather than just capping the confidence number on an unchanged bet.
    prediction = {
        "signals": [
            {"name": "odds_edge", "impact": -3},
            {"name": "recent_history_edge", "impact": 5},
            {"name": "common_opponent_edge", "impact": 4},
        ]
    }
    picks = [{"type": "match_result", "selection": "Home Win", "confidence": 73, "reason": "edge", "clear_winner": True}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["selection"] == "Home or Draw"
    assert picks[0]["type"] == "double_chance"
    assert picks[0]["clear_winner"] is False
    assert picks[0]["publication_filter"]["downgraded"] is True
    assert picks[0]["publication_filter"]["dissenting_signals"] == ["odds_edge"]


def test_away_win_downgrades_to_away_or_draw_when_any_signal_dissents():
    # Mirror of the home-side case above. Previously nothing gated an
    # outright Away Win pick at all -- this closes that asymmetry.
    prediction = {
        "signals": [
            {"name": "odds_edge", "impact": -4},
            {"name": "recent_history_edge", "impact": 5},
            {"name": "common_opponent_edge", "impact": -3},
        ]
    }
    picks = [{"type": "match_result", "selection": "Away Win", "confidence": 70, "reason": "edge", "clear_winner": True}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["selection"] == "Away or Draw"
    assert picks[0]["type"] == "double_chance"
    assert picks[0]["clear_winner"] is False
    assert picks[0]["publication_filter"]["downgraded"] is True
    assert picks[0]["publication_filter"]["dissenting_signals"] == ["recent_history_edge"]
    # the resulting hedge has its own support (odds_edge + common_opponent_edge
    # both agree away), so it isn't ALSO capped on top of the downgrade
    assert picks[0]["confidence"] == 70


def test_home_win_stays_outright_when_no_signal_dissents():
    # Unanimous agreement (or no side signals having an opinion at all)
    # should never trigger a downgrade -- only actual disagreement does.
    prediction = {
        "signals": [
            {"name": "odds_edge", "impact": 4},
            {"name": "recent_history_edge", "impact": 5},
            {"name": "common_opponent_edge", "impact": 3},
            {"name": "h2h_edge", "impact": 2},
        ]
    }
    picks = [{"type": "match_result", "selection": "Home Win", "confidence": 73, "reason": "edge", "clear_winner": True}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["selection"] == "Home Win"
    assert picks[0]["type"] == "match_result"
    assert picks[0]["confidence"] == 73
    assert picks[0]["clear_winner"] is True
    assert "publication_filter" not in picks[0]


def test_directional_conflict_caps_high_confidence_pick():
    # h2h_edge and common_opponent_edge both dissent from the away pick, so
    # this now downgrades to Away or Draw (not just a confidence cap); the
    # resulting hedge also fails its own support check (no away corroborator
    # left after the downgrade) and the legacy 3-way conflict flag still
    # fires too, so both mechanisms land on it at once.
    prediction = {
        "signals": [
            {"name": "h2h_edge", "impact": 3},
            {"name": "odds_edge", "impact": -3},
            {"name": "common_opponent_edge", "impact": 4},
        ]
    }
    picks = [{"type": "match_result", "selection": "Away Win", "confidence": 75, "reason": "edge"}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["selection"] == "Away or Draw"
    assert picks[0]["publication_filter"]["downgraded"] is True
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


def test_away_or_draw_upgrades_to_away_win_when_all_side_signals_agree():
    # Real shape seen in prod: 6 side-evidence signals unanimously favored
    # away, but the market_selector/ensemble never cleared its own
    # clear_winner gap, so only "Away or Draw" got generated as a candidate.
    # With every signal agreeing, this should upgrade to the outright pick
    # instead of leaving value on the table -- confidence comes from the
    # ensemble's own away_win probability (48%), not the hedge's inflated 59%.
    prediction = {
        "signals": [
            {"name": "common_opponent_edge", "impact": -4},
            {"name": "recent_history_edge", "impact": -3},
            {"name": "odds_edge", "impact": -5},
            {"name": "venue_form_edge", "impact": -3},
            {"name": "league_position_edge", "impact": -2},
            {"name": "h2h_edge", "impact": -2},
            {"name": "ensemble_model", "value": {"probabilities": {"home_win": 30, "draw": 22, "away_win": 48}}},
        ]
    }
    picks = [{"type": "double_chance", "selection": "Away or Draw", "confidence": 59, "reason": "edge"}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["selection"] == "Away Win"
    assert picks[0]["type"] == "match_result"
    assert picks[0]["confidence"] == 48
    assert picks[0]["publication_filter"]["upgraded"] is True
    assert picks[0]["publication_filter"]["upgraded_from"] == "Away or Draw"
    assert set(picks[0]["publication_filter"]["agreeing_signals"]) == {
        "common_opponent_edge", "h2h_edge", "league_position_edge",
        "odds_edge", "recent_history_edge", "venue_form_edge",
    }


def test_hedge_upgrade_requires_at_least_three_agreeing_signals():
    # Only 2 side signals have an opinion at all -- unanimous, but too thin
    # a basis to trust for taking on an outright bet's extra risk.
    prediction = {
        "signals": [
            {"name": "odds_edge", "impact": -5},
            {"name": "h2h_edge", "impact": -2},
        ]
    }
    picks = [{"type": "double_chance", "selection": "Away or Draw", "confidence": 59, "reason": "edge"}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["selection"] == "Away or Draw"
    assert (picks[0].get("publication_filter") or {}).get("upgraded") is not True


def test_hedge_stays_hedge_when_side_signals_are_not_unanimous():
    prediction = {
        "signals": [
            {"name": "odds_edge", "impact": -5},
            {"name": "h2h_edge", "impact": -2},
            {"name": "venue_form_edge", "impact": -3},
            {"name": "recent_history_edge", "impact": 2},
        ]
    }
    picks = [{"type": "double_chance", "selection": "Away or Draw", "confidence": 59, "reason": "edge"}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["selection"] == "Away or Draw"
    assert (picks[0].get("publication_filter") or {}).get("upgraded") is not True


def test_hedge_upgrade_falls_back_to_discounted_confidence_without_ensemble_model():
    # No ensemble_model signal present -- confidence should fall back to the
    # hedge's own confidence minus a flat discount, floored at 55.
    prediction = {
        "signals": [
            {"name": "odds_edge", "impact": 4},
            {"name": "h2h_edge", "impact": 3},
            {"name": "venue_form_edge", "impact": 2},
        ]
    }
    picks = [{"type": "double_chance", "selection": "Home or Draw", "confidence": 70, "reason": "edge"}]

    _apply_publication_filters(prediction, picks)

    assert picks[0]["selection"] == "Home Win"
    assert picks[0]["type"] == "match_result"
    assert picks[0]["confidence"] == 58
    assert picks[0]["publication_filter"]["upgraded"] is True
