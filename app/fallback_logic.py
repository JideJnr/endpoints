"""
Fallback Logic — Edge Case Handling for Pick Generation
=========================================================

When the main pick generation logic cannot produce a directional pick,
the fallback is simply no_bet. There is no fallback to double chance
or draw — the system only returns picks it is confident about.

Priority order:
1. Directional pick with high odds + proven win history
2. Directional pick with high odds (even without proven history)
3. No-bet (when nothing qualifies)
"""
from __future__ import annotations

from typing import Any

from app.probability_learner import ProbabilityLearner


# ── Fallback configuration ──────────────────────────────

FALLBACK_CONFIG = {
    "min_odds": 1.5,            # Minimum odds for any pick
    "min_confidence": 0.35,     # Minimum confidence for a pick
    "high_odds_threshold": 2.5,  # Odds above this are "high odds"
    "proven_win_rate": 0.50,    # Minimum win rate for "proven" picks
    "proven_samples": 10,       # Minimum samples for "proven" status
    "away_baseline": 0.54,      # Away win baseline when all signals favor away
    "home_baseline": 0.50,      # Home win baseline when all signals favor home
}


# ── Fallback handler ──────────────────────────────────────

class FallbackHandler:
    """Handles edge cases in pick generation.

    The handler only returns directional picks (home/away) or no_bet.
    There is no fallback to double chance or draw — if the main logic
    cannot produce a directional pick, the result is no_bet.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = {**FALLBACK_CONFIG, **(config or {})}
        self._learner = ProbabilityLearner()

    def get_fallback_pick(
        self,
        signals: list[dict[str, Any]],
        odds: dict[str, float],
        prob_result: dict[str, Any],
        match_name: str = "",
        league_name: str = "",
    ) -> dict[str, Any]:
        """Get the best fallback pick for a match.

        Priority order:
        1. Directional pick with high odds + proven win history
        2. Directional pick with high odds (even without proven history)
        3. No-bet (when nothing qualifies)
        """
        # Strategy 1: High-odds directional pick with proven win history
        proven_pick = self._try_proven_directional(
            signals, odds, prob_result, match_name, league_name
        )
        if proven_pick:
            return proven_pick

        # Strategy 2: High-odds directional pick (even without proven history)
        directional_pick = self._try_directional(
            signals, odds, prob_result, match_name, league_name
        )
        if directional_pick:
            return directional_pick

        # Strategy 3: No bet — the main logic is strong enough,
        # so if it can't produce a pick, we don't force one
        return {
            "type": "no_bet",
            "selection": "No strong bet",
            "confidence": 0,
            "probability": 0,
            "odds": 0,
            "score": 0,
            "value_edge": 0,
            "match_name": match_name,
            "league_name": league_name,
            "source": "fallback_no_bet",
            "reason": "No directional pick meets minimum criteria",
        }

    def _try_proven_directional(
        self,
        signals: list[dict[str, Any]],
        odds: dict[str, float],
        prob_result: dict[str, Any],
        match_name: str,
        league_name: str,
    ) -> dict[str, Any] | None:
        """Try to find a directional pick with high odds AND proven win history.

        A pick is 'proven' if:
        - It has high odds (>= high_odds_threshold)
        - It has a proven win rate (>= proven_win_rate) from betbuilder_pick_memory
        - It has enough samples (>= proven_samples)
        """
        for direction in ["home", "away"]:
            dir_odds = odds.get(direction, 0)
            dir_prob = prob_result.get(f"{direction}_prob", 0)

            # Check if odds are high enough
            if dir_odds < self.config["high_odds_threshold"]:
                continue

            # Check if probability is reasonable
            if dir_prob < self.config["min_confidence"]:
                continue

            # Check proven win history
            proven = self._check_proven_history(
                direction, dir_odds, league_name
            )
            if proven and proven["win_rate"] >= self.config["proven_win_rate"]:
                return self._create_fallback_pick(
                    direction=direction,
                    prob=dir_prob,
                    confidence=min(0.85, proven["win_rate"]),
                    odds=dir_odds,
                    match_name=match_name,
                    league_name=league_name,
                    source="proven_directional",
                    proven=True,
                    win_rate=proven["win_rate"],
                    samples=proven["samples"],
                )

        return None

    def _try_directional(
        self,
        signals: list[dict[str, Any]],
        odds: dict[str, float],
        prob_result: dict[str, Any],
        match_name: str,
        league_name: str,
    ) -> dict[str, Any] | None:
        """Try to find a directional pick with high odds (even without proven history).

        Directional picks are the only acceptable fallback — double chance
        and draw are not used because they have lower odds and lower value.
        """
        for direction in ["home", "away"]:
            dir_odds = odds.get(direction, 0)
            dir_prob = prob_result.get(f"{direction}_prob", 0)

            # Check if odds are high enough
            if dir_odds < self.config["min_odds"]:
                continue

            # Check if probability is reasonable
            if dir_prob < self.config["min_confidence"]:
                continue

            # Check if this direction is favored by signals
            if direction == "home" and not prob_result.get("all_signals_favor_home", False):
                if dir_prob < 0.40:
                    continue
            if direction == "away" and not prob_result.get("all_signals_favor_away", False):
                if dir_prob < 0.40:
                    continue

            return self._create_fallback_pick(
                direction=direction,
                prob=dir_prob,
                confidence=max(0.40, dir_prob),
                odds=dir_odds,
                match_name=match_name,
                league_name=league_name,
                source="directional",
                proven=False,
            )

        return None

    def _check_proven_history(
        self,
        direction: str,
        odds: float,
        league_name: str,
    ) -> dict[str, Any] | None:
        """Check if a directional pick has proven win history.

        Uses betbuilder_pick_memory to check if the pick type + selection
        has a proven win rate at the given odds level.
        """
        try:
            from app.league_memory import betbuilder_pick_memory

            pick_type = "match_result"
            selection = direction
            odds_band = self._odds_band(odds)

            result = betbuilder_pick_memory(
                pick_type=pick_type,
                selection=selection,
                league=league_name,
                odds=odds,
            )

            if result.get("samples", 0) >= self.config["proven_samples"]:
                return {
                    "win_rate": result.get("win_rate", 0),
                    "samples": result.get("samples", 0),
                    "adjustment": result.get("adjustment", 0),
                }
        except Exception:
            pass

        return None

    def _odds_band(self, odds: float) -> str:
        """Categorize odds into bands for learning."""
        if odds < 1.5:
            return "1.0-1.5"
        elif odds < 2.0:
            return "1.5-2.0"
        elif odds < 3.0:
            return "2.0-3.0"
        elif odds < 5.0:
            return "3.0-5.0"
        else:
            return "5.0+"

    def _create_fallback_pick(
        self,
        direction: str,
        prob: float,
        confidence: float,
        odds: float,
        match_name: str,
        league_name: str,
        source: str,
        proven: bool = False,
        win_rate: float = 0,
        samples: int = 0,
    ) -> dict[str, Any]:
        """Create a fallback pick dict."""
        selection_map = {
            "home": "Home Win",
            "away": "Away Win",
        }

        pick = {
            "type": "match_result",
            "selection": selection_map.get(direction, direction),
            "confidence": round(confidence * 100, 1),
            "probability": round(prob, 4),
            "odds": odds,
            "score": round(prob * (1 + (odds - 1) * 0.5), 4),
            "value_edge": round(prob - (1 / odds) if odds > 1 else 0, 4),
            "match_name": match_name,
            "league_name": league_name,
            "source": source,
            "proven": proven,
            "fallback": True,
        }

        if proven:
            pick["proven_win_rate"] = round(win_rate, 4)
            pick["proven_samples"] = samples

        return pick


# ── Convenience function ──────────────────────────────────

def get_fallback_pick(
    signals: list[dict[str, Any]],
    odds: dict[str, float],
    prob_result: dict[str, Any],
    match_name: str = "",
    league_name: str = "",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convenience function to get a fallback pick."""
    handler = FallbackHandler(config)
    return handler.get_fallback_pick(
        signals, odds, prob_result, match_name, league_name
    )