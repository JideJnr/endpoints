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
from datetime import datetime, timedelta, timezone
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

MIN_SAMPLES = 15          # historical "trust it fully" sample count.
                          # Still used as the shrinkage prior strength below
                          # (see _shrink_win_rate) and by the model-weight /
                          # bias-correction gates further down this file.
MIN_LEAGUE_SAMPLES = 5
MIN_COMBINATION_SAMPLES = 12
MIN_SLIP_RISK_SAMPLES = 12   # graded slips needed in a leg-count/odds band before it's trusted
# How far a band's win rate can fall below the BEST band on the same
# dimension before it's flagged "risky". Relative, not an absolute win-rate
# constant, so this adapts to whatever this system's baseline win rate
# actually is instead of assuming e.g. "under 45%" is always bad.
SLIP_RISK_DEGRADATION_TOLERANCE = 0.12
BLEND_WEIGHT = 0.45       # how much learned accuracy pulls the raw signal weight
                          # 0 = no adjustment, 1 = fully replace with learned rate
DECAY_FACTOR = 0.92       # older data matters less — applied per 30-day window

# Signal weight_adj used to be gated by a flat cliff: below MIN_SAMPLES a
# signal got zero influence, at/above MIN_SAMPLES it got the *raw* win rate
# with full confidence — regardless of whether that was 15 samples or 1500.
# At n=15 a Bernoulli win-rate estimate has a standard error of ~13 points,
# so a short losing streak (pure variance) could swing weight_adj by close
# to its full range and the system would "learn" a lesson from noise.
#
# SIGNAL_ROW_MIN_SAMPLES is now just an existence floor (avoid persisting a
# row from a literal handful of observations); the actual trust-scaling is
# handled continuously by _shrink_win_rate below, so there is no more hard
# cliff at MIN_SAMPLES.
SIGNAL_ROW_MIN_SAMPLES = 3


def _shrink_win_rate(win_rate: float, samples: float, strength: float = MIN_SAMPLES, prior: float = 0.5) -> float:
    """Empirical-Bayes (Beta-prior) shrinkage of a win rate toward `prior`.

    Equivalent to blending in `strength` phantom observations at the prior
    rate before averaging:

        shrunk = (win_rate * samples + prior * strength) / (samples + strength)

    This is a simple, well-known fix for "small-sample win rates are noisy":
    instead of trusting a win rate 100% once it clears a fixed sample-count
    cliff (and 0% below it), the estimate is pulled toward the neutral prior
    by an amount that shrinks smoothly as `samples` grows relative to
    `strength`. With strength == MIN_SAMPLES, a signal with exactly
    MIN_SAMPLES samples is pulled halfway back to neutral; a signal needs
    several multiples of MIN_SAMPLES before the raw win rate dominates.

    Not research-grade (a real James-Stein or Wilson-interval estimator
    would vary the effective strength with the observed variance), but it
    directly fixes the "short losing streak swings the learned weight"
    failure mode with one extra line of arithmetic per bucket.
    """
    if samples <= 0:
        return prior
    return (win_rate * samples + prior * strength) / (samples + strength)


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
    conn.execute("""
        create table if not exists ai_analysis_feedback (
            id INTEGER primary key autoincrement,
            match_id TEXT not null,
            competition_key TEXT not null,
            analysis_correct INTEGER not null default 0,
            analysis_confidence_direction TEXT,
            actual_result TEXT,
            created_at TEXT not null default current_timestamp,
            unique(match_id, competition_key)
        )
    """)
    conn.execute("create index if not exists idx_ai_analysis_feedback_competition on ai_analysis_feedback(competition_key)")
    conn.execute("""
        create table if not exists user_behavior_outcomes (
            id INTEGER primary key autoincrement,
            match_id TEXT not null,
            pick_type TEXT not null,
            user_agreed INTEGER not null default 0,
            result TEXT not null,
            created_at TEXT not null default current_timestamp,
            unique(match_id, pick_type)
        )
    """)
    conn.execute("""
        create table if not exists system_events (
            id INTEGER primary key autoincrement,
            event_type TEXT not null,
            league_key TEXT,
            pick_type TEXT,
            detail_json TEXT,
            created_at TEXT not null default current_timestamp
        )
    """)
    conn.execute("create index if not exists idx_system_events_type_created on system_events(event_type, created_at desc)")
    conn.execute("""
        create table if not exists league_outcome_distribution (
            league_key TEXT primary key,
            home_rate REAL not null default 0.45,
            draw_rate REAL not null default 0.30,
            away_rate REAL not null default 0.25,
            samples INTEGER not null default 0,
            last_updated TEXT not null default current_timestamp
        )
    """)
    conn.execute("""
        create table if not exists context_penalty_adjustments (
            context_tag TEXT not null,
            league_key TEXT not null default '__global__',
            penalty_override REAL,
            samples INTEGER not null default 0,
            win_rate REAL,
            last_updated TEXT not null default current_timestamp,
            primary key (context_tag, league_key)
        )
    """)
    conn.execute("""
        create table if not exists signal_outcomes (
            id INTEGER primary key autoincrement,
            signal_name TEXT not null,
            match_id TEXT not null,
            tournament TEXT,
            country TEXT,
            result TEXT,
            created_at TEXT,
            unique(signal_name, match_id)
        )
    """)
    # AI analysis quality feedback — used to calibrate the LLM model weight (R1)
    conn.execute("""
        create table if not exists ai_analysis_feedback (
            id                          integer primary key autoincrement,
            match_id                    text not null,
            competition_key             text not null,
            analysis_correct            integer not null default 0,
            analysis_confidence_direction text,
            actual_result               text,
            created_at                  text not null default current_timestamp,
            unique (match_id, competition_key)
        )
    """)
    conn.execute("""
        create index if not exists idx_ai_analysis_feedback_competition_key
        on ai_analysis_feedback (competition_key)
    """)
    # User pick-agreement outcomes — used to calibrate user_pick_signal impact (R2)
    conn.execute("""
        create table if not exists user_behavior_outcomes (
            id          integer primary key autoincrement,
            match_id    text not null,
            pick_type   text not null,
            user_agreed integer not null default 0,
            result      text not null,
            created_at  text not null default current_timestamp,
            unique (match_id, pick_type)
        )
    """)
    # System-health events — drift alerts, recovery events, etc. (R4)
    conn.execute("""
        create table if not exists system_events (
            id          integer primary key autoincrement,
            event_type  text not null,
            league_key  text,
            pick_type   text,
            detail_json text,
            created_at  text not null default current_timestamp
        )
    """)
    conn.execute("""
        create index if not exists idx_system_events_type_created
        on system_events (event_type, created_at desc)
    """)
    # Slip-level risk learning: win rate by leg count and by combined odds,
    # from graded bet_builder tickets (betbuilder_history / betbuilder_leg_history).
    # Deliberately separate from learned_thresholds above -- that table learns
    # whether ONE leg clears its own bar; this one learns whether STACKING
    # legs together (regardless of each leg individually clearing its bar)
    # still wins. A slip can pass every per-leg threshold and still lose
    # because it has too many legs or too long a combined price -- that's a
    # property of the combination, not any single leg, so it needs its own
    # learned signal. dimension is 'leg_count' or 'combined_odds'; band is
    # the bucket label (see _leg_count_band / _combined_odds_band).
    conn.execute("""
        create table if not exists learned_slip_risk (
            dimension    text not null,
            band         text not null,
            band_order   integer not null default 0,
            samples      integer not null default 0,
            wins         integer not null default 0,
            losses       integer not null default 0,
            voids        integer not null default 0,
            win_rate     real,
            last_updated text not null default current_timestamp,
            primary key (dimension, band)
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

    now_ref = datetime.now(timezone.utc)
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
            _tally(signal_stats, (name, "__global__"), result, row=row, now=now_ref)
            _tally(signal_pick_stats, (name, "__global__", pick_type), result, row=row, now=now_ref)
            # League-specific bucket
            if league_key:
                _tally(signal_stats, (name, league_key), result, row=row, now=now_ref)
                _tally(signal_pick_stats, (name, league_key, pick_type), result, row=row, now=now_ref)

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
                "weighted_wins": 0.0,
                "weighted_total": 0.0,
            }
        row_result = row["result"]
        w = _row_weight(row, now_ref)
        league_stats[key]["samples"] += 1
        league_stats[key]["confidence_sum"] += float(row["confidence"] or 0)
        league_stats[key]["weighted_total"] += w
        if row_result == "win":
            league_stats[key]["wins"] += 1
            league_stats[key]["weighted_wins"] += w

    # ── 3. Aggregate per-model accuracy from signals ──────────────────────────
    model_signal_map = {
        "goal_model_family": {"goal_model_family", "dixon_coles_model", "poisson_model"},
        "elo":         {"elo_model"},
        "rules":       {"h2h_edge", "league_position_edge",
                        "recent_history_edge", "common_opponent_edge",
                        "avg_rating_edge", "market_steam", "odds_edge"},
        "openrouter":  {"openrouter_agent", "ai_brain_review"},
        # Matches the "name": "competition_intelligence" signal emitted by
        # enriched_prediction.py::_competition_intelligence_signal, and the
        # matching ensemble weight key in app/models/ensemble.py -- same
        # goal_model_family lesson applies: the signal name here MUST match
        # what the signal layer actually emits, or this model can never
        # earn samples and stays permanently at its base weight.
        "competition_intelligence": {"competition_intelligence"},
    }
    model_stats: dict[str, dict] = {
        m: {"samples": 0, "wins": 0, "weighted_wins": 0.0, "weighted_total": 0.0}
        for m in model_signal_map
    }

    for row in rows:
        signals = _decision_signals_for_row(row)
        signal_names = {str(s.get("name") or "") for s in signals}
        result = row["result"]
        w = _row_weight(row, now_ref)
        for model, model_signals in model_signal_map.items():
            if signal_names & model_signals:
                model_stats[model]["samples"] += 1
                model_stats[model]["weighted_total"] += w
                if result == "win":
                    model_stats[model]["wins"] += 1
                    model_stats[model]["weighted_wins"] += w

    # ── 3b. Direct model accuracy from models_json ──────────────────
    # Uses the actual models stored with each prediction to determine
    # which models passed, rather than inferring from signal names.
    direct_model_stats: dict[str, dict] = {
        m: {"samples": 0, "wins": 0, "weighted_wins": 0.0, "weighted_total": 0.0}
        for m in model_signal_map
    }

    for row in rows:
        models_json = _safe_json(row["models_json"] if "models_json" in row.keys() else "{}", {})
        if not models_json:
            continue
        result = row["result"]
        w = _row_weight(row, now_ref)
        passed = _get_passed_models(models_json, result)
        for model_name in passed:
            if model_name in direct_model_stats:
                direct_model_stats[model_name]["samples"] += 1
                direct_model_stats[model_name]["weighted_total"] += w
                if result == "win":
                    direct_model_stats[model_name]["wins"] += 1
                    direct_model_stats[model_name]["weighted_wins"] += w

    # ── 4. Write everything to DB ─────────────────────────────────────────────
    signal_updates = 0
    league_updates = 0
    model_updates = 0

    with db_conn(timeout=30) as conn:
        _init_learner_tables(conn)
        now = datetime.now(timezone.utc).isoformat()
        # ── Crash-safe write strategy ────────────────────────────────────────
        # Previously this block did DELETE + INSERT, which left the tables empty
        # if a crash, DB lock, or exception occurred mid-cycle.  The prediction
        # pipeline reads these tables on every request, so an interrupted learning
        # cycle would cause the system to treat every signal as neutral (weight=0)
        # until the next successful full cycle.
        #
        # Fix: every insert below uses ON CONFLICT DO UPDATE (already the case),
        # so we never need to pre-delete.  Stale rows — signals that no longer
        # appear in the current graded history — are pruned *after* all writes
        # succeed, not before, using a NOT IN (...) delete that only runs when
        # the new data is already safely in place.

        # Signal weights
        for (signal_name, league_key), stats in signal_stats.items():
            samples = stats["samples"]
            wins = stats["wins"]
            losses = stats["losses"]
            if samples < SIGNAL_ROW_MIN_SAMPLES:
                continue
            effective_n = stats.get("weighted_total", samples)
            win_rate = stats.get("weighted_wins", wins) / effective_n
            # weight_adj: positive = boost this signal, negative = suppress it
            # Neutral is 0.50 win rate → adj = 0.
            # win_rate is shrunk toward 0.50 based on effective sample size
            # first (see _shrink_win_rate) so a handful of graded picks can't
            # swing a signal's weight as hard as thousands of them would.
            shrunk_win_rate = _shrink_win_rate(win_rate, effective_n)
            weight_adj = round((shrunk_win_rate - 0.50) * 2.0, 3)  # range ≈ -1.0 to +1.0
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
            if samples < SIGNAL_ROW_MIN_SAMPLES:
                continue
            effective_n = stats.get("weighted_total", samples)
            win_rate = stats.get("weighted_wins", wins) / effective_n
            # Same variance-aware shrinkage as the global signal_weights loop above.
            shrunk_win_rate = _shrink_win_rate(win_rate, effective_n)
            weight_adj = round((shrunk_win_rate - 0.50) * 2.0, 3)
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

        signal_updates += _populate_country_signal_weights(conn, rows, now_ref)

        # League accuracy
        for (league_key, pick_type), stats in league_stats.items():
            samples = stats["samples"]
            wins = stats["wins"]
            if samples < 5:
                continue
            weighted_total = stats.get("weighted_total", 0.0)
            win_rate = (
                stats["weighted_wins"] / weighted_total
                if weighted_total > 0
                else (wins / samples if samples > 0 else 0.0)
            )
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
        #
        # "goal_model_family" (not separate "dixon_coles"/"poisson" keys):
        # those two models are almost always reported as one combined
        # "goal_model_family" signal (they're correlated goal-scoring models,
        # deliberately not double-counted as two independent votes -- see
        # enriched_prediction.py::_model_signals and app/models/ensemble.py).
        # model_signal_map above already tracks accuracy under the
        # "goal_model_family" key to match; this dict previously used
        # separate "dixon_coles"/"poisson" keys that model_stats/
        # direct_model_stats never actually populated (confirmed: 0 of 376
        # graded predictions), so neither model could ever clear MIN_SAMPLES
        # and both silently got weight=0 in the ensemble blend forever.
        # 0.30 + 0.15 preserves the same total default weight as before.
        base_weights = {
            # Keep in sync with app/models/ensemble.py's _BASE_WEIGHTS --
            # see the comment there for why these five specific numbers.
            "goal_model_family": 0.414, "elo": 0.23,
            "rules": 0.184, "openrouter": 0.092,
            "competition_intelligence": 0.08,
        }
        for model in base_weights:
            direct = direct_model_stats.get(model, {"samples": 0, "wins": 0, "weighted_wins": 0.0, "weighted_total": 0.0})
            heuristic = model_stats.get(model, {"samples": 0, "wins": 0, "weighted_wins": 0.0, "weighted_total": 0.0})
            # Use direct data when we have enough samples, else fall back
            use_direct = direct["samples"] >= MIN_SAMPLES
            stats = direct if use_direct else heuristic
            samples = stats["samples"]
            wins = stats["wins"]
            if samples < MIN_SAMPLES:
                continue
            weighted_total = stats.get("weighted_total", 0.0)
            win_rate = (
                stats["weighted_wins"] / weighted_total
                if weighted_total > 0
                else (wins / samples if samples > 0 else 0.0)
            )
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
        try:
            from app.monitoring.learned_parameters import clear_learned_parameter_cache
            clear_learned_parameter_cache()
        except Exception:
            pass

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
        slip_risk_updates = _learn_slip_risk(conn)
        outcome_distribution_updates = _populate_league_outcome_distribution(conn, rows)
        context_penalty_updates = _learn_context_penalties(conn, rows)
        drift_events = _detect_and_handle_drift(conn, rows)
        signal_outcome_backfills = _backfill_signal_outcomes(conn, rows)

        # ── Prune stale rows after all writes succeed ────────────────────────
        # Any signal/league/model that was in the DB from a previous cycle but
        # no longer appears in the current graded history (e.g. a retired signal
        # name, a league with no recent graded picks) should be removed — but
        # only NOW, when the fresh data is safely committed above.
        if signal_stats:
            active_sw = list({(sn, lk) for sn, lk in signal_stats})
            # SQLite doesn't support multi-column NOT IN cleanly with large sets,
            # so delete by last_updated timestamp: any row not touched this cycle
            # is stale.  `now` was set at the start of this connection block so
            # every row written above carries exactly that timestamp.
            conn.execute(
                "DELETE FROM signal_weights WHERE last_updated != ?", (now,)
            )
            conn.execute(
                "DELETE FROM signal_pick_weights WHERE last_updated != ?", (now,)
            )
        if league_stats:
            conn.execute(
                "DELETE FROM league_accuracy WHERE last_updated != ?", (now,)
            )
        if direct_model_stats:
            conn.execute(
                "DELETE FROM learned_model_weights WHERE last_updated != ?", (now,)
            )

        conn.commit()
        try:
            from app.monitoring.learned_parameters import clear_learned_parameter_cache
            clear_learned_parameter_cache()
        except Exception:
            pass

    # -- 10. Risk-control rebuild -----------------------------------------------
    risk_control_result: dict[str, Any] = {}
    try:
        from app.risk.risk_learner import rebuild_risk_controls
        risk_control_result = rebuild_risk_controls()
    except Exception:
        pass
    risk_buckets = risk_control_result.get("buckets_written", 0)

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
        f"{slip_risk_updates} slip-risk bands | "
        f"{pref_updates} tournament preferences | "
        f"{drift_events} drift events | "
        f"{risk_buckets} risk-control buckets"
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
        "slip_risk_updates": slip_risk_updates,
        "tournament_preference_updates": pref_updates,
        "drift_events": drift_events,
        "league_outcome_distribution_updates": outcome_distribution_updates,
        "context_penalty_updates": context_penalty_updates,
        "signal_outcome_backfills": signal_outcome_backfills,
        "risk_control_buckets": risk_buckets,
    }


# ── Read-back helpers used by prediction pipeline ─────────────────────────────

def get_signal_weights(
    league: str | None = None,
    pick_type: str | None = None,
    country: str | None = None,
) -> dict[str, float]:
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
        country_key = _norm_league(country or "")
        fallback_keys = [league_key]
        if country_key and country_key != league_key:
            fallback_keys.append(country_key)
        fallback_keys.append("__global__")
        placeholders = ",".join("?" * len(fallback_keys))
        if pick_type:
            rows = conn.execute(f"""
                select signal_name, weight_adj, league_key, samples
                from signal_pick_weights
                where league_key in ({placeholders})
                  and pick_type = ?
                  and samples >= ?
                order by case league_key
                    when ? then 0
                    when ? then 1
                    else 2
                end
            """, (*fallback_keys, pick_type, MIN_SAMPLES, league_key, country_key)).fetchall()
        else:
            rows = []
        if not rows:
            rows = conn.execute(f"""
                select signal_name, weight_adj, league_key, samples
                from signal_weights
                where league_key in ({placeholders})
                  and samples >= ?
                order by case league_key
                    when ? then 0
                    when ? then 1
                    else 2
                end
            """, (*fallback_keys, MIN_SAMPLES, league_key, country_key)).fetchall()

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
    Requires enough samples before writing a row.
    """
    from app.signal_combinations import build_signal_combination  # noqa: PLC0415

    stats: dict[tuple, dict] = {}
    for row in rows:
        signals = _safe_json(row["signals_json"], [])
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
        if s["samples"] < MIN_COMBINATION_SAMPLES:
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


def _populate_country_signal_weights(conn: sqlite3.Connection, rows: list, now: datetime) -> int:
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        country_key = _norm_league(row["country_name"] or "")
        if not country_key:
            continue
        for signal in _decision_signals_for_row(row):
            name = str(signal.get("name") or "")
            if not name:
                continue
            _tally(stats, (name, country_key), row["result"], row=row, now=now)
    written = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for (signal_name, country_key), bucket in stats.items():
        samples = int(bucket["samples"])
        if samples < MIN_LEAGUE_SAMPLES:
            continue
        wins = int(bucket["wins"])
        losses = int(bucket["losses"])
        effective_n = bucket.get("weighted_total", samples)
        win_rate = bucket.get("weighted_wins", wins) / effective_n
        # Same variance-aware shrinkage as the global signal_weights loop in
        # run_learning_cycle (see _shrink_win_rate).
        shrunk_win_rate = _shrink_win_rate(win_rate, effective_n)
        weight_adj = round((shrunk_win_rate - 0.50) * 2.0, 3)
        conn.execute("""
            insert into signal_weights
                (signal_name, league_key, samples, wins, losses, win_rate, weight_adj, last_updated)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(signal_name, league_key) do update set
                samples = excluded.samples,
                wins = excluded.wins,
                losses = excluded.losses,
                win_rate = excluded.win_rate,
                weight_adj = excluded.weight_adj,
                last_updated = excluded.last_updated
        """, (signal_name, country_key, samples, wins, losses, round(win_rate, 4), weight_adj, now_iso))
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
            loss_rate = losses / samples
            multiplier_floor = _dynamic_bias_multiplier_floor(loss_rate)
            multiplier = max(multiplier_floor, round(1.0 - min(0.28, overconfidence * 0.9), 4))
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


def _learn_slip_risk(conn: sqlite3.Connection) -> int:
    """Learn win rate by leg count and by combined odds from graded
    bet-builder tickets (betbuilder_history / betbuilder_leg_history).

    This is the slip-level counterpart to _learn_thresholds above:
    _learn_thresholds learns whether ONE leg clears its own confidence bar.
    This function learns whether STACKING legs together -- regardless of
    whether each leg individually cleared its bar -- still wins. A slip can
    pass every per-leg threshold and still lose because it carries too many
    legs or too long a combined price; that is a property of the
    combination, not of any single leg, so it needs its own learned signal.
    (This gap is exactly what the real SportyBet ticket review that led to
    this feature surfaced: combination-level risk, not per-leg pick
    quality, was the dominant driver of real losses.)

    Requires MIN_SLIP_RISK_SAMPLES graded slips in a band before writing a
    row for it, same "don't learn from noise" discipline as the rest of
    this module.
    """
    from app.storage.league_memory._helpers import _combined_odds_band, _leg_count_band, LEG_COUNT_BAND_ORDER, COMBINED_ODDS_BAND_ORDER

    try:
        rows = conn.execute("""
            select h.id, h.combined_odds, h.result,
                   (select count(*) from betbuilder_leg_history l where l.bet_id = h.id) as leg_count
            from betbuilder_history h
            where h.result in ('win', 'loss', 'void')
        """).fetchall()
    except sqlite3.OperationalError:
        # betbuilder_history doesn't exist yet in this environment.
        return 0

    if not rows:
        return 0

    def _bump(stats: dict, band: str, result: str) -> None:
        bucket = stats.setdefault(band, {"samples": 0, "wins": 0, "losses": 0, "voids": 0})
        bucket["samples"] += 1
        if result == "win":
            bucket["wins"] += 1
        elif result == "loss":
            bucket["losses"] += 1
        else:
            bucket["voids"] += 1

    leg_stats: dict[str, dict[str, int]] = {}
    odds_stats: dict[str, dict[str, int]] = {}
    for row in rows:
        leg_count = int(row["leg_count"] or 0)
        if leg_count <= 0:
            continue
        result = row["result"]
        _bump(leg_stats, _leg_count_band(leg_count), result)
        _bump(odds_stats, _combined_odds_band(row["combined_odds"]), result)

    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for dimension, stats, order in (
        ("leg_count", leg_stats, LEG_COUNT_BAND_ORDER),
        ("combined_odds", odds_stats, COMBINED_ODDS_BAND_ORDER),
    ):
        for band, bucket in stats.items():
            samples = bucket["samples"]
            if samples < MIN_SLIP_RISK_SAMPLES:
                continue
            decided = bucket["wins"] + bucket["losses"]
            win_rate = round(bucket["wins"] / decided, 4) if decided > 0 else None
            band_order = order.index(band) if band in order else len(order)
            conn.execute("""
                insert into learned_slip_risk
                    (dimension, band, band_order, samples, wins, losses, voids, win_rate, last_updated)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(dimension, band) do update set
                    band_order   = excluded.band_order,
                    samples      = excluded.samples,
                    wins         = excluded.wins,
                    losses       = excluded.losses,
                    voids        = excluded.voids,
                    win_rate     = excluded.win_rate,
                    last_updated = excluded.last_updated
            """, (dimension, band, band_order, samples,
                  bucket["wins"], bucket["losses"], bucket["voids"], win_rate, now))
            written += 1
    return written


def _decay_weight(created_at: str, now: datetime) -> float:
    try:
        row_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if row_dt.tzinfo is None:
            row_dt = row_dt.replace(tzinfo=timezone.utc)
        age_days = max(0, (now - row_dt.astimezone(timezone.utc)).days)
        return DECAY_FACTOR ** (age_days / 7.0)
    except Exception:
        return 1.0


def _row_weight(row: Any, now: datetime) -> float:
    confidence = 50.0
    try:
        value = row["confidence"] if hasattr(row, "keys") and "confidence" in row.keys() else row.get("confidence")
        confidence = float(value if value is not None else 50.0)
    except Exception:
        confidence = 50.0
    confidence_weight = max(0.0, min(1.0, confidence / 100.0))
    try:
        created_at = row["created_at"] if hasattr(row, "keys") and "created_at" in row.keys() else row.get("created_at")
    except Exception:
        created_at = None
    return _decay_weight(str(created_at or ""), now) * confidence_weight


def _dynamic_bias_multiplier_floor(loss_rate: float) -> float:
    return max(0.72, 1.0 - (loss_rate - 0.50) * 1.4)


def _priority_for_win_rate(samples: int, win_rate: float) -> int:
    if samples >= 10 and win_rate >= 0.60:
        return 0
    if samples >= 5 and win_rate >= 0.55:
        return 1
    if samples >= 5 and win_rate >= 0.50:
        return 2
    if samples >= 5 and win_rate >= 0.45:
        return 3
    if samples < 5:
        return 4
    if win_rate >= 0.40:
        return 5
    if win_rate >= 0.35:
        return 6
    return 7


def _selection_side(selection: Any) -> str:
    text = str(selection or "").lower()
    if "draw" in text or text in {"x", "tie"}:
        return "draw"
    if "away" in text or text in {"2", "away_win"}:
        return "away"
    if "home" in text or text in {"1", "home_win"}:
        return "home"
    return ""


def _tally(stats: dict, key: tuple, result: str, row: Any | None = None, now: datetime | None = None) -> None:
    if key not in stats:
        stats[key] = {"samples": 0, "wins": 0, "losses": 0, "weighted_wins": 0.0, "weighted_total": 0.0}
    weight = _row_weight(row, now or datetime.now(timezone.utc)) if row is not None else 1.0
    stats[key]["samples"] += 1
    stats[key]["weighted_total"] += weight
    if result == "win":
        stats[key]["wins"] += 1
        stats[key]["weighted_wins"] += weight
    else:
        stats[key]["losses"] += 1


def _decision_signals_for_row(row: sqlite3.Row) -> list[dict[str, Any]]:
    """Return only signals that plausibly drove this row's market decision.

    Prediction rows keep many background signals for auditability. Learning from
    all of them blurs attribution, so this filter trains weights on market-
    relevant evidence first and falls back to strong support signals only when
    the pick type is unknown.
    """
    signals = _safe_json(row["signals_json"], [])
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
        "goal_model_family",
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
        "goal_model_family",
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


def get_learned_slip_risk() -> dict[str, Any]:
    """Return the learned slip-level (multi-leg) risk signal.

    Two independent dimensions are reported, each as a list of bands
    ordered from least to most risky:
      - "leg_count": win rate by how many legs the slip has
      - "combined_odds": win rate by the slip's multiplied-out combined price

    For each dimension, `risky_from_band` names the first band (walking in
    risk order) whose win rate has fallen SLIP_RISK_DEGRADATION_TOLERANCE or
    more below that dimension's best-performing band. That band and every
    riskier one after it should be treated as learned-risky. This is
    relative to this system's own observed best band rather than a fixed
    win-rate constant, so it adapts to whatever baseline win rate this
    dataset actually has instead of assuming a number that might not fit it.

    Returns {"known": False, ...} until enough slips have been graded in at
    least one band -- callers should keep their existing (non-learned)
    behaviour in that case rather than acting on too little data.
    """
    _init_db()
    with db_conn(timeout=30) as conn:
        _init_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            select dimension, band, band_order, samples, wins, losses, voids, win_rate
            from learned_slip_risk
            order by dimension, band_order
        """).fetchall()

    empty_dim = {"bands": [], "best_band_win_rate": None, "risky_from_band": None}
    if not rows:
        return {"known": False, "leg_count": dict(empty_dim), "combined_odds": dict(empty_dim)}

    by_dim: dict[str, list[dict[str, Any]]] = {"leg_count": [], "combined_odds": []}
    for row in rows:
        dim = row["dimension"]
        if dim not in by_dim:
            continue
        by_dim[dim].append({
            "band":        row["band"],
            "band_order":  row["band_order"],
            "samples":     row["samples"],
            "wins":        row["wins"],
            "losses":      row["losses"],
            "voids":       row["voids"],
            "win_rate":    float(row["win_rate"]) if row["win_rate"] is not None else None,
        })

    result: dict[str, Any] = {"known": True}
    for dim, entries in by_dim.items():
        best_rate = max((e["win_rate"] for e in entries if e["win_rate"] is not None), default=None)
        risky_from: str | None = None
        if best_rate is not None:
            for e in entries:
                if e["win_rate"] is not None and e["win_rate"] <= best_rate - SLIP_RISK_DEGRADATION_TOLERANCE:
                    risky_from = e["band"]
                    break
        result[dim] = {
            "bands": entries,
            "best_band_win_rate": round(best_rate, 4) if best_rate is not None else None,
            "risky_from_band": risky_from,
        }
    return result


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


def _json_dumps(value: Any) -> str:
    try:
        import json as _json
        return _json.dumps(value, sort_keys=True)
    except Exception:
        return "{}"


def _incorporate_ai_analysis(conn: sqlite3.Connection, rows: list) -> int:
    """
    Compare AI competition-analysis predictions against actual outcomes and
    write results to `ai_analysis_feedback`.  When enough rows accumulate,
    update the 'llm' row in `learned_model_weights` using the standard blend
    formula.

    Returns the count of rows inserted into `ai_analysis_feedback`.
    Requirements: R1.1–R1.7
    """
    import json as _json
    import logging

    log = logging.getLogger(__name__)
    upserted = 0
    now = datetime.now(timezone.utc).isoformat()

    for row in rows:
        competition_key = _norm_league(row["league_name"] or "")
        if not competition_key:
            continue

        # Extract round_name from audit_json if available (informational only)
        audit = _safe_json_object(row["audit_json"] if "audit_json" in row.keys() else None)

        # Query the most recent competition_analysis row within 30 days
        try:
            analysis_row = conn.execute(
                """
                SELECT analysis_text
                FROM competition_analysis
                WHERE competition_key = ?
                  AND datetime(created_at) >= datetime('now', '-30 days')
                ORDER BY datetime(created_at) DESC
                LIMIT 1
                """,
                (competition_key,),
            ).fetchone()
        except sqlite3.OperationalError:
            # competition_analysis table doesn't exist yet
            return 0

        if not analysis_row:
            continue

        # Parse analysis_text to extract top_table
        try:
            analysis_data = _json.loads(analysis_row[0] or "{}")
        except Exception:
            continue

        top_table = analysis_data.get("top_table") or []
        if not top_table:
            continue

        # Derive the AI's confidence direction from rank-1 team
        try:
            top_entry = top_table[0]
            top_team_name = str(top_entry.get("team") or "").lower()
        except (IndexError, AttributeError):
            continue

        # Determine home / away team names from the row's audit or signals
        home_name = str(audit.get("home_team") or audit.get("home") or "").lower()
        away_name = str(audit.get("away_team") or audit.get("away") or "").lower()

        if home_name and (home_name in top_team_name or top_team_name in home_name):
            analysis_confidence_direction = "home"
        elif away_name and (away_name in top_team_name or top_team_name in away_name):
            analysis_confidence_direction = "away"
        else:
            # Can't determine direction — skip
            continue

        # Compare direction against actual selection/result
        selection = str(row["selection"] or "").lower()
        actual_result = str(row["result"] or "")

        # analysis_correct = 1 when the AI direction matches the winning selection
        analysis_correct = 1 if (
            actual_result == "win"
            and analysis_confidence_direction in selection
        ) else 0

        match_id = str(row["match_id"] or "")

        try:
            cursor = conn.execute(
                """
                INSERT INTO ai_analysis_feedback
                    (match_id, competition_key, analysis_correct,
                     analysis_confidence_direction, actual_result, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(match_id, competition_key) DO NOTHING
                """,
                (
                    match_id,
                    competition_key,
                    analysis_correct,
                    analysis_confidence_direction,
                    actual_result,
                    now,
                ),
            )
            upserted += cursor.rowcount
        except Exception:
            log.debug(
                "[self_learner] ai_analysis_feedback upsert failed for match_id=%s", match_id
            )
            continue

    # When enough feedback rows exist, update the LLM model weight
    try:
        stats = conn.execute(
            "SELECT COUNT(*) as cnt, SUM(analysis_correct) as correct FROM ai_analysis_feedback"
        ).fetchone()
        total_cnt = stats[0] if stats else 0
        correct_cnt = stats[1] if stats and stats[1] is not None else 0

        if total_cnt >= 10:
            ai_win_rate = correct_cnt / total_cnt
            base = 0.10  # base weight for 'llm' (matches _BASE_WEIGHTS R9)
            performance_factor = 0.5 + (ai_win_rate - 0.50) * 2.0
            learned = round(base * (1 - BLEND_WEIGHT) + base * performance_factor * BLEND_WEIGHT, 4)
            learned = max(0.05, min(0.50, learned))
            conn.execute(
                """
                INSERT INTO learned_model_weights
                    (model_name, base_weight, learned_weight, samples, win_rate, last_updated)
                VALUES ('llm', ?, ?, ?, ?, ?)
                ON CONFLICT(model_name) DO UPDATE SET
                    base_weight    = excluded.base_weight,
                    learned_weight = excluded.learned_weight,
                    samples        = excluded.samples,
                    win_rate       = excluded.win_rate,
                    last_updated   = excluded.last_updated
                """,
                (base, learned, total_cnt, round(ai_win_rate, 4), now),
            )
    except Exception:
        log.debug("[self_learner] failed to update llm model weight from ai_analysis_feedback")

    return upserted


def _incorporate_user_behavior(conn: sqlite3.Connection, rows: list) -> int:
    """Incorporate explicit user-pick signals into calibration memory."""
    written = 0
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        for signal in _safe_json(row["signals_json"], []):
            if signal.get("name") != "user_pick_signal":
                continue
            try:
                impact = float(signal.get("impact") or signal.get("value") or 0)
            except Exception:
                impact = 0.0
            cur = conn.execute("""
                insert into user_behavior_outcomes
                    (match_id, pick_type, user_agreed, result, created_at)
                values (?, ?, ?, ?, ?)
                on conflict(match_id, pick_type) do nothing
            """, (
                str(row["match_id"] or ""),
                str(row["pick_type"] or "unknown"),
                1 if impact > 0 else 0,
                str(row["result"] or ""),
                now,
            ))
            written += int(cur.rowcount > 0)
    for agreed, model_name in ((1, "user_behavior_calibration"), (0, "user_behavior_disagree_calibration")):
        stats = conn.execute("""
            select count(*) as samples,
                   sum(case when result = 'win' then 1 else 0 end) as wins
            from user_behavior_outcomes
            where user_agreed = ?
        """, (agreed,)).fetchone()
        samples = int(stats[0] or 0)
        wins = int(stats[1] or 0)
        if samples < 15:
            continue
        win_rate = wins / samples
        learned = round((win_rate - 0.5) * 8, 1) if agreed else round((0.5 - win_rate) * 4, 1)
        learned = max(0, min(6, learned)) if agreed else max(-4, min(0, learned))
        conn.execute("""
            insert into learned_model_weights
                (model_name, base_weight, learned_weight, samples, win_rate, last_updated)
            values (?, 0, ?, ?, ?, ?)
            on conflict(model_name) do update set
                learned_weight = excluded.learned_weight,
                samples = excluded.samples,
                win_rate = excluded.win_rate,
                last_updated = excluded.last_updated
        """, (model_name, learned, samples, round(win_rate, 4), now))
    return written


def _populate_league_outcome_distribution(conn: sqlite3.Connection, rows: list) -> int:
    buckets: dict[str, dict[str, int]] = {}
    for row in rows:
        if str(row["pick_type"] or "") != "match_result":
            continue
        league_key = _norm_league(row["league_name"] or "")
        side = _selection_side(row["selection"])
        if not league_key or side not in {"home", "draw", "away"}:
            continue
        bucket = buckets.setdefault(league_key, {"home": 0, "draw": 0, "away": 0, "samples": 0})
        bucket[side] += 1
        bucket["samples"] += 1
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for league_key, bucket in buckets.items():
        samples = bucket["samples"]
        if samples < 20:
            continue
        conn.execute("""
            insert into league_outcome_distribution
                (league_key, home_rate, draw_rate, away_rate, samples, last_updated)
            values (?, ?, ?, ?, ?, ?)
            on conflict(league_key) do update set
                home_rate = excluded.home_rate,
                draw_rate = excluded.draw_rate,
                away_rate = excluded.away_rate,
                samples = excluded.samples,
                last_updated = excluded.last_updated
        """, (
            league_key,
            round(bucket["home"] / samples, 4),
            round(bucket["draw"] / samples, 4),
            round(bucket["away"] / samples, 4),
            samples,
            now,
        ))
        written += 1
    return written


def _learn_context_penalties(conn: sqlite3.Connection, rows: list) -> int:
    buckets: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        context = _safe_json_object(row["context_json"] if "context_json" in row.keys() else None)
        match_context = context.get("match_context") if isinstance(context, dict) else {}
        tags = match_context.get("tags") if isinstance(match_context, dict) else []
        if not isinstance(tags, list):
            continue
        league_key = _norm_league(row["league_name"] or "") or "__global__"
        for tag in tags:
            key = (str(tag), league_key)
            bucket = buckets.setdefault(key, {"samples": 0, "wins": 0})
            bucket["samples"] += 1
            if row["result"] == "win":
                bucket["wins"] += 1
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for (tag, league_key), bucket in buckets.items():
        samples = bucket["samples"]
        if samples < 10:
            continue
        win_rate = bucket["wins"] / samples
        override = max(-10, min(4, round((0.5 - win_rate) * 12, 1)))
        conn.execute("""
            insert into context_penalty_adjustments
                (context_tag, league_key, penalty_override, samples, win_rate, last_updated)
            values (?, ?, ?, ?, ?, ?)
            on conflict(context_tag, league_key) do update set
                penalty_override = excluded.penalty_override,
                samples = excluded.samples,
                win_rate = excluded.win_rate,
                last_updated = excluded.last_updated
        """, (tag, league_key, override, samples, round(win_rate, 4), now))
        written += 1
    return written


def _detect_and_handle_drift(conn: sqlite3.Connection, rows: list) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    buckets: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        try:
            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if created.astimezone(timezone.utc) < cutoff:
            continue
        league_key = _norm_league(row["league_name"] or "")
        pick_type = str(row["pick_type"] or "unknown")
        if not league_key:
            continue
        bucket = buckets.setdefault((league_key, pick_type), {"samples": 0, "wins": 0})
        bucket["samples"] += 1
        if row["result"] == "win":
            bucket["wins"] += 1
    now = datetime.now(timezone.utc).isoformat()
    events = 0
    for (league_key, pick_type), bucket in buckets.items():
        samples = bucket["samples"]
        if samples < 10:
            continue
        win_rate = bucket["wins"] / samples
        current = conn.execute("select priority from tournament_preferences where league_key = ?", (league_key,)).fetchone()
        priority = int(current[0]) if current else 4
        if win_rate < 0.40:
            conn.execute("""
                insert into tournament_preferences (league_key, priority, samples, win_rate, last_updated)
                values (?, 7, ?, ?, ?)
                on conflict(league_key) do update set
                    priority     = 7,
                    last_updated = excluded.last_updated
            """, (league_key, samples, round(win_rate, 4), now))
            detail = {"win_rate": round(win_rate, 4), "samples": samples, "days_window": 7, "action": "priority_set_to_7"}
            conn.execute("""
                insert into system_events(event_type, league_key, pick_type, detail_json, created_at)
                values ('drift_detected', ?, ?, ?, ?)
            """, (league_key, pick_type, _json_dumps(detail), now))
            events += 1
        elif priority == 7 and win_rate >= 0.45:
            new_priority = _priority_for_win_rate(samples, win_rate)
            conn.execute("update tournament_preferences set priority = ?, win_rate = ?, samples = ?, last_updated = ? where league_key = ?", (new_priority, round(win_rate, 4), samples, now, league_key))
            detail = {"win_rate": round(win_rate, 4), "samples": samples, "days_window": 7, "action": f"priority_set_to_{new_priority}"}
            conn.execute("""
                insert into system_events(event_type, league_key, pick_type, detail_json, created_at)
                values ('drift_recovery', ?, ?, ?, ?)
            """, (league_key, pick_type, _json_dumps(detail), now))
            events += 1
    if events:
        try:
            from app.monitoring.learned_parameters import clear_learned_parameter_cache
            clear_learned_parameter_cache()
        except Exception:
            pass
    return events


def _backfill_signal_outcomes(conn: sqlite3.Connection, rows: list) -> int:
    written = 0
    for row in rows:
        match_id = str(row["match_id"] or "")
        if not match_id:
            continue
        exists = conn.execute("select 1 from signal_outcomes where match_id = ? limit 1", (match_id,)).fetchone()
        if exists:
            continue
        for signal in _decision_signals_for_row(row):
            name = str(signal.get("name") or signal.get("signal_name") or "")
            if not name:
                continue
            cur = conn.execute("""
                insert into signal_outcomes
                    (signal_name, match_id, tournament, country, result, created_at)
                values (?, ?, ?, ?, ?, ?)
                on conflict(signal_name, match_id) do nothing
            """, (
                name,
                match_id,
                row["league_name"],
                row["country_name"],
                row["result"],
                row["created_at"],
            ))
            written += int(cur.rowcount > 0)
    return written


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
