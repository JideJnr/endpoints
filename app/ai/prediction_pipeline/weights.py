"""Specialist accuracy tracking.

Each evidence-step "specialist" (H2H Analyst, Form Analyst, etc.) earns a
learned weight from its graded track record, stored in the
``specialist_performance`` SQLite table. The decider step (see
orchestration.py) uses these weights to tell the LLM which analyst findings
to trust more.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any

from app.storage.db import db_conn, _init_db

SPECIALIST_NAMES = [
    "H2H Analyst",
    "Common Opponent Analyst",
    "Form Analyst",
    "Market Odds Analyst",
    "Similar Match Analyst",
    "Team Previous Matches Analyst",
]

MIN_SPECIALIST_SAMPLES = 10   # minimum graded predictions before trusting a specialist's ratio


def get_specialist_weights(league: str | None = None, pick_type: str | None = None) -> dict[str, float]:
    """
    Return {specialist_name: weight} for the given scope.
    Falls back to global weights, then to 1.0 (neutral) if no data.
    """
    _init_db()
    league_key = (league or "").lower().strip().replace(" ", "_")[:60] or "__global__"
    with db_conn(timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            select specialist_name, weight, league_key, samples
            from specialist_performance
            where league_key in (?, '__global__')
              and pick_type in (?, '__all__')
              and samples >= ?
            order by
                case when league_key = ? then 0 else 1 end,
                case when pick_type = ? then 0 else 1 end
        """, (league_key, pick_type or "__all__", MIN_SPECIALIST_SAMPLES, league_key, pick_type or "__all__")).fetchall()
    weights: dict[str, float] = {}
    for row in rows:
        name = row["specialist_name"]
        if name not in weights:  # league-specific wins over global
            weights[name] = round(float(row["weight"]), 4)
    # Fill missing specialists with neutral weight 1.0
    for name in SPECIALIST_NAMES:
        weights.setdefault(name, 1.0)
    return weights


def record_specialist_outcome(
    specialist_name: str,
    result: str,           # 'win' or 'loss'
    league: str | None = None,
    pick_type: str | None = None,
) -> None:
    """
    Record one graded outcome for a specialist.
    Called after a prediction is graded — the specialist contributed if its
    evidence_status was 'available' for that prediction.
    """
    _init_db()
    league_key = (league or "").lower().strip().replace(" ", "_")[:60] or "__global__"
    pt = pick_type or "__all__"
    win = 1 if result == "win" else 0
    loss = 1 if result == "loss" else 0
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(timeout=20) as conn:
        for lk in (league_key, "__global__"):
            for pk in ({pt, "__all__"}):
                conn.execute("""
                    insert into specialist_performance
                        (specialist_name, league_key, pick_type, samples, wins, losses, win_rate, weight, last_updated)
                    values (?, ?, ?, 1, ?, ?, null, 1.0, ?)
                    on conflict(specialist_name, league_key, pick_type) do update set
                        samples      = samples + 1,
                        wins         = wins + excluded.wins,
                        losses       = losses + excluded.losses,
                        last_updated = excluded.last_updated
                """, (specialist_name, lk, pk, win, loss, now))
        # Recompute win_rate and weight for touched rows
        conn.execute("""
            update specialist_performance
            set win_rate = cast(wins as real) / samples,
                weight   = max(0.3, min(2.0, 0.3 + (cast(wins as real) / samples) * 1.7))
            where specialist_name = ? and samples >= ?
        """, (specialist_name, MIN_SPECIALIST_SAMPLES))
        conn.commit()


def grade_specialist_contributions(
    reasoning_context: dict[str, Any],
    result: str,
    league: str | None = None,
    pick_type: str | None = None,
) -> int:
    """
    After a prediction is graded, credit each specialist whose evidence was
    'available' (i.e. actually contributed, not a fallback placeholder).
    Returns the number of specialists credited.
    """
    analysts: list[dict[str, Any]] = reasoning_context.get("analysts") or []
    if not analysts:
        return 0
    credited = 0
    for analyst in analysts:
        name = str(analyst.get("name") or "")
        status = str(analyst.get("evidence_status") or "available")  # default available for legacy rows
        if not name or status == "unavailable":
            continue
        try:
            record_specialist_outcome(name, result, league=league, pick_type=pick_type)
            credited += 1
        except Exception:
            pass
    return credited


def get_specialist_summary() -> list[dict[str, Any]]:
    """Return global specialist performance for the analytics endpoint."""
    _init_db()
    with db_conn(timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            select specialist_name, samples, wins, losses, win_rate, weight
            from specialist_performance
            where league_key = '__global__' and pick_type = '__all__'
            order by coalesce(win_rate, 0) desc
        """).fetchall()
    return [
        {
            "specialist": row["specialist_name"],
            "samples":    row["samples"],
            "wins":       row["wins"],
            "losses":     row["losses"],
            "win_rate":   round(float(row["win_rate"]) * 100, 1) if row["win_rate"] is not None else None,
            "weight":     round(float(row["weight"]), 3),
            "status":     "trusted" if (row["samples"] or 0) >= MIN_SPECIALIST_SAMPLES else "learning",
        }
        for row in rows
    ]
