from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.db import db_conn
from app.buffer import get_buffered_match
from app.enriched_prediction import prediction_readiness
from app.db import DB_PATH
from app.league_memory import _init_db


def list_recent_dashboard_predictions(hours: int = 36, limit: int = 800) -> list[dict[str, Any]]:
    """Recent ungraded rows that are still eligible for current-pick views."""
    _init_db()
    try:
        with db_conn(timeout=20) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select id, source, match_id, match_name, league_name, country_name,
                       pick_type, selection, confidence, reason, signals_json,
                       picks_json, created_at, result, graded_at
                from prediction_history
                where created_at >= datetime('now', ?)
                  and graded_at is null
                  and pick_type != 'no_bet'
                order by created_at desc
                limit ?
                """,
                (f"-{int(hours)} hours", int(limit)),
            ).fetchall()
    except Exception:
        return []

    predictions: list[dict[str, Any]] = []
    seen_matches: set[str] = set()
    for row in rows:
        match_id = str(row["match_id"] or "")
        if not match_id or match_id in seen_matches:
            continue
        doc = get_buffered_match(match_id)
        if not doc:
            continue
        readiness = prediction_readiness(doc)
        if not readiness.get("ready"):
            continue
        seen_matches.add(match_id)
        picks = _loads(row["picks_json"], [])
        signals = _loads(row["signals_json"], [])
        stored_best = picks[0] if picks else {}
        predictions.append({
            "id": row["id"],
            "source": row["source"],
            "match_id": row["match_id"],
            "match_name": row["match_name"],
            "league_name": row["league_name"],
            "country_name": row["country_name"],
            "best_pick": {
                **stored_best,
                "type": stored_best.get("type") or row["pick_type"],
                "selection": stored_best.get("selection") or row["selection"],
                "confidence": stored_best.get("confidence") or row["confidence"],
                "reason": stored_best.get("reason") or row["reason"],
            },
            "signals": signals,
            "picks": picks,
            "prediction_readiness": readiness,
            "result": row["result"],
            "graded_at": row["graded_at"],
            "created_at": row["created_at"],
        })
    return predictions


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return default
