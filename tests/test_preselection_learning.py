# Match the application's import order; importing the enrichment package first
# exposes an existing market/league-memory circular-import path.
import app.main  # noqa: F401
from app.enrichment import enriched_prediction as prediction


def test_preselection_learning_preserves_a_clear_model_favourite():
    ensemble = {
        "probabilities": {"home_win": 64.0, "draw": 21.0, "away_win": 15.0},
        "prediction": "Home Win",
        "confidence": 64.0,
    }

    result = prediction._apply_preselection_learning(
        ensemble, {"tournament": "Test League"}, [], is_live=False, minute=0,
    )

    assert result["applied"] is False
    assert result["reason"] == "clear_model_favourite"
    assert ensemble["probabilities"]["home_win"] == 64.0


def test_preselection_learning_can_use_a_learned_league_distribution(monkeypatch):
    ensemble = {
        "probabilities": {"home_win": 38.0, "draw": 32.0, "away_win": 30.0},
        "prediction": "Home Win",
        "confidence": 38.0,
    }
    monkeypatch.setattr(
        prediction,
        "_calculate_signal_probabilities",
        lambda *args, **kwargs: {
            "home_prob": 0.25,
            "draw_prob": 0.25,
            "away_prob": 0.50,
            "base_probs_source": "learned",
        },
    )
    monkeypatch.setattr(
        prediction,
        "_normalize_aggregator_signal",
        lambda *args, **kwargs: {"direction": 1},
    )
    monkeypatch.setattr(prediction, "live_context_from_doc", lambda *args: {})

    result = prediction._apply_preselection_learning(
        ensemble,
        {"tournament": "Test League"},
        [{"name": "home_form", "impact": 3}] * 3,
        is_live=False,
        minute=0,
    )

    assert result["applied"] is True
    assert result["signal_aggregator_used"] is True
    assert ensemble["probabilities"]["away_win"] > 30.0
    assert round(sum(ensemble["probabilities"].values()), 1) == 100.0
