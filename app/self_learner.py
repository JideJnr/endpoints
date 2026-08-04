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

from app.db import db_conn
from app.db import DB_PATH
from app.league_memory import _init_db, _get_passed_models

UNIQUE_GRADED_HISTORY = """
    select *
    from (
        select
            ph.*,
            row_number() over (
                partition by match_id, pick_type, selection
                order by datetime(coalesce(graded_at, created_at)) desc, id desc
            ) as rn
        from (
            select id, match_id, league_name, country_name, pick_type,
                   selection, confidence, result, signals_json, audit_json, models_json, '{}' as context_json,
                   created_at, graded_at
            from prediction_history
            where graded_at is not null
              and result in ('win', 'loss')
              and pick_type != 'no_bet'
            union all
            select id, match_id, league_name, country_name, pick_type,
                   selection, confidence, result, signals_json, audit_json, '{}' as models_json, context_json,
                   created_at, graded_at
            from prediction_candidate_history
            where graded_at is not null
              and result in ('win', 'loss')
              and pick_type != 'no_bet'
        ) ph
    )
    where rn = 1
"""

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
    with db_conn(timeout=30) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row

        # Pull one settled row per match + pick. Live refreshes can record the
        # same pick repeatedly, but learning should only count the outcome once.
        rows = conn.execute(f"""
            select
                match_id, league_name, country_name, pick_type,
                selection, confidence, result, signals_json, audit_json, context_json, created_at
            from ({UNIQUE_GRADED_HISTORY})
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
        signals = _decision_signals_for_row(row)

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
        "openrouter":  {"openrouter_agent", "ai_brain_review"},
    }
    model_stats: dict[str, dict] = {m: {"samples": 0, "wins": 0} for m in model_signal_map}

    for row in rows:
        signals = _decision_signals_for_row(row)
        signal_names = {str(s.get("name") or "") for s in signals}
        result = row["result"]
        for model, model_signals in model_signal_map.items():
            if signal_names & model_signals:
                model_stats[model]["samples"] += 1
                if result == "win":
                    model_stats[model]["wins"] += 1

    # ── 3b. Direct model accuracy from models_json ──────────────────
    # Uses the actual models stored with each prediction to determine
    # which models passed, rather than inferring from signal names.
    direct_model_stats: dict[str, dict] = {m: {"samples": 0, "wins": 0} for m in model_signal_map}

    for row in rows:
        models_json = _safe_json(row["models_json"] if "models_json" in row.keys() else "{}", {})
        if not models_json:
            continue
        result = row["result"]
        passed = _get_passed_models(models_json, result)
        for model_name in passed:
            if model_name in direct_model_stats:
                direct_model_stats[model_name]["samples"] += 1
                if result == "win":
                    direct_model_stats[model_name]["wins"] += 1

    # ── 4. Write everything to DB ─────────────────────────────────────────────
    signal_updates = 0
    league_updates = 0
    model_updates = 0

    with db_conn(timeout=30) as conn:
        _init_learner_tables(conn)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("delete from signal_weights")
        conn.execute("delete from signal_pick_weights")
        conn.execute("delete from league_accuracy")
        conn.execute("delete from learned_model_weights")

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

        # Learned model weights — prefer direct models_json accuracy,
        # fall back to signal-based heuristics when models_json is empty.
        base_weights = {
            "dixon_coles": 0.30, "elo": 0.25, "poisson": 0.15,
            "rules": 0.20, "openrouter": 0.10,
        }
        for model in base_weights:
            direct = direct_model_stats.get(model, {"samples": 0, "wins": 0})
            heuristic = model_stats.get(model, {"samples": 0, "wins": 0})
            # Use direct data when we have enough samples, else fall back
            use_direct = direct["samples"] >= MIN_SAMPLES
            stats = direct if use_direct else heuristic
            samples = stats["samples"]
            wins = stats["wins"]
            if samples < MIN_SAMPLES:
                continue
            win_rate = wins / samples
            base = base_weights.get(model, 0.20)
            performance_factor = 0.5 + (win_rate - 0.50) * 2.0
            learned = round(base * (1 - BLEND_WEIGHT) + base * performance_factor * BLEND_WEIGHT, 4)
            learned = max(0.05, min(0.50, learned))
            source = "models_json" if use_direct else "signal_heuristic"
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

        # ── 5. AI analysis feedback loop ────────────────────────────────
        ai_updates = _incorporate_ai_analysis(conn, rows)

        # ── 6. Incorporate user behavior for self-learning ────────────
        behavior_updates = _incorporate_user_behavior(conn, rows)

        # ── 7. Grade specialist contributions ─────────────────────────
        specialist_updates = _grade_specialists_from_history(rows)

        conn.commit()

    print(
        f"[self_learner] cycle complete: "
        f"{signal_updates} signal weights | "
        f"{league_updates} league accuracy rows | "
        f"{model_updates} model weights updated | "
        f"{ai_updates} AI analysis adjustments | "
        f"{behavior_updates} behavior adjustments | "
        f"{specialist_updates} specialist credits"
    )
    return {
        "status": "ok",
        "total_graded_predictions": len(rows),
        "signal_updates": signal_updates,
        "league_updates": league_updates,
        "model_weight_updates": model_updates,
        "ai_analysis_adjustments": ai_updates,
        "behavior_adjustments": behavior_updates,
        "specialist_credits": specialist_updates,
    }


# ── Read-back helpers used by prediction pipeline ─────────────────────────────

def get_signal_weights(league: str | None = None, pick_type: str | None = None) -> dict[str, float]:
    """
    Return a dict of signal_name → weight_adj for the given league.
    Falls back to global weights if no league-specific data exists.
    Used by enriched_prediction.py to adjust signal impacts.
    """
    _init_db()
    with db_conn(timeout=30) as conn:
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
    with db_conn(timeout=30) as conn:
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
    with db_conn(timeout=30) as conn:
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
    with db_conn(timeout=30) as conn:
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
    with db_conn(timeout=30) as conn:
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


def _decision_signals_for_row(row: sqlite3.Row) -> list[dict[str, Any]]:
    """Return only signals that plausibly drove this row's market decision.

    Prediction rows keep many background signals for auditability. Learning from
    all of them blurs attribution, so this filter trains weights on market-
    relevant evidence first and falls back to strong support signals only when
    the pick type is unknown.
    """
    signals = _safe_json(row["signals_json"])
    if not signals:
        return []

    audit = _safe_json_object(row["audit_json"] if "audit_json" in row.keys() else None)
    context = _safe_json_object(row["context_json"] if "context_json" in row.keys() else None)
    pick_type = str(row["pick_type"] or "").lower()
    selection = str(row["selection"] or "").lower()
    allowed = _market_signal_names(pick_type, selection, context)
    support_names = _audit_support_signal_names(audit)

    decision: list[dict[str, Any]] = []
    seen: set[str] = set()
    for signal in signals:
        name = str(signal.get("name") or "")
        if not name or name in seen:
            continue
        if name in allowed or name in support_names or _signal_mentions_selection(signal, selection):
            decision.append(signal)
            seen.add(name)

    if decision:
        return decision

    # Conservative fallback: use high-impact support/risk signals, but keep
    # generic source-presence signals out of the learner.
    for signal in signals:
        name = str(signal.get("name") or "")
        if name in _BACKGROUND_SIGNAL_NAMES:
            continue
        try:
            impact = abs(float(signal.get("impact") or 0))
        except Exception:
            impact = 0
        if impact >= 5:
            decision.append(signal)
    return decision[:8]


_BACKGROUND_SIGNAL_NAMES = {
    "web_context",
    "web_sentiment",
    "web_probability",
    "league_sentiment",
    "sportybet_detail_available",
    "sportybet_markets_available",
    "sofascore_detail_available",
    "sofascore_statistics_available",
    "source_blend_full_signal_plus_sporty",
    "source_blend_full_signal",
    "source_blend_sportybet_live_signal",
    "source_blend_sportybet_prematch_minimum",
    "source_blend_sportybet_market_signal",
    "data_depth",
    # Meta/modifier signals — these reflect the learner's own adjustments, not
    # match evidence.  Including them in signal stats creates a feedback loop
    # where the learner measures its own impact rather than match predictors.
    "learned_signal_adjustment",
    "calibration_gap_severe",
    "calibration_gap_moderate",
    "sportradar_widget_unavailable",
}


def _market_signal_names(pick_type: str, selection: str, context: dict[str, Any]) -> set[str]:
    base = {
        "prediction_memory",
        "finished_database_memory",
        "close_match_strength_memory",
        "contextual_intelligence",
        "risk_management",
        "ai_brain_review",
    }
    goal = {
        "goal_pressure",
        "goal_environment",
        "poisson_model",
        "dixon_coles_model",
        "odds_progression",
        "odds_pattern",
        "market_steam",
        "live_inplay_state",
        "late_goal_window",
        "red_card_state",
        "sofascore_grade",
        "avg_rating_edge",
        "recent_history_edge",
    }
    side = {
        "ensemble_model",
        "elo_model",
        "poisson_model",
        "dixon_coles_model",
        "league_strength_edge",
        "h2h_edge",
        "recent_history_edge",
        "common_opponent_edge",
        "league_position_edge",
        "odds_edge",
        "market_steam",
        "odds_progression",
        "odds_pattern",
        "market_favorite",
        "venue_form_edge",
        "sofascore_grade",
        "avg_rating_edge",
    }
    value = {
        "consensus_longshot_value",
        "consensus_longshot_market_value",
        "market_favorite",
        "odds_edge",
        "odds_progression",
        "odds_pattern",
        "market_steam",
        "ensemble_model",
    }
    live = {"live_inplay_state", "red_card_state", "late_goal_window"}

    market_intent = context.get("market_intent") if isinstance(context.get("market_intent"), dict) else {}
    family = str(market_intent.get("family") or market_intent.get("market") or "").lower()
    if pick_type in {"goals", "live_goals", "live_total_goals", "live_next_goal", "live_team_to_score"} or "goal" in selection or "btts" in selection or "goals" in family:
        return base | goal | live
    if pick_type in {"value_bet", "market_value", "consensus_longshot_value", "value_overlay"}:
        return base | value | (goal if ("goal" in selection or "over" in selection or "under" in selection) else side)
    if pick_type.startswith("live_"):
        return base | live | side | goal
    return base | side


def _audit_support_signal_names(audit: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    signals = audit.get("signals") if isinstance(audit, dict) else {}
    if not isinstance(signals, dict):
        return names
    for bucket in ("support", "risk"):
        for item in signals.get(bucket) or []:
            name = str((item or {}).get("name") or "")
            if name and name not in _BACKGROUND_SIGNAL_NAMES:
                names.add(name)
    return names


def _signal_mentions_selection(signal: dict[str, Any], selection: str) -> bool:
    if not selection:
        return False
    value = signal.get("value") if isinstance(signal.get("value"), dict) else {}
    signal_selection = str(value.get("selection") or value.get("pick") or "").lower()
    return bool(signal_selection and (signal_selection in selection or selection in signal_selection))


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


def _safe_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _calibration_verdict(gap: float | None) -> str:
    if gap is None:
        return "unknown"
    g = float(gap)
    if g > 0.10:
        return "underconfident"   # model is more accurate than it thinks
    if g < -0.10:
        return "overconfident"    # model claims more than it delivers
    return "well_calibrated"


def _grade_specialists_from_history(graded_rows: list) -> int:
    """
    For every graded prediction that has reasoning_context.analysts,
    credit each available specialist with the outcome.
    """
    try:
        from app.ai_prediction_pipeline import grade_specialist_contributions
    except Exception:
        return 0

    credited = 0
    for row in graded_rows:
        result = row["result"] if "result" in row.keys() else None
        if result not in ("win", "loss"):
            continue
        # reasoning_context lives in audit_json for prediction_history rows
        audit = _safe_json_object(row["audit_json"] if "audit_json" in row.keys() else None)
        reasoning_context = audit.get("reasoning_context") or {}
        # Also check context_json (candidate history)
        if not reasoning_context.get("analysts"):
            ctx = _safe_json_object(row["context_json"] if "context_json" in row.keys() else None)
            reasoning_context = ctx.get("reasoning_context") or reasoning_context
        if not reasoning_context.get("analysts"):
            continue
        league = row["league_name"] if "league_name" in row.keys() else None
        pick_type = row["pick_type"] if "pick_type" in row.keys() else None
        try:
            credited += grade_specialist_contributions(
                reasoning_context, result, league=league, pick_type=pick_type
            )
        except Exception:
            continue
    return credited


def _grade_specialists_from_history(graded_rows: list) -> int:
    """
    For every graded prediction that has reasoning_context.analysts,
    credit each available specialist with the outcome.
    """
    try:
        from app.ai_prediction_pipeline import grade_specialist_contributions
    except Exception:
        return 0

    credited = 0
    for row in graded_rows:
        result = row["result"] if "result" in row.keys() else None
        if result not in ("win", "loss"):
            continue
        audit = _safe_json_object(row["audit_json"] if "audit_json" in row.keys() else None)
        reasoning_context = audit.get("reasoning_context") or {}
        if not reasoning_context.get("analysts"):
            ctx = _safe_json_object(row["context_json"] if "context_json" in row.keys() else None)
            reasoning_context = ctx.get("reasoning_context") or reasoning_context
        if not reasoning_context.get("analysts"):
            continue
        league = row["league_name"] if "league_name" in row.keys() else None
        pick_type = row["pick_type"] if "pick_type" in row.keys() else None
        try:
            credited += grade_specialist_contributions(
                reasoning_context, result, league=league, pick_type=pick_type
            )
        except Exception:
            continue
    return credited


def _incorporate_ai_analysis(conn: sqlite3.Connection, graded_rows: list) -> int:
    """
    Incorporate AI analysis feedback from finished matches into the learning system.
    Reads AI analysis results from MongoDB finished_matches and adjusts signal weights
    and model weights based on AI verdicts on predictions.
    """
    from app.mongo_store import list_finished_matches, is_configured

    if not is_configured():
        return 0

    try:
        finished = list_finished_matches(limit=500)
    except Exception:
        return 0

    if not finished:
        return 0

    updates = 0
    now = datetime.now(timezone.utc).isoformat()

    for doc in finished:
        ai_analysis = doc.get("ai_analysis") or doc.get("ai_analysis_ollama")
        if not ai_analysis:
            continue

        prediction = doc.get("prediction")
        if not prediction:
            continue

        pick = prediction.get("picks", [{}])[0] if prediction.get("picks") else {}
        if not pick:
            continue

        pick_type = pick.get("type") or prediction.get("pick_type") or "unknown"
        selection = pick.get("selection") or pick.get("pick") or ""
        confidence = pick.get("confidence") or prediction.get("confidence") or 50
        result = doc.get("result")

        # AI verdict on the prediction
        ai_recommendation = ai_analysis.get("recommendation") or ai_analysis.get("prediction") or ""
        ai_confidence = ai_analysis.get("confidence") or 50
        ai_verdict = ai_analysis.get("verdict") or ai_recommendation

        # Determine if AI agrees with the prediction
        ai_agrees = False
        if ai_verdict and selection:
            sel_lower = str(selection).lower()
            verdict_lower = str(ai_verdict).lower()
            if sel_lower in ("home", "1", "home win") and verdict_lower in ("home", "1", "home win", "home"):
                ai_agrees = True
            elif sel_lower in ("away", "2", "away win") and verdict_lower in ("away", "2", "away win", "away"):
                ai_agrees = True
            elif sel_lower in ("draw", "x", "draw") and verdict_lower in ("draw", "x", "draw"):
                ai_agrees = True
            elif sel_lower in ("home or draw", "1x") and verdict_lower in ("home", "1", "home win", "draw", "x"):
                ai_agrees = True
            elif sel_lower in ("away or draw", "x2") and verdict_lower in ("away", "2", "away win", "draw", "x"):
                ai_agrees = True

        # Adjust signal weights based on AI analysis
        signals = _decision_signals_for_row({
            "pick_type": pick_type,
            "selection": selection,
            "confidence": confidence,
            "result": result,
            "signals_json": pick.get("signals_json") or "[]",
            "audit_json": pick.get("audit_json") or "{}",
            "context_json": pick.get("context_json") or "{}",
            "league_name": doc.get("tournament") or "",
            "country_name": doc.get("country") or "",
        })

        for sig in signals:
            name = str(sig.get("name") or "")
            if not name:
                continue

            # Boost signals that AI agrees with, suppress those it disagrees with
            adjustment = 0.05 if ai_agrees else -0.03

            try:
                conn.execute("""
                    update signal_weights
                    set weight_adj = weight_adj + ?, last_updated = ?
                    where signal_name = ? and league_key = ?
                """, (adjustment, now, name, _norm_league(doc.get("tournament") or "")))
                updates += 1
            except Exception:
                pass

        # Adjust model weights based on AI confidence vs prediction confidence
        if ai_confidence and confidence:
            ai_conf = float(ai_confidence) / 100 if float(ai_confidence) <= 1 else float(ai_confidence)
            pred_conf = float(confidence) / 100 if float(confidence) <= 1 else float(confidence)
            confidence_gap = ai_conf - pred_conf

            # If AI is more confident than our prediction, boost the model
            # If AI is less confident, suppress the model
            if abs(confidence_gap) > 0.1:
                model_name = "groq" if doc.get("ai_analysis") else "ollama"
                try:
                    conn.execute("""
                        update learned_model_weights
                        set learned_weight = CASE
                            WHEN confidence_gap > 0 THEN LEAST(learned_weight + 0.02, 0.50)
                            ELSE GREATEST(learned_weight - 0.02, 0.05)
                        END,
                        last_updated = ?
                        WHERE model_name = ?
                    """, (now, model_name))
                    updates += 1
                except Exception:
                    pass

    return updates


def _incorporate_user_behavior(conn: sqlite3.Connection, graded_rows: list) -> int:
    """
    Adjust signal weights based on user behavior feedback.

    Scoring per match (net_signal drives the adjustment magnitude):
      accepted            → +1   (user agreed with the model pick)
      user_pick (agrees)  → +3   (user picked same as model = strong agreement)
      user_pick (differs) → -2   (user picked differently = disagreement)
      rejected            → -1   (user dismissed the pick)
      prediction_dismissed→ -0.5 (soft negative)
      bet_placed (agrees) → +2   (user put money on the model pick)
      bet_graded win      → +2   (bet won = model + user were right)
      bet_graded loss     → -1   (bet lost = penalise slightly)

    When user pick == model pick the signals that drove that pick get an
    extra agreement_bonus on top of the normal net_signal adjustment.
    """
    try:
        from app.league_memory import get_user_behavior_summary
    except Exception:
        return 0

    if not graded_rows:
        return 0

    match_ids = list({row["match_id"] for row in graded_rows if row.get("match_id")})
    if not match_ids:
        return 0

    behavior_adjustments = 0
    now = datetime.now(timezone.utc).isoformat()

    for match_id in match_ids[:50]:
        try:
            summary = get_user_behavior_summary(match_id=match_id, days=7)
            if summary["total_interactions"] == 0:
                continue

            pred = conn.execute(
                """
                select pick_type, selection, confidence, signals_json, league_name
                from prediction_history
                where match_id = ?
                order by confidence desc limit 1
                """,
                (match_id,),
            ).fetchone()
            if not pred:
                continue

            pick_type  = pred["pick_type"] or "unknown"
            model_sel  = (pred["selection"] or "").lower().strip()
            signals    = _safe_json(pred["signals_json"])
            league_key = _norm_league(pred["league_name"] or "")
            by_action  = summary["by_action"]

            def _count(action: str) -> int:
                return int((by_action.get(action) or {}).get("count") or 0)

            # ── Resolve user_pick selection ───────────────────────────────────
            user_pick_selections = list(
                (by_action.get("user_pick") or {}).get("selections", {}).keys()
            )
            user_sel = user_pick_selections[0].lower().strip() if user_pick_selections else ""
            user_agrees = bool(model_sel and user_sel and model_sel == user_sel)

            # ── Resolve bet_placed selection ──────────────────────────────────
            bet_selections = list(
                (by_action.get("bet_placed") or {}).get("selections", {}).keys()
            )
            bet_sel = bet_selections[0].lower().strip() if bet_selections else ""
            bet_agrees = bool(model_sel and bet_sel and model_sel == bet_sel)

            # ── Net signal score ──────────────────────────────────────────────
            net = 0.0
            net += _count("accepted") * 1.0
            net += _count("user_pick") * (3.0 if user_agrees else -2.0)
            net += _count("rejected") * -1.0
            net += _count("prediction_dismissed") * -0.5
            net += _count("bet_placed") * (2.0 if bet_agrees else 0.0)
            # bet_graded: check metadata for win/loss
            for action_key in ("bet_graded",):
                graded_data = by_action.get(action_key) or {}
                graded_meta = graded_data.get("selections") or {}
                for sel_key, cnt in graded_meta.items():
                    if "win" in sel_key.lower():
                        net += cnt * 2.0
                    elif "loss" in sel_key.lower():
                        net += cnt * -1.0

            if net == 0.0:
                continue

            # ── Agreement bonus: extra boost when user + model agree ──────────
            # Applied on top of net_signal for the signals that drove the pick.
            # This makes those signals progressively stronger over time.
            agreement_bonus = 0.0
            if user_agrees:
                agreement_bonus += 0.08 * _count("user_pick")
            if bet_agrees:
                agreement_bonus += 0.06 * _count("bet_placed")

            # ── Apply to signal_weights ───────────────────────────────────────
            base_adj   = round(net * 0.04, 4)   # ±4% per unit of net signal
            bonus_adj  = round(agreement_bonus, 4)

            for sig in signals:
                name = str(sig.get("name") or "")
                if not name:
                    continue

                current = conn.execute(
                    """
                    select weight_adj from signal_weights
                    where signal_name = ? and league_key in (?, '__global__')
                    order by case when league_key = ? then 0 else 1 end
                    limit 1
                    """,
                    (name, league_key, league_key),
                ).fetchone()
                cur_adj = float((current or {}).get("weight_adj") or 0.0)
                new_adj = max(-1.0, min(1.0, round(cur_adj + base_adj + bonus_adj, 4)))

                conn.execute("""
                    insert into signal_weights
                        (signal_name, league_key, samples, wins, losses, win_rate, weight_adj, last_updated)
                    values (?, ?, 0, 0, 0, NULL, ?, ?)
                    on conflict(signal_name, league_key) do update set
                        weight_adj   = excluded.weight_adj,
                        last_updated = excluded.last_updated
                """, (name, league_key, new_adj, now))
                behavior_adjustments += 1

                # ── Apply to signal_pick_weights (market-scoped) ──────────────
                current_pt = conn.execute(
                    """
                    select weight_adj from signal_pick_weights
                    where signal_name = ? and league_key = ? and pick_type = ?
                    limit 1
                    """,
                    (name, league_key, pick_type),
                ).fetchone()
                cur_pt = float((current_pt or {}).get("weight_adj") or 0.0)
                new_pt = max(-1.0, min(1.0, round(cur_pt + base_adj + bonus_adj, 4)))

                conn.execute("""
                    insert into signal_pick_weights
                        (signal_name, league_key, pick_type, samples, wins, losses, win_rate, weight_adj, last_updated)
                    values (?, ?, ?, 0, 0, 0, NULL, ?, ?)
                    on conflict(signal_name, league_key, pick_type) do update set
                        weight_adj   = excluded.weight_adj,
                        last_updated = excluded.last_updated
                """, (name, league_key, pick_type, new_pt, now))

        except Exception:
            continue

    return behavior_adjustments
