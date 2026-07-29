"""
Bot 2 — Value Pick Curator
--------------------------
Reviews Bot 1 predictions and selects only the highest-value bets.
Uses pure Python scoring (rules mode only).
"""
from __future__ import annotations

from datetime import date as dt
from typing import Any

from app.league_memory import list_prediction_history


def _run_bot2_rules(predictions: list[dict], match_date: str) -> list[dict]:
    picks = []
    for prediction in predictions:
        best = prediction.get("best_pick") or {}
        confidence = (best.get("confidence") or 0) / 100
        if confidence < 0.68:
            continue
        if best.get("type") == "no_bet" or "Draw" in str(best.get("selection", "")):
            continue
        odds = _estimate_odds(confidence)
        if odds < 1.8:
            continue
        signal_score = min(len(prediction.get("signals") or []) / 8, 0.25)
        value_score = round(min(0.99, confidence * 0.7 + min(odds / 5, 1) * 0.2 + signal_score), 3)
        risk = (
            "Low" if confidence >= 0.78 and odds <= 3
            else "Medium" if confidence >= 0.70 and odds <= 5
            else "High"
        )
        picks.append({
            "match": prediction.get("match_name"),
            "tournament": prediction.get("league_name"),
            "category": "",
            "kickoff_utc": "",
            "prediction": best.get("selection"),
            "odds": str(odds),
            "bot1_confidence": confidence,
            "value_score": value_score,
            "why_selected": (
                f"{best.get('reason') or 'Strong model pick'} "
                f"Signals count: {len(prediction.get('signals') or [])}."
            ),
            "risk": risk,
            "match_date": match_date,
        })
    picks.sort(key=lambda item: item["value_score"], reverse=True)
    return picks[:20]


def run_bot2(match_date: str | None = None, limit: int = 200) -> dict[str, Any]:
    target_date = match_date or dt.today().isoformat()
    predictions = list_prediction_history(limit=limit)["predictions"]

    if not predictions:
        return {"status": "no_predictions", "date": target_date, "picks": []}

    picks = _run_bot2_rules(predictions, target_date)
    return {
        "status": "success",
        "date": target_date,
        "mode": "rules",
        "total_reviewed": len(predictions),
        "picks_selected": len(picks),
        "picks": picks,
    }


def _estimate_odds(confidence: float) -> float:
    if confidence <= 0:
        return 0
    return round(max(1.01, 1 / confidence), 2)
