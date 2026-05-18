"""
Odds-Only Predictor
-------------------
A lightweight model that uses ONLY the 1x2 odds to make a prediction.
No form data, no SofaScore, no team history required.

Logic:
  1. Extract implied probabilities from 1x2 odds
  2. Remove bookmaker margin (overround)
  3. Pick the side with highest true probability
  4. Adjust confidence using historical signal win rates from MongoDB
     (odds_edge signal win rate for this tournament/country scope)
  5. Apply regime minimum confidence gate

This runs on ANY match that has odds — even unenriched ones.
Use it as a fast baseline when full enrichment hasn't run yet.
"""
from __future__ import annotations

from typing import Any


def odds_only_prediction(doc: dict[str, Any]) -> dict[str, Any] | None:
    """
    Predict using only 1x2 odds from the match doc.
    Returns a prediction dict or None if no odds available.
    """
    odds = _extract_1x2(doc)
    if not odds.get("home") or not odds.get("away"):
        return None

    home_dec = float(odds["home"])
    draw_dec = float(odds.get("draw") or 0)
    away_dec = float(odds["away"])

    # Remove overround — normalise to true probabilities
    raw = {
        "home": 1 / home_dec,
        "draw": 1 / draw_dec if draw_dec > 1 else 0,
        "away": 1 / away_dec,
    }
    total = sum(raw.values()) or 1
    true_probs = {k: round(v / total * 100, 1) for k, v in raw.items()}

    # Pick the strongest side
    best_side = max(true_probs, key=true_probs.get)
    best_prob  = true_probs[best_side]

    # Only predict when there's a clear favourite (>45% true probability)
    if best_prob < 45:
        return None

    selection_map = {"home": "Home", "draw": "Draw", "away": "Away"}
    selection = selection_map[best_side]

    # Base confidence = true probability, capped at 85
    confidence = min(85, round(best_prob))

    # Boost if odds shortened (market steam signal)
    movement = doc.get("odds_movement") or {}
    move = (movement.get("movement") or {}).get(best_side)
    if move == "shortened":
        confidence = min(88, confidence + 4)
    elif move == "drifted":
        confidence = max(50, confidence - 4)

    # Apply regime gate
    tournament = _tournament_name(doc)
    category   = doc.get("category") or ""
    try:
        from app.regime import passes_regime_gate
        gate = passes_regime_gate(tournament, confidence, category=category)
        if not gate["passed"]:
            return None
        confidence = gate["adjusted_confidence"]
    except Exception:
        pass

    return {
        "match_id":   str(doc.get("sportybet_id") or doc.get("id") or ""),
        "name":       doc.get("sportybet_name") or doc.get("name"),
        "tournament": tournament,
        "source":     "odds_only",
        "picks": [{
            "type":       "match_result",
            "selection":  selection,
            "confidence": confidence,
            "reason":     f"Odds imply {best_prob}% true probability for {selection}",
        }],
        "signals": [{
            "name":   "odds_implied_probability",
            "value":  true_probs,
            "impact": round(best_prob - 33.3, 1),
        }],
        "odds": {
            "home": home_dec,
            "draw": draw_dec or None,
            "away": away_dec,
        },
        "true_probabilities": true_probs,
        "market_move": move,
    }


def _extract_1x2(doc: dict[str, Any]) -> dict[str, Any]:
    # Try sportybet_markets first
    for market in (doc.get("sportybet_markets") or doc.get("markets") or []):
        name = (market.get("name") or "").lower()
        if market.get("id") == "1" or "1x2" in name or name == "match result":
            sels = {s.get("name"): s.get("odds") for s in market.get("selections", [])}
            return {
                "home": sels.get("Home") or sels.get("1"),
                "draw": sels.get("Draw") or sels.get("X"),
                "away": sels.get("Away") or sels.get("2"),
            }
    # Fallback to odds_1x2 summary field
    return doc.get("odds_1x2") or {}


def _tournament_name(doc: dict[str, Any]) -> str | None:
    t = doc.get("tournament")
    if isinstance(t, dict):
        return t.get("name")
    return t or None
