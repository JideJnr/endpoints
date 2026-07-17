"""
Odds Pattern Recogniser
-----------------------
Extracts the *shape* of odds movement from odds_snapshots — independent of
team names. The hypothesis: how odds move tells you more than where they start.

Patterns extracted per match:
  - slope          : overall direction (shortened / drifted / stable)
  - velocity       : speed of movement (fast / moderate / slow)
  - reversal       : odds moved one way then reversed (public fade / sharp fade)
  - sharp_timing   : big drop in first 20% of snapshots = early sharp money
  - late_move      : significant move in last 20% = late sharp / insider
  - stability      : std deviation of odds series (low = confident market)
  - implied_prob   : current implied probability

These features are stored in odds_pattern_features and used to:
  1. Look up similar historical patterns and their outcomes
  2. Produce a pattern-based confidence adjustment
  3. Feed into the ensemble as an additional signal

Usage:
    from app.odds_pattern import extract_pattern, pattern_signal

    # Get pattern features for a match
    features = extract_pattern(match_id)

    # Get a prediction signal from pattern similarity
    signal = pattern_signal(match_id)
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.league_memory import DB_PATH, _init_db


# ── Table ─────────────────────────────────────────────────────────────────────

def _init_pattern_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        create table if not exists odds_pattern_features (
            match_id        text not null,
            selection       text not null,   -- 'home', 'draw', 'away', or market key
            snapshots       integer,
            opening_odds    real,
            closing_odds    real,
            min_odds        real,
            max_odds        real,
            slope           real,            -- (closing - opening) / opening
            velocity        real,            -- avg abs change per snapshot
            reversal        integer,         -- 1 if direction changed significantly
            sharp_timing    integer,         -- 1 if big drop in first 20% of snapshots
            late_move       integer,         -- 1 if significant move in last 20%
            stability       real,            -- std deviation of series
            implied_prob    real,            -- 1 / closing_odds
            result          text,            -- 'win' / 'loss' / null (filled after grading)
            match_date      text,
            computed_at     text not null default current_timestamp,
            primary key (match_id, selection)
        )
    """)
    conn.execute("create index if not exists idx_pattern_result on odds_pattern_features(result)")
    conn.execute("create index if not exists idx_pattern_slope  on odds_pattern_features(slope)")


# ── Extract features from a match's odds series ───────────────────────────────

def extract_pattern(match_id: str) -> list[dict[str, Any]]:
    """
    Compute pattern features for all tracked selections of a match.
    Stores results in odds_pattern_features.
    Returns list of feature dicts.
    """
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_pattern_table(conn)
        conn.row_factory = sqlite3.Row

        # 1X2 series
        rows_1x2 = conn.execute("""
            select home_odds, draw_odds, away_odds, snapshot_time, match_date
            from odds_snapshots
            where match_id = ?
            order by snapshot_time asc
        """, (str(match_id),)).fetchall()

        # Per-market series (prefer lean change points when present)
        market_table = "odds_market_snapshots"
        try:
            conn.execute("select 1 from odds_market_changes limit 1")
            has_changes = conn.execute(
                "select 1 from odds_market_changes where match_id = ? limit 1",
                (str(match_id),),
            ).fetchone()
            if has_changes:
                market_table = "odds_market_changes"
        except sqlite3.OperationalError:
            market_table = "odds_market_snapshots"

        market_rows = conn.execute(
            f"""
            select market_name, specifier, selection_name, odds, snapshot_time, match_date
            from {market_table}
            where match_id = ? and odds is not null
            order by market_name, specifier, selection_name, snapshot_time asc
            """,
            (str(match_id),),
        ).fetchall()

    features = []
    match_date = rows_1x2[0]["match_date"] if rows_1x2 else None

    # Extract 1X2 features
    if rows_1x2:
        for sel, col in [("home", "home_odds"), ("draw", "draw_odds"), ("away", "away_odds")]:
            series = [float(r[col]) for r in rows_1x2 if r[col] is not None]
            if len(series) >= 2:
                feat = _compute_features(series, match_id, sel, match_date)
                features.append(feat)

    # Extract per-market features
    grouped: dict[tuple, list[float]] = {}
    for r in market_rows:
        key = (r["market_name"] or "", r["specifier"] or "", r["selection_name"] or "")
        grouped.setdefault(key, []).append(float(r["odds"]))

    for (market, spec, sel_name), series in grouped.items():
        if len(series) >= 2:
            key = f"{market}|{spec}|{sel_name}" if spec else f"{market}|{sel_name}"
            feat = _compute_features(series, match_id, key, match_date)
            features.append(feat)

    # Persist
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_pattern_table(conn)
        for feat in features:
            conn.execute("""
                insert into odds_pattern_features
                    (match_id, selection, snapshots, opening_odds, closing_odds,
                     min_odds, max_odds, slope, velocity, reversal, sharp_timing,
                     late_move, stability, implied_prob, match_date, computed_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
                on conflict(match_id, selection) do update set
                    snapshots    = excluded.snapshots,
                    opening_odds = excluded.opening_odds,
                    closing_odds = excluded.closing_odds,
                    min_odds     = excluded.min_odds,
                    max_odds     = excluded.max_odds,
                    slope        = excluded.slope,
                    velocity     = excluded.velocity,
                    reversal     = excluded.reversal,
                    sharp_timing = excluded.sharp_timing,
                    late_move    = excluded.late_move,
                    stability    = excluded.stability,
                    implied_prob = excluded.implied_prob,
                    match_date   = excluded.match_date,
                    computed_at  = current_timestamp
            """, (
                feat["match_id"], feat["selection"], feat["snapshots"],
                feat["opening_odds"], feat["closing_odds"],
                feat["min_odds"], feat["max_odds"],
                feat["slope"], feat["velocity"],
                feat["reversal"], feat["sharp_timing"],
                feat["late_move"], feat["stability"],
                feat["implied_prob"], feat["match_date"],
            ))
        conn.commit()

    return features


def _compute_features(
    series: list[float],
    match_id: str,
    selection: str,
    match_date: str | None,
) -> dict[str, Any]:
    n = len(series)
    opening = series[0]
    closing = series[-1]
    mn = min(series)
    mx = max(series)

    # Slope: relative change opening → closing
    slope = round((closing - opening) / opening, 4) if opening else 0.0

    # Velocity: mean absolute change per step
    diffs = [abs(series[i + 1] - series[i]) for i in range(n - 1)]
    velocity = round(sum(diffs) / len(diffs), 4) if diffs else 0.0

    # Reversal: did the series go one way then reverse?
    reversal = 0
    if n >= 4:
        mid = n // 2
        first_half_slope = series[mid] - series[0]
        second_half_slope = series[-1] - series[mid]
        # Reversal if first and second halves moved in opposite directions by > 0.05
        if first_half_slope * second_half_slope < 0 and abs(first_half_slope) > 0.05:
            reversal = 1

    # Sharp timing: meaningful percentage drop in first 20% of snapshots
    sharp_timing = 0
    early_end = max(1, n // 5)
    early_drop = series[0] - min(series[:early_end + 1])
    early_drop_pct = early_drop / series[0] if series[0] else 0
    if early_drop_pct >= 0.05:
        sharp_timing = 1

    # Late move: significant percentage change in last 20%
    late_move = 0
    late_start = max(0, n - max(1, n // 5))
    late_change = abs(series[-1] - series[late_start])
    late_change_pct = late_change / series[late_start] if series[late_start] else 0
    if late_change_pct >= 0.05:
        late_move = 1

    # Stability: std deviation
    mean = sum(series) / n
    variance = sum((v - mean) ** 2 for v in series) / n
    stability = round(math.sqrt(variance), 4)

    implied_prob = round(1 / closing, 4) if closing and closing > 0 else None
    opening_implied = (1 / opening) if opening and opening > 0 else None
    implied_change_percent = (
        round(((implied_prob - opening_implied) / opening_implied) * 100, 2)
        if implied_prob is not None and opening_implied
        else None
    )
    odds_change_percent = round(slope * 100, 2)
    if odds_change_percent <= -10:
        market_pull = "strong_backed"
    elif odds_change_percent <= -5:
        market_pull = "backed"
    elif odds_change_percent >= 10:
        market_pull = "strong_faded"
    elif odds_change_percent >= 5:
        market_pull = "faded"
    else:
        market_pull = "stable"

    return {
        "match_id":     match_id,
        "selection":    selection,
        "snapshots":    n,
        "opening_odds": round(opening, 3),
        "closing_odds": round(closing, 3),
        "min_odds":     round(mn, 3),
        "max_odds":     round(mx, 3),
        "slope":        slope,
        "velocity":     velocity,
        "reversal":     reversal,
        "sharp_timing": sharp_timing,
        "late_move":    late_move,
        "stability":    stability,
        "implied_prob": implied_prob,
        "odds_change_percent": odds_change_percent,
        "implied_change_percent": implied_change_percent,
        "market_pull": market_pull,
        "match_date":   match_date,
    }


# ── Grade patterns after results are known ────────────────────────────────────

def grade_patterns_for_date(match_date: str) -> dict[str, Any]:
    """
    After grading runs, attach win/loss results to pattern features.
    Joins odds_pattern_features with prediction_history on match_id.
    """
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_pattern_table(conn)
        # Get graded predictions for this date
        graded = conn.execute("""
            select match_id, pick_type, selection, result
            from prediction_history
            where graded_at is not null
              and result in ('win', 'loss')
              and date(created_at) = ?
        """, (match_date,)).fetchall()

        updated = 0
        for row in graded:
            # Map prediction result to the relevant pattern selection
            # For 1x2 picks: 'Home' → 'home', 'Away' → 'away', 'Draw' → 'draw'
            sel_map = {"Home": "home", "Away": "away", "Draw": "draw",
                       "1": "home", "2": "away", "X": "draw"}
            pattern_sel = sel_map.get(row["selection"], row["selection"].lower() if row["selection"] else None)
            if not pattern_sel:
                continue
            conn.execute("""
                update odds_pattern_features
                set result = ?
                where match_id = ? and selection = ?
            """, (row["result"], str(row["match_id"]), pattern_sel))
            updated += conn.total_changes
        conn.commit()

    return {"graded_patterns": updated, "date": match_date}


# ── Pattern-based prediction signal ───────────────────────────────────────────

def pattern_signal(match_id: str) -> dict[str, Any]:
    """
    For a given match, extract its current odds pattern features and find
    historically similar patterns. Returns a signal dict with:
      - pattern_win_rate: win rate of similar historical patterns
      - similar_matches: count of similar historical matches
      - sharp_signal: whether sharp money indicators are present
      - recommendation: 'back' / 'oppose' / 'neutral'
      - confidence_adjustment: integer to add/subtract from model confidence
    """
    features = extract_pattern(match_id)
    if not features:
        return {"signal": "no_data", "confidence_adjustment": 0}

    # Focus on the home selection as primary signal (most predictive)
    home_feat = next((f for f in features if f["selection"] == "home"), features[0])

    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_pattern_table(conn)
        conn.row_factory = sqlite3.Row

        # Find historically similar patterns:
        # Same selection, similar slope (within 0.05), similar sharp_timing
        similar = conn.execute("""
            select result, slope, sharp_timing, late_move, reversal, stability
            from odds_pattern_features
            where selection = ?
              and result in ('win', 'loss')
              and match_id != ?
              and abs(slope - ?) <= 0.05
              and sharp_timing = ?
            order by abs(slope - ?) asc
            limit 50
        """, (
            home_feat["selection"],
            match_id,
            home_feat["slope"],
            home_feat["sharp_timing"],
            home_feat["slope"],
        )).fetchall()

    if len(similar) < 5:
        # Not enough history yet — return raw sharp signals only
        sharp = home_feat.get("sharp_timing") or home_feat.get("late_move")
        pull_adj = _pull_adjustment(home_feat)
        return {
            "signal":               "sharp_only" if sharp else "insufficient_history",
            "sharp_timing":         bool(home_feat.get("sharp_timing")),
            "late_move":            bool(home_feat.get("late_move")),
            "reversal":             bool(home_feat.get("reversal")),
            "slope":                home_feat.get("slope"),
            "odds_change_percent":  home_feat.get("odds_change_percent"),
            "implied_change_percent": home_feat.get("implied_change_percent"),
            "market_pull":          home_feat.get("market_pull"),
            "similar_matches":      len(similar),
            "confidence_adjustment": (3 if sharp else 0) + pull_adj,
        }

    wins = sum(1 for r in similar if r["result"] == "win")
    win_rate = wins / len(similar)

    # Confidence adjustment: +5 if win_rate > 65%, -5 if < 40%, else 0
    if win_rate >= 0.65:
        adj = 5
        recommendation = "back"
    elif win_rate <= 0.40:
        adj = -5
        recommendation = "oppose"
    else:
        adj = 0
        recommendation = "neutral"

    # Extra boost for sharp timing + high win rate
    if home_feat.get("sharp_timing") and win_rate >= 0.60:
        adj += 3
    adj += _pull_adjustment(home_feat)

    return {
        "signal":               "pattern_match",
        "pattern_win_rate":     round(win_rate * 100, 1),
        "similar_matches":      len(similar),
        "sharp_timing":         bool(home_feat.get("sharp_timing")),
        "late_move":            bool(home_feat.get("late_move")),
        "reversal":             bool(home_feat.get("reversal")),
        "slope":                home_feat.get("slope"),
        "odds_change_percent":  home_feat.get("odds_change_percent"),
        "implied_change_percent": home_feat.get("implied_change_percent"),
        "market_pull":          home_feat.get("market_pull"),
        "stability":            home_feat.get("stability"),
        "recommendation":       recommendation,
        "confidence_adjustment": adj,
    }


def _pull_adjustment(feature: dict[str, Any]) -> int:
    pull = feature.get("market_pull")
    if pull == "strong_backed":
        return 4
    if pull == "backed":
        return 2
    if pull == "strong_faded":
        return -4
    if pull == "faded":
        return -2
    return 0


# ── Summary stats for analytics ───────────────────────────────────────────────

def pattern_stats() -> dict[str, Any]:
    """Return aggregate stats on pattern features — useful for the analytics page."""
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_pattern_table(conn)
        conn.row_factory = sqlite3.Row

        total = conn.execute("select count(*) from odds_pattern_features").fetchone()[0]
        graded = conn.execute(
            "select count(*) from odds_pattern_features where result is not null"
        ).fetchone()[0]

        # Win rate by sharp_timing
        sharp_stats = conn.execute("""
            select sharp_timing,
                   count(*) as samples,
                   sum(case when result='win' then 1 else 0 end) as wins
            from odds_pattern_features
            where result in ('win','loss') and selection = 'home'
            group by sharp_timing
        """).fetchall()

        # Win rate by slope bucket
        slope_stats = conn.execute("""
            select
                case
                    when slope < -0.10 then 'strongly_shortened'
                    when slope < -0.03 then 'shortened'
                    when slope >  0.10 then 'strongly_drifted'
                    when slope >  0.03 then 'drifted'
                    else 'stable'
                end as slope_bucket,
                count(*) as samples,
                sum(case when result='win' then 1 else 0 end) as wins
            from odds_pattern_features
            where result in ('win','loss') and selection = 'home'
            group by slope_bucket
        """).fetchall()

    return {
        "total_patterns":  total,
        "graded_patterns": graded,
        "by_sharp_timing": [
            {
                "sharp_timing": bool(r["sharp_timing"]),
                "samples":      r["samples"],
                "win_rate":     round(r["wins"] / r["samples"] * 100, 1) if r["samples"] else None,
            }
            for r in sharp_stats
        ],
        "by_slope": [
            {
                "slope_bucket": r["slope_bucket"],
                "samples":      r["samples"],
                "win_rate":     round(r["wins"] / r["samples"] * 100, 1) if r["samples"] else None,
            }
            for r in slope_stats
        ],
    }
