"""
Signal Aggregator — Mixed Prediction Signal Processing
======================================================
Processes mixed prediction signals (home/away/table/H2H/odds) into
calibrated win probabilities for home win, draw, and away win.

Key design principles:
  - Away wins occur ~54% of the time when ALL signals favor away
  - Home win % varies with signal mix (not fixed)
  - Probabilities are learned from historical graded results
  - Confidence thresholds and fallback logic handle edge cases
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.storage.db import db_conn, DB_PATH, _conn, _init_db
from app.storage.league_memory import _init_db as lm_init_db


# ── Signal categories ──────────────────────────────────────────

SIGNAL_CATEGORIES = {
    "home_form": {"home", "home_win", "home_form", "home_last5", "home_wd"},
    "away_form": {"away", "away_win", "away_form", "away_last5", "away_wd"},
    "home_table": {"home_table", "home_position", "home_standing", "home_league_pos"},
    "away_table": {"away_table", "away_position", "away_standing", "away_league_pos"},
    "h2h_home": {"h2h_home", "h2h_home_win", "h2h_home_wins", "home_h2h"},
    "h2h_away": {"h2h_away", "h2h_away_win", "h2h_away_wins", "away_h2h"},
    "h2h_draw": {"h2h_draw", "h2h_draws", "h2h_tie"},
    "home_odds": {"home_odds", "home_win_odds", "odds_home", "1x2_home"},
    "away_odds": {"away_odds", "away_win_odds", "odds_away", "1x2_away"},
    "draw_odds": {"draw_odds", "odds_draw", "1x2_draw"},
    "home_goal_pressure": {"home_goal_pressure", "home_attack", "home_scoring"},
    "away_goal_pressure": {"away_goal_pressure", "away_attack", "away_scoring"},
    "home_defense": {"home_defense", "home_conceding", "home_clean_sheet"},
    "away_defense": {"away_defense", "away_conceding", "away_clean_sheet"},
    "market_steam_home": {"market_steam_home", "steam_home", "home_steam"},
    "market_steam_away": {"market_steam_away", "steam_away", "away_steam"},
    "venue_home": {"venue_home", "home_advantage", "home_venue"},
    "venue_away": {"venue_away", "away_venue", "away_disadvantage"},
}


# ── Signal normalizer ──────────────────────────────────────────

def normalize_signal(signal_name: str, value: Any) -> dict[str, Any]:
    """Normalize a raw signal into a standard format.

    Returns dict with: category, direction (+1 home-favoring, -1 away-favoring, 0 neutral),
    strength (0-1), and raw_value.
    """
    name_lower = str(signal_name).lower().strip()

    direction = 0
    strength = 0.5

    # Determine direction
    for category, keywords in SIGNAL_CATEGORIES.items():
        if name_lower in keywords:
            if category in {"home_form", "home_table", "h2h_home", "home_odds",
                           "home_goal_pressure", "home_defense", "market_steam_home",
                           "venue_home"}:
                direction = 1  # favors home
            elif category in {"away_form", "away_table", "h2h_away", "away_odds",
                             "away_goal_pressure", "away_defense", "market_steam_away",
                             "venue_away"}:
                direction = -1  # favors away
            elif category in {"h2h_draw", "draw_odds"}:
                direction = 0  # neutral
            break

    # Normalize strength
    if isinstance(value, (int, float)):
        if value < 0:
            strength = 0.0
            direction = -direction if direction != 0 else 0
        elif value <= 1:
            strength = abs(value)
        elif value <= 100:
            strength = abs(value) / 100
        else:
            strength = min(1.0, abs(value) / 100)
    elif isinstance(value, str):
        val_lower = value.lower().strip()
        if val_lower in {"high", "strong", "yes", "true", "win", "w"}:
            strength = 0.8
        elif val_lower in {"medium", "moderate", "neutral"}:
            strength = 0.5
        elif val_lower in {"low", "weak", "no", "false", "loss", "l"}:
            strength = 0.2
        else:
            try:
                strength = min(1.0, abs(float(val_lower)) / 100)
            except (ValueError, TypeError):
                strength = 0.5

    return {
        "category": _category_for_signal(name_lower),
        "direction": direction,
        "strength": strength,
        "raw_value": value,
        "signal_name": signal_name,
    }


def _category_for_signal(name: str) -> str:
    for category, keywords in SIGNAL_CATEGORIES.items():
        if name in keywords:
            return category
    return "unknown"


# ── Signal aggregator ──────────────────────────────────────────

class SignalAggregator:
    """Aggregates mixed prediction signals into calibrated win probabilities.

    The aggregator processes signals from multiple sources (form, table, H2H,
    odds, goal pressure, defense, market steam, venue) and produces:
    - home_prob: probability of home win
    - draw_prob: probability of draw
    - away_prob: probability of away win
    - confidence: overall confidence in the probability distribution
    - signal_scores: individual signal contributions
    """

    def __init__(self, league_key: str = "__global__"):
        self.league_key = league_key
        self.signals: list[dict[str, Any]] = []
        self._signal_weights: dict[str, float] = {}
        self._learned_baseline = False

    def add_signal(self, name: str, value: Any, source: str = "unknown") -> None:
        """Add a raw signal to the aggregator."""
        normalized = normalize_signal(name, value)
        normalized["source"] = source
        self.signals.append(normalized)

    def add_signals(self, signals: list[dict[str, Any]]) -> None:
        """Add multiple signals at once."""
        for sig in signals:
            name = sig.get("name") or sig.get("signal_name") or ""
            value = sig.get("value") or sig.get("signal_value") or 0
            source = sig.get("source", "unknown")
            self.add_signal(name, value, source)

    def set_signal_weights(self, weights: dict[str, float]) -> None:
        """Set custom weights for signal categories.

        Weights should be in range [-1, 1] where:
        - Positive weight = boost home-favoring signals
        - Negative weight = boost away-favoring signals
        - Zero = neutral
        """
        self._signal_weights = weights

    def load_learned_weights(self) -> None:
        """Load learned signal weights from the database."""
        _init_db()
        try:
            with db_conn(timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    select signal_name, weight_adj, league_key, samples, win_rate
                    from signal_weights
                    where league_key in (?, '__global__')
                    order by league_key desc, samples desc
                    """,
                    (self.league_key,),
                ).fetchall()

                for row in rows:
                    name = str(row["signal_name"] or "")
                    adj = float(row["weight_adj"] or 0)
                    samples = int(row["samples"] or 0)
                    # Only use weights with sufficient samples
                    if samples >= 10:
                        self._signal_weights[name] = adj
        except Exception:
            pass

        self._learned_baseline = True

    def _get_category_weight(self, category: str) -> float:
        """Get the weight for a signal category, using learned weights if available."""
        # Check for direct category weight
        if category in self._signal_weights:
            return self._signal_weights[category]

        # Check for learned weight by signal name
        for sig in self.signals:
            if sig.get("category") == category:
                name = sig.get("signal_name", "")
                if name in self._signal_weights:
                    return self._signal_weights[name]

        # Default weight based on category importance
        default_weights = {
            "home_form": 0.15,
            "away_form": 0.15,
            "home_table": 0.10,
            "away_table": 0.10,
            "h2h_home": 0.12,
            "h2h_away": 0.12,
            "h2h_draw": 0.05,
            "home_odds": 0.10,
            "away_odds": 0.10,
            "draw_odds": 0.05,
            "home_goal_pressure": 0.08,
            "away_goal_pressure": 0.08,
            "home_defense": 0.06,
            "away_defense": 0.06,
            "market_steam_home": 0.04,
            "market_steam_away": 0.04,
            "venue_home": 0.03,
            "venue_away": 0.03,
        }
        return default_weights.get(category, 0.05)

    def calculate_probabilities(self) -> dict[str, Any]:
        """Calculate win probabilities from aggregated signals.

        Returns dict with:
        - home_prob: probability of home win (0-1)
        - draw_prob: probability of draw (0-1)
        - away_prob: probability of away win (0-1)
        - confidence: overall confidence (0-1)
        - signal_scores: breakdown by category
        - all_signals_favor_away: whether all signals favor away
        - all_signals_favor_home: whether all signals favor home
        """
        if not self.signals:
            return self._default_probabilities()

        # Calculate weighted signal scores
        category_scores: dict[str, float] = defaultdict(float)
        category_strengths: dict[str, list[float]] = defaultdict(list)

        for sig in self.signals:
            category = sig.get("category", "unknown")
            direction = sig.get("direction", 0)
            strength = sig.get("strength", 0.5)
            weight = self._get_category_weight(category)

            # Weighted score: direction * strength * weight
            score = direction * strength * weight
            category_scores[category] += score
            category_strengths[category].append(abs(score))

        # Calculate home and away scores
        home_score = sum(
            score for cat, score in category_scores.items()
            if cat in {
                "home_form", "home_table", "h2h_home", "home_odds",
                "home_goal_pressure", "home_defense", "market_steam_home",
                "venue_home",
            }
        )
        away_score = sum(
            -score for cat, score in category_scores.items()
            if cat in {
                "away_form", "away_table", "h2h_away", "away_odds",
                "away_goal_pressure", "away_defense", "market_steam_away",
                "venue_away",
            }
        )
        neutral_score = sum(
            score for cat, score in category_scores.items()
            if cat in {"h2h_draw", "draw_odds"}
        )

        # Apply learned baseline: away wins = 54% when all signals favor away
        all_favor_away = all(
            sig.get("direction", 0) <= 0 for sig in self.signals
            if sig.get("category") not in {"h2h_draw", "draw_odds", "unknown"}
        )
        all_favor_home = all(
            sig.get("direction", 0) >= 0 for sig in self.signals
            if sig.get("category") not in {"h2h_draw", "draw_odds", "unknown"}
        )

        if all_favor_away and away_score > 0:
            # Baseline: away wins 54% when all signals favor away
            away_baseline = 0.54
            # Scale based on signal strength
            total_strength = sum(
                abs(sig.get("strength", 0.5)) for sig in self.signals
                if sig.get("direction", 0) < 0
            )
            away_boost = min(0.15, total_strength * 0.1)
            away_prob = min(0.85, away_baseline + away_boost)
            draw_prob = max(0.10, (1 - away_prob) * 0.40)
            home_prob = max(0.05, 1 - away_prob - draw_prob)
        elif all_favor_home and home_score > 0:
            # Home win % varies with signal mix
            total_strength = sum(
                abs(sig.get("strength", 0.5)) for sig in self.signals
                if sig.get("direction", 0) > 0
            )
            home_prob = min(0.85, 0.50 + total_strength * 0.15)
            away_prob = (1 - home_prob) * 0.65
            away_prob = max(0.05, away_prob)
            draw_prob = max(0.05, 1 - home_prob - away_prob)
        else:
            # Mixed signals — home win % varies with signal balance
            total_abs = abs(home_score) + abs(away_score) + abs(neutral_score)
            if total_abs == 0:
                return self._default_probabilities()

            home_ratio = home_score / total_abs
            away_ratio = away_score / total_abs
            neutral_ratio = abs(neutral_score) / total_abs

            # Home win probability varies with signal mix
            # Base: 45% home, 30% draw, 25% away (slight home advantage)
            home_prob = 0.45 + home_ratio * 0.25 - away_ratio * 0.10
            away_prob = 0.25 + away_ratio * 0.25 - home_ratio * 0.10
            draw_prob = 0.30 + neutral_ratio * 0.10 - abs(home_ratio - away_ratio) * 0.05

            # Clamp probabilities
            home_prob = max(0.05, min(0.85, home_prob))
            away_prob = max(0.05, min(0.85, away_prob))
            draw_prob = max(0.05, min(0.50, draw_prob))

            # Normalize to sum to 1
            total = home_prob + away_prob + draw_prob
            if total > 0:
                home_prob /= total
                away_prob /= total
                draw_prob /= total

            # Apply away baseline when all signals favor away
            if all_favor_away:
                away_prob = max(away_prob, 0.54)
                # Redistribute
                excess = away_prob - 0.54
                home_prob = max(0.05, home_prob - excess * 0.5)
                draw_prob = max(0.05, draw_prob - excess * 0.5)
                # Renormalize
                total = home_prob + away_prob + draw_prob
                if total > 0:
                    home_prob /= total
                    away_prob /= total
                    draw_prob /= total

        # Calculate confidence
        confidence = self._calculate_confidence(category_scores, category_strengths)

        return {
            "home_prob": round(home_prob, 4),
            "draw_prob": round(draw_prob, 4),
            "away_prob": round(away_prob, 4),
            "confidence": round(confidence, 4),
            "signal_scores": {
                cat: round(score, 4) for cat, score in category_scores.items()
            },
            "all_signals_favor_away": all_favor_away,
            "all_signals_favor_home": all_favor_home,
            "home_score": round(home_score, 4),
            "away_score": round(away_score, 4),
            "neutral_score": round(neutral_score, 4),
        }

    def _calculate_confidence(
        self,
        category_scores: dict[str, float],
        category_strengths: dict[str, list[float]],
    ) -> float:
        """Calculate overall confidence based on signal agreement and strength."""
        if not self.signals:
            return 0.0

        # Agreement: how much do signals agree on direction?
        home_signals = sum(
            1 for sig in self.signals if sig.get("direction", 0) > 0
        )
        away_signals = sum(
            1 for sig in self.signals if sig.get("direction", 0) < 0
        )
        neutral_signals = sum(
            1 for sig in self.signals if sig.get("direction", 0) == 0
        )
        total = home_signals + away_signals + neutral_signals

        if total == 0:
            return 0.5

        # Agreement ratio: how dominant is the majority direction?
        majority = max(home_signals, away_signals)
        agreement = majority / total

        # Strength: average signal strength
        avg_strength = sum(
            abs(sig.get("strength", 0.5)) for sig in self.signals
        ) / total

        # Sample size: more signals = more confidence
        sample_factor = min(1.0, len(self.signals) / 10)

        # Learned confidence boost if available
        learned_boost = 0.0
        if self._learned_baseline:
            # Check if we have learned accuracy for this league
            try:
                with db_conn(timeout=10) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        """
                        select win_rate, samples from league_accuracy
                        where league_key = ? and pick_type = 'match_result'
                        order by samples desc limit 1
                        """,
                        (self.league_key,),
                    ).fetchone()
                    if row and row["samples"] >= 20:
                        learned_boost = min(0.15, (row["win_rate"] - 0.50) * 0.3)
            except Exception:
                pass

        confidence = (
            agreement * 0.4
            + avg_strength * 0.3
            + sample_factor * 0.2
            + learned_boost
        )

        return max(0.1, min(0.95, confidence))

    def _default_probabilities(self) -> dict[str, Any]:
        """Return default probabilities when no signals are available."""
        return {
            "home_prob": 0.45,
            "draw_prob": 0.30,
            "away_prob": 0.25,
            "confidence": 0.1,
            "signal_scores": {},
            "all_signals_favor_away": False,
            "all_signals_favor_home": False,
            "home_score": 0,
            "away_score": 0,
            "neutral_score": 0,
        }


# ── Probability calculator ─────────────────────────────────────

def calculate_win_probabilities(
    signals: list[dict[str, Any]],
    league_key: str = "__global__",
) -> dict[str, Any]:
    """Convenience function to calculate win probabilities from a list of signals."""
    aggregator = SignalAggregator(league_key=league_key)
    aggregator.add_signals(signals)
    aggregator.load_learned_weights()
    return aggregator.calculate_probabilities()


# ── Signal scoring for pick generation ─────────────────────────

def score_pick_direction(
    home_prob: float,
    away_prob: float,
    draw_prob: float,
    confidence: float,
    odds_home: float = 1.0,
    odds_draw: float = 1.0,
    odds_away: float = 1.0,
) -> dict[str, Any]:
    """Score a pick direction based on probability and odds.

    Returns dict with:
    - direction: 'home', 'draw', or 'away'
    - score: combined probability-odds score
    - value_edge: edge over implied probability
    """
    implied_home = 1 / odds_home if odds_home > 1 else 1
    implied_draw = 1 / odds_draw if odds_draw > 1 else 1
    implied_away = 1 / odds_away if odds_away > 1 else 1

    # Normalize implied probabilities
    total_implied = implied_home + implied_draw + implied_away
    if total_implied > 0:
        implied_home /= total_implied
        implied_draw /= total_implied
        implied_away /= total_implied

    # Value edge: probability - implied probability
    home_edge = home_prob - implied_home
    draw_edge = draw_prob - implied_draw
    away_edge = away_prob - implied_away

    # Combined score: probability * (1 + value_edge)
    home_score = home_prob * (1 + home_edge)
    draw_score = draw_prob * (1 + draw_edge)
    away_score = away_prob * (1 + away_edge)

    # Determine best direction
    scores = {"home": home_score, "draw": draw_score, "away": away_score}
    best_direction = max(scores, key=scores.get)

    return {
        "direction": best_direction,
        "score": round(scores[best_direction], 4),
        "value_edge": round(
            {"home": home_edge, "draw": draw_edge, "away": away_edge}[best_direction], 4
        ),
        "home_score": round(home_score, 4),
        "draw_score": round(draw_score, 4),
        "away_score": round(away_score, 4),
        "home_prob": round(home_prob, 4),
        "draw_prob": round(draw_prob, 4),
        "away_prob": round(away_prob, 4),
        "confidence": round(confidence, 4),
    }


# ── Signal stat helpers (extracted from enriched_prediction.py) ────────────────

# Batch cache populated once per prediction pass to avoid N separate DB
# connections inside _cap_learned_signal_adjustment's per-signal loop.
_SIGNAL_STATS_BATCH_CACHE: dict[str, dict[str, Any]] = {}


def get_signal_stats_cache() -> dict[str, dict[str, Any]]:
    """Return the current batch cache (used by orchestrators to reset between passes)."""
    return _SIGNAL_STATS_BATCH_CACHE


def reset_signal_stats_cache() -> None:
    """Reset the per-prediction batch cache. Call this at the start of each prediction pass."""
    global _SIGNAL_STATS_BATCH_CACHE
    _SIGNAL_STATS_BATCH_CACHE = {}


def prefetch_signal_stats(signal_names: list[str]) -> None:
    """Load stats for all signal names in one query and populate the batch cache."""
    global _SIGNAL_STATS_BATCH_CACHE
    if not signal_names:
        return
    try:
        from app.storage.db import _conn
        from app.storage.league_memory import _init_db
        _init_db()
        placeholders = ",".join("?" * len(signal_names))
        with _conn(timeout=5) as conn:
            rows = conn.execute(
                f"""
                select signal_name,
                       count(*) as samples,
                       sum(case when result = 'win' then 1 else 0 end) as wins,
                       sum(case when result = 'loss' then 1 else 0 end) as losses
                from signal_outcomes
                where signal_name in ({placeholders}) and result in ('win', 'loss')
                group by signal_name
                """,
                signal_names,
            ).fetchall()
        for row in rows:
            name = row["signal_name"]
            samples = int(row["samples"] or 0)
            wins = int(row["wins"] or 0)
            _SIGNAL_STATS_BATCH_CACHE[name] = {
                "samples": samples,
                "wins": wins,
                "losses": int(row["losses"] or 0),
                "win_rate": round(wins / samples * 100, 1) if samples else None,
            }
        # Fill missing names with empty stats so callers don't re-query.
        for name in signal_names:
            _SIGNAL_STATS_BATCH_CACHE.setdefault(name, {"samples": 0, "wins": 0, "losses": 0, "win_rate": None})
    except Exception:
        pass


def global_signal_stats(signal_name: str) -> dict[str, Any]:
    """Return win/loss statistics for a named signal from the signal_outcomes table.

    Uses the batch cache if it has been pre-populated via prefetch_signal_stats().
    Falls back to a single-row DB query if the cache is cold.
    """
    cached = _SIGNAL_STATS_BATCH_CACHE.get(signal_name)
    if cached is not None:
        return cached
    try:
        from app.storage.db import _conn
        from app.storage.league_memory import _init_db
        _init_db()
        with _conn(timeout=5) as conn:
            row = conn.execute(
                """
                select count(*) as samples,
                       sum(case when result = 'win' then 1 else 0 end) as wins,
                       sum(case when result = 'loss' then 1 else 0 end) as losses
                from signal_outcomes
                where signal_name = ? and result in ('win', 'loss')
                """,
                (signal_name,),
            ).fetchone()
    except Exception as exc:
        try:
            from app.utils.health_counters import record_health_event
            record_health_event("signal_aggregator", "global_signal_stats_failed", exc)
        except Exception:
            pass
        return {"samples": 0, "wins": 0, "losses": 0, "win_rate": None}
    samples = int((row or [0])[0] or 0)
    wins = int((row or [0, 0])[1] or 0)
    losses = int((row or [0, 0, 0])[2] or 0)
    return {
        "samples": samples,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / samples * 100, 1) if samples else None,
    }


def signal_value(signals: list[dict[str, Any]], name: str) -> float:
    """Return the numeric value or impact for a named signal in a list, or 0.0 if absent."""
    for signal in signals:
        if signal.get("name") == name:
            try:
                v = signal.get("value")
                if v is not None and not isinstance(v, (dict, list)):
                    return float(v)
            except (TypeError, ValueError):
                pass
            try:
                impact = signal.get("impact")
                if impact is not None:
                    return float(impact)
            except (TypeError, ValueError):
                pass
    return 0.0


def signal_metric(rules: dict[str, Any], name: str, *, default: float = 0.0) -> float:
    """Return the mean impact (or value) for a named signal in a rules dict.

    Args:
        rules: A dict that may have a ``signals`` list (as produced by _rules_prediction).
        name:  The signal name to look up.
        default: Returned when no matching signal is found.
    """
    values = []
    for signal in rules.get("signals") or []:
        if signal.get("name") != name:
            continue
        value: float | None = None
        raw_impact = signal.get("impact")
        if raw_impact is not None:
            try:
                value = float(raw_impact)
            except (TypeError, ValueError):
                pass
        if value is None:
            raw_val = signal.get("value")
            if raw_val is not None and not isinstance(raw_val, (dict, list)):
                try:
                    value = float(raw_val)
                except (TypeError, ValueError):
                    pass
        if value is not None:
            values.append(value)
    if not values:
        return default
    return round(sum(values) / len(values), 2)
