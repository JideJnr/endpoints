from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException

from app.league_memory import DB_PATH, _init_db

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get("/live-prediction-gaps")
def live_prediction_gaps() -> dict[str, Any]:
    try:
        from app.enriched_prediction import prediction_readiness
        from app.live_retry_queue import list_active

        _init_db()
        gaps: list[dict[str, Any]] = []
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
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
