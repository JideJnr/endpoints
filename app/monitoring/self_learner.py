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
    from app.monitoring.self_learner import run_learning_cycle, get_signal_weights, get_learned_weights

    # After grading — called automatically by job_grade_predictions
    result = run_learning_cycle()

    # At prediction time — get adjusted signal weights
    weights = get_signal_weights(league="Premier League")

    # Get auto-tuned ensemble model weights
    model_weights = get_learned_weights()
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.storage.db import db_conn
from app.storage.db import _init_db
from app.storage.league_memory._helpers import _get_passed_models

from app.utils.primitives import _safe_json

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
    # Signal combination memory — which signal patterns win/lose per league+pick_type
    conn.execute("""
        create table if not exists signal_combination_memory (
            combination_key text not null,
            league_key      text not null default '__global__',
            pick_type       text not null default '__all__',
            selection       text not null default '__all__',
            samples         integer not null default 0,
            wins            integer not null default 0,
            losses          integer not null default 0,
            win_rate        real,
            avg_confidence  real,
            last_updated    text not null default current_timestamp,
            primary key (combination_key, league_key, pick_type, selection)
        )
    """)
    # Learned thresholds per league + pick_type (replaces hardcoded filter/engine values)
    conn.execute("""
        create table if not exists learned_thresholds (
            league_key          text not null,
            pick_type           text not null,
            min_confidence      real,
            block_loss_rate     real,
            caution_loss_rate   real,
            trust_win_rate      real,
            confidence_cap      real,
            samples             integer not null default 0,
            last_updated        text not null default current_timestamp,
            primary key (league_key, pick_type)
        )
    """)
    # Dynamic tournament preferences for enrichment priority ordering
    conn.execute("""
        create table if not exists tournament_preferences (
            league_key      text not null primary key,
            priority        integer not null default 4,
            samples         integer not null default 0,
            win_rate        real,
            last_updated    text not null default current_timestamp
        )
    """)
    # Outcome bias corrections learned from overconfident losses by side.
    conn.execute("""
        create table if not exists model_bias_corrections (
            bias_key        text not null primary key,
            samples         integer not null default 0,
            wins            integer not null default 0,
            losses          integer not null default 0,
            win_rate        real,
            avg_confidence  real,
            multiplier      real not null default 1.0,
            flagged         integer not null default 0,
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
        conn.execute("delete from model_bias_corrections")

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

        bias_updates = _learn_bias_corrections(conn, rows)

        conn.commit()

        # -- 4. Tournament preferences for enrichment priority ------------------
        pref_result = update_tournament_preferences()

        # ── 5. AI analysis feedback loop ────────────────────────────────
        ai_updates = _incorporate_ai_analysis(conn, rows)

        # ── 6. Incorporate user behavior for self-learning ────────────
        behavior_updates = _incorporate_user_behavior(conn, rows)

        # ── 7. Grade specialist contributions ─────────────────────────
        specialist_updates = _grade_specialists_from_history(rows)


        # -- 8. Signal combination memory -----------------------------------------
        combination_updates = _learn_signal_combinations(conn, rows)

        # -- 9. Learned thresholds ------------------------------------------------
        threshold_updates = _learn_thresholds(conn, rows)

        conn.commit()
        try:
            from app.monitoring.learned_parameters import clear_learned_parameter_cache
            clear_learned_parameter_cache()
        except Exception:
            pass

    pref_updates = pref_result.get("updates", 0) if isinstance(pref_result, dict) else 0
    print(
        f"[self_learner] cycle complete: "
        f"{signal_updates} signal weights | "
        f"{league_updates} league accuracy rows | "
        f"{model_updates} model weights updated | "
        f"{bias_updates} bias corrections | "
        f"{ai_updates} AI analysis adjustments | "
        f"{behavior_updates} behavior adjustments | "
        f"{specialist_updates} specialist credits | "
        f"{combination_updates} combination patterns | "
        f"{threshold_updates} learned thresholds | "
        f"{pref_updates} tournament preferences"
    )
    return {
        "status": "ok",
        "total_graded_predictions": len(rows),
        "signal_updates": signal_updates,
        "league_updates": league_updates,
        "model_weight_updates": model_updates,
        "bias_correction_updates": bias_updates,
        "ai_analysis_adjustments": ai_updates,
        "behavior_adjustments": behavior_updates,
        "specialist_credits": specialist_updates,
        "combination_updates": combination_updates,
        "threshold_updates": threshold_updates,
        "tournament_preference_updates": pref_updates,
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
    Returns an empty dict when no historical weights have been learned yet.
    """
    try:
        from app.monitoring.learned_parameters import get_learned_ensemble_weights
        learned = get_learned_ensemble_weights()
        if learned:
            return learned
    except Exception:
        pass
    _init_db()
    with db_conn(timeout=30) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select model_name, learned_weight from learned_model_weights"
        ).fetchall()

    if not rows:
        return {}

    weights = {
        ("llm" if row["model_name"] == "openrouter" else row["model_name"]): float(row["learned_weight"])
        for row in rows
    }
    # Normalise so weights sum to 1.0
    total = sum(weights.values())
    if total > 0:
        weights = {k: round(v / total, 4) for k, v in weights.items()}
    return weights


def get_bias_corrections() -> dict[str, Any]:
    """
    Return learned outcome multipliers used to correct systemic side bias.

    Multipliers are neutral at 1.0. Values below 1.0 suppress outcomes that
    have been overconfident and loss-heavy in graded history.
    """
    defaults = {
        "home_win_multiplier": 1.0,
        "draw_multiplier": 1.0,
        "away_win_multiplier": 1.0,
        "home_advantage_multiplier": 1.0,
        "flagged": [],
        "source": "neutral_default",
    }
    _init_db()
    with db_conn(timeout=30) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            select bias_key, samples, wins, losses, win_rate,
                   avg_confidence, multiplier, flagged
            from model_bias_corrections
        """).fetchall()
    if not rows:
        return defaults

    result = dict(defaults)
    result["source"] = "learned"
    flagged: list[dict[str, Any]] = []
    for row in rows:
        key = str(row["bias_key"] or "")
        multiplier = float(row["multiplier"] or 1.0)
        if key in {"home_win", "draw", "away_win"}:
            result[f"{key}_multiplier"] = multiplier
        if key == "home_advantage":
            result["home_advantage_multiplier"] = multiplier
        if int(row["flagged"] or 0):
            flagged.append({
                "bias": key,
                "samples": int(row["samples"] or 0),
                "win_rate": round(float(row["win_rate"] or 0), 4),
                "avg_confidence": round(float(row["avg_confidence"] or 0), 2),
                "multiplier": multiplier,
            })
    result["flagged"] = flagged
    return result


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
                "verdict":          _calibration_verdict(float(row["calibration_gap"] or 0)),
            }
            for row in rows
        ],
    }


def update_tournament_preferences() -> dict[str, Any]:
    """
    Recompute tournament_preferences from league_accuracy.

    Aggregates win rates across all pick_types per league, then maps the
    overall accuracy to a priority score used by buffer.py for enrichment
    queue ordering.

    Priority mapping (lower = liked, processed first):
      0 = high accuracy (win_rate >= 60%, samples >= 10)
      1 = good accuracy (win_rate >= 55%, samples >= 5)
      2 = decent (win_rate >= 50%, samples >= 5)
      3 = neutral (win_rate >= 45%, samples >= 5)
      4 = unknown / insufficient data (default)
      5 = below average (win_rate >= 40%, samples >= 5)
      6 = poor (win_rate >= 35%, samples >= 5)
      7 = avoid (win_rate < 35%, samples >= 5)
    """
    _init_db()
    with db_conn(timeout=30) as conn:
        _init_learner_tables(conn)
        now = datetime.now(timezone.utc).isoformat()

        # Aggregate league_accuracy across all pick_types per league
        rows = conn.execute("""
            select league_key, league_name, sum(samples) as total_samples,
                   sum(wins) as total_wins
            from league_accuracy
            where samples >= 1
            group by league_key
        """).fetchall()

        updates = 0
        for row in rows:
            league_key = row["league_key"]
            samples = row["total_samples"] or 0
            wins = row["total_wins"] or 0
            win_rate = wins / samples if samples > 0 else 0.0

            if samples >= 10 and win_rate >= 0.60:
                priority = 0
            elif samples >= 5 and win_rate >= 0.55:
                priority = 1
            elif samples >= 5 and win_rate >= 0.50:
                priority = 2
            elif samples >= 5 and win_rate >= 0.45:
                priority = 3
            elif samples < 5:
                priority = 4
            elif win_rate >= 0.40:
                priority = 5
            elif win_rate >= 0.35:
                priority = 6
            else:
                priority = 7

            conn.execute("""
                insert into tournament_preferences
                    (league_key, priority, samples, win_rate, last_updated)
                values (?, ?, ?, ?, ?)
                on conflict(league_key) do update set
                    priority = excluded.priority,
                    samples = excluded.samples,
                    win_rate = excluded.win_rate,
                    last_updated = excluded.last_updated
            """, (league_key, priority, samples, round(win_rate, 4), now))
            updates += 1

        conn.commit()

    return {"status": "ok", "updates": updates}


def get_tournament_priority(league: str) -> dict[str, Any]:
    """
    Return the dynamic priority for a tournament/league.

    Used by buffer.py to order the enrichment queue.  Falls back to
    priority 4 (default) when no learned data exists yet.
    """
    _init_db()
    league_key = _norm_league(league)
    with db_conn(timeout=30) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            select priority, samples, win_rate, last_updated
            from tournament_preferences
            where league_key = ?
        """, (league_key,)).fetchone()

    if not row:
        return {"league": league, "priority": 4, "known": False}

    return {
        "league": league,
        "priority": row["priority"],
        "samples": row["samples"],
        "win_rate": round(float(row["win_rate"] or 0) * 100, 1),
        "known": True,
        "last_updated": row["last_updated"],
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

# -- Signal combination learning -----------------------------------------

def _learn_signal_combinations(conn: sqlite3.Connection, rows: list) -> int:
    """Learn which signal combinations win/lose per league + pick_type.

    Uses build_signal_combination() from signal_combinations.py to produce
    a stable 16-char key for each prediction's signal set, then tallies
    wins/losses per (key, league_key, pick_type, selection).
    Requires >= 5 samples before writing a row.
    """
    from app.signal_combinations import build_signal_combination  # noqa: PLC0415

    stats: dict[tuple, dict] = {}
    for row in rows:
        signals = _safe_json(row["signals_json"])
        if not signals:
            continue
        league_key = _norm_league(row["league_name"] or "")
        pick_type  = str(row["pick_type"] or "unknown")
        selection  = str(row["selection"] or "unknown")
        result     = row["result"]
        try:
            combo = build_signal_combination(
                signals=signals,
                pick_type=pick_type,
                selection=selection,
            )
            key = combo["key"]
        except Exception:
            continue
        for lk in ("__global__", league_key) if league_key else ("__global__",):
            bucket = (key, lk, pick_type, selection)
            if bucket not in stats:
                stats[bucket] = {"samples": 0, "wins": 0, "losses": 0, "conf_sum": 0.0}
            stats[bucket]["samples"] += 1
            stats[bucket]["conf_sum"] += float(row["confidence"] or 0)
            if result == "win":
                stats[bucket]["wins"] += 1
            else:
                stats[bucket]["losses"] += 1

    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for (ckey, lk, pt, sel), s in stats.items():
        if s["samples"] < 5:
            continue
        wr = s["wins"] / s["samples"]
        avg_conf = s["conf_sum"] / s["samples"]
        conn.execute("""
            insert into signal_combination_memory
                (combination_key, league_key, pick_type, selection,
                 samples, wins, losses, win_rate, avg_confidence, last_updated)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(combination_key, league_key, pick_type, selection) do update set
                samples        = excluded.samples,
                wins           = excluded.wins,
                losses         = excluded.losses,
                win_rate       = excluded.win_rate,
                avg_confidence = excluded.avg_confidence,
                last_updated   = excluded.last_updated
        """, (ckey, lk, pt, sel, s["samples"], s["wins"], s["losses"],
              round(wr, 4), round(avg_conf, 2), now))
        written += 1
    return written


def _learn_bias_corrections(conn: sqlite3.Connection, rows: list) -> int:
    """Flag and correct systemic home/draw/away overconfidence.

    The learner watches settled picks by selected side. If a side has enough
    samples, a weak win rate, and confidence that ran ahead of reality, it
    writes a multiplier below 1.0. Prediction models then apply that multiplier
    to the final 1X2 distribution instead of relying on fixed priors.
    """
    stats: dict[str, dict[str, float]] = {
        "home_win": {"samples": 0, "wins": 0, "losses": 0, "confidence_sum": 0.0},
        "draw": {"samples": 0, "wins": 0, "losses": 0, "confidence_sum": 0.0},
        "away_win": {"samples": 0, "wins": 0, "losses": 0, "confidence_sum": 0.0},
    }
    for row in rows:
        side = _selection_bias_key(str(row["selection"] or row["pick_type"] or ""))
        if not side:
            continue
        bucket = stats[side]
        bucket["samples"] += 1
        bucket["confidence_sum"] += float(row["confidence"] or 0)
        if row["result"] == "win":
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1

    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for key, bucket in stats.items():
        samples = int(bucket["samples"])
        if samples < MIN_SAMPLES:
            continue
        wins = int(bucket["wins"])
        losses = int(bucket["losses"])
        win_rate = wins / samples if samples else 0.0
        avg_conf = bucket["confidence_sum"] / samples if samples else 0.0
        expected = avg_conf / 100 if avg_conf > 1 else avg_conf
        overconfidence = max(0.0, expected - win_rate)
        flagged = int(overconfidence >= 0.08 or (losses / samples) >= 0.58)
        multiplier = 1.0
        if flagged:
            multiplier = max(0.72, round(1.0 - min(0.28, overconfidence * 0.9), 4))
        conn.execute("""
            insert into model_bias_corrections
                (bias_key, samples, wins, losses, win_rate,
                 avg_confidence, multiplier, flagged, last_updated)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(bias_key) do update set
                samples        = excluded.samples,
                wins           = excluded.wins,
                losses         = excluded.losses,
                win_rate       = excluded.win_rate,
                avg_confidence = excluded.avg_confidence,
                multiplier     = excluded.multiplier,
                flagged        = excluded.flagged,
                last_updated   = excluded.last_updated
        """, (key, samples, wins, losses, round(win_rate, 4),
              round(avg_conf, 2), multiplier, flagged, now))
        written += 1

    home = stats["home_win"]
    away = stats["away_win"]
    paired_samples = int(min(home["samples"], away["samples"]))
    if paired_samples >= MIN_SAMPLES:
        home_rate = home["wins"] / home["samples"] if home["samples"] else 0.0
        away_rate = away["wins"] / away["samples"] if away["samples"] else 0.0
        gap = home_rate - away_rate
        flagged = int(gap < -0.08 or (home["losses"] / home["samples"] if home["samples"] else 0.0) >= 0.58)
        multiplier = max(0.80, round(1.0 + min(0.0, gap) * 0.6, 4)) if flagged else 1.0
        conn.execute("""
            insert into model_bias_corrections
                (bias_key, samples, wins, losses, win_rate,
                 avg_confidence, multiplier, flagged, last_updated)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(bias_key) do update set
                samples        = excluded.samples,
                wins           = excluded.wins,
                losses         = excluded.losses,
                win_rate       = excluded.win_rate,
                avg_confidence = excluded.avg_confidence,
                multiplier     = excluded.multiplier,
                flagged        = excluded.flagged,
                last_updated   = excluded.last_updated
        """, (
            "home_advantage", paired_samples, int(home["wins"]), int(home["losses"]),
            round(home_rate, 4), round(home["confidence_sum"] / home["samples"], 2),
            multiplier, flagged, now,
        ))
        written += 1

    return written


def _learn_thresholds(conn: sqlite3.Connection, rows: list) -> int:
    """Derive per-league + pick_type learned thresholds from graded history.

    For each (league_key, pick_type) bucket with >= 20 samples:
      - min_confidence : lowest confidence that still achieved >= 50% win rate
        (Youden-style: scan confidence bands, pick the floor that maximises
         win_rate - loss_rate)
      - block_loss_rate  : observed loss rate when confidence < 60
      - caution_loss_rate: observed loss rate when confidence 60-70
      - trust_win_rate   : observed win rate when confidence >= 75
      - confidence_cap   : 95th-percentile confidence seen in wins
    """
    # Bucket rows by (league_key, pick_type)
    buckets: dict[tuple, list] = {}
    for row in rows:
        lk = _norm_league(row["league_name"] or "")
        pt = str(row["pick_type"] or "unknown")
        for key in (("__global__", pt), (lk, pt) if lk else None):
            if key is None:
                continue
            buckets.setdefault(key, []).append(row)

    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for (lk, pt), bucket_rows in buckets.items():
        if len(bucket_rows) < 20:
            continue

        confs  = [float(r["confidence"] or 0) for r in bucket_rows]
        results = [r["result"] for r in bucket_rows]

        # min_confidence: scan bands [40,45,50,55,60,65,70], pick floor
        # where win_rate - loss_rate is maximised
        best_floor = 50.0
        best_j = -1.0
        for floor in (40, 45, 50, 55, 60, 65, 70):
            above = [(c, r) for c, r in zip(confs, results) if c >= floor]
            if len(above) < 5:
                continue
            n = len(above)
            wr = sum(1 for _, r in above if r == "win") / n
            lr = sum(1 for _, r in above if r == "loss") / n
            j = wr - lr
            if j > best_j:
                best_j = j
                best_floor = float(floor)

        def _band_loss_rate(lo: float, hi: float) -> float | None:
            band = [r for c, r in zip(confs, results) if lo <= c < hi]
            if len(band) < 5:
                return None
            return round(sum(1 for r in band if r == "loss") / len(band), 4)

        def _band_win_rate(lo: float, hi: float = 101.0) -> float | None:
            band = [r for c, r in zip(confs, results) if lo <= c < hi]
            if len(band) < 5:
                return None
            return round(sum(1 for r in band if r == "win") / len(band), 4)

        # 95th-percentile confidence among wins
        win_confs = sorted(c for c, r in zip(confs, results) if r == "win")
        conf_cap: float | None = None
        if win_confs:
            idx = max(0, int(len(win_confs) * 0.95) - 1)
            conf_cap = round(win_confs[idx], 1)

        conn.execute("""
            insert into learned_thresholds
                (league_key, pick_type, min_confidence,
                 block_loss_rate, caution_loss_rate, trust_win_rate,
                 confidence_cap, samples, last_updated)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(league_key, pick_type) do update set
                min_confidence    = excluded.min_confidence,
                block_loss_rate   = excluded.block_loss_rate,
                caution_loss_rate = excluded.caution_loss_rate,
                trust_win_rate    = excluded.trust_win_rate,
                confidence_cap    = excluded.confidence_cap,
                samples           = excluded.samples,
                last_updated      = excluded.last_updated
        """, (
            lk, pt, best_floor,
            _band_loss_rate(0, 60), _band_loss_rate(60, 70), _band_win_rate(75),
            conf_cap, len(bucket_rows), now,
        ))
        written += 1
    return written



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

    audit = _safe_json_object(row["audit_json"] if "audit_json" in row.keys() else None)  # type: ignore[arg-type]
    context = _safe_json_object(row["context_json"] if "context_json" in row.keys() else None)  # type: ignore[arg-type]
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


def _selection_bias_key(selection: str) -> str | None:
    text = selection.lower().strip().replace("_", " ")
    if text in {"home", "1", "home win"} or "home win" in text:
        return "home_win"
    if text in {"draw", "x"} or text == "match draw":
        return "draw"
    if text in {"away", "2", "away win"} or "away win" in text:
        return "away_win"
    return None


def _norm_league(name: str) -> str:
    return name.lower().strip().replace(" ", "_")[:60] if name else ""

def get_signal_combination_performance(
    combination_key: str,
    league: str | None = None,
    pick_type: str | None = None,
) -> dict[str, Any]:
    """Return win-rate stats for a signal combination key.

    Prefers league-specific data; falls back to global.
    Returns {"samples": 0, "win_rate": None} when no data exists.
    """
    _init_db()
    league_key = _norm_league(league or "")
    pt = pick_type or "__all__"
    with db_conn(timeout=30) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            select samples, wins, losses, win_rate, avg_confidence
            from signal_combination_memory
            where combination_key = ?
              and league_key in (?, '__global__')
              and pick_type = ?
            order by case when league_key = ? then 0 else 1 end
            limit 1
        """, (combination_key, league_key, pt, league_key)).fetchone()
    if not row:
        return {"samples": 0, "win_rate": None, "avg_confidence": None}
    return {
        "samples":        row["samples"],
        "wins":           row["wins"],
        "losses":         row["losses"],
        "win_rate":       float(row["win_rate"] or 0),
        "avg_confidence": float(row["avg_confidence"] or 0),
    }


def get_learned_thresholds(
    league: str | None = None,
    pick_type: str | None = None,
) -> dict[str, Any]:
    """Return learned thresholds for a league + pick_type.

    Falls back to global thresholds, then to hardcoded defaults.
    Returned keys: min_confidence, block_loss_rate, caution_loss_rate,
                   trust_win_rate, confidence_cap, samples, source.
    """
    _DEFAULTS: dict[str, Any] = {
        "min_confidence":    50.0,
        "block_loss_rate":   0.75,
        "caution_loss_rate": 0.55,
        "trust_win_rate":    0.65,
        "confidence_cap":    88.0,
        "samples":           0,
        "source":            "hardcoded_default",
    }
    _init_db()
    league_key = _norm_league(league or "")
    pt = pick_type or "__all__"
    with db_conn(timeout=30) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            select min_confidence, block_loss_rate, caution_loss_rate,
                   trust_win_rate, confidence_cap, samples, league_key
            from learned_thresholds
            where league_key in (?, '__global__')
              and pick_type in (?, '__all__')
            order by
                case when league_key = ? then 0 else 1 end,
                case when pick_type  = ? then 0 else 1 end
            limit 1
        """, (league_key, pt, league_key, pt)).fetchone()
    if not row:
        return _DEFAULTS
    source = "league_learned" if row["league_key"] == league_key else "global_learned"
    return {
        "min_confidence":    float(row["min_confidence"]   or _DEFAULTS["min_confidence"]),
        "block_loss_rate":   float(row["block_loss_rate"]  or _DEFAULTS["block_loss_rate"]) if row["block_loss_rate"]  is not None else _DEFAULTS["block_loss_rate"],
        "caution_loss_rate": float(row["caution_loss_rate"] or _DEFAULTS["caution_loss_rate"]) if row["caution_loss_rate"] is not None else _DEFAULTS["caution_loss_rate"],
        "trust_win_rate":    float(row["trust_win_rate"]   or _DEFAULTS["trust_win_rate"])  if row["trust_win_rate"]   is not None else _DEFAULTS["trust_win_rate"],
        "confidence_cap":    float(row["confidence_cap"]   or _DEFAULTS["confidence_cap"])  if row["confidence_cap"]   is not None else _DEFAULTS["confidence_cap"],
        "samples":           int(row["samples"]),
        "source":            source,
    }


def _calibration_verdict(gap: float | None) -> str:
    """Classify a calibration gap into a human-readable verdict."""
    if gap is None:
        return "unknown"
    g = float(gap)
    if g > 0.10:
        return "underconfident"
    if g < -0.10:
        return "overconfident"
    return "well_calibrated"


def _safe_json_object(value: Any) -> dict[str, Any]:
    """Parse a JSON string into a dict; return {} on any failure."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        import json as _json
        result = _json.loads(value)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _incorporate_ai_analysis(conn: sqlite3.Connection, rows: list) -> int:
    """Stub: incorporate AI analysis feedback. Returns count of updates."""
    return 0


def _incorporate_user_behavior(conn: sqlite3.Connection, rows: list) -> int:
    """Stub: incorporate user behavior signals. Returns count of updates."""
    return 0


def _grade_specialists_from_history(rows: list) -> int:
    """Grade specialist contributions from graded prediction history."""
    credited = 0
    try:
        from app.ai.ai_prediction_pipeline import grade_specialist_contributions
        for row in rows:
            audit = _safe_json_object(row["audit_json"] if "audit_json" in row.keys() else None)
            reasoning = audit.get("reasoning_context") if isinstance(audit, dict) else {}
            if not reasoning:
                continue
            result = str(row["result"] or "")
            league = str(row["league_name"] or "")
            pick_type = str(row["pick_type"] or "")
            try:
                credited += grade_specialist_contributions(
                    reasoning, result, league=league, pick_type=pick_type
                )
            except Exception:
                pass
    except Exception:
        pass
    return credited
