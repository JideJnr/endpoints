"""
Self-Learner
------------
The system's feedback loop. Reads graded prediction history and learns:

  1. Per-signal win rates  — which signals actually predict outcomes
  2. Per-league accuracy   — which leagues the models understand best
  3. Per-pick-type accuracy — which market types are most reliable
  4. Signal weight table   — auto-adjusted weights fed back into predictions

The learner runs after every grading cycle (job_grade_predictions) and
writes its findings to `signal_weights` and `league_accuracy` tables.

These tables are then read by:
  - enriched_prediction.py  → adjusts signal impact before ensemble
  - ai_brain.py             → memory_context block for LLM reasoning
  - ensemble.py             → dynamic model weights (via get_learned_weights)

Usage:
    from app.self_learner import run_learning_cycle, get_signal_weights, get_learned_weights

    # After grading — called automatically by job_grade_predictions
    result = run_learning_cycle()

    # At prediction time — get adjusted signal weights
    weights = get_signal_weights(league="Premier League")

    # Get auto-tuned ensemble model weights
    model_weights = get_learned_weights()
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.league_memory import DB_PATH, _init_db

MIN_SAMPLES = 15          # minimum graded predictions before trusting a signal
BLEND_WEIGHT = 0.45       # how much learned accuracy pulls the raw signal weight
                          # 0 = no adjustment, 1 = fully replace with learned rate
DECAY_FACTOR = 0.92       # older data matters less — applied per 30-day window


# ── Table setup ───────────────────────────────────────────────────────────────

def _init_learner_tables(conn: sqlite3.Connection) -> None:
    # Per-signal win rates (global + per league)
    conn.execute("""
        create table if not exists signal_weights (
            signal_name     text not null,
            league_key      text not null default '__global__',
            samples         integer not null default 0,
            wins            integer not null default 0,
            losses          integer not null default 0,
            win_rate        real,
            weight_adj      real not null default 0.0,
            last_updated    text not null default current_timestamp,
            primary key (signal_name, league_key)
        )
    """)
    conn.execute("""
        create table if not exists signal_pick_weights (
            signal_name     text not null,
            league_key      text not null default '__global__',
            pick_type       text not null default '__all__',
            samples         integer not null default 0,
            wins            integer not null default 0,
            losses          integer not null default 0,
            win_rate        real,
            weight_adj      real not null default 0.0,
            last_updated    text not null default current_timestamp,
            primary key (signal_name, league_key, pick_type)
        )
    """)
    # Per-league model accuracy
    conn.execute("""
        create table if not exists league_accuracy (
            league_key      text not null,
            league_name     text,
            pick_type       text not null,
            samples         integer not null default 0,
            wins            integer not null default 0,
            win_rate        real,
            avg_confidence  real,
            calibration_gap real,   -- win_rate - avg_confidence/100 (positive = underconfident)
            last_updated    text not null default current_timestamp,
            primary key (league_key, pick_type)
        )
    """)
    # Auto-tuned ensemble model weights
    conn.execute("""
        create table if not exists learned_model_weights (
            model_name      text primary key,
            base_weight     real not null,
            learned_weight  real not null,
            samples         integer not null default 0,
            win_rate        real,
            last_updated    text not null default current_timestamp
        )
    """)


# ── Main learning cycle ───────────────────────────────────────────────────────

def run_learning_cycle() -> dict[str, Any]:
    """
    Full learning pass over all graded predictions.
    Updates signal_weights, league_accuracy, and learned_model_weights.
    Call this after every grading run.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row

        # Pull all graded predictions with their signals
        rows = conn.execute("""
            select
                match_id, league_name, country_name, pick_type,
                selection, confidence, result, signals_json, created_at
            from prediction_history
            where graded_at is not null
              and result in ('win', 'loss')
              and pick_type != 'no_bet'
            order by created_at desc
        """).fetchall()

    if not rows:
        return {"status": "no_graded_data", "signal_updates": 0, "league_updates": 0}

    # ── 1. Aggregate signal win rates ─────────────────────────────────────────
    signal_stats: dict[tuple[str, str], dict] = {}
    signal_pick_stats: dict[tuple[str, str, str], dict] = {}

    for row in rows:
        league_key = _norm_league(row["league_name"] or "")
        result = row["result"]
        pick_type = row["pick_type"] or "unknown"
        signals = _safe_json(row["signals_json"])

        for sig in signals:
            name = str(sig.get("name") or "")
            if not name:
                continue
            # Global bucket
            _tally(signal_stats, (name, "__global__"), result)
            _tally(signal_pick_stats, (name, "__global__", pick_type), result)
            # League-specific bucket
            if league_key:
                _tally(signal_stats, (name, league_key), result)
                _tally(signal_pick_stats, (name, league_key, pick_type), result)

    # ── 2. Aggregate league accuracy ──────────────────────────────────────────
    league_stats: dict[tuple[str, str], dict] = {}  # (league_key, pick_type) → stats

    for row in rows:
        league_key = _norm_league(row["league_name"] or "")
        if not league_key:
            continue
        key = (league_key, row["pick_type"] or "unknown")
        if key not in league_stats:
            league_stats[key] = {
                "league_name": row["league_name"],
                "samples": 0, "wins": 0,
                "confidence_sum": 0.0,
            }
        league_stats[key]["samples"] += 1
        league_stats[key]["confidence_sum"] += float(row["confidence"] or 0)
        if result == "win":
            league_stats[key]["wins"] += 1

    # ── 3. Aggregate per-model accuracy from signals ──────────────────────────
    model_signal_map = {
        "dixon_coles": {"dixon_coles_model"},
        "elo":         {"elo_model"},
        "poisson":     {"poisson_model"},
        "rules":       {"goal_pressure", "h2h_edge", "league_position_edge",
                        "recent_history_edge", "common_opponent_edge",
                        "avg_rating_edge", "market_steam", "odds_edge"},
        "groq":        {"groq_agent", "ai_brain_review"},
    }
    model_stats: dict[str, dict] = {m: {"samples": 0, "wins": 0} for m in model_signal_map}

    for row in rows:
        signals = _safe_json(row["signals_json"])
        signal_names = {str(s.get("name") or "") for s in signals}
        result = row["result"]
        for model, model_signals in model_signal_map.items():
            if signal_names & model_signals:
                model_stats[model]["samples"] += 1
                if result == "win":
                    model_stats[model]["wins"] += 1

    # ── 4. Write everything to DB ─────────────────────────────────────────────
    signal_updates = 0
    league_updates = 0
    model_updates = 0

    with sqlite3.connect(DB_PATH) as conn:
        _init_learner_tables(conn)
        now = datetime.now(timezone.utc).isoformat()

        # Signal weights
        for (signal_name, league_key), stats in signal_stats.items():
            samples = stats["samples"]
            wins = stats["wins"]
            losses = stats["losses"]
            if samples < MIN_SAMPLES:
                continue
            win_rate = wins / samples
            # weight_adj: positive = boost this signal, negative = suppress it
            # Neutral is 0.50 win rate → adj = 0
            weight_adj = round((win_rate - 0.50) * 2.0, 3)  # range ≈ -1.0 to +1.0
            conn.execute("""
                insert into signal_weights
                    (signal_name, league_key, samples, wins, losses, win_rate, weight_adj, last_updated)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(signal_name, league_key) do update set
                    samples      = excluded.samples,
                    wins         = excluded.wins,
                    losses       = excluded.losses,
                    win_rate     = excluded.win_rate,
                    weight_adj   = excluded.weight_adj,
                    last_updated = excluded.last_updated
            """, (signal_name, league_key, samples, wins, losses,
                  round(win_rate, 4), weight_adj, now))
            signal_updates += 1

        # Signal weights scoped by market/pick type.
        for (signal_name, league_key, pick_type), stats in signal_pick_stats.items():
            samples = stats["samples"]
            wins = stats["wins"]
            losses = stats["losses"]
            if samples < MIN_SAMPLES:
                continue
            win_rate = wins / samples
            weight_adj = round((win_rate - 0.50) * 2.0, 3)
            conn.execute("""
                insert into signal_pick_weights
                    (signal_name, league_key, pick_type, samples, wins, losses, win_rate, weight_adj, last_updated)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(signal_name, league_key, pick_type) do update set
                    samples      = excluded.samples,
                    wins         = excluded.wins,
                    losses       = excluded.losses,
                    win_rate     = excluded.win_rate,
                    weight_adj   = excluded.weight_adj,
                    last_updated = excluded.last_updated
            """, (signal_name, league_key, pick_type, samples, wins, losses,
                  round(win_rate, 4), weight_adj, now))
            signal_updates += 1

        # League accuracy
        for (league_key, pick_type), stats in league_stats.items():
            samples = stats["samples"]
            wins = stats["wins"]
            if samples < 5:
                continue
            win_rate = wins / samples
            avg_conf = stats["confidence_sum"] / samples
            calibration_gap = round(win_rate - avg_conf / 100, 4)
            conn.execute("""
                insert into league_accuracy
                    (league_key, league_name, pick_type, samples, wins,
                     win_rate, avg_confidence, calibration_gap, last_updated)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(league_key, pick_type) do update set
                    league_name     = excluded.league_name,
                    samples         = excluded.samples,
                    wins            = excluded.wins,
                    win_rate        = excluded.win_rate,
                    avg_confidence  = excluded.avg_confidence,
                    calibration_gap = excluded.calibration_gap,
                    last_updated    = excluded.last_updated
            """, (league_key, stats["league_name"], pick_type, samples, wins,
                  round(win_rate, 4), round(avg_conf, 2), calibration_gap, now))
            league_updates += 1

        # Learned model weights
        base_weights = {
            "dixon_coles": 0.30, "elo": 0.25, "poisson": 0.15,
            "rules": 0.20, "groq": 0.10,
        }
        for model, stats in model_stats.items():
            samples = stats["samples"]
            wins = stats["wins"]
            if samples < MIN_SAMPLES:
                continue
            win_rate = wins / samples
            base = base_weights.get(model, 0.20)
            # Blend: if model win_rate > 55%, increase weight; < 45%, decrease
            performance_factor = 0.5 + (win_rate - 0.50) * 2.0  # 0.0 to 1.0
            learned = round(base * (1 - BLEND_WEIGHT) + base * performance_factor * BLEND_WEIGHT, 4)
            learned = max(0.05, min(0.50, learned))  # clamp to sane range
            conn.execute("""
                insert into learned_model_weights
                    (model_name, base_weight, learned_weight, samples, win_rate, last_updated)
                values (?, ?, ?, ?, ?, ?)
                on conflict(model_name) do update set
                    base_weight    = excluded.base_weight,
                    learned_weight = excluded.learned_weight,
                    samples        = excluded.samples,
                    win_rate       = excluded.win_rate,
                    last_updated   = excluded.last_updated
            """, (model, base, learned, samples, round(win_rate, 4), now))
            model_updates += 1

        conn.commit()

    print(
        f"[self_learner] cycle complete: "
        f"{signal_updates} signal weights | "
        f"{league_updates} league accuracy rows | "
        f"{model_updates} model weights updated"
    )
    return {
        "status": "ok",
        "total_graded_predictions": len(rows),
        "signal_updates": signal_updates,
        "league_updates": league_updates,
        "model_weight_updates": model_updates,
    }


# ── Read-back helpers used by prediction pipeline ─────────────────────────────

def get_signal_weights(league: str | None = None, pick_type: str | None = None) -> dict[str, float]:
    """
    Return a dict of signal_name → weight_adj for the given league.
    Falls back to global weights if no league-specific data exists.
    Used by enriched_prediction.py to adjust signal impacts.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row

        league_key = _norm_league(league or "")
        if pick_type:
            rows = conn.execute("""
                select signal_name, weight_adj, league_key, samples
                from signal_pick_weights
                where league_key in (?, '__global__')
                  and pick_type = ?
                  and samples >= ?
                order by case when league_key = ? then 0 else 1 end
            """, (league_key, pick_type, MIN_SAMPLES, league_key)).fetchall()
        else:
            rows = []
        if not rows:
            rows = conn.execute("""
                select signal_name, weight_adj, league_key, samples
                from signal_weights
                where league_key in (?, '__global__')
                  and samples >= ?
                order by case when league_key = ? then 0 else 1 end
            """, (league_key, MIN_SAMPLES, league_key)).fetchall()

    weights: dict[str, float] = {}
    for row in rows:
        name = row["signal_name"]
        if name not in weights:  # league-specific wins over global
            weights[name] = float(row["weight_adj"])
    return weights


def get_learned_weights() -> dict[str, float]:
    """
    Return auto-tuned ensemble model weights.
    Falls back to hardcoded defaults if not enough data yet.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select model_name, learned_weight from learned_model_weights"
        ).fetchall()

    if not rows:
        # Return hardcoded defaults
        return {"dixon_coles": 0.30, "elo": 0.25, "poisson": 0.15, "rules": 0.20, "groq": 0.10}

    weights = {row["model_name"]: float(row["learned_weight"]) for row in rows}
    # Normalise so weights sum to 1.0
    total = sum(weights.values())
    if total > 0:
        weights = {k: round(v / total, 4) for k, v in weights.items()}
    return weights


def get_league_accuracy(league: str) -> dict[str, Any]:
    """
    Return accuracy stats for a specific league.
    Used by ai_brain.py to build memory_context.
    """
    _init_db()
    league_key = _norm_league(league)
    with sqlite3.connect(DB_PATH) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            select pick_type, samples, wins, win_rate, avg_confidence, calibration_gap
            from league_accuracy
            where league_key = ?
            order by samples desc
        """, (league_key,)).fetchall()

    if not rows:
        return {"league": league, "known": False}

    return {
        "league": league,
        "known": True,
        "by_pick_type": [
            {
                "pick_type":        row["pick_type"],
                "samples":          row["samples"],
                "win_rate":         round(float(row["win_rate"] or 0) * 100, 1),
                "avg_confidence":   round(float(row["avg_confidence"] or 0), 1),
                "calibration_gap":  round(float(row["calibration_gap"] or 0) * 100, 1),
                "verdict":          _calibration_verdict(row["calibration_gap"]),
            }
            for row in rows
        ],
    }


def get_top_signals(limit: int = 20) -> list[dict[str, Any]]:
    """Return the highest and lowest performing signals globally."""
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            select signal_name, samples, wins, losses, win_rate, weight_adj
            from signal_weights
            where league_key = '__global__' and samples >= ?
            order by win_rate desc
            limit ?
        """, (MIN_SAMPLES, limit)).fetchall()

    return [
        {
            "signal":     row["signal_name"],
            "samples":    row["samples"],
            "wins":       row["wins"],
            "losses":     row["losses"],
            "win_rate":   round(float(row["win_rate"] or 0) * 100, 1),
            "weight_adj": round(float(row["weight_adj"] or 0), 3),
            "verdict":    "boost" if (row["weight_adj"] or 0) > 0.1
                          else "suppress" if (row["weight_adj"] or 0) < -0.1
                          else "neutral",
        }
        for row in rows
    ]


def get_learning_summary() -> dict[str, Any]:
    """Full summary of what the system has learned. Used by analytics endpoints."""
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row

        signal_count = conn.execute(
            "select count(*) from signal_weights where samples >= ?", (MIN_SAMPLES,)
        ).fetchone()[0]

        league_count = conn.execute(
            "select count(distinct league_key) from league_accuracy"
        ).fetchone()[0]

        model_rows = conn.execute(
            "select model_name, base_weight, learned_weight, samples, win_rate from learned_model_weights"
        ).fetchall()

        top_signals = conn.execute("""
            select signal_name, win_rate, weight_adj, samples
            from signal_weights
            where league_key = '__global__' and samples >= ?
            order by win_rate desc limit 5
        """, (MIN_SAMPLES,)).fetchall()

        bottom_signals = conn.execute("""
            select signal_name, win_rate, weight_adj, samples
            from signal_weights
            where league_key = '__global__' and samples >= ?
            order by win_rate asc limit 5
        """, (MIN_SAMPLES,)).fetchall()

    return {
        "signals_learned": signal_count,
        "leagues_profiled": league_count,
        "model_weights": [
            {
                "model":          row["model_name"],
                "base_weight":    row["base_weight"],
                "learned_weight": row["learned_weight"],
                "samples":        row["samples"],
                "win_rate":       round(float(row["win_rate"] or 0) * 100, 1),
                "shift":          round(float(row["learned_weight"] or 0) - float(row["base_weight"] or 0), 4),
            }
            for row in model_rows
        ],
        "top_signals": [
            {"signal": r["signal_name"], "win_rate": round(float(r["win_rate"] or 0) * 100, 1),
             "samples": r["samples"], "adj": r["weight_adj"]}
            for r in top_signals
        ],
        "bottom_signals": [
            {"signal": r["signal_name"], "win_rate": round(float(r["win_rate"] or 0) * 100, 1),
             "samples": r["samples"], "adj": r["weight_adj"]}
            for r in bottom_signals
        ],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tally(stats: dict, key: tuple, result: str) -> None:
    if key not in stats:
        stats[key] = {"samples": 0, "wins": 0, "losses": 0}
    stats[key]["samples"] += 1
    if result == "win":
        stats[key]["wins"] += 1
    else:
        stats[key]["losses"] += 1


def _norm_league(name: str) -> str:
    return name.lower().strip().replace(" ", "_")[:60] if name else ""


def _safe_json(raw: str | None) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _calibration_verdict(gap: float | None) -> str:
    if gap is None:
        return "unknown"
    g = float(gap)
    if g > 0.10:
        return "underconfident"   # model is more accurate than it thinks
    if g < -0.10:
        return "overconfident"    # model claims more than it delivers
    return "well_calibrated"
