"""Regime detection backed by learned tournament performance."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Regime:
    tier: int
    name: str
    min_confidence: int
    edge_threshold: float
    clv_min_samples: int
    stake_cap: float
    description: str


TIER_1 = Regime(
    tier=1,
    name="Elite",
    min_confidence=78,
    edge_threshold=0.06,
    clv_min_samples=20,
    stake_cap=2.0,
    description="High-performing learned league context",
)

TIER_2 = Regime(
    tier=2,
    name="Major",
    min_confidence=72,
    edge_threshold=0.05,
    clv_min_samples=15,
    stake_cap=1.75,
    description="Good learned league context",
)

TIER_3 = Regime(
    tier=3,
    name="Mid",
    min_confidence=68,
    edge_threshold=0.04,
    clv_min_samples=10,
    stake_cap=1.5,
    description="Neutral or insufficient learned league context",
)

TIER_4 = Regime(
    tier=4,
    name="Fringe",
    min_confidence=82,
    edge_threshold=0.08,
    clv_min_samples=5,
    stake_cap=1.0,
    description="Poor learned league context",
)


# Backward-compatible symbol. Active regime selection no longer uses a static
# league-name pattern table.
_TIER_RULES: list[tuple[str, int]] = []


def get_regime(tournament: str | None, category: str | None = None) -> Regime:
    """Return the learned regime for a tournament or a neutral fallback."""
    try:
        from app.monitoring.self_learner import get_tournament_priority

        learned = get_tournament_priority(tournament or category or "")
        if not learned.get("known"):
            return TIER_3
        priority = int(learned.get("priority", 4))
    except Exception:
        return TIER_3

    if priority <= 1:
        return TIER_1
    if priority <= 3:
        return TIER_2
    if priority <= 5:
        return TIER_3
    return TIER_4


def get_regime_for_doc(doc: dict[str, Any]) -> Regime:
    """Extract tournament/category from a match document and return a regime."""
    tournament = doc.get("tournament") or ""
    if isinstance(tournament, dict):
        tournament = tournament.get("name") or ""
    category = doc.get("category") or ""
    return get_regime(str(tournament), str(category))


def _tier(n: int) -> Regime:
    return {1: TIER_1, 2: TIER_2, 3: TIER_3, 4: TIER_4}[n]


def passes_regime_gate(
    tournament: str | None,
    confidence: int,
    edge: float = 0.0,
    category: str | None = None,
) -> dict[str, Any]:
    """
    Return whether a pick passes the learned regime thresholds.

    The regime is neutral for unknown leagues and stricter only after the
    learner has observed weak performance for that tournament.
    """
    regime = get_regime(tournament, category)
    penalty = {1: 0, 2: 0, 3: 0, 4: -5}.get(regime.tier, 0)
    adjusted = max(1, confidence + penalty)

    passed = adjusted >= regime.min_confidence
    edge_ok = edge == 0.0 or edge >= regime.edge_threshold

    reason = None
    if not passed:
        reason = f"confidence {adjusted}% below {regime.name} threshold ({regime.min_confidence}%)"
    elif not edge_ok:
        reason = f"edge {edge * 100:.1f}% below {regime.name} threshold ({regime.edge_threshold * 100:.0f}%)"

    return {
        "passed": passed and edge_ok,
        "tier": regime.tier,
        "regime": regime.name,
        "min_confidence": regime.min_confidence,
        "adjusted_confidence": adjusted,
        "stake_cap": regime.stake_cap,
        "reason": reason,
        "description": regime.description,
    }


def apply_regime_stake_cap(
    raw_multiplier: float,
    tournament: str | None,
    category: str | None = None,
) -> float:
    """Cap stake multiplier according to the learned regime."""
    regime = get_regime(tournament, category)
    return min(raw_multiplier, regime.stake_cap)


def regime_summary_for_predictions(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return prediction counts by learned regime tier."""
    counts: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0}
    for pred in predictions:
        tournament = pred.get("tournament") or pred.get("league_name") or ""
        regime = get_regime(str(tournament))
        counts[regime.tier] = counts.get(regime.tier, 0) + 1

    total = sum(counts.values()) or 1
    return {
        "by_tier": [
            {
                "tier": tier,
                "name": _tier(tier).name,
                "count": counts[tier],
                "pct": round(counts[tier] / total * 100, 1),
                "description": _tier(tier).description,
            }
            for tier in [1, 2, 3, 4]
            if counts[tier] > 0
        ]
    }
