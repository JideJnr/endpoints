from __future__ import annotations

from typing import Any


def kelly_fraction(probability: float, decimal_odds: float, fraction: float = 0.25) -> dict[str, Any]:
    """
    Full Kelly = (bp - q) / b where b = decimal_odds - 1, p = win prob, q = 1 - p.
    The default is quarter-Kelly to reduce bankroll volatility.
    """
    if decimal_odds <= 1:
        return {
            "probability": round(probability, 3),
            "decimal_odds": decimal_odds,
            "edge_percent": 0,
            "full_kelly": 0,
            "quarter_kelly": 0,
            "stake_per_100": 0,
            "value_bet": False,
            "recommendation": "skip",
        }

    p = max(0.0, min(1.0, probability))
    b = decimal_odds - 1
    q = 1 - p
    full_kelly = (b * p - q) / b
    fractional_kelly = max(0.0, full_kelly * fraction)
    edge = round((p * decimal_odds - 1) * 100, 2)
    return {
        "probability": round(p, 3),
        "decimal_odds": decimal_odds,
        "edge_percent": edge,
        "full_kelly": round(full_kelly, 4),
        "quarter_kelly": round(fractional_kelly, 4),
        "stake_per_100": round(fractional_kelly * 100, 2),
        "value_bet": edge > 3,
        "recommendation": "bet" if edge > 3 else "skip",
    }


def kelly_for_prediction(confidence: int, decimal_odds: float) -> dict[str, Any]:
    return kelly_fraction(confidence / 100, decimal_odds)
