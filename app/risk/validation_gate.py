from __future__ import annotations

import math
import sqlite3
from typing import Any

from app.config.config import get_settings
from app.storage.db import DB_PATH, _conn


MIN_CALIBRATION_SAMPLES = 30
MIN_CLV_SAMPLES = 25
MAX_CALIBRATION_GAP_POINTS = 12.0
MAX_RECENT_LOSS_RATE = 0.60
MAX_RECENT_LOSS_STREAK = 3


def evaluate_promotion_gate(doc: dict[str, Any], pick: dict[str, Any]) -> dict[str, Any]:
    """Return whether a pick has enough market-specific proof to be promoted.

    This gate is intentionally stricter than confidence capping. Confidence can
    describe conviction, but promotion requires historical calibration and CLV.
    """
    pick_type = str(pick.get("type") or "").strip()
    if not pick_type or pick_type == "no_bet":
        return {"allowed": True, "status": "not_applicable", "reasons": [], "metrics": {}, "bootstrap_mode": False}

    settings = get_settings()
    min_calibration_samples = max(0, int(settings.validation_gate_min_calibration_samples))
    min_clv_samples = max(0, int(settings.validation_gate_min_clv_samples))
    market = _market_key(pick_type)
    confidence = _to_float(pick.get("confidence")) or 0.0
    league = _league_name(doc)
    reasons: list[str] = []
    metrics: dict[str, Any] = {"market": market, "league": league}

    try:
        with _conn(timeout=15) as conn:
            calibration = _calibration_metrics(conn, market, pick_type, confidence)
            clv = _clv_metrics(conn, market, pick_type, league)
            drawdown = _drawdown_metrics(conn, market, pick_type, league)
    except Exception as exc:
        return {
            "allowed": False,
            "status": "blocked",
            "reasons": ["validation_gate_unavailable"],
            "metrics": {"error": str(exc), **metrics},
            "bootstrap_mode": False,
        }

    metrics.update({"calibration": calibration, "clv": clv, "drawdown": drawdown})

    bootstrap_mode = (
        (calibration["samples"] == 0 and clv["samples"] == 0 and drawdown["samples"] == 0)
        or (calibration["samples"] < min_calibration_samples and clv["samples"] < min_clv_samples)
    )

    if min_calibration_samples > 0 and calibration["samples"] < min_calibration_samples:
        reasons.append("insufficient_calibration_samples")
    elif calibration["calibration_gap_points"] is not None and calibration["calibration_gap_points"] > MAX_CALIBRATION_GAP_POINTS:
        reasons.append("calibration_gap_too_wide")

    if min_clv_samples > 0 and clv["samples"] < min_clv_samples:
        reasons.append("insufficient_clv_samples")
    elif clv["avg_clv_percent"] is not None and clv["avg_clv_percent"] <= 0:
        reasons.append("negative_or_flat_clv")

    if drawdown["samples"] >= 5:
        if drawdown["recent_loss_rate"] >= MAX_RECENT_LOSS_RATE:
            reasons.append("recent_drawdown_breach")
        if drawdown["loss_streak"] >= MAX_RECENT_LOSS_STREAK:
            reasons.append("recent_loss_streak_breach")

    # Bootstrap mode: sample-count shortfalls (insufficient_*) are NOT blocking.
    # Only genuine quality failures (drawdown, CLV quality) can block in bootstrap.
    _BOOTSTRAP_PASSTHROUGH = {"insufficient_calibration_samples", "insufficient_clv_samples"}
    _HARD_BLOCK_IN_BOOTSTRAP = {"recent_drawdown_breach", "recent_loss_streak_breach", "negative_or_flat_clv"}

    if bootstrap_mode:
        # In bootstrap, only block on genuine quality failures.
        # negative_or_flat_clv requires actual CLV data to be meaningful —
        # with zero samples it is noise, not a real signal.
        blocking_reasons = [
            r for r in reasons
            if r in _HARD_BLOCK_IN_BOOTSTRAP
            and not (r == "negative_or_flat_clv" and clv["samples"] == 0)
        ]
    else:
        blocking_reasons = reasons

    allowed = not blocking_reasons
    if bootstrap_mode and allowed:
        status = "bootstrap"
    elif allowed:
        status = "passed"
    else:
        status = "blocked"

    return {
        "allowed": allowed,
        "status": status,
        "reasons": blocking_reasons,
        "bootstrap_reasons": [r for r in reasons if r in _BOOTSTRAP_PASSTHROUGH] if bootstrap_mode else [],
        "metrics": metrics,
        "bootstrap_mode": bootstrap_mode,
    }


def _calibration_metrics(conn: sqlite3.Connection, market: str, pick_type: str, confidence: float) -> dict[str, Any]:
    if not _table_exists(conn, "confidence_calibration"):
        return {"samples": 0, "win_rate": None, "calibration_gap_points": None, "source": "missing_table"}

    band_low = min(80, int(confidence // 10) * 10)
    row = conn.execute(
        """
        select pick_type, samples, win_rate
        from confidence_calibration
        where pick_type in (?, ?, 'match_result', '__global__') and band_low = ?
        order by
          case pick_type
            when ? then 0
            when ? then 1
            when 'match_result' then 2
            else 3
          end
        limit 1
        """,
        (pick_type, market, band_low, pick_type, market),
    ).fetchone()
    if not row:
        return {"samples": 0, "win_rate": None, "calibration_gap_points": None, "source": "empty"}

    win_rate = _to_float(row["win_rate"])
    win_rate_percent = round(win_rate * 100, 2) if win_rate is not None else None
    gap = abs(confidence - win_rate_percent) if win_rate_percent is not None else None
    return {
        "samples": int(row["samples"] or 0),
        "win_rate": win_rate_percent,
        "calibration_gap_points": round(gap, 2) if gap is not None else None,
        "source": row["pick_type"],
        "band_low": band_low,
    }


def _clv_metrics(conn: sqlite3.Connection, market: str, pick_type: str, league: str | None) -> dict[str, Any]:
    if not _table_exists(conn, "clv_entries"):
        return {"samples": 0, "avg_clv_percent": None, "positive_clv_rate": None, "source": "missing_table"}

    min_clv_samples = max(0, int(get_settings().validation_gate_min_clv_samples))
    row = None
    if league and _has_column(conn, "clv_entries", "league_name"):
        row = conn.execute(
            """
            select count(*) as samples,
                   avg(clv_percent) as avg_clv,
                   avg(case when clv > 0 then 1.0 else 0.0 end) as positive_rate
            from clv_entries
            where pick_type in (?, ?) and league_name = ? and clv is not null
            """,
            (pick_type, market, league),
        ).fetchone()
        if row and int(row["samples"] or 0) < min_clv_samples:
            row = None

    if not row:
        row = conn.execute(
            """
            select count(*) as samples,
                   avg(clv_percent) as avg_clv,
                   avg(case when clv > 0 then 1.0 else 0.0 end) as positive_rate
            from clv_entries
            where pick_type in (?, ?) and clv is not null
            """,
            (pick_type, market),
        ).fetchone()

    samples = int(row["samples"] or 0) if row else 0
    positive_rate = _to_float(row["positive_rate"]) if row else None
    return {
        "samples": samples,
        "avg_clv_percent": round(float(row["avg_clv"]), 2) if row and row["avg_clv"] is not None else None,
        "positive_clv_rate": round(positive_rate * 100, 1) if positive_rate is not None else None,
        "source": "market",
    }


def _drawdown_metrics(conn: sqlite3.Connection, market: str, pick_type: str, league: str | None) -> dict[str, Any]:
    if not _table_exists(conn, "prediction_history"):
        return {"samples": 0, "recent_loss_rate": 0.0, "loss_streak": 0, "source": "missing_table"}

    params: list[Any] = [pick_type, market]
    league_clause = ""
    if league and _has_column(conn, "prediction_history", "league_name"):
        league_clause = "and league_name = ?"
        params.append(league)
    params.append(20)
    rows = conn.execute(
        f"""
        select result
        from prediction_history
        where pick_type in (?, ?)
          and graded_at is not null
          and result in ('win', 'loss')
          {league_clause}
        order by datetime(coalesce(graded_at, created_at)) desc, id desc
        limit ?
        """,
        params,
    ).fetchall()
    if len(rows) < 5 and league_clause:
        rows = conn.execute(
            """
            select result
            from prediction_history
            where pick_type in (?, ?)
              and graded_at is not null
              and result in ('win', 'loss')
            order by datetime(coalesce(graded_at, created_at)) desc, id desc
            limit 20
            """,
            (pick_type, market),
        ).fetchall()

    results = [str(row["result"]) for row in rows]
    losses = sum(1 for result in results if result == "loss")
    streak = 0
    for result in results:
        if result != "loss":
            break
        streak += 1
    return {
        "samples": len(results),
        "recent_loss_rate": round(losses / len(results), 3) if results else 0.0,
        "loss_streak": streak,
        "source": "league" if league_clause and len(rows) >= 5 else "market",
    }


def _market_key(pick_type: str) -> str:
    normalized = pick_type.lower()
    if normalized in {"home_win", "away_win", "draw"}:
        return "match_result"
    if "over" in normalized or "under" in normalized or "goal" in normalized:
        return "goals"
    if normalized in {"btts", "gg", "both_teams_to_score"}:
        return "btts"
    return normalized


def _league_name(doc: dict[str, Any]) -> str | None:
    value = doc.get("league_name") or doc.get("tournament") or doc.get("competition")
    return str(value).strip() if value else None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (table,),
    ).fetchone() is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    return any(row["name"] == column for row in conn.execute(f"pragma table_info({table})").fetchall())


def _to_float(value: Any) -> float | None:
    try:
        result = float(value)
    except Exception:
        return None
    return result if math.isfinite(result) else None

