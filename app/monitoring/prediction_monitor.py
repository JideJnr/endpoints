from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from app.storage.db import db_conn
from app.utils.activity_log import record_activity
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db, get_grading_metrics, grade_betbuilder_history, grade_overdue_predictions


SNAPSHOT_KEEP_ROWS = 1000


def run_prediction_monitor(*, auto_correct: bool = True) -> dict[str, Any]:
    """Hourly closed-loop monitor for prediction quality.

    This layer settles overdue results, looks for repeated mismatch patterns,
    tracks accuracy drift, and applies conservative maintenance. It does not
    rewrite prediction history or force model strategy changes.
    """
    started = time.time()
    errors: list[str] = []
    corrections: list[dict[str, Any]] = []
    authority: dict[str, Any] = {"source": "prediction_monitor", "scope": "learning", "active": False}

    grading = _safe_call(
        "grade_overdue_predictions",
        lambda: grade_overdue_predictions(hours_after_kickoff=2, limit=700),
        errors,
    )
    betbuilder = _safe_call("grade_betbuilder_history", lambda: grade_betbuilder_history(limit=500), errors)
    metrics = _safe_call("get_grading_metrics", get_grading_metrics, errors)
    trend = _safe_call("performance_trend", _performance_trend, errors)
    mismatches = _safe_call("mismatch_report", _mismatch_report, errors)
    pipeline = _safe_call("pipeline_health", _pipeline_health, errors)

    graded_now = int((grading or {}).get("graded") or 0) + int((grading or {}).get("candidate_graded") or 0)
    if auto_correct:
        from app.scheduling.loop_authority import CorrectionAuthorityBusy, correction_authority

        try:
            with correction_authority("prediction_monitor", "learning", reason="grading-driven calibration and weight refresh") as lease:
                authority = {**lease, "active": True}
                if graded_now or _trend_is_degrading(trend) or int((mismatches or {}).get("recent_losses") or 0):
                    calibration = _safe_call("rebuild_calibration", _rebuild_calibration, errors)
                    if calibration:
                        corrections.append({"action": "rebuild_calibration", **calibration})

                    learning = _safe_call("optimise_ensemble_weights", _optimise_weights, errors)
                    if learning:
                        corrections.append({"action": "optimise_ensemble_weights", **learning})

                maintenance = _safe_call("memory_maintenance", _memory_maintenance, errors)
                if int((maintenance or {}).get("rows_changed") or 0):
                    corrections.append({"action": "memory_maintenance", **maintenance})
        except CorrectionAuthorityBusy as exc:
            authority = {
                "source": "prediction_monitor",
                "scope": exc.scope,
                "active": False,
                "blocked_by": exc.source or exc.owner,
            }
            errors.append(f"correction_authority_busy: {exc}")

    result = {
        "status": "ok" if not errors else "degraded",
        "mode": "auto_correct" if auto_correct else "observe",
        "duration_seconds": round(time.time() - started, 2),
        "grading": grading,
        "betbuilder": betbuilder,
        "metrics": metrics,
        "trend": trend,
        "mismatches": mismatches,
        "pipeline": pipeline,
        "corrections": corrections,
        "authority": authority,
        "errors": errors[:10],
        "principle": "settle truth first; learning corrections require one active authority lease; do not rewrite historical predictions",
    }
    _persist_monitor_snapshot(result)
    _record_monitor_activity(result)
    return result


def latest_prediction_monitor_snapshots(limit: int = 50) -> dict[str, Any]:
    _init_db()
    limit = max(1, min(int(limit or 50), 300))
    with db_conn(timeout=20) as conn:
        _init_monitor_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select created_at, status, mode, duration_seconds, metrics_json,
                   trend_json, mismatches_json, pipeline_json, corrections_json, errors_json
            from prediction_monitor_snapshots
            order by datetime(created_at) desc, id desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    snapshots = [_snapshot_row(row) for row in rows]
    return {
        "status": "success",
        "count": len(snapshots),
        "snapshots": snapshots,
        "summary": _snapshot_summary(snapshots),
    }


def _performance_trend() -> dict[str, Any]:
    _init_db()
    windows = [
        ("last_24h", "-24 hours", "+1 seconds"),
        ("previous_24h", "-48 hours", "-24 hours"),
        ("last_7d", "-7 days", "+1 seconds"),
        ("previous_7d", "-14 days", "-7 days"),
    ]
    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        by_window = {
            name: _window_stats(conn, start_modifier, end_modifier)
            for name, start_modifier, end_modifier in windows
        }
        by_type = conn.execute(
            """
            select pick_type,
                   count(*) as samples,
                   sum(case when result = 'win' then 1 else 0 end) as wins,
                   sum(case when result = 'loss' then 1 else 0 end) as losses
            from prediction_history
            where graded_at is not null
              and result in ('win', 'loss')
              and datetime(graded_at) >= datetime('now', '-7 days')
              and pick_type != 'no_bet'
            group by pick_type
            having count(*) >= 3
            order by samples desc
            limit 20
            """
        ).fetchall()

    current = by_window["last_24h"]
    previous = by_window["previous_24h"]
    delta = None
    if current["samples"] and previous["samples"]:
        delta = round(current["win_rate"] - previous["win_rate"], 3)
    return {
        "windows": by_window,
        "delta_24h_vs_previous": delta,
        "direction": _trend_direction(delta, current["samples"], previous["samples"]),
        "by_type_7d": [_rate_row(row) for row in by_type],
    }


def _mismatch_report(limit: int = 80) -> dict[str, Any]:
    _init_db()
    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        losses = conn.execute(
            """
            select id, match_id, match_name, league_name, country_name, pick_type,
                   selection, confidence, final_home, final_away, signals_json,
                   audit_json, grading_reason_json, created_at, graded_at
            from prediction_history
            where graded_at is not null
              and result = 'loss'
              and pick_type != 'no_bet'
              and datetime(graded_at) >= datetime('now', '-7 days')
            order by datetime(graded_at) desc, id desc
            limit ?
            """,
            (max(1, min(limit, 300)),),
        ).fetchall()
        repeated = conn.execute(
            """
            select pick_type, selection, count(*) as losses, avg(confidence) as avg_confidence
            from prediction_history
            where graded_at is not null
              and result = 'loss'
              and pick_type != 'no_bet'
              and datetime(graded_at) >= datetime('now', '-7 days')
            group by pick_type, selection
            having count(*) >= 2
            order by losses desc, avg_confidence desc
            limit 20
            """
        ).fetchall()

    samples = [_loss_sample(row) for row in losses]
    reason_counts: dict[str, int] = {}
    signal_counts: dict[str, int] = {}
    for sample in samples:
        for reason in sample.get("likely_reasons") or []:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        for signal in sample.get("signal_names") or []:
            signal_counts[signal] = signal_counts.get(signal, 0) + 1

    return {
        "recent_losses": len(samples),
        "likely_reason_counts": _top_counts(reason_counts),
        "loss_signal_counts": _top_counts(signal_counts),
        "repeated_losing_markets": [
            {
                "pick_type": row["pick_type"],
                "selection": row["selection"],
                "losses": row["losses"],
                "avg_confidence": round(float(row["avg_confidence"] or 0), 1),
            }
            for row in repeated
        ],
        "samples": samples[:20],
    }


def _pipeline_health() -> dict[str, Any]:
    _init_db()
    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        pending = conn.execute(
            """
            select count(distinct match_id) as count
            from prediction_history
            where graded_at is null and pick_type != 'no_bet'
            """
        ).fetchone()["count"]
        stale_pending = conn.execute(
            """
            select count(distinct match_id) as count
            from prediction_history
            where graded_at is null
              and pick_type != 'no_bet'
              and datetime(created_at) < datetime('now', '-30 hours')
            """
        ).fetchone()["count"]
        deferred_24h = conn.execute(
            """
            select count(*) as count
            from prediction_decision_log
            where decision_type = 'deferred'
              and datetime(created_at) >= datetime('now', '-24 hours')
            """
        ).fetchone()["count"]
        published_24h = conn.execute(
            """
            select count(*) as count
            from prediction_decision_log
            where decision_type = 'published'
              and datetime(created_at) >= datetime('now', '-24 hours')
            """
        ).fetchone()["count"]
    return {
        "pending_prediction_matches": pending or 0,
        "stale_pending_prediction_matches": stale_pending or 0,
        "deferred_decisions_24h": deferred_24h or 0,
        "published_decisions_24h": published_24h or 0,
        "deferred_ratio_24h": round((deferred_24h or 0) / max(1, (deferred_24h or 0) + (published_24h or 0)), 3),
    }


def _window_stats(conn: sqlite3.Connection, start_modifier: str, end_modifier: str) -> dict[str, Any]:
    row = conn.execute(
        """
        select count(*) as samples,
               sum(case when result = 'win' then 1 else 0 end) as wins,
               sum(case when result = 'loss' then 1 else 0 end) as losses,
               avg(confidence) as avg_confidence
        from prediction_history
        where graded_at is not null
          and result in ('win', 'loss')
          and pick_type != 'no_bet'
          and datetime(graded_at) >= datetime('now', ?)
          and datetime(graded_at) < datetime('now', ?)
        """,
        (start_modifier, end_modifier),
    ).fetchone()
    samples = int(row["samples"] or 0)
    wins = int(row["wins"] or 0)
    return {
        "samples": samples,
        "wins": wins,
        "losses": int(row["losses"] or 0),
        "win_rate": round(wins / samples, 3) if samples else 0.0,
        "avg_confidence": round(float(row["avg_confidence"] or 0), 1) if samples else None,
    }


def _loss_sample(row: sqlite3.Row) -> dict[str, Any]:
    signals = _loads(row["signals_json"], [])
    audit = _loads(row["audit_json"], {})
    grading_reason = _loads(row["grading_reason_json"], {})
    signal_names = [str(item.get("name")) for item in signals if isinstance(item, dict) and item.get("name")]
    likely = []
    confidence = int(row["confidence"] or 0)
    if confidence >= 75:
        likely.append("overconfident_pick")
    if not audit:
        likely.append("missing_prediction_audit")
    readiness = _audit_readiness(audit)
    if isinstance(readiness, dict):
        if readiness.get("assurance") in {"sportybet_live_signal", "sportybet_prematch_minimum", "sportybet_market_signal"}:
            likely.append("low_assurance_enrichment")
        if readiness.get("missing"):
            likely.append("readiness_gap")
    if any(name in signal_names for name in ("market_steam", "odds_progression", "odds_pattern")):
        likely.append("market_signal_misread")
    if row["pick_type"] in {"goals", "live_goals", "live_total_goals", "live_next_goal"}:
        likely.append("goal_market_miss")
    if row["pick_type"] in {"match_result", "double_chance", "ensemble_1x2", "value_bet"}:
        likely.append("side_market_miss")
    return {
        "id": row["id"],
        "match_id": row["match_id"],
        "match": row["match_name"],
        "league": row["league_name"],
        "country": row["country_name"],
        "pick_type": row["pick_type"],
        "selection": row["selection"],
        "confidence": confidence,
        "final_score": f"{row['final_home']}-{row['final_away']}",
        "graded_at": row["graded_at"],
        "likely_reasons": likely or ["unclassified_model_miss"],
        "signal_names": signal_names[:20],
        "grading_reason": grading_reason,
    }


def _rate_row(row: sqlite3.Row) -> dict[str, Any]:
    samples = int(row["samples"] or 0)
    wins = int(row["wins"] or 0)
    return {
        "pick_type": row["pick_type"],
        "samples": samples,
        "wins": wins,
        "losses": int(row["losses"] or 0),
        "win_rate": round(wins / samples, 3) if samples else 0.0,
    }


def _audit_readiness(audit: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(audit, dict):
        return {}
    legacy = (audit.get("data_quality") or {}).get("prediction_readiness")
    if isinstance(legacy, dict):
        return legacy
    enrichment = audit.get("enrichment")
    if not isinstance(enrichment, dict):
        return {}
    return {
        "assurance": enrichment.get("assurance"),
        "missing": enrichment.get("missing") or [],
        "minimum_enrichment_status": enrichment.get("minimum_enrichment_status"),
    }


def _trend_direction(delta: float | None, current_samples: int, previous_samples: int) -> str:
    if delta is None or current_samples < 5 or previous_samples < 5:
        return "insufficient_sample"
    if delta <= -0.08:
        return "degrading"
    if delta >= 0.08:
        return "improving"
    return "stable"


def _trend_is_degrading(trend: Any) -> bool:
    return isinstance(trend, dict) and trend.get("direction") == "degrading"


def _rebuild_calibration() -> dict[str, Any]:
    from app.enrichment.confidence_calibrator import rebuild_calibration

    return rebuild_calibration()


def _optimise_weights() -> dict[str, Any]:
    from app.models.weight_optimiser import optimise_ensemble_weights

    return optimise_ensemble_weights()


def _memory_maintenance() -> dict[str, Any]:
    from app.storage.league_memory import run_memory_maintenance

    return run_memory_maintenance(raw_retention_days=30, odds_retention_days=60)


def _safe_call(name: str, fn, errors: list[str]) -> Any:
    try:
        return fn()
    except Exception as exc:
        errors.append(f"{name}: {exc}")
        return {}


def _persist_monitor_snapshot(result: dict[str, Any]) -> None:
    try:
        _init_db()
        with db_conn(timeout=20) as conn:
            _init_monitor_table(conn)
            conn.execute(
                """
                insert into prediction_monitor_snapshots (
                    created_at, status, mode, duration_seconds, metrics_json,
                    trend_json, mismatches_json, pipeline_json, corrections_json, errors_json
                ) values (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.get("status"),
                    result.get("mode"),
                    result.get("duration_seconds"),
                    json.dumps(result.get("metrics") or {}, default=str),
                    json.dumps(result.get("trend") or {}, default=str),
                    json.dumps(result.get("mismatches") or {}, default=str),
                    json.dumps(result.get("pipeline") or {}, default=str),
                    json.dumps(result.get("corrections") or [], default=str),
                    json.dumps(result.get("errors") or [], default=str),
                ),
            )
            conn.execute(
                """
                delete from prediction_monitor_snapshots
                where id not in (
                    select id
                    from prediction_monitor_snapshots
                    order by datetime(created_at) desc, id desc
                    limit ?
                )
                """,
                (SNAPSHOT_KEEP_ROWS,),
            )
            conn.commit()
    except Exception:
        pass


def _init_monitor_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists prediction_monitor_snapshots (
            id integer primary key autoincrement,
            created_at text not null,
            status text,
            mode text,
            duration_seconds real,
            metrics_json text not null default '{}',
            trend_json text not null default '{}',
            mismatches_json text not null default '{}',
            pipeline_json text not null default '{}',
            corrections_json text not null default '[]',
            errors_json text not null default '[]'
        )
        """
    )
    conn.execute("create index if not exists idx_prediction_monitor_created on prediction_monitor_snapshots(created_at)")


def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "created_at": row["created_at"],
        "status": row["status"],
        "mode": row["mode"],
        "duration_seconds": row["duration_seconds"],
        "metrics": _loads(row["metrics_json"], {}),
        "trend": _loads(row["trend_json"], {}),
        "mismatches": _loads(row["mismatches_json"], {}),
        "pipeline": _loads(row["pipeline_json"], {}),
        "corrections": _loads(row["corrections_json"], []),
        "errors": _loads(row["errors_json"], []),
    }


def _snapshot_summary(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    if not snapshots:
        return {}
    latest = snapshots[0]
    degrading = sum(1 for item in snapshots if (item.get("trend") or {}).get("direction") == "degrading")
    corrections = sum(len(item.get("corrections") or []) for item in snapshots)
    return {
        "latest_status": latest.get("status"),
        "latest_created_at": latest.get("created_at"),
        "latest_win_percent": (latest.get("metrics") or {}).get("win_percent"),
        "latest_trend": (latest.get("trend") or {}).get("direction"),
        "latest_pipeline": latest.get("pipeline") or {},
        "degrading_snapshots": degrading,
        "corrections_recorded": corrections,
    }


def _top_counts(counts: dict[str, int], limit: int = 10) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _record_monitor_activity(result: dict[str, Any]) -> None:
    try:
        trend = (result.get("trend") or {}).get("direction")
        mismatches = (result.get("mismatches") or {}).get("recent_losses")
        corrections = len(result.get("corrections") or [])
        record_activity(
            f"Prediction monitor pass: trend={trend}, losses={mismatches}, corrections={corrections}",
            job="prediction_monitor",
            status="ok" if result.get("status") == "ok" else "error",
            details={
                "trend": result.get("trend"),
                "mismatches": result.get("mismatches"),
                "pipeline": result.get("pipeline"),
                "corrections": result.get("corrections"),
                "errors": result.get("errors"),
            },
        )
    except Exception:
        pass

