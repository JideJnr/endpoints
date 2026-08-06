"""
Closing Line Value (CLV)
------------------------
The single best long-term measure of betting edge.

Definition:
  CLV = (entry_odds / closing_odds) - 1

  Positive CLV → you got better odds than the market settled at = you beat the market
  Negative CLV → you got worse odds than closing = market knew more than you

Why it matters more than win rate:
  - Win rate is noisy (variance over hundreds of bets)
  - CLV measures whether your *entry price* was correct
  - A bettor with consistent +CLV is profitable long-term even through losing streaks
  - Hedge funds use this to measure alpha: did you buy before the market moved?

How it works here:
  1. At prediction time, record the odds available for the pick (entry_odds)
     → stored in clv_entries when a prediction is made
  2. When a match finishes, find the last odds snapshot before kick-off (closing_odds)
     → computed in compute_clv_for_date()
  3. CLV = (entry / closing) - 1, expressed as a percentage

Tables:
  clv_entries  — one row per prediction, stores entry_odds + closing_odds once known
  
Usage:
    from app.risk.clv import record_clv_entry, compute_clv_for_date, get_clv_summary

    # Called at prediction time (inside record_prediction)
    record_clv_entry(match_id, pick_type, selection, entry_odds)

    # Called after grading (inside job_grade_predictions)
    compute_clv_for_date('2025-05-17')

    # Analytics
    summary = get_clv_summary()
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db

CLV_MIN_SAMPLES = 25


# ── Table ─────────────────────────────────────────────────────────────────────

def _init_clv_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        create table if not exists clv_entries (
            id              integer primary key autoincrement,
            match_id        text not null,
            match_name      text,
            match_date      text,
            pick_type       text,
            selection       text,       -- 'Home', 'Away', 'Draw'
            entry_odds      real,       -- odds at prediction time
            closing_odds    real,       -- last snapshot before kick-off (filled later)
            clv             real,       -- (entry / closing) - 1, filled after closing
            clv_percent     real,       -- clv * 100
            result          text,       -- 'win' / 'loss' / null
            confidence      integer,
            created_at      text not null default current_timestamp,
            closing_at      text        -- when closing_odds was filled
        )
    """)
    conn.execute("create index if not exists idx_clv_match   on clv_entries(match_id)")
    conn.execute("create index if not exists idx_clv_date    on clv_entries(match_date)")
    conn.execute("create index if not exists idx_clv_result  on clv_entries(result)")
    conn.execute("create index if not exists idx_clv_closing on clv_entries(closing_odds)")


# ── Record entry odds at prediction time ──────────────────────────────────────

def record_clv_entry(
    match_id: str,
    pick_type: str,
    selection: str,
    confidence: int,
    match_name: str | None = None,
    match_date: str | None = None,
) -> bool:
    """
    Called when a prediction is made. Looks up the current odds snapshot
    for this match and records them as the entry odds.
    Returns True if entry was recorded.
    """
    _init_db()
    entry_odds = _get_current_odds(match_id, selection)
    if not entry_odds:
        return False

    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")
        _init_clv_table(conn)
        # Avoid duplicate entries for the same match+selection on the same day
        existing = conn.execute("""
            select id from clv_entries
            where match_id = ? and selection = ? and date(created_at) = date('now')
        """, (str(match_id), selection)).fetchone()
        if existing:
            return False

        conn.execute("""
            insert into clv_entries
                (match_id, match_name, match_date, pick_type, selection,
                 entry_odds, confidence, created_at)
            values (?, ?, ?, ?, ?, ?, ?, current_timestamp)
        """, (str(match_id), match_name, match_date, pick_type, selection,
               entry_odds, confidence))
        conn.commit()
    return True


def _get_current_odds(match_id: str, selection: str) -> float | None:
    """Get the most recent odds snapshot for a selection."""
    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")
        col = _primary_odds_column(selection)
        if col:
            row = conn.execute(f"""
                select {col} from odds_snapshots
                where match_id = ? and {col} is not null
                order by snapshot_time desc
                limit 1
            """, (str(match_id),)).fetchone()
            if row and row[0]:
                return float(row[0])
        return _get_market_odds(conn, str(match_id), selection)


def _primary_odds_column(selection: str) -> str | None:
    sel = _norm_key(selection)
    if sel in {"home", "1", "home win", "home to win"}:
        return "home_odds"
    if sel in {"draw", "x"}:
        return "draw_odds"
    if sel in {"away", "2", "away win", "away to win"}:
        return "away_odds"
    return None


def _get_market_odds(
    conn: sqlite3.Connection,
    match_id: str,
    selection: str,
    *,
    before_time: str | None = None,
) -> float | None:
    aliases = _selection_aliases(selection)
    if not aliases:
        return None
    try:
        table = "odds_market_changes"
        try:
            conn.execute("select 1 from odds_market_changes limit 1")
            exists = conn.execute(
                "select 1 from odds_market_changes where match_id = ? limit 1",
                (match_id,),
            ).fetchone()
            if not exists:
                # Only fall back if the legacy table still exists.
                legacy = conn.execute(
                    "select 1 from sqlite_master where type='table' and name='odds_market_snapshots'",
                ).fetchone()
                if legacy:
                    table = "odds_market_snapshots"
                else:
                    return None
        except sqlite3.OperationalError:
            table = "odds_market_snapshots"
        cutoff_clause = ""
        params: list[Any] = [match_id]
        if before_time:
            cutoff_clause = "and datetime(snapshot_time) <= datetime(?)"
            params.append(before_time)
        rows = conn.execute(
            f"""
            select market_name, specifier, selection_name, odds
            from {table}
            where match_id = ? and odds is not null
              {cutoff_clause}
            order by snapshot_time desc
            limit 500
            """,
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    for row in rows:
        market_name = str(row[0] or "")
        specifier = str(row[1] or "")
        selection_name = str(row[2] or "")
        candidates = {
            _norm_key(selection_name),
            _norm_key(f"{selection_name} {specifier}"),
            _norm_key(f"{selection_name} {market_name}"),
            _norm_key(f"{market_name} {specifier} {selection_name}"),
        }
        if candidates & aliases:
            return float(row[3])
    return None


def _selection_aliases(selection: str) -> set[str]:
    text = _norm_key(selection)
    aliases = {text}
    if "home or draw" in text or text == "1x":
        aliases.update({"home or draw", "1x", "1 x"})
    if "away or draw" in text or "draw or away" in text or text == "x2":
        aliases.update({"away or draw", "draw or away", "x2", "x 2"})
    if "home or away" in text or text == "12":
        aliases.update({"home or away", "12", "1 2"})
    if "both teams" in text or "btts" in text:
        aliases.update({"both teams to score", "btts yes"} if "no" not in text else {"both teams to score no", "btts no", "no"})
    for line in ("0.5", "1.5", "2.5", "3.5", "4.5"):
        if line in text and "under" in text:
            aliases.update({f"under {line}", f"under {line} goals", f"total under {line}"})
        if line in text and "over" in text:
            aliases.update({f"over {line}", f"over {line} goals", f"total over {line}"})
    return {_norm_key(alias) for alias in aliases if alias}


def _norm_key(value: str) -> str:
    return " ".join(str(value or "").lower().replace("-", " ").replace("/", " ").split())


# ── Compute CLV after matches close ───────────────────────────────────────────

def compute_clv_for_date(match_date: str) -> dict[str, Any]:
    """
    For all CLV entries on a given date that don't yet have closing_odds,
    find the last odds snapshot for each match and compute CLV.

    Call this after grading runs (job_grade_predictions).
    Returns count of entries updated.
    """
    _init_db()
    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")
        _init_clv_table(conn)
        conn.row_factory = sqlite3.Row

        # Get entries missing closing odds
        pending = conn.execute("""
            select id, match_id, selection, entry_odds, pick_type
            from clv_entries
            where match_date = ? and closing_odds is null and entry_odds is not null
        """, (match_date,)).fetchall()

        updated = 0
        for row in pending:
            kickoff = _match_kickoff_iso(conn, str(row["match_id"]))
            closing = _get_closing_odds(conn, str(row["match_id"]), row["selection"], before_time=kickoff)
            if not closing:
                continue

            entry = float(row["entry_odds"])
            clv = round((entry / closing) - 1, 4)
            clv_pct = round(clv * 100, 2)

            conn.execute("""
                update clv_entries
                set closing_odds = ?, clv = ?, clv_percent = ?, closing_at = current_timestamp
                where id = ?
            """, (closing, clv, clv_pct, row["id"]))
            updated += 1

        # Also pull in results from prediction_history
        conn.execute("""
            update clv_entries
            set result = ph.result
            from (
                select match_id, selection, result
                from prediction_history
                where graded_at is not null and result in ('win','loss')
                  and date(created_at) = ?
            ) as ph
            where clv_entries.match_id = ph.match_id
              and lower(clv_entries.selection) = lower(ph.selection)
              and clv_entries.result is null
        """, (match_date,))

        conn.commit()

    return {"date": match_date, "clv_entries_updated": updated}


def _get_closing_odds(
    conn: sqlite3.Connection,
    match_id: str,
    selection: str,
    *,
    before_time: str | None = None,
) -> float | None:
    """Get the last odds snapshot — this is the closing line."""
    col = _primary_odds_column(selection)
    if col:
        cutoff_clause = ""
        params: list[Any] = [match_id]
        if before_time:
            cutoff_clause = "and datetime(snapshot_time) <= datetime(?)"
            params.append(before_time)
        row = conn.execute(f"""
            select {col} from odds_snapshots
            where match_id = ? and {col} is not null
              {cutoff_clause}
            order by snapshot_time desc
            limit 1
        """, params).fetchone()
        if row and row[0]:
            return float(row[0])
    return _get_market_odds(conn, match_id, selection, before_time=before_time)


def _match_kickoff_iso(conn: sqlite3.Connection, match_id: str) -> str | None:
    for table in ("match_buffer", "future_match_buffer"):
        try:
            row = conn.execute(f"select start_time from {table} where match_id = ? limit 1", (match_id,)).fetchone()
        except sqlite3.OperationalError:
            continue
        if row and row[0] not in (None, ""):
            return _to_utc_iso(row[0])
    return None


def _to_utc_iso(value: Any) -> str | None:
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            timestamp = float(value)
            if timestamp > 1e10:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except Exception:
        return None


# ── Analytics ─────────────────────────────────────────────────────────────────

def get_clv_summary(days: int = 30) -> dict[str, Any]:
    """
    Full CLV analytics summary:
      - avg_clv: your average edge vs closing line
      - clv_positive_rate: % of bets where you beat the closing line
      - clv_by_pick_type: breakdown per market
      - clv_by_confidence_band: does higher confidence = better CLV?
      - recent: last 20 entries
    """
    _init_db()
    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")
        _init_clv_table(conn)
        conn.row_factory = sqlite3.Row

        # Overall stats
        overall = conn.execute("""
            select
                count(*)                                        as total,
                avg(clv_percent)                                as avg_clv,
                sum(case when clv > 0 then 1 else 0 end)        as positive_clv,
                sum(case when clv <= 0 then 1 else 0 end)       as negative_clv,
                avg(case when result='win'  then clv_percent end) as avg_clv_wins,
                avg(case when result='loss' then clv_percent end) as avg_clv_losses
            from clv_entries
            where closing_odds is not null
              and datetime(created_at) >= datetime('now', ?)
        """, (f"-{days} days",)).fetchone()

        # By pick type
        by_type = conn.execute("""
            select
                pick_type,
                count(*)                                    as samples,
                avg(clv_percent)                            as avg_clv,
                sum(case when clv > 0 then 1 else 0 end)   as positive_clv,
                sum(case when result='win' then 1 else 0 end) as wins
            from clv_entries
            where closing_odds is not null
              and datetime(created_at) >= datetime('now', ?)
            group by pick_type
            order by avg_clv desc
        """, (f"-{days} days",)).fetchall()

        # By confidence band
        by_band = conn.execute("""
            select
                min(90, (confidence / 10) * 10)             as band_low,
                count(*)                                    as samples,
                avg(clv_percent)                            as avg_clv,
                sum(case when clv > 0 then 1 else 0 end)   as positive_clv,
                sum(case when result='win' then 1 else 0 end) as wins
            from clv_entries
            where closing_odds is not null
              and confidence is not null
              and datetime(created_at) >= datetime('now', ?)
            group by band_low
            order by band_low asc
        """, (f"-{days} days",)).fetchall()

        # Recent entries
        recent = conn.execute("""
            select match_name, selection, entry_odds, closing_odds,
                   clv_percent, result, confidence, created_at
            from clv_entries
            where closing_odds is not null
            order by created_at desc
            limit 20
        """).fetchall()

        # Daily CLV trend
        daily = conn.execute("""
            select
                date(created_at)    as day,
                count(*)            as bets,
                avg(clv_percent)    as avg_clv,
                sum(case when clv > 0 then 1 else 0 end) as positive
            from clv_entries
            where closing_odds is not null
              and datetime(created_at) >= datetime('now', ?)
            group by day
            order by day asc
        """, (f"-{days} days",)).fetchall()

    total = overall["total"] or 0
    positive = overall["positive_clv"] or 0

    return {
        "period_days":        days,
        "total_entries":      total,
        "avg_clv_percent":    round(float(overall["avg_clv"] or 0), 2),
        "positive_clv_rate":  round(positive / total * 100, 1) if total else None,
        "avg_clv_on_wins":    round(float(overall["avg_clv_wins"] or 0), 2),
        "avg_clv_on_losses":  round(float(overall["avg_clv_losses"] or 0), 2),
        # Key insight: if avg_clv_on_wins > avg_clv_on_losses, you're finding real edges
        "edge_quality":       _edge_quality(overall),
        "by_pick_type": [
            {
                "pick_type":        r["pick_type"],
                "samples":          r["samples"],
                "avg_clv":          round(float(r["avg_clv"] or 0), 2),
                "positive_clv_pct": round((r["positive_clv"] or 0) / r["samples"] * 100, 1) if r["samples"] else None,
                "win_rate":         round((r["wins"] or 0) / r["samples"] * 100, 1) if r["samples"] else None,
            }
            for r in by_type
        ],
        "by_confidence_band": [
            {
                "band":             f"{r['band_low']}-{r['band_low'] + 9}%",
                "samples":          r["samples"],
                "avg_clv":          round(float(r["avg_clv"] or 0), 2),
                "positive_clv_pct": round((r["positive_clv"] or 0) / r["samples"] * 100, 1) if r["samples"] else None,
                "win_rate":         round((r["wins"] or 0) / r["samples"] * 100, 1) if r["samples"] else None,
            }
            for r in by_band
        ],
        "daily_trend": [
            {
                "day":          r["day"],
                "bets":         r["bets"],
                "avg_clv":      round(float(r["avg_clv"] or 0), 2),
                "positive_pct": round((r["positive"] or 0) / r["bets"] * 100, 1) if r["bets"] else None,
            }
            for r in daily
        ],
        "recent": [
            {
                "match":        r["match_name"],
                "selection":    r["selection"],
                "entry_odds":   r["entry_odds"],
                "closing_odds": r["closing_odds"],
                "clv_percent":  r["clv_percent"],
                "result":       r["result"],
                "confidence":   r["confidence"],
                "date":         r["created_at"],
                "beat_market":  (r["clv_percent"] or 0) > 0,
            }
            for r in recent
        ],
    }


def _edge_quality(row: sqlite3.Row) -> str:
    """
    Interpret the relationship between CLV and results.
    Positive CLV on wins + negative CLV on losses = genuine skill.
    """
    clv_wins   = float(row["avg_clv_wins"]   or 0)
    clv_losses = float(row["avg_clv_losses"] or 0)
    avg        = float(row["avg_clv"]        or 0)

    if avg > 2:
        return "strong_edge"       # consistently beating closing line by 2%+
    if avg > 0:
        return "positive_edge"     # beating closing line on average
    if clv_wins > 0 and clv_losses < 0:
        return "skill_present"     # wins have positive CLV, losses don't — real signal
    if avg > -1:
        return "marginal"          # slightly below closing line — normal variance
    return "no_edge"               # consistently getting worse than closing price


# ── Stake sizing using CLV ─────────────────────────────────────────────────────

def clv_stake_multiplier(pick_type: str, confidence: int) -> float:
    """
    Returns a stake multiplier based on historical CLV for this pick_type
    and confidence band. Combines CLV quality with win rate.

    This is more accurate than win-rate-only sizing because CLV measures
    whether you're finding real edges, not just getting lucky.
    """
    _init_db()
    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")
        _init_clv_table(conn)
        conn.row_factory = sqlite3.Row
        band_low = min(90, (confidence // 10) * 10)

        row = conn.execute("""
            select
                count(*)                                    as samples,
                avg(clv_percent)                            as avg_clv,
                sum(case when clv > 0 then 1 else 0 end)   as positive_clv,
                sum(case when result='win' then 1 else 0 end) as wins
            from clv_entries
            where closing_odds is not null
              and pick_type = ?
              and min(90, (confidence / 10) * 10) = ?
        """, (pick_type, band_low)).fetchone()

    if not row or (row["samples"] or 0) < CLV_MIN_SAMPLES:
        return 1.0  # not enough data

    avg_clv = float(row["avg_clv"] or 0)
    win_rate = (row["wins"] or 0) / row["samples"]

    # Both CLV and win rate must be good to increase stake
    if avg_clv >= 3 and win_rate >= 0.65:   return 2.0   # strong edge
    if avg_clv >= 1 and win_rate >= 0.60:   return 1.5   # good edge
    if avg_clv >= 0 and win_rate >= 0.55:   return 1.0   # neutral
    if avg_clv < -2 or win_rate < 0.40:     return 0.5   # reduce
    return 0.75
