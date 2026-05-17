from __future__ import annotations

import sqlite3
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
    finished_matches.create_index("sportybet_id")
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


# ── Incidents: goals, cards, substitutions ────────────────────────────────────

def _parse_incidents(incidents: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Extract goal times, card events and substitutions from SofaScore incidents."""
    if not incidents:
        return {"goals": [], "yellow_cards": [], "red_cards": [], "substitutions": []}

    goals: list[dict] = []
    yellow_cards: list[dict] = []
    red_cards: list[dict] = []
    substitutions: list[dict] = []

    for inc in incidents:
        inc_type = (inc.get("incidentType") or inc.get("incident_type") or "").lower()
        minute = inc.get("time") or inc.get("minute")
        extra = inc.get("addedTime") or inc.get("added_time")
        player = (inc.get("player") or {}).get("name") or inc.get("playerName")
        team_side = inc.get("isHome") if inc.get("isHome") is not None else inc.get("is_home")
        side = "home" if team_side else "away"

        base = {"minute": minute, "added_time": extra, "player": player, "side": side}

        if inc_type == "goal":
            goals.append({
                **base,
                "type": inc.get("incidentClass") or inc.get("goalType") or "regular",
                "score_home": (inc.get("homeScore") or inc.get("home_score")),
                "score_away": (inc.get("awayScore") or inc.get("away_score")),
                "assist": (inc.get("assist1") or {}).get("name"),
            })
        elif inc_type == "card":
            card_class = (inc.get("incidentClass") or "").lower()
            entry = {**base, "card_type": card_class, "reason": inc.get("reason")}
            if "yellow" in card_class:
                yellow_cards.append(entry)
            elif "red" in card_class:
                red_cards.append(entry)
        elif inc_type == "substitution":
            substitutions.append({
                **base,
                "player_in": (inc.get("playerIn") or {}).get("name"),
                "player_out": (inc.get("playerOut") or {}).get("name"),
            })

    return {
        "goals": goals,
        "yellow_cards": yellow_cards,
        "red_cards": red_cards,
        "substitutions": substitutions,
    }


# ── Buffer → MongoDB flush ────────────────────────────────────────────────────

def flush_buffer_to_mongo(match_date: str | None = None) -> dict[str, Any]:
    """
    Upsert live/upcoming enriched buffer rows into MongoDB enriched_matches.
    Finished matches are never here — they are archived immediately on transition.
    """
    if not is_configured():
        return {"status": "skipped", "reason": "mongodb_not_configured"}

    from app.league_memory import DB_PATH, _init_db
    from app.market import get_movement
    import json

    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        clauses = ["raw_enriched is not null", "is_finished = 0"]
        params: list[Any] = []
        if match_date:
            clauses.append("match_date = ?")
            params.append(match_date)
        rows = conn.execute(
            f"""
            select match_id, match_date, is_finished, raw_enriched
            from match_buffer
            where {" and ".join(clauses)}
            order by start_time asc
            """,
            params,
        ).fetchall()

    flushed = errors = 0
    for row in rows:
        try:
            doc = json.loads(row["raw_enriched"])
            match_id = str(row["match_id"])

            # pull incidents from sofascore_detail
            detail = doc.get("sofascore_detail") or {}
            incidents_parsed = _parse_incidents(detail.get("incidents"))

            # pull odds movement history from SQLite
            movement = get_movement(match_id)

            # pull latest prediction from SQLite
            prediction = _latest_prediction_for_match(match_id)

            mongo_doc = {
                "sportybet_id":      match_id,
                "sofascore_id":      str(doc.get("sofascore_id") or ""),
                "match_date":        row["match_date"],
                "tournament":        doc.get("tournament"),
                "category":          doc.get("category"),
                "name":              doc.get("sportybet_name") or doc.get("name"),
                "home_team":         _team_name(doc, "home"),
                "away_team":         _team_name(doc, "away"),
                "start_time":        str(doc.get("start_time") or ""),
                "period":            doc.get("period") or "",
                "score":             doc.get("score"),
                "is_finished":       bool(row["is_finished"]),
                "venue":             doc.get("venue"),
                "enriched_at":       doc.get("enriched_at"),
                "updated_at":        datetime.now(timezone.utc).isoformat(),
                # vital match events
                "goals":             incidents_parsed["goals"],
                "yellow_cards":      incidents_parsed["yellow_cards"],
                "red_cards":         incidents_parsed["red_cards"],
                "substitutions":     incidents_parsed["substitutions"],
                # odds movement
                "odds_movement":     movement,
                # prediction
                "prediction":        prediction,
                # full enriched payload
                "raw":               doc,
            }

            enriched_matches.update_one(
                {"_id": match_id},
                {"$set": mongo_doc},
                upsert=True,
            )
            flushed += 1
        except Exception as exc:
            print(f"[mongo_flush] failed for {row['match_id']}: {exc}")
            errors += 1

    return {
        "status": "ok",
        "flushed": flushed,
        "errors": errors,
        "match_date": match_date,
    }


def _latest_prediction_for_match(match_id: str) -> dict[str, Any] | None:
    """Fetch the most recent prediction for a match from SQLite."""
    try:
        from app.league_memory import DB_PATH, _init_db
        import json
        _init_db()
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select pick_type, selection, confidence, reason, picks_json, signals_json, created_at
                from prediction_history
                where match_id = ?
                order by created_at desc
                limit 1
                """,
                (match_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "pick_type":  row["pick_type"],
            "selection":  row["selection"],
            "confidence": row["confidence"],
            "reason":     row["reason"],
            "picks":      json.loads(row["picks_json"] or "[]"),
            "signals":    json.loads(row["signals_json"] or "[]"),
            "created_at": row["created_at"],
        }
    except Exception:
        return None


# ── Immediate finished-match archival from buffer ─────────────────────────────

def archive_finished_match_from_buffer(match_id: str) -> bool:
    """
    Called the moment a match transitions to finished.
    Reads the enriched doc from the buffer, saves to MongoDB finished_matches
    with full odds progression + our prediction pick, then deletes the buffer row.
    Returns True if archived successfully.
    """
    if not is_configured():
        return False

    from app.league_memory import DB_PATH, _init_db
    from app.market import get_movement
    import json

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

    import json as _json
    doc = _json.loads(raw)
    detail = doc.get("sofascore_detail") or {}
    incidents_parsed = _parse_incidents(detail.get("incidents"))
    movement = get_movement(match_id)
    prediction = _latest_prediction_for_match(match_id)

    score = doc.get("score") or {}
    now = datetime.now(timezone.utc).isoformat()

    archive_doc = {
        "_id":            match_id,
        "source":         "sportybet",
        "match_id":       match_id,
        "sofascore_id":   str(doc.get("sofascore_id") or ""),
        "match_date":     row["match_date"],
        "tournament":     doc.get("tournament"),
        "category":       doc.get("category"),
        "name":           doc.get("sportybet_name") or doc.get("name"),
        "home_team":      _team_name(doc, "home"),
        "away_team":      _team_name(doc, "away"),
        "start_time":     str(doc.get("start_time") or ""),
        "venue":          doc.get("venue"),
        "score":          score,
        "final_home_goals": _to_int(score.get("home")),
        "final_away_goals": _to_int(score.get("away")),
        # vital match events
        "goals":          incidents_parsed["goals"],
        "yellow_cards":   incidents_parsed["yellow_cards"],
        "red_cards":      incidents_parsed["red_cards"],
        "substitutions":  incidents_parsed["substitutions"],
        # full odds progression from opening to close
        "odds_progression": movement,
        # our prediction pick
        "prediction":     prediction,
        "finished_at":    now,
        "updated_at":     now,
        "raw":            doc,
    }

    finished_matches.update_one(
        {"_id": match_id},
        {
            "$set": {k: v for k, v in archive_doc.items() if k != "finished_at"},
            "$setOnInsert": {"finished_at": now},
        },
        upsert=True,
    )

    # immediately remove from buffer — buffer is for live/upcoming only
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("delete from match_buffer where match_id = ?", (match_id,))
        conn.commit()

    return True


# ── Buffer cleanup (junk/stale rows only) ─────────────────────────────────────

def cleanup_buffer() -> dict[str, Any]:
    """
    Remove any finished rows still in the buffer (safety net — should already
    be gone via archive_finished_match_from_buffer) and never-enriched rows
    older than 1 day (matches that never got a SofaScore match).
    """
    from app.league_memory import DB_PATH, _init_db
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        r1 = conn.execute("delete from match_buffer where is_finished = 1")
        r2 = conn.execute(
            "delete from match_buffer where enriched_at is null and datetime(ingested_at) < datetime('now', '-1 day')"
        )
        conn.commit()
    return {
        "status": "ok",
        "deleted_finished": r1.rowcount,
        "deleted_stale_unenriched": r2.rowcount,
    }
