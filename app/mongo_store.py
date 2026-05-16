from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pymongo import DESCENDING, MongoClient

from app.config import get_settings


_client: MongoClient | None = None
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


class _LazyCollection:
    def __init__(self, name: str):
        self._name = name

    def __getattr__(self, item: str):
        return getattr(_get_db()[self._name], item)

    def __call__(self, *args, **kwargs):
        return _get_db()[self._name](*args, **kwargs)


finished_matches = _LazyCollection("finished_matches")
scheduled_matches = _LazyCollection("scheduled_matches")
enriched_matches = _LazyCollection("enriched_matches")
odds_snapshots = _LazyCollection("odds_snapshots")
predictions = _LazyCollection("predictions")
bot2_picks = _LazyCollection("bot2_picks")


def init_mongo() -> dict[str, Any]:
    if not is_configured():
        return {"configured": False, "indexes_created": False}

    finished_matches.create_index("match_date")
    finished_matches.create_index("source")
    finished_matches.create_index("league_key")
    finished_matches.create_index([("finished_at", DESCENDING)])
    scheduled_matches.create_index("match_date")
    enriched_matches.create_index("match_date")
    odds_snapshots.create_index("sportybet_id")
    odds_snapshots.create_index("match_date")
    predictions.create_index("match_date")
    bot2_picks.create_index("match_date")
    return {"configured": True, "indexes_created": True, "database": get_settings().mongodb_db}


def store_scheduled_matches(events: list[dict[str, Any]], match_date: str | None = None) -> int:
    if not is_configured() or not events:
        return 0
    for event in events:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        scheduled_matches.update_one(
            {"_id": event_id},
            {
                "$set": {
                    "name": event.get("name"),
                    "match_date": match_date or _match_date(event),
                    "raw": event,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
            upsert=True,
        )
    return len(events)


def store_enriched_matches(documents: list[dict[str, Any]]) -> int:
    if not is_configured() or not documents:
        return 0
    for doc in documents:
        match_id = str(doc.get("sportybet_id") or doc.get("id") or doc.get("match_id") or "")
        if not match_id:
            continue
        enriched_matches.update_one(
            {"_id": match_id},
            {
                "$set": {
                    "sofascore_id": str(doc.get("sofascore_id") or ""),
                    "match_date": doc.get("match_date"),
                    "tournament": doc.get("tournament"),
                    "category": doc.get("category"),
                    "start_time": str(doc.get("start_time") or ""),
                    "period": doc.get("period") or "",
                    "sportybet_name": doc.get("sportybet_name") or doc.get("name"),
                    "raw": doc,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
            upsert=True,
        )
    return len(documents)


def get_enriched_match(match_id: str) -> dict[str, Any] | None:
    if not is_configured():
        return None
    doc = enriched_matches.find_one({"_id": str(match_id)})
    return doc.get("raw") if doc else None


def get_enriched_matches(match_date: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    if not is_configured():
        return []
    query = {"match_date": match_date} if match_date else {}
    docs = enriched_matches.find(query).limit(limit)
    return [doc.get("raw") for doc in docs if doc.get("raw")]


def save_odds_snapshot(snapshot: dict[str, Any]) -> bool:
    if not is_configured():
        return False
    odds_snapshots.insert_one(snapshot)
    return True


def save_finished_match(source: str, match: dict[str, Any]) -> bool:
    if not is_configured():
        return False

    match_id = str(match.get("id") or match.get("match_id") or "")
    if not match_id:
        return False

    score = match.get("score") or {}
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "_id": f"{source}:{match_id}",
        "source": source,
        "match_id": match_id,
        "match_date": _match_date(match),
        "league_key": match.get("league_key"),
        "league_name": _league_name(match),
        "name": match.get("name"),
        "home_team": _team_name(match, "home"),
        "away_team": _team_name(match, "away"),
        "score": score,
        "final_home_goals": _to_int(score.get("home")),
        "final_away_goals": _to_int(score.get("away")),
        "status": match.get("status"),
        "start_time": match.get("start_time") or match.get("start_timestamp"),
        "finished_at": now,
        "updated_at": now,
        "raw": match,
    }

    finished_matches.update_one(
        {"_id": doc["_id"]},
        {"$set": {k: v for k, v in doc.items() if k != "finished_at"}, "$setOnInsert": {"finished_at": now}},
        upsert=True,
    )
    return True


def list_finished_matches(match_date: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if not is_configured():
        return []
    query = {"match_date": match_date} if match_date else {}
    docs = finished_matches.find(query, {"_id": 0, "raw": 0}).sort("finished_at", DESCENDING).limit(limit)
    return list(docs)


def mongo_status() -> dict[str, Any]:
    if not is_configured():
        return {"configured": False}
    db = _get_db()
    return {
        "configured": True,
        "database": get_settings().mongodb_db,
        "collections": db.list_collection_names(),
    }


def _league_name(match: dict[str, Any]) -> str | None:
    tournament = match.get("tournament")
    if isinstance(tournament, dict):
        return tournament.get("name")
    return tournament or match.get("league_name")


def _team_name(match: dict[str, Any], side: str) -> str | None:
    team = match.get(f"{side}_team")
    if isinstance(team, dict):
        return team.get("name")
    return team


def _match_date(match: dict[str, Any]) -> str | None:
    value = match.get("match_date")
    if value:
        return str(value)
    start = match.get("start_time") or match.get("start_timestamp")
    if not start:
        return None
    try:
        ts = int(start)
        if ts > 10_000_000_000:
            ts = ts // 1000
        return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        text = str(start)
        return text[:10] if len(text) >= 10 else None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
