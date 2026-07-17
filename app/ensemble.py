from __future__ import annotations

from typing import Any


# Hardcoded fallback weights — overridden by learned weights when enough data exists
_BASE_WEIGHTS = {
    "dixon_coles": 0.30,
    "elo": 0.25,
    "poisson": 0.15,
    "rules": 0.20,
    "groq": 0.10,
}

# Module-level cache so we don't hit SQLite on every prediction
_cached_weights: dict[str, float] | None = None
_cache_hits = 0
_CACHE_REFRESH_EVERY = 50  # refresh learned weights every N predictions


def _get_weights() -> dict[str, float]:
    """Return learned weights if available, else hardcoded defaults."""
    global _cached_weights, _cache_hits
    _cache_hits += 1
    if _cached_weights is None or _cache_hits % _CACHE_REFRESH_EVERY == 0:
        try:
            from app.self_learner import get_learned_weights
            learned = get_learned_weights()
            if learned:
                _cached_weights = learned
                return _cached_weights
        except Exception:
            pass
        _cached_weights = dict(_BASE_WEIGHTS)
    return _cached_weights


# Keep WEIGHTS accessible for backward compatibility
WEIGHTS = _BASE_WEIGHTS


def ensemble_prediction(
    dixon: dict[str, Any] | None,
    elo: dict[str, Any] | None,
    poisson: dict[str, Any] | None,
    rules_confidence: int,
    rules_pick: str,
    groq: dict[str, Any] | None = None,
) -> dict[str, Any]:
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

    if dixon and dixon.get("probabilities"):
        _add(dixon["probabilities"], weights.get("dixon_coles", 0.30))
        models_used.append("dixon_coles")
    if elo:
        draw_estimate = max(5, 30 - abs(float(elo["home_win_probability"]) - 50) * 0.4)
        _add(
            {
                "home_win": elo["home_win_probability"],
                "draw": draw_estimate,
                "away_win": elo["away_win_probability"],
            },
            weights.get("elo", 0.25),
        )
        models_used.append("elo")
    if poisson and poisson.get("probabilities"):
        _add(poisson["probabilities"], weights.get("poisson", 0.15))
        models_used.append("poisson")

    rules_prob = max(0, min(100, rules_confidence))
    pick = (rules_pick or "").lower()
    if "home" in pick:
        _add({"home_win": rules_prob, "draw": 10, "away_win": 100 - rules_prob}, weights.get("rules", 0.20))
        models_used.append("rules")
    elif "away" in pick:
        _add({"home_win": 100 - rules_prob, "draw": 10, "away_win": rules_prob}, weights.get("rules", 0.20))
        models_used.append("rules")
    elif "draw" in pick:
        _add({"home_win": (100 - rules_prob) / 2, "draw": rules_prob, "away_win": (100 - rules_prob) / 2}, weights.get("rules", 0.20))
        models_used.append("rules")

    if groq and groq.get("probabilities"):
        _add(groq["probabilities"], weights.get("groq", 0.10))
        models_used.append("groq")

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
    weights_source = "learned" if _cached_weights and _cached_weights != _BASE_WEIGHTS else "default"
    if models_used == ["rules"]:
        weights_source = "rules_only"
        confidence = max(50, confidence - 10)
    elif models_used == ["elo"]:
        weights_source = "partial"
        confidence = min(confidence, 68)
    elif len(models_used) < 4:
        weights_source = "partial"
    return {
        "model": "ensemble",
        "probabilities": scores,
        "prediction": best.replace("_", " ").title(),
        "confidence": confidence,
        "weights_used": weights,
        "weights_source": weights_source,
        "models_used": models_used,
    }
