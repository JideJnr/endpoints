from __future__ import annotations

from typing import Any


WEIGHTS = {
    "dixon_coles": 0.30,
    "elo": 0.25,
    "poisson": 0.15,
    "rules": 0.20,
    "groq": 0.10,
}


def ensemble_prediction(
    dixon: dict[str, Any] | None,
    elo: dict[str, Any] | None,
    poisson: dict[str, Any] | None,
    rules_confidence: int,
    rules_pick: str,
    groq: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scores = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0}
    total_weight = 0.0

    def _add(probs: dict[str, Any], weight: float) -> None:
        nonlocal total_weight
        scores["home_win"] += float(probs.get("home_win") or 0) * weight / 100
        scores["draw"] += float(probs.get("draw") or 0) * weight / 100
        scores["away_win"] += float(probs.get("away_win") or 0) * weight / 100
        total_weight += weight

    if dixon and dixon.get("probabilities"):
        _add(dixon["probabilities"], WEIGHTS["dixon_coles"])
    if elo:
        draw_estimate = max(5, 30 - abs(float(elo["home_win_probability"]) - 50) * 0.4)
        _add(
            {
                "home_win": elo["home_win_probability"],
                "draw": draw_estimate,
                "away_win": elo["away_win_probability"],
            },
            WEIGHTS["elo"],
        )
    if poisson and poisson.get("probabilities"):
        _add(poisson["probabilities"], WEIGHTS["poisson"])

    rules_prob = max(0, min(100, rules_confidence))
    pick = (rules_pick or "").lower()
    if "home" in pick:
        _add({"home_win": rules_prob, "draw": 10, "away_win": 100 - rules_prob}, WEIGHTS["rules"])
    elif "away" in pick:
        _add({"home_win": 100 - rules_prob, "draw": 10, "away_win": rules_prob}, WEIGHTS["rules"])
    elif "draw" in pick:
        _add({"home_win": (100 - rules_prob) / 2, "draw": rules_prob, "away_win": (100 - rules_prob) / 2}, WEIGHTS["rules"])

    if groq and groq.get("probabilities"):
        _add(groq["probabilities"], WEIGHTS["groq"])

    if total_weight == 0:
        return {"error": "no models available"}

    for key in scores:
        scores[key] = round(scores[key] / total_weight * 100, 1)

    best = max(scores, key=scores.get)
    return {
        "model": "ensemble",
        "probabilities": scores,
        "prediction": best.replace("_", " ").title(),
        "confidence": round(scores[best], 1),
        "models_used": [
            name
            for name, value in {
                "dixon_coles": dixon,
                "elo": elo,
                "poisson": poisson,
                "rules": bool(rules_pick),
                "groq": groq,
            }.items()
            if value
        ],
    }
