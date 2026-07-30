from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException

from app.db import db_conn
from app.db import DB_PATH
from app.league_memory import _init_db

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get("/live-prediction-gaps")
def live_prediction_gaps() -> dict[str, Any]:
    try:
        from app.enriched_prediction import prediction_readiness
        from app.live_retry_queue import list_active

        _init_db()
        gaps: list[dict[str, Any]] = []
        with db_conn(timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select mb.match_id, mb.sofascore_id, mb.period, mb.score_home, mb.score_away, mb.raw_enriched
                from match_buffer mb
                where mb.is_live = 1
                  and mb.raw_enriched is not null
                  and exists (
                      select 1 from prediction_history ph
                      where ph.match_id = mb.match_id
                        and coalesce(ph.prediction_mode, 'prematch') = 'prematch'
                  )
                  and not exists (
                      select 1 from prediction_history ph
                      where ph.match_id = mb.match_id
                        and coalesce(ph.prediction_mode, 'prematch') = 'live'
                  )
                order by mb.enriched_at desc
                limit 200
                """
            ).fetchall()
        for row in rows:
            try:
                doc = json.loads(row["raw_enriched"] or "{}")
            except Exception:
                doc = {}
            readiness = prediction_readiness(doc)
            gaps.append({
                "match_id": row["match_id"],
                "sofascore_id": row["sofascore_id"] or doc.get("sofascore_id"),
                "sportybet_id": doc.get("sportybet_id") or row["match_id"],
                "name": doc.get("name") or doc.get("sportybet_name"),
                "period": row["period"] or doc.get("period"),
                "score_home": row["score_home"] or doc.get("score_home"),
                "score_away": row["score_away"] or doc.get("score_away"),
                "prediction_readiness": readiness,
                "deferred_reason": readiness.get("deferred_reason"),
                "data_sources": doc.get("data_sources") or {},
                "data_source": doc.get("data_source") or readiness.get("data_source"),
            })
        retry_entries = list_active()
        return {"status": "success", "count": len(gaps), "gaps": gaps, "live_retry_queue": retry_entries}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/prediction-coverage")
def prediction_coverage() -> dict[str, Any]:
    """Show prediction coverage: which matches have predictions and which don't."""
    try:
        _init_db()
        with db_conn(timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            # Total matches in buffer
            total = conn.execute(
                "select count(*) as count from match_buffer where is_finished = 0"
            ).fetchone()["count"]

            # Matches with predictions
            with_preds = conn.execute(
                """
                select count(distinct match_id) as count
                from prediction_history
                where result is null
                  and datetime(created_at) >= datetime('now', '-24 hours')
                """
            ).fetchone()["count"]

            # Matches without predictions
            without_preds = total - with_preds

            # Coverage by league
            by_league = conn.execute(
                """
                select ph.league_name,
                       count(distinct ph.match_id) as total_matches,
                       count(distinct case when ph.result is null then ph.match_id end) as predicted_matches,
                       round(100.0 * count(distinct case when ph.result is null then ph.match_id end) /
                             nullif(count(distinct ph.match_id), 0), 1) as coverage_pct
                from prediction_history ph
                where datetime(ph.created_at) >= datetime('now', '-24 hours')
                group by ph.league_name
                order by coverage_pct desc
                """
            ).fetchall()

            # Matches needing enrichment
            needs_enrichment = conn.execute(
                """
                select count(*) as count
                from match_buffer mb
                where mb.is_finished = 0
                  and (mb.raw_enriched is null or mb.enriched_at is null
                       or datetime(mb.enriched_at) < datetime('now', '-6 hours'))
                """
            ).fetchone()["count"]

            # Matches needing prediction
            needs_prediction = conn.execute(
                """
                select count(*) as count
                from match_buffer mb
                where mb.is_finished = 0
                  and not exists (
                      select 1 from prediction_history ph
                      where ph.match_id = mb.match_id
                        and ph.result is null
                        and datetime(ph.created_at) >= datetime('now', '-24 hours')
                  )
                """
            ).fetchone()["count"]

            return {
                "status": "success",
                "total_matches_in_buffer": total,
                "matches_with_predictions_24h": with_preds,
                "matches_without_predictions": without_preds,
                "coverage_pct": round(100.0 * with_preds / total, 1) if total > 0 else 0,
                "needs_enrichment": needs_enrichment,
                "needs_prediction": needs_prediction,
                "by_league": [dict(row) for row in by_league],
            }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
