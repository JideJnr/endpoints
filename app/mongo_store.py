from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient

from app.config import get_settings

_client = None
_db = None


def is_configured() -> bool:
    return bool(get_settings().mongodb_uri)


def _get_db():
    global _client, _db
    if _db is None:
        settings = get_settings()
        if not settings.mongodb_uri:
            raise RuntimeError("MONGODB_URI is not configured")
        _client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=8000)
        _db = _client[settings.mongodb_db]
    return _db


def init_mongo() -> dict[str, Any]:
    if not is_configured():
        return {"configured": False}
    _get_db()["finished_matches"].create_index("match_date")
    return {"configured": True, "database": get_settings().mongodb_db}


# ── The only write: archive a finished match ──────────────────────────────────

def archive_finished_match_from_buffer(match_id: str) -> bool:
    """
    Called the moment a match transitions to finished.
    Stores ONE lean document per match in finished_matches, then deletes the buffer row.
    Nothing else is ever written to MongoDB.
    """
    if not is_configured():
        return False

    from app.league_memory import DB_PATH, _init_db
    from app.market import get_movement

    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select match_id, match_date, raw_enriched, raw_sporty from match_buffer where match_id = ?",
            (match_id,),
        ).fetchone()

    if not row:
        return False

    raw = row["raw_enriched"] or row["raw_sporty"]
    if not raw:
        return False

    doc = json.loads(raw)
    score = doc.get("score") or {}
    now = datetime.now(timezone.utc).isoformat()

    # lean odds movement — just opening/closing 1x2
    movement = get_movement(match_id) or {}
    snapshots = movement.get("snapshots", [])
    odds_open  = snapshots[0].get("odds_1x2")  if snapshots else None
    odds_close = snapshots[-1].get("odds_1x2") if snapshots else None

    # lean prediction — just the top pick
    prediction = _latest_prediction(match_id)

    archive_doc = {
        "_id":          match_id,
        "match_date":   row["match_date"],
        "name":         doc.get("sportybet_name") or doc.get("name"),
        "home_team":    _team(doc, "home"),
        "away_team":    _team(doc, "away"),
        "tournament":   _tournament_name(doc),
        "score":        {"home": _to_int(score.get("home")), "away": _to_int(score.get("away"))},
        "odds_open":    odds_open,
        "odds_close":   odds_close,
        "prediction":   prediction,
        "finished_at":  now,
    }

    _get_db()["finished_matches"].update_one(
        {"_id": match_id},
        {"$set": archive_doc},
        upsert=True,
    )

    # remove from buffer immediately
    from app.league_memory import DB_PATH as _DB
    with sqlite3.connect(_DB) as conn:
        conn.execute("delete from match_buffer where match_id = ?", (match_id,))
        conn.commit()

    return True


# ── Reads (used by platform router) ──────────────────────────────────────────

def list_finished_matches(match_date: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if not is_configured():
        return []
    query = {"match_date": match_date} if match_date else {}
    return list(_get_db()["finished_matches"].find(query, {"_id": 0}).limit(limit))


def mongo_status() -> dict[str, Any]:
    if not is_configured():
        return {"configured": False}
    db = _get_db()
    counts = {c: db[c].estimated_document_count() for c in db.list_collection_names()}
    return {"configured": True, "database": get_settings().mongodb_db, "collections": counts}


# ── Stubs kept so existing imports don't break ────────────────────────────────

def flush_buffer_to_mongo(match_date: str | None = None) -> dict[str, Any]:
    return {"status": "skipped", "reason": "only finished matches are stored in mongo"}

def cleanup_buffer() -> dict[str, Any]:
    from app.league_memory import DB_PATH, _init_db
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        r1 = conn.execute("delete from match_buffer where is_finished = 1")
        r2 = conn.execute(
            "delete from match_buffer where enriched_at is null and datetime(ingested_at) < datetime('now', '-1 day')"
        )
        conn.commit()
    return {"deleted_finished": r1.rowcount, "deleted_stale_unenriched": r2.rowcount}

def store_scheduled_matches(*args, **kwargs) -> int:
    return 0

def store_enriched_matches(*args, **kwargs) -> int:
    return 0

def get_enriched_match(match_id: str) -> dict[str, Any] | None:
    return None

def get_enriched_matches(match_date: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    return []

def save_odds_snapshot(*args, **kwargs) -> bool:
    return False

def save_finished_match(*args, **kwargs) -> bool:
    return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _team(doc: dict, side: str) -> str | None:
    team = doc.get(f"{side}_team")
    if isinstance(team, dict):
        return team.get("name")
    return team or None

def _tournament_name(doc: dict) -> str | None:
    t = doc.get("tournament")
    if isinstance(t, dict):
        return t.get("name")
    return t or None

def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _latest_prediction(match_id: str) -> dict[str, Any] | None:
    try:
        from app.league_memory import DB_PATH, _init_db
        _init_db()
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select pick_type, selection, confidence, reason from prediction_history where match_id = ? order by created_at desc limit 1",
                (match_id,),
            ).fetchone()
        if not row:
            return None
        return {"type": row["pick_type"], "selection": row["selection"], "confidence": row["confidence"], "reason": row["reason"]}
    except Exception:
        return None
