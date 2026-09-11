import app.main  # noqa: F401 -- establishes the application's import order

from app.enrichment.enriched_prediction import _aggregator_signals
from app.enrichment.signal_aggregator import calculate_win_probabilities
from app.models.ensemble import ensemble_prediction
from app.signal_combinations import build_signal_combination


def test_models_are_preserved_and_become_a_signal_prior():
    signals = _aggregator_signals([
        {
            "name": "goal_model_family",
            "value": {
                "poisson": {"home_win": 64, "draw": 22, "away_win": 14},
                "dixon_coles": {"home_win": 60, "draw": 25, "away_win": 15},
            },
            "impact": 0,
        },
        {"name": "elo_model", "value": {"home_win_probability": 58, "draw_probability": 22, "away_win_probability": 20}, "impact": 3},
        {"name": "ensemble_model", "value": {"probabilities": {"home_win": 61, "draw": 24, "away_win": 15}}, "impact": 5},
        {"name": "away_form", "impact": -4},
    ])

    result = calculate_win_probabilities(signals, "model-signal-test")

    assert set(result["model_sources"]) == {"goal_model_family", "elo", "ensemble"}
    assert result["model_prior"]["home"] > result["model_prior"]["away"]
    assert result["home_prob"] > result["away_prob"]
    assert result["signal_blend_weight"] > 0


def test_normalized_signal_names_survive_combination_learning_key():
    combo = build_signal_combination(
        signals=[{"signal_name": "poisson_model", "impact": 3}],
        pick_type="match_result",
        selection="Home Win",
    )
    assert combo["signal_names"] == ["poisson_model"]


def test_ensemble_exposes_one_goal_family_scoreline():
    poisson = {
        "probabilities": {"home_win": 55, "draw": 25, "away_win": 20},
        "top_scorelines": [{"score": "1-0", "probability": "18%"}],
    }
    dixon = {
        "probabilities": {"home_win": 54, "draw": 27, "away_win": 19},
        "top_scorelines": [{"score": "1-0", "probability": "20%"}],
    }
    result = ensemble_prediction(dixon, None, poisson, 50, "")
    assert result["most_likely_scoreline"]["score"] == "1-0"
    assert result["most_likely_scoreline"]["source"] == "goal_model_family"
