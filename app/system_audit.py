from __future__ import annotations

import sqlite3
import time
from typing import Any

from app.db import db_conn
from app.buffer import get_buffer_stats, get_buffered_match
from app.enriched_prediction import prediction_readiness
from app.job_state import list_job_states
from app.db import DB_PATH
from app.league_memory import _init_db


def prediction_system_audit(limit: int = 200) -> dict[str, Any]:
    """Fast operational audit of the ingest -> enrich -> predict -> grade flow."""
    _init_db()
    limit = max(20, min(int(limit or 200), 1000))
    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        duplicate_buffer_rows = conn.execute(
            """
            select count(*)
            from match_buffer mb
            join future_match_buffer fb on fb.match_id = mb.match_id
            """
        ).fetchone()[0]
        pending_predictions = conn.execute(
            """
            select count(distinct match_id)
            from prediction_history
            where graded_at is null and pick_type != 'no_bet'
            """
        ).fetchone()[0]
        pending_candidates = conn.execute(
            """
            select count(distinct match_id)
            from prediction_candidate_history
            where graded_at is null
            """
        ).fetchone()[0]
        pending_decisions = conn.execute(
            """
            select count(distinct match_id)
            from prediction_decision_log
            where graded_at is null
            """
        ).fetchone()[0]
        recent_no_pick = conn.execute(
            """
            select count(*)
            from prediction_decision_log
            where decision_type in ('no_bet', 'deferred')
              and datetime(created_at) >= datetime('now', '-72 hours')
            """
        ).fetchone()[0]
        recent_rows = conn.execute(
            """
            select id, match_id, match_name, pick_type, selection, confidence, created_at
            from prediction_history
            where graded_at is null
              and pick_type != 'no_bet'
              and datetime(created_at) >= datetime('now', '-72 hours')
            order by datetime(created_at) desc, id desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        no_match_ready = conn.execute(
            """
            select count(*)
            from match_buffer
            where is_finished = 0
              and is_live = 0
              and json_extract(raw_enriched, '$.sofascore_match_status') = 'no_match'
              and coalesce(cast(json_extract(raw_enriched, '$.sofascore_retry_after_ts') as real), 0) <= strftime('%s','now')
            """
        ).fetchone()[0]

    unready_predictions = []
    checked_matches: set[str] = set()
    for row in recent_rows:
        match_id = str(row["match_id"] or "")
        if not match_id or match_id in checked_matches:
            continue
        checked_matches.add(match_id)
        doc = get_buffered_match(match_id)
        if not doc:
            continue
        readiness = prediction_readiness(doc)
        if not readiness.get("ready"):
            unready_predictions.append({
                "id": row["id"],
                "match_id": match_id,
                "match_name": row["match_name"],
                "pick_type": row["pick_type"],
                "selection": row["selection"],
                "confidence": row["confidence"],
                "created_at": row["created_at"],
                "missing": readiness.get("missing") or [],
            })

    jobs = list_job_states()
    stuck_jobs = [
        job for job in jobs
        if job.get("status") == "running" and _age_seconds(job.get("heartbeat_at")) > 900
    ]
    return {
        "status": "success",
        "buffer": get_buffer_stats(),
        "jobs": jobs,
        "issues": {
            "duplicate_buffer_rows": duplicate_buffer_rows,
            "pending_prediction_matches": pending_predictions,
            "pending_candidate_matches": pending_candidates,
            "pending_decision_matches": pending_decisions,
            "recent_no_pick_decisions": recent_no_pick,
            "expired_no_match_rows": no_match_ready,
            "historical_predictions_now_not_ready": len(unready_predictions),
            "stuck_jobs": len(stuck_jobs),
        },
        "samples": {
            "historical_predictions_now_not_ready": unready_predictions[:20],
            "stuck_jobs": stuck_jobs[:20],
        },
        "contract": {
            "prediction_current_view": "Only readiness=true buffer rows are shown as current predictions.",
            "history_policy": "prediction_history is append-only and used for grading/learning.",
            "decision_policy": "prediction_decision_log records every published, no-bet, and deferred decision for audit and no-pick learning.",
            "production_prediction_path": "Sporty ingest -> Sofa/Sporty enrichment -> prediction_readiness -> apply_prediction_state -> grading.",
        },
    }


def _age_seconds(value: Any) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime

        return max(0.0, time.time() - datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0.0
