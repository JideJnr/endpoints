from __future__ import annotations

import math
from typing import Any


# Hardcoded fallback weights — overridden by learned weights when enough data exists
#
# "goal_model_family" combines Poisson and Dixon-Coles into one learnable
# slot (0.30 + 0.15 from the old separate weights, so the total influence is
# unchanged from before this fix). They're correlated goal-scoring models
# built the same way, so they're intentionally never weighted as two
# independent "votes" -- see the matching "goal_model_family" signal built in
# enriched_prediction.py::_model_signals, which merges them for the exact
# same reason. Previously the weight-LEARNING code (self_learner.py,
# learned_parameters.py) still looked for separate "dixon_coles"/"poisson"
# keys that the signal layer almost never emits (confirmed: 0 of 376 graded
# predictions ever had either name on its own), so neither model could ever
# earn a learned weight -- they silently contributed 0 to every blended
# prediction, regardless of how much graded data existed. That's why the
# ensemble's draw% was so often identical across unrelated matches: with the
# real goal models zeroed out, the blend was driven almost entirely by elo
# and rules, whose own draw estimates are both the same generic
# distance-from-50 formula, not a real per-match calculation.
_BASE_WEIGHTS: dict[str, float] = {
    "goal_model_family": 0.45,
    "elo": 0.25,
    "rules": 0.20,
    "llm": 0.10,
}

# Module-level cache so we don't hit SQLite on every prediction
_cached_weights: dict[str, float] | None = None
_weights_are_learned: bool = False
_cache_hits = 0
_CACHE_REFRESH_EVERY = 50  # refresh learned weights every N predictions


def _get_weights() -> dict[str, float]:
    """Return learned weights from graded history."""
    global _cached_weights, _weights_are_learned, _cache_hits
    _cache_hits += 1
    if _cached_weights is None or _cache_hits % _CACHE_REFRESH_EVERY == 0:
        try:
            from app.monitoring.self_learner import get_learned_weights
            learned = get_learned_weights()
            if learned:
                _cached_weights = learned
                _weights_are_learned = True
                return _cached_weights
        except Exception:
            pass
        _cached_weights = dict(_BASE_WEIGHTS)
        _weights_are_learned = False
    return _cached_weights


# Keep WEIGHTS accessible for backward compatibility
WEIGHTS = _BASE_WEIGHTS


def compute_ensemble_diversity(
    dixon: dict[str, Any] | None,
    elo: dict[str, Any] | None,
    poisson: dict[str, Any] | None,
    rules_confidence: int,
    rules_pick: str,
    llm: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute diversity metrics across ensemble models.
    
    Returns a dict with:
    - diversity_score: 0-100 (higher = more disagreement)
    - supporting_models: list of models that agree with the majority outcome
    - opposing_models: list of models that disagree
    - outcome_std: standard deviation of probabilities for the predicted outcome
    """
    model_probs: dict[str, dict[str, float]] = {}
    
    if dixon and dixon.get("probabilities"):
        model_probs["dixon_coles"] = {
            "home_win": float(dixon["probabilities"].get("home_win") or 0),
            "draw": float(dixon["probabilities"].get("draw") or 0),
            "away_win": float(dixon["probabilities"].get("away_win") or 0),
        }
    if elo:
        draw_estimate = max(5, 30 - abs(float(elo["home_win_probability"]) - 50) * 0.4)
        model_probs["elo"] = {
            "home_win": float(elo.get("home_win_probability") or 0),
            "draw": draw_estimate,
            "away_win": float(elo.get("away_win_probability") or 0),
        }
    if poisson and poisson.get("probabilities"):
        model_probs["poisson"] = {
            "home_win": float(poisson["probabilities"].get("home_win") or 0),
            "draw": float(poisson["probabilities"].get("draw") or 0),
            "away_win": float(poisson["probabilities"].get("away_win") or 0),
        }
    
    rules_prob = max(0, min(100, rules_confidence))
    pick = str(rules_pick or "").lower()
    if "home" in pick:
        _de = max(5, 30 - abs(rules_prob - 50) * 0.4)
        model_probs["rules"] = {"home_win": rules_prob, "draw": _de, "away_win": max(0, 100 - rules_prob - _de)}
    elif "away" in pick:
        _de = max(5, 30 - abs(rules_prob - 50) * 0.4)
        model_probs["rules"] = {"home_win": max(0, 100 - rules_prob - _de), "draw": _de, "away_win": rules_prob}
    elif "draw" in pick:
        model_probs["rules"] = {"home_win": (100 - rules_prob) / 2, "draw": rules_prob, "away_win": (100 - rules_prob) / 2}
    
    if llm and llm.get("probabilities"):
        model_probs["llm"] = {
            "home_win": float(llm["probabilities"].get("home_win") or 0),
            "draw": float(llm["probabilities"].get("draw") or 0),
            "away_win": float(llm["probabilities"].get("away_win") or 0),
        }
    
    if len(model_probs) < 2:
        return {
            "diversity_score": 0,
            "supporting_models": list(model_probs.keys()),
            "opposing_models": [],
            "outcome_std": 0.0,
            "entropy": 0.0,
        }
    
    # Determine majority outcome from averaged probabilities
    avg_probs = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
    for probs in model_probs.values():
        for outcome in avg_probs:
            avg_probs[outcome] += probs.get(outcome, 0)
    for outcome in avg_probs:
        avg_probs[outcome] /= len(model_probs)
    
    majority_outcome = max(avg_probs, key=avg_probs.get)
    # majority_prob used for diversity_score calculation below
    
    supporting = []
    opposing = []
    outcome_probs = []
    
    for model_name, probs in model_probs.items():
        model_best = max(probs, key=probs.get)
        if model_best == majority_outcome:
            supporting.append(model_name)
        else:
            opposing.append(model_name)
        outcome_probs.append(probs.get(majority_outcome, 0))
    
    # Standard deviation of the majority outcome probability across models
    mean_prob = sum(outcome_probs) / len(outcome_probs)
    variance = sum((p - mean_prob) ** 2 for p in outcome_probs) / len(outcome_probs)
    std_dev = math.sqrt(variance)
    
    # Entropy of the averaged probability distribution
    entropy = 0.0
    for prob in avg_probs.values():
        if prob > 0:
            p = prob / 100
            entropy -= p * math.log2(p)
    entropy = min(entropy, 1.0)  # Normalize to 0-1
    
    # Diversity score: combines std_dev and entropy
    # Higher std_dev and higher entropy = more diversity/disagreement
    diversity_score = min(100, int(std_dev * 1.5 + entropy * 50))
    
    return {
        "diversity_score": diversity_score,
        "supporting_models": supporting,
        "opposing_models": opposing,
        "outcome_std": round(std_dev, 2),
        "entropy": round(entropy, 3),
        "majority_outcome": majority_outcome,
    }


def ensemble_prediction(
    dixon: dict[str, Any] | None,
    elo: dict[str, Any] | None,
    poisson: dict[str, Any] | None,
    rules_confidence: int,
    rules_pick: str,
    llm: dict[str, Any] | None = None,
) -> dict[str, Any]:
    diversity = compute_ensemble_diversity(dixon, elo, poisson, rules_confidence, rules_pick, llm)
    weights = _get_weights()
    scores = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
    total_weight = 0.0
    models_used: list[str] = []

    def _add(probs: dict[str, Any], weight: float) -> None:
        nonlocal total_weight
        scores["home_win"] += float(probs.get("home_win") or 0) * weight / 100
        scores["draw"] += float(probs.get("draw") or 0) * weight / 100
        scores["away_win"] += float(probs.get("away_win") or 0) * weight / 100
        total_weight += weight

    # Poisson and Dixon-Coles are correlated goal-scoring models -- treat them
    # as one "goal_model_family" contribution with one learned weight instead
    # of two separate ones, matching how the signal layer already reports
    # them (see enriched_prediction.py::_model_signals) and how the weight
    # LEARNING code now tracks them (see _BASE_WEIGHTS above for the full
    # why). When both are available, average their probabilities so the
    # family's overall influence stays equal to one model's weight, not two.
    has_dixon = bool(dixon and dixon.get("probabilities"))
    has_poisson = bool(poisson and poisson.get("probabilities"))
    if has_dixon and has_poisson:
        goal_family_probs = {
            outcome: (float(dixon["probabilities"].get(outcome) or 0) + float(poisson["probabilities"].get(outcome) or 0)) / 2
            for outcome in ("home_win", "draw", "away_win")
        }
        _add(goal_family_probs, weights.get("goal_model_family", 0.0))
        models_used.append("dixon_coles")
        models_used.append("poisson")
    elif has_dixon:
        _add(dixon["probabilities"], weights.get("goal_model_family", 0.0))
        models_used.append("dixon_coles")
    elif has_poisson:
        _add(poisson["probabilities"], weights.get("goal_model_family", 0.0))
        models_used.append("poisson")
    if elo:
        draw_estimate = max(5, 30 - abs(float(elo["home_win_probability"]) - 50) * 0.4)
        _add(
            {
                "home_win": elo["home_win_probability"],
                "draw": draw_estimate,
                "away_win": elo["away_win_probability"],
            },
            weights.get("elo", 0.0),
        )
        models_used.append("elo")

    rules_prob = max(0, min(100, rules_confidence))
    pick = str(rules_pick or "").lower()
    if "home" in pick:
        _de2 = max(5, 30 - abs(rules_prob - 50) * 0.4)
        _add({"home_win": rules_prob, "draw": _de2, "away_win": max(0, 100 - rules_prob - _de2)}, weights.get("rules", 0.0))
        models_used.append("rules")
    elif "away" in pick:
        _de2 = max(5, 30 - abs(rules_prob - 50) * 0.4)
        _add({"home_win": max(0, 100 - rules_prob - _de2), "draw": _de2, "away_win": rules_prob}, weights.get("rules", 0.0))
        models_used.append("rules")
    elif "draw" in pick:
        _add({"home_win": (100 - rules_prob) / 2, "draw": rules_prob, "away_win": (100 - rules_prob) / 2}, weights.get("rules", 0.0))
        models_used.append("rules")

    if llm and llm.get("probabilities"):
        _add(llm["probabilities"], weights.get("llm", 0.0))
        models_used.append("llm")

    if total_weight == 0:
        neutral = {"home_win": 33.3, "draw": 33.4, "away_win": 33.3}
        return {
            "model": "ensemble",
            "probabilities": neutral,
            "prediction": "Draw",
            "confidence": 50,
            "weights_used": {},
            "weights_source": "none",
            "models_used": [],
            "limited_signal": True,
        }

    for key in scores:
        scores[key] = round(scores[key] / total_weight * 100, 1)

    best = max(scores, key=scores.get)
    confidence = round(scores[best], 1)
    weights_source = "learned" if _weights_are_learned else "default"
    if models_used == ["rules"]:
        weights_source = "rules_only"
        confidence = max(50, confidence - 10)
    elif models_used == ["elo"]:
        weights_source = "partial"
        confidence = min(confidence, 68)
    elif len(models_used) < 4:
        weights_source = "partial"
    result = {
        "model": "ensemble",
        "probabilities": scores,
        "prediction": best.replace("_", " ").title(),
        "confidence": confidence,
        "weights_used": weights,
        "weights_source": weights_source,
        "models_used": models_used,
    }
    result.update(diversity)
    return result
