from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db
from app.monitoring.system_audit import prediction_system_audit


def signal_attribution_report(min_samples: int = 5, limit: int = 5000) -> dict[str, Any]:
    """Grade signal contribution across settled primary and candidate decisions."""
    rows = _graded_rows(limit=limit)
    signal_stats: dict[tuple[str, str], dict[str, Any]] = defaultdict(_blank_stats)
    for row in rows:
        result = row["result"]
        for signal in _safe_json(row["signals_json"], []):
            name = str(signal.get("name") or "")
            if not name:
                continue
            impact = _to_float(signal.get("impact")) or 0.0
            direction = "support" if impact > 0 else "risk" if impact < 0 else "context"
            key = (name, direction)
            _tally(signal_stats[key], result, row, impact)

    rows_out = []
    for (name, direction), stats in signal_stats.items():
        if stats["samples"] < min_samples:
            continue
        rows_out.append(_finish_stats({
            "signal": name,
            "direction": direction,
            **stats,
        }))
    rows_out.sort(key=lambda item: (item["win_rate"], item["samples"]), reverse=True)
    return {
        "status": "success",
        "sampled_rows": len(rows),
        "min_samples": min_samples,
        "signals": rows_out,
        "best": rows_out[:20],
        "worst": sorted(rows_out, key=lambda item: (item["win_rate"], -item["samples"]))[:20],
    }


def backtest_gate(limit: int = 1000, min_samples: int = 50) -> dict[str, Any]:
    """Stored-decision replay gate before trusting a model/rule change.

    This uses the decisions already stored and graded. It is not a full model
    re-run from raw historical snapshots, but it is the deploy gate available
    from current persisted state.
    """
    rows = _graded_rows(limit=limit)
    primary_rows = [row for row in rows if row["source_table"] == "prediction_history"]
    candidate_rows = [row for row in rows if row["source_table"] == "prediction_candidate_history"]
    all_stats = _result_stats(rows)
    high_conf = _result_stats([row for row in rows if int(row["confidence"] or 0) >= 80])
    longshots = _result_stats([
        row for row in rows
        if row["pick_type"] in {"consensus_longshot_value", "value_bet", "market_value"}
    ])
    by_pick_type = []
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["pick_type"] or "unknown")].append(row)
    for pick_type, group in grouped.items():
        if len(group) >= 5:
            by_pick_type.append({"pick_type": pick_type, **_result_stats(group)})
    by_pick_type.sort(key=lambda item: item["samples"], reverse=True)

    failures: list[str] = []
    warnings: list[str] = []
    if all_stats["samples"] < min_samples:
        failures.append("not_enough_settled_predictions_for_gate")
    if high_conf["samples"] >= 20 and high_conf["win_rate"] < 65:
        failures.append("high_confidence_bucket_under_65_percent")
    if high_conf["samples"] >= 20 and high_conf["calibration_gap"] <= -10:
        failures.append("high_confidence_bucket_overconfident_by_10_plus_points")
    elif high_conf["samples"] >= 20 and high_conf["calibration_gap"] <= -6:
        warnings.append("high_confidence_bucket_overconfident_by_6_plus_points")
    if longshots["samples"] >= 10 and longshots["win_rate"] < 45:
        warnings.append("longshot_value_bucket_under_45_percent")
    for item in by_pick_type:
        if item["samples"] >= 20 and item["win_rate"] < 45:
            warnings.append(f"{item['pick_type']}_under_45_percent")

    return {
        "status": "success",
        "gate": "pass" if not failures else "fail",
        "can_deploy_model_change": not failures,
        "failures": failures,
        "warnings": warnings[:20],
        "summary": all_stats,
        "primary": _result_stats(primary_rows),
        "candidate": _result_stats(candidate_rows),
        "high_confidence_80_plus": high_conf,
        "longshot_value": longshots,
        "by_pick_type": by_pick_type,
        "note": "Stored-decision replay gate. Full raw-feature backtesting still requires historical feature snapshots.",
    }


def desk_observability(limit: int = 200) -> dict[str, Any]:
    """Operational desk view: breaks, pending work, risk log health."""
    _init_db()
    audit = prediction_system_audit(limit=max(20, min(limit, 1000)))
    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        risk_actions = conn.execute(
            """
            select decision_type, pick_type, selection, confidence, reason, contextual_json, audit_json, created_at
            from prediction_decision_log
            order by datetime(created_at) desc, id desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        ungraded_over_24h = conn.execute(
            """
            select count(*)
            from prediction_history
            where graded_at is null
              and datetime(created_at) < datetime('now', '-24 hours')
            """
        ).fetchone()[0]
        candidate_ungraded_over_24h = conn.execute(
            """
            select count(*)
            from prediction_candidate_history
            where graded_at is null
              and datetime(created_at) < datetime('now', '-24 hours')
            """
        ).fetchone()[0]

    recent_decisions = []
    for row in risk_actions:
        audit_json = _safe_json(row["audit_json"], {})
        recent_decisions.append({
            "decision_type": row["decision_type"],
            "pick_type": row["pick_type"],
            "selection": row["selection"],
            "confidence": row["confidence"],
            "reason": row["reason"],
            "risk_management": audit_json.get("risk_management") or {},
            "contextual": _safe_json(row["contextual_json"], {}),
            "created_at": row["created_at"],
        })

    severity = "red" if audit["issues"].get("stuck_jobs") or ungraded_over_24h > 20 else "amber" if ungraded_over_24h or candidate_ungraded_over_24h else "green"
    return {
        "status": "success",
        "desk_status": severity,
        "system_audit": audit,
        "ungraded_over_24h": ungraded_over_24h,
        "candidate_ungraded_over_24h": candidate_ungraded_over_24h,
        "recent_decisions": recent_decisions,
    }


def _graded_rows(limit: int = 5000) -> list[sqlite3.Row]:
    _init_db()
    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            select *
            from (
                select 'prediction_history' as source_table, id, match_id, league_name, country_name,
                       pick_type, selection, confidence, result, signals_json, created_at, graded_at
                from prediction_history
                where graded_at is not null and result in ('win', 'loss')
                union all
                select 'prediction_candidate_history' as source_table, id, match_id, league_name, country_name,
                       pick_type, selection, confidence, result, signals_json, created_at, graded_at
                from prediction_candidate_history
                where graded_at is not null and result in ('win', 'loss')
            )
            order by datetime(coalesce(graded_at, created_at)) desc, id desc
            limit ?
            """,
            (max(1, min(int(limit or 5000), 20000)),),
        ).fetchall()


def _blank_stats() -> dict[str, Any]:
    return {"samples": 0, "wins": 0, "losses": 0, "impact_sum": 0.0, "pick_types": defaultdict(int)}


def _tally(stats: dict[str, Any], result: str, row: sqlite3.Row, impact: float = 0.0) -> None:
    stats["samples"] += 1
    stats["wins"] += 1 if result == "win" else 0
    stats["losses"] += 1 if result == "loss" else 0
    stats["impact_sum"] += impact
    stats["pick_types"][str(row["pick_type"] or "unknown")] += 1


def _finish_stats(stats: dict[str, Any]) -> dict[str, Any]:
    samples = int(stats["samples"] or 0)
    wins = int(stats["wins"] or 0)
    win_rate = round(wins / samples * 100, 1) if samples else 0.0
    pick_types = stats.get("pick_types") or {}
    return {
        **stats,
        "pick_types": dict(sorted(pick_types.items(), key=lambda item: item[1], reverse=True)[:8]),
        "win_rate": win_rate,
        "avg_impact": round(float(stats.get("impact_sum") or 0) / samples, 3) if samples else 0.0,
    }


def _result_stats(rows: list[sqlite3.Row]) -> dict[str, Any]:
    samples = len(rows)
    wins = sum(1 for row in rows if row["result"] == "win")
    losses = sum(1 for row in rows if row["result"] == "loss")
    avg_conf = round(sum(float(row["confidence"] or 0) for row in rows) / samples, 1) if samples else 0.0
    win_rate = round(wins / samples * 100, 1) if samples else 0.0
    return {
        "samples": samples,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "avg_confidence": avg_conf,
        "calibration_gap": round(win_rate - avg_conf, 1) if samples else 0.0,
    }


def _safe_json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except Exception:
        return fallback


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None
