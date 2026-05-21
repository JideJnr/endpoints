"""
Confidence Calibrator
---------------------
Learns from prediction_history wins/losses to produce per-pick_type,
per-confidence-band accuracy rates. These are used to:

  1. Adjust raw model confidence at prediction time (shrink overconfident bands)
  2. Decide when to "double down" — i.e. when historical accuracy in a band
     is high enough to warrant a higher stake multiplier

Calibration bands: 50-59, 60-69, 70-79, 80+
Minimum samples before trusting a band: MIN_SAMPLES (default 10)

Usage:
    from app.confidence_calibrator import calibrate_confidence, get_calibration_table

    # At prediction time — adjust a raw confidence score
    adjusted = calibrate_confidence('match_result', 75)

    # Get the full calibration table for display / analytics
    table = get_calibration_table()
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.league_memory import DB_PATH, _init_db

MIN_SAMPLES = 10          # bands with fewer samples are not adjusted
BLEND_WEIGHT = 0.4        # how much historical accuracy pulls the raw confidence
                          # 0 = no adjustment, 1 = fully replace with historical rate


UNIQUE_GRADED_HISTORY = """
    select *
    from (
        select
            ph.*,
            row_number() over (
                partition by match_id, pick_type, selection
                order by datetime(coalesce(graded_at, created_at)) desc, id desc
            ) as rn
        from prediction_history ph
        where graded_at is not null
          and result in ('win', 'loss')
          and pick_type not in ('no_bet')
          and confidence is not null
    )
    where rn = 1
"""


# ── Table ─────────────────────────────────────────────────────────────────────

def _init_calibration_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        create table if not exists confidence_calibration (
            pick_type       text not null,
            band_low        integer not null,   -- e.g. 50, 60, 70, 80, 90
            samples         integer not null default 0,
            wins            integer not null default 0,
            losses          integer not null default 0,
            win_rate        real,               -- wins / (wins + losses)
            last_updated    text not null default current_timestamp,
            primary key (pick_type, band_low)
        )
    """)


# ── Build / refresh calibration from prediction_history ───────────────────────

def rebuild_calibration() -> dict[str, Any]:
    """
    Recompute win rates per pick_type × confidence band from all graded predictions.
    Call this after grading runs (scheduler does it every 6 hrs).
    """
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_calibration_table(conn)
        conn.row_factory = sqlite3.Row
        conn.execute("delete from confidence_calibration")

        rows = conn.execute(f"""
            select
                pick_type,
                -- bucket into 10-point bands, cap at 80 so top band is 80+
                min(80, (confidence / 10) * 10) as band_low,
                count(*) as samples,
                sum(case when result = 'win'  then 1 else 0 end) as wins,
                sum(case when result = 'loss' then 1 else 0 end) as losses
            from ({UNIQUE_GRADED_HISTORY})
            group by pick_type, band_low
        """).fetchall()

        global_rows = conn.execute(f"""
            select
                '__global__' as pick_type,
                min(80, (confidence / 10) * 10) as band_low,
                count(*) as samples,
                sum(case when result = 'win'  then 1 else 0 end) as wins,
                sum(case when result = 'loss' then 1 else 0 end) as losses
            from ({UNIQUE_GRADED_HISTORY})
            group by band_low
        """).fetchall()

        updated = 0
        for row in list(rows) + list(global_rows):
            total = (row['wins'] or 0) + (row['losses'] or 0)
            win_rate = round(row['wins'] / total, 4) if total > 0 else None
            conn.execute("""
                insert into confidence_calibration
                    (pick_type, band_low, samples, wins, losses, win_rate, last_updated)
                values (?, ?, ?, ?, ?, ?, current_timestamp)
                on conflict(pick_type, band_low) do update set
                    samples      = excluded.samples,
                    wins         = excluded.wins,
                    losses       = excluded.losses,
                    win_rate     = excluded.win_rate,
                    last_updated = current_timestamp
            """, (row['pick_type'], row['band_low'], row['samples'],
                  row['wins'], row['losses'], win_rate))
            updated += 1
        conn.commit()

    return {"status": "ok", "bands_updated": updated}


# ── Calibrate a single confidence score ───────────────────────────────────────

def calibrate_confidence(pick_type: str, raw_confidence: int) -> dict[str, Any]:
    """
    Given a raw model confidence (0-100) and pick type, return:
      - adjusted_confidence: blended with historical win rate
      - double_down: True if historical accuracy in this band is >= 65% with enough samples
      - win_rate: historical win rate for this band (None if not enough data)
      - samples: how many graded predictions back this band
    """
    band_low = min(80, (raw_confidence // 10) * 10)
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_calibration_table(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            select samples, wins, losses, win_rate
            from confidence_calibration
            where pick_type = ? and band_low = ?
        """, (pick_type, band_low)).fetchone()

        # Also try the generic 'match_result' bucket as fallback
        if not row or (row['samples'] or 0) < MIN_SAMPLES:
            row_generic = conn.execute("""
                select samples, wins, losses, win_rate
                from confidence_calibration
                where pick_type = 'match_result' and band_low = ?
            """, (band_low,)).fetchone()
            if row_generic and (row_generic['samples'] or 0) >= MIN_SAMPLES:
                row = row_generic

        # Final fallback: all settled predictions in this confidence band.
        # This is the system-level learning loop, so every model benefits from
        # the observed fact that high-confidence bands are currently performing
        # much better than low-confidence bands.
        if not row or (row['samples'] or 0) < MIN_SAMPLES:
            row_global = conn.execute("""
                select samples, wins, losses, win_rate
                from confidence_calibration
                where pick_type = '__global__' and band_low = ?
            """, (band_low,)).fetchone()
            if row_global and (row_global['samples'] or 0) >= MIN_SAMPLES:
                row = row_global

    if not row or (row['samples'] or 0) < MIN_SAMPLES:
        return {
            "adjusted_confidence": raw_confidence,
            "double_down": False,
            "win_rate": None,
            "samples": row['samples'] if row else 0,
            "calibrated": False,
        }

    win_rate = float(row['win_rate'] or 0)
    historical_as_confidence = round(win_rate * 100)

    # Blend: pull raw confidence toward historical accuracy
    adjusted = round(raw_confidence * (1 - BLEND_WEIGHT) + historical_as_confidence * BLEND_WEIGHT)
    adjusted = max(1, min(99, adjusted))

    # Double down: historical win rate >= 65% with solid sample
    double_down = win_rate >= 0.65 and (row['samples'] or 0) >= MIN_SAMPLES

    return {
        "adjusted_confidence": adjusted,
        "double_down": double_down,
        "win_rate": round(win_rate * 100, 1),
        "samples": row['samples'],
        "calibrated": True,
        "band": "80%+" if band_low >= 80 else f"{band_low}-{band_low + 9}%",
    }


# ── Full calibration table for analytics / display ────────────────────────────

def get_calibration_table() -> list[dict[str, Any]]:
    """Return the full calibration table sorted by pick_type, band."""
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_calibration_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            select pick_type, band_low, samples, wins, losses, win_rate, last_updated
            from confidence_calibration
            order by pick_type asc, band_low asc
        """).fetchall()
    return [
        {
            "pick_type":    row["pick_type"],
            "band":         "80%+" if int(row["band_low"] or 0) >= 80 else f"{row['band_low']}-{row['band_low'] + 9}%",
            "band_low":     row["band_low"],
            "samples":      row["samples"],
            "wins":         row["wins"],
            "losses":       row["losses"],
            "win_rate":     round(float(row["win_rate"]) * 100, 1) if row["win_rate"] is not None else None,
            "double_down":  (row["win_rate"] or 0) >= 0.65 and (row["samples"] or 0) >= MIN_SAMPLES,
            "last_updated": row["last_updated"],
        }
        for row in rows
    ]


# ── Stake multiplier based on calibration ─────────────────────────────────────

def stake_multiplier(pick_type: str, raw_confidence: int) -> float:
    """
    Returns a stake multiplier (0.5x – 2.0x) based on historical accuracy.
    Use this to size bets: base_stake × stake_multiplier(pick_type, confidence).

    Rules:
      - No calibration data → 1.0x (neutral)
      - Win rate < 40%      → 0.5x (reduce)
      - Win rate 40-54%     → 0.75x (cautious)
      - Win rate 55-64%     → 1.0x (neutral)
      - Win rate 65-74%     → 1.5x (double down)
      - Win rate 75%+       → 2.0x (max confidence)
    """
    cal = calibrate_confidence(pick_type, raw_confidence)
    if not cal["calibrated"]:
        return 1.0
    wr = cal["win_rate"] or 0
    if wr < 40:   return 0.5
    if wr < 55:   return 0.75
    if wr < 65:   return 1.0
    if wr < 75:   return 1.5
    return 2.0
