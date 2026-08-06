"""
Pick Generator — Optimized Betting Picks from Learned Probabilities
====================================================================
Uses learned probability distributions to generate optimized betting picks
with confidence thresholds and fallback logic for edge cases.

Key features:
- Generates picks based on learned probability distributions
- Applies confidence thresholds to filter low-quality picks
- Falls back to safe picks when model is uncertain
- Prioritizes directional picks (home/away) over safe picks (double chance)
- Uses odds + probability to identify value bets
"""
from __future__ import annotations

import math
from typing import Any

from app.enrichment.signal_aggregator import SignalAggregator, calculate_win_probabilities, score_pick_direction
from app.models.probability_learner import ProbabilityLearner, get_learned_probabilities


# ── Confidence thresholds ──────────────────────────────────

CONFIDENCE_THRESHOLDS = {
    "high": 0.70,      # High-confidence pick — strong signal agreement
    "medium": 0.55,    # Medium-confidence pick — moderate signal agreement
    "low": 0.40,       # Low-confidence pick — weak signal agreement
    "minimum": 0.35,   # Absolute minimum — below this, no pick is generated
}


# ── Pick generator ──────────────────────────────────────────

class PickGenerator:
    """Generates optimized betting picks from learned probability distributions.

    The generator:
    1. Aggregates signals into win probabilities
    2. Looks up learned probability distributions
    3. Applies confidence thresholds
    4. Generates picks with fallback logic for edge cases
    """

    def __init__(
        self,
        league_key: str = "__global__",
        pick_type: str = "match_result",
        min_confidence: float = 0.55,
        target_odds_min: float = 1.5,
        target_odds_max: float = 5.0,
    ):
        self.league_key = league_key
        self.pick_type = pick_type
        self.min_confidence = min_confidence
        self.target_odds_min = target_odds_min
        self.target_odds_max = target_odds_max
        self._learner = ProbabilityLearner()

    def generate_picks(
        self,
        signals: list[dict[str, Any]],
        odds: dict[str, float] | None = None,
        match_name: str = "",
        league_name: str = "",
    ) -> list[dict[str, Any]]:
        """Generate optimized betting picks from signals and odds.

        Args:
            signals: list of signal dicts with category, direction, strength
            odds: dict with 'home', 'draw', 'away' odds (decimal)
            match_name: match name for the pick
            league_name: league name for the pick

        Returns:
            List of pick dicts sorted by confidence score (descending)
        """
        # Step 1: Calculate probabilities from signals
        prob_result = calculate_win_probabilities(signals, self.league_key)

        # Step 2: Get learned probabilities if available
        learned = self._learner.get_learned_probabilities(
            signals, self.pick_type, self.league_key, min_samples=10
        )

        # Step 3: Blend calculated and learned probabilities
        home_prob, draw_prob, away_prob, confidence = self._blend_probabilities(
            prob_result, learned
        )

        # Step 4: Score each direction
        odds = odds or {}
        odds_home = odds.get("home", 1.0)
        odds_draw = odds.get("draw", 1.0)
        odds_away = odds.get("away", 1.0)

        home_scored = score_pick_direction(
            home_prob, away_prob, draw_prob, confidence,
            odds_home, odds_draw, odds_away,
        )

        # Step 5: Generate picks based on confidence thresholds
        picks = []

        # Primary pick: highest scored direction
        primary_direction = home_scored["direction"]
        primary_score = home_scored["score"]
        primary_confidence = confidence

        # Apply confidence threshold
        if primary_confidence >= self.min_confidence:
            pick = self._create_pick(
                direction=primary_direction,
                prob=home_scored[f"{primary_direction}_prob"],
                confidence=primary_confidence,
                score=primary_score,
                odds=odds.get(primary_direction, 1.0),
                match_name=match_name,
                league_name=league_name,
                signal_scores=prob_result.get("signal_scores", {}),
                learned=learned is not None,
            )
            picks.append(pick)

        # Secondary pick: second best direction (if confidence is medium+)
        secondary_directions = [
            d for d in ["home", "draw", "away"]
            if d != primary_direction
        ]
        secondary_scored = sorted(
            [score_pick_direction(
                home_scored["home_prob"],
                home_scored["away_prob"],
                home_scored["draw_prob"],
                confidence,
                odds.get("home", 1.0),
                odds.get("draw", 1.0),
                odds.get("away", 1.0),
            ) for _ in secondary_directions],
            key=lambda x: x["score"],
            reverse=True,
        )

        for sec in secondary_directions[:1]:
            sec_scored = score_pick_direction(
                home_scored["home_prob"],
                home_scored["away_prob"],
                home_scored["draw_prob"],
                confidence,
                odds.get("home", 1.0),
                odds.get("draw", 1.0),
                odds.get("away", 1.0),
            )
            if sec_scored["confidence"] >= CONFIDENCE_THRESHOLDS["medium"]:
                pick = self._create_pick(
                    direction=sec_scored["direction"],
                    prob=sec_scored[f"{sec_scored['direction']}_prob"],
                    confidence=sec_scored["confidence"],
                    score=sec_scored["score"],
                    odds=odds.get(sec_scored["direction"], 1.0),
                    match_name=match_name,
                    league_name=league_name,
                    signal_scores=prob_result.get("signal_scores", {}),
                    learned=learned is not None,
                    secondary=True,
                )
                picks.append(pick)

        # Step 6: Fallback logic for edge cases
        if not picks:
            picks = self._fallback_picks(
                prob_result, odds, match_name, league_name, signals
            )

        # Step 7: Sort by confidence score (descending)
        picks.sort(key=lambda p: p.get("confidence", 0), reverse=True)

        return picks

    def _blend_probabilities(
        self,
        calculated: dict[str, Any],
        learned: dict[str, Any] | None,
    ) -> tuple[float, float, float, float]:
        """Blend calculated and learned probabilities.

        If learned probabilities are available with sufficient samples,
        blend them with the calculated probabilities (70% learned, 30% calculated).
        Otherwise, use calculated probabilities.
        """
        home_prob = calculated["home_prob"]
        draw_prob = calculated["draw_prob"]
        away_prob = calculated["away_prob"]
        confidence = calculated["confidence"]

        if learned and learned.get("samples", 0) >= 10:
            # Blend: 70% learned, 30% calculated
            blend = 0.70
            home_prob = (
                learned["home_prob"] * blend
                + home_prob * (1 - blend)
            )
            draw_prob = (
                learned["draw_prob"] * blend
                + draw_prob * (1 - blend)
            )
            away_prob = (
                learned["away_prob"] * blend
                + away_prob * (1 - blend)
            )
            # Boost confidence when we have learned data
            confidence = min(0.95, confidence + 0.10)

        return home_prob, draw_prob, away_prob, confidence

    def _create_pick(
        self,
        direction: str,
        prob: float,
        confidence: float,
        score: float,
        odds: float,
        match_name: str,
        league_name: str,
        signal_scores: dict[str, float],
        learned: bool = False,
        secondary: bool = False,
    ) -> dict[str, Any]:
        """Create a pick dict from the scored direction."""
        selection_map = {
            "home": "Home Win",
            "draw": "Draw",
            "away": "Away Win",
        }

        return {
            "type": "match_result",
            "selection": selection_map.get(direction, direction),
            "confidence": round(confidence * 100, 1),
            "probability": round(prob, 4),
            "odds": odds,
            "score": round(score, 4),
            "value_edge": round(score - 1.0, 4),
            "match_name": match_name,
            "league_name": league_name,
            "signal_scores": signal_scores,
            "learned": learned,
            "secondary": secondary,
            "source": "signal_aggregator",
        }

    def _fallback_picks(
        self,
        prob_result: dict[str, Any],
        odds: dict[str, float],
        match_name: str,
        league_name: str,
        signals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Generate fallback picks when confidence is below threshold.

        Fallback logic:
        1. If all signals favor away and away prob >= 54%, generate an away pick
        2. If odds are favorable (high odds + proven win history), generate a value pick
        3. Otherwise, return a no_bet pick
        """
        picks = []

        # Fallback 1: Away win when all signals favor away (54% baseline)
        if prob_result.get("all_signals_favor_away") and prob_result.get("away_prob", 0) >= 0.54:
            away_odds = odds.get("away", 1.0)
            if away_odds >= self.target_odds_min:
                picks.append(self._create_pick(
                    direction="away",
                    prob=prob_result["away_prob"],
                    confidence=0.54,  # Baseline confidence for away-favored
                    score=prob_result["away_prob"] * (1 + (away_odds - 1) * 0.5),
                    odds=away_odds,
                    match_name=match_name,
                    league_name=league_name,
                    signal_scores=prob_result.get("signal_scores", {}),
                    learned=False,
                ))

        # Fallback 2: Home win when all signals favor home
        if prob_result.get("all_signals_favor_home") and prob_result.get("home_prob", 0) >= 0.50:
            home_odds = odds.get("home", 1.0)
            if home_odds >= self.target_odds_min:
                picks.append(self._create_pick(
                    direction="home",
                    prob=prob_result["home_prob"],
                    confidence=0.50,  # Baseline confidence for home-favored
                    score=prob_result["home_prob"] * (1 + (home_odds - 1) * 0.5),
                    odds=home_odds,
                    match_name=match_name,
                    league_name=league_name,
                    signal_scores=prob_result.get("signal_scores", {}),
                    learned=False,
                ))

        # Fallback 3: Value bet — high odds with proven win history
        # Check if any direction has high odds and a reasonable probability
        for direction in ["home", "draw", "away"]:
            dir_odds = odds.get(direction, 1.0)
            dir_prob = prob_result.get(f"{direction}_prob", 0)
            implied_prob = 1 / dir_odds if dir_odds > 1 else 1

            # Value bet: probability > implied probability + margin
            value_edge = dir_prob - implied_prob
            if value_edge > 0.05 and dir_odds >= self.target_odds_min:
                picks.append(self._create_pick(
                    direction=direction,
                    prob=dir_prob,
                    confidence=max(0.40, dir_prob),
                    score=dir_prob * (1 + value_edge),
                    odds=dir_odds,
                    match_name=match_name,
                    league_name=league_name,
                    signal_scores=prob_result.get("signal_scores", {}),
                    learned=False,
                ))

        # Fallback 4: No bet if nothing qualifies
        if not picks:
            picks.append({
                "type": "no_bet",
                "selection": "No strong bet",
                "confidence": 0,
                "probability": 0,
                "odds": 0,
                "score": 0,
                "value_edge": 0,
                "match_name": match_name,
                "league_name": league_name,
                "signal_scores": prob_result.get("signal_scores", {}),
                "learned": False,
                "secondary": False,
                "source": "fallback_no_bet",
                "reason": "No signal pattern meets confidence threshold",
            })

        return picks


# ── Convenience functions ────────────────────────────────────

def generate_picks(
    signals: list[dict[str, Any]],
    odds: dict[str, float] | None = None,
    league_key: str = "__global__",
    pick_type: str = "match_result",
    min_confidence: float = 0.55,
) -> list[dict[str, Any]]:
    """Convenience function to generate picks from signals and odds."""
    generator = PickGenerator(
        league_key=league_key,
        pick_type=pick_type,
        min_confidence=min_confidence,
    )
    return generator.generate_picks(signals, odds)


def generate_optimized_slip(
    signals_list: list[list[dict[str, Any]]],
    odds_list: list[dict[str, float]],
    match_names: list[str],
    league_names: list[str],
    target_odds: float = 3.0,
    max_total_odds: float = 5.0,
    league_key: str = "__global__",
) -> dict[str, Any]:
    """Generate an optimized slip from multiple matches.

    Args:
        signals_list: list of signal lists (one per match)
        odds_list: list of odds dicts (one per match)
        match_names: list of match names
        league_names: list of league names
        target_odds: target combined odds for the slip
        max_total_odds: maximum combined odds ceiling
        league_key: league identifier

    Returns:
        Dict with picks, combined odds, and confidence
    """
    generator = PickGenerator(league_key=league_key)

    all_picks = []
    for i, (signals, odds, match_name, league_name) in enumerate(
        zip(signals_list, odds_list, match_names, league_names)
    ):
        picks = generator.generate_picks(signals, odds, match_name, league_name)
        if picks:
            best_pick = picks[0]
            best_pick["match_index"] = i
            all_picks.append(best_pick)

    # Sort by confidence score and select picks that reach target odds
    all_picks.sort(key=lambda p: p.get("score", 0), reverse=True)

    selected = []
    combined_odds = 1.0
    for pick in all_picks:
        pick_odds = pick.get("odds", 1.0)
        if combined_odds * pick_odds <= max_total_odds:
            selected.append(pick)
            combined_odds *= pick_odds
            if combined_odds >= target_odds:
                break

    avg_confidence = (
        sum(p.get("confidence", 0) for p in selected) / len(selected)
        if selected else 0
    )

    return {
        "picks": selected,
        "combined_odds": round(combined_odds, 3),
        "avg_confidence": round(avg_confidence, 1),
        "target_met": combined_odds >= target_odds,
        "pick_count": len(selected),
        "source": "signal_aggregator",
    }
