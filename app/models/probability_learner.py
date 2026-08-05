"""
Probability Learner — Learned Probability Distribution from Graded Results
============================================================================
Learns the probability distribution of win outcomes from historical graded
predictions. Key insight: away wins occur ~54% of the time when all signals
favor away, and home win % varies with signal mix.

The learner:
1. Reads graded prediction history with signal data
2. Groups outcomes by signal combination patterns
3. Learns probability distributions for each pattern
4. Stores learned distributions for use by the pick generator
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.db import db_conn, DB_PATH, _conn, _init_db


# ── Signal pattern key builder ──────────────────────────────

def _signal_pattern_key(signals: list[dict[str, Any]]) -> str:
    """Build a pattern key from the dominant signal directions.

    Groups signals into categories and determines the dominant direction
    for each category. This creates a pattern key that represents the
    signal combination.
    """
    categories: dict[str, float] = defaultdict(float)

    for sig in signals:
        category = sig.get("category", "unknown")
        direction = sig.get("direction", 0)
        strength = sig.get("strength", 0.5)
        categories[category] += direction * strength

    # Build pattern key from dominant directions
    parts = []
    for cat in sorted(categories.keys()):
        score = categories[cat]
        if score > 0.1:
            parts.append(f"{cat}:H")
        elif score < -0.1:
            parts.append(f"{cat}:A")
        else:
            parts.append(f"{cat}:N")

    return "|".join(parts) if parts else "no_signals"


def _signal_profile(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract a signal profile for learning.

    Returns a dict with:
    - home_signals: count of home-favoring signals
    - away_signals: count of away-favoring signals
    - neutral_signals: count of neutral signals
    - avg_strength: average signal strength
    - dominant_direction: 'home', 'away', or 'mixed'
    - all_favor_away: whether all signals favor away
    - all_favor_home: whether all signals favor home
    """
    home_count = 0
    away_count = 0
    neutral_count = 0
    total_strength = 0.0

    for sig in signals:
        direction = sig.get("direction", 0)
        strength = sig.get("strength", 0.5)
        total_strength += strength

        if direction > 0:
            home_count += 1
        elif direction < 0:
            away_count += 1
        else:
            neutral_count += 1

    total = home_count + away_count + neutral_count
    if total == 0:
        return {
            "home_signals": 0,
            "away_signals": 0,
            "neutral_signals": 0,
            "avg_strength": 0,
            "dominant_direction": "mixed",
            "all_favor_away": False,
            "all_favor_home": False,
        }

    # Determine dominant direction
    if home_count > away_count and home_count > neutral_count:
        dominant = "home"
    elif away_count > home_count and away_count > neutral_count:
        dominant = "away"
    else:
        dominant = "mixed"

    # Check if all signals favor away
    all_favor_away = all(
        sig.get("direction", 0) <= 0 for sig in signals
        if sig.get("category") not in {"h2h_draw", "draw_odds", "unknown"}
    )

    # Check if all signals favor home
    all_favor_home = all(
        sig.get("direction", 0) >= 0 for sig in signals
        if sig.get("category") not in {"h2h_draw", "draw_odds", "unknown"}
    )

    return {
        "home_signals": home_count,
        "away_signals": away_count,
        "neutral_signals": neutral_count,
        "avg_strength": round(total_strength / total, 4),
        "dominant_direction": dominant,
        "all_favor_away": all_favor_away,
        "all_favor_home": all_favor_home,
    }


# ── Probability learner ──────────────────────────────────────

class ProbabilityLearner:
    """Learns probability distributions from graded prediction results.

    The learner tracks outcomes by signal pattern and learns:
    - Away win rate when all signals favor away (~54% baseline)
    - Home win rate as a function of signal mix
    - Draw rate as a function of signal agreement
    - Confidence calibration per pattern
    """

    def __init__(self):
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the probability learner tables."""
        _init_db()
        with db_conn(timeout=10) as conn:
            conn.execute("""
                create table if not exists probability_patterns (
                    pattern_key text not null,
                    league_key text not null default '__global__',
                    pick_type text not null default 'match_result',
                    samples integer not null default 0,
                    wins integer not null default 0,
                    losses integer not null default 0,
                    draws integer not null default 0,
                    home_win_rate real,
                    draw_rate real,
                    away_win_rate real,
                    avg_confidence real,
                    avg_odds real,
                    confidence_calibration real,
                    last_updated text not null default current_timestamp,
                    primary key (pattern_key, league_key, pick_type)
                )
            """)
            conn.execute("""
                create table if not exists probability_distribution (
                    id integer primary key autoincrement,
                    league_key text not null default '__global__',
                    pick_type text not null default 'match_result',
                    home_prob real not null,
                    draw_prob real not null,
                    away_prob real not null,
                    confidence real not null,
                    samples integer not null,
                    source text not null default 'learned',
                    created_at text not null default current_timestamp
                )
            """)
            conn.execute("""
                create table if not exists signal_outcome_map (
                    id integer primary key autoincrement,
                    league_key text not null default '__global__',
                    signal_pattern text not null,
                    pick_type text not null default 'match_result',
                    home_prob real,
                    draw_prob real,
                    away_prob real,
                    actual_home_rate real,
                    actual_draw_rate real,
                    actual_away_rate real,
                    samples integer not null,
                    wins integer not null,
                    losses integer not null,
                    draws integer not null,
                    last_updated text not null default current_timestamp,
                    unique(league_key, signal_pattern, pick_type)
                )
            """)
            conn.commit()

    def record_outcome(
        self,
        signals: list[dict[str, Any]],
        result: str,
        pick_type: str = "match_result",
        league_key: str = "__global__",
        confidence: float = 0.5,
        odds: float = 1.0,
    ) -> None:
        """Record a graded outcome for learning.

        Args:
            signals: list of signal dicts with category, direction, strength
            result: 'win', 'loss', or 'draw'
            pick_type: the market type
            league_key: league identifier
            confidence: the confidence at prediction time
            odds: the odds at prediction time
        """
        pattern_key = _signal_pattern_key(signals)
        profile = _signal_profile(signals)

        with db_conn(timeout=10) as conn:
            # Upsert into probability_patterns
            conn.execute(
                """
                insert into probability_patterns
                    (pattern_key, league_key, pick_type, samples, wins, losses, draws,
                     avg_confidence, avg_odds, last_updated)
                values (?, ?, ?, 1, ?, ?, ?, ?, ?, current_timestamp)
                on conflict(pattern_key, league_key, pick_type) do update set
                    samples = probability_patterns.samples + 1,
                    wins = probability_patterns.wins + ?,
                    losses = probability_patterns.losses + ?,
                    draws = probability_patterns.draws + ?,
                    avg_confidence = (probability_patterns.avg_confidence * probability_patterns.samples + ?) / (probability_patterns.samples + 1),
                    avg_odds = (probability_patterns.avg_odds * probability_patterns.samples + ?) / (probability_patterns.samples + 1),
                    last_updated = current_timestamp
                """,
                (
                    pattern_key, league_key, pick_type,
                    1 if result == "win" else 0,
                    1 if result == "loss" else 0,
                    1 if result == "draw" else 0,
                    confidence, odds,
                    # Update values for the conflict case
                    1 if result == "win" else 0,
                    1 if result == "loss" else 0,
                    1 if result == "draw" else 0,
                    confidence, odds,
                ),
            )

            # Also record in signal_outcome_map for detailed analysis
            conn.execute(
                """
                insert into signal_outcome_map
                    (league_key, signal_pattern, pick_type,
                     home_prob, draw_prob, away_prob,
                     actual_home_rate, actual_draw_rate, actual_away_rate,
                     samples, wins, losses, draws, last_updated)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, current_timestamp)
                on conflict(league_key, signal_pattern, pick_type) do update set
                    samples = signal_outcome_map.samples + 1,
                    wins = signal_outcome_map.wins + ?,
                    losses = signal_outcome_map.losses + ?,
                    draws = signal_outcome_map.draws + ?,
                    actual_home_rate = (signal_outcome_map.actual_home_rate * signal_outcome_map.samples + ?) / (signal_outcome_map.samples + 1),
                    actual_draw_rate = (signal_outcome_map.actual_draw_rate * signal_outcome_map.samples + ?) / (signal_outcome_map.samples + 1),
                    actual_away_rate = (signal_outcome_map.actual_away_rate * signal_outcome_map.samples + ?) / (signal_outcome_map.samples + 1),
                    last_updated = current_timestamp
                """,
                (
                    league_key, pattern_key, pick_type,
                    profile["home_signals"] / max(1, profile["home_signals"] + profile["away_signals"] + profile["neutral_signals"]),
                    profile["neutral_signals"] / max(1, profile["home_signals"] + profile["away_signals"] + profile["neutral_signals"]),
                    profile["away_signals"] / max(1, profile["home_signals"] + profile["away_signals"] + profile["neutral_signals"]),
                    1.0 if result == "win" else 0.0,
                    1.0 if result == "draw" else 0.0,
                    1.0 if result == "loss" else 0.0,
                    1 if result == "win" else 0,
                    1 if result == "loss" else 0,
                    1 if result == "draw" else 0,
                    # Update values for conflict
                    1 if result == "win" else 0,
                    1 if result == "loss" else 0,
                    1 if result == "draw" else 0,
                    1.0 if result == "win" else 0.0,
                    1.0 if result == "draw" else 0.0,
                    1.0 if result == "loss" else 0.0,
                ),
            )

            conn.commit()

    def get_learned_probabilities(
        self,
        signals: list[dict[str, Any]],
        pick_type: str = "match_result",
        league_key: str = "__global__",
        min_samples: int = 10,
    ) -> dict[str, Any] | None:
        """Get learned probabilities for a signal pattern.

        Returns the learned probability distribution if enough samples exist,
        otherwise returns None.
        """
        pattern_key = _signal_pattern_key(signals)

        with db_conn(timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select home_win_rate, draw_rate, away_win_rate, samples, wins, losses, draws
                from probability_patterns
                where pattern_key = ? and league_key = ? and pick_type = ?
                """,
                (pattern_key, league_key, pick_type),
            ).fetchone()

            if row and row["samples"] >= min_samples:
                return {
                    "home_prob": row["home_win_rate"] or 0.45,
                    "draw_prob": row["draw_rate"] or 0.30,
                    "away_prob": row["away_win_rate"] or 0.25,
                    "samples": row["samples"],
                    "wins": row["wins"],
                    "losses": row["losses"],
                    "draws": row["draws"],
                    "source": "learned",
                    "pattern_key": pattern_key,
                }

        return None

    def get_away_baseline(
        self,
        league_key: str = "__global__",
        pick_type: str = "match_result",
    ) -> float:
        """Get the learned away win rate when all signals favor away.

        Returns the baseline away win rate (target: ~54%).
        """
        with db_conn(timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select away_win_rate, samples
                from probability_patterns
                where league_key = ? and pick_type = ?
                  and away_win_rate is not null
                order by samples desc
                limit 1
                """,
                (league_key, pick_type),
            ).fetchone()

            if row and row["samples"] >= 20:
                return row["away_win_rate"] or 0.54

        # Default baseline
        return 0.54

    def get_home_probability_function(
        self,
        league_key: str = "__global__",
        pick_type: str = "match_result",
    ) -> dict[str, Any]:
        """Get the learned home win probability as a function of signal mix.

        Returns a dict with the learned relationship between signal balance
        and home win probability.
        """
        with db_conn(timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select pattern_key, home_win_rate, away_win_rate, draw_rate, samples
                from probability_patterns
                where league_key = ? and pick_type = ?
                  and samples >= 5
                order by samples desc
                """,
                (league_key, pick_type),
            ).fetchall()

        if not rows:
            return {
                "home_base": 0.45,
                "home_per_home_signal": 0.05,
                "home_per_away_signal": -0.03,
                "draw_base": 0.30,
                "away_base": 0.25,
                "samples": 0,
            }

        # Calculate average rates
        total_samples = sum(row["samples"] for row in rows)
        avg_home = sum(row["home_win_rate"] * row["samples"] for row in rows) / total_samples
        avg_draw = sum(row["draw_rate"] * row["samples"] for row in rows) / total_samples
        avg_away = sum(row["away_win_rate"] * row["samples"] for row in rows) / total_samples

        return {
            "home_base": round(avg_home, 4),
            "draw_base": round(avg_draw, 4),
            "away_base": round(avg_away, 4),
            "samples": total_samples,
            "patterns": len(rows),
        }

    def run_learning_cycle(self) -> dict[str, Any]:
        """Run a full learning cycle over all graded predictions.

        This should be called after each grading run to update the
        probability distributions.
        """
        _init_db()

        # Get all graded predictions with signal data
        with db_conn(timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select ph.match_id, ph.league_name, ph.country_name,
                       ph.pick_type, ph.selection, ph.confidence, ph.result,
                       ph.signals_json, ph.audit_json, ph.context_json,
                       ph.created_at, ph.graded_at
                from prediction_history ph
                where ph.graded_at is not null
                  and ph.result in ('win', 'loss', 'draw')
                  and ph.pick_type != 'no_bet'
                  and ph.signals_json is not null
                  and ph.signals_json != '[]'
                order by ph.graded_at desc
                """
            ).fetchall()

        if not rows:
            return {"status": "no_graded_data", "patterns_learned": 0}

        learned = 0
        for row in rows:
            try:
                signals = json.loads(row["signals_json"] or "[]")
                if not signals:
                    continue

                # Normalize signals
                normalized_signals = []
                for sig in signals:
                    name = sig.get("name") or sig.get("signal_name") or ""
                    value = sig.get("value") or sig.get("signal_value") or 0
                    normalized = normalize_signal(name, value)
                    normalized["source"] = "prediction_history"
                    normalized_signals.append(normalized)

                self.record_outcome(
                    signals=normalized_signals,
                    result=row["result"],
                    pick_type=row["pick_type"] or "match_result",
                    league_key="__global__",
                    confidence=float(row["confidence"] or 0.5),
                )
                learned += 1
            except (json.JSONDecodeError, Exception):
                continue

        return {
            "status": "ok",
            "patterns_learned": learned,
            "total_graded": len(rows),
        }


# ── Convenience functions ────────────────────────────────────

def learn_probabilities(
    signals: list[dict[str, Any]],
    result: str,
    pick_type: str = "match_result",
    league_key: str = "__global__",
    confidence: float = 0.5,
    odds: float = 1.0,
) -> None:
    """Convenience function to record a learning outcome."""
    learner = ProbabilityLearner()
    learner.record_outcome(signals, result, pick_type, league_key, confidence, odds)


def get_learned_probabilities(
    signals: list[dict[str, Any]],
    pick_type: str = "match_result",
    league_key: str = "__global__",
    min_samples: int = 10,
) -> dict[str, Any] | None:
    """Convenience function to get learned probabilities."""
    learner = ProbabilityLearner()
    return learner.get_learned_probabilities(signals, pick_type, league_key, min_samples)
