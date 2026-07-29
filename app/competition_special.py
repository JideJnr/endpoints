from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.league_memory import DB_PATH, _init_db
from app.match_state import classify_match_state


DEFAULT_WORLD_CUP = {
    "key": "world-cup-2026",
    "name": "FIFA World Cup 2026",
    "unique_tournament_id": 16,
    "season_id": 58210,
    "start_date": "2026-06-11",
    "end_date": "2026-07-19",
}

# Curated, stable SofaScore unique-tournament identifiers.  The catalogue is
# intentionally configuration data rather than prediction logic: each entry
# uses the same SofaScore-only enrichment and prediction lane below.
TOP_30_COMPETITIONS: tuple[dict[str, Any], ...] = (
    {"key": "premier-league", "name": "Premier League", "unique_tournament_id": 17},
    {"key": "la-liga", "name": "LaLiga", "unique_tournament_id": 8},
    {"key": "serie-a", "name": "Serie A", "unique_tournament_id": 23},
    {"key": "bundesliga", "name": "Bundesliga", "unique_tournament_id": 35},
    {"key": "ligue-1", "name": "Ligue 1", "unique_tournament_id": 34},
    {"key": "champions-league", "name": "UEFA Champions League", "unique_tournament_id": 7},
    {"key": "europa-league", "name": "UEFA Europa League", "unique_tournament_id": 679},
    {"key": "conference-league", "name": "UEFA Conference League", "unique_tournament_id": 329},
    {"key": "championship", "name": "EFL Championship", "unique_tournament_id": 37},
    {"key": "eredivisie", "name": "Eredivisie", "unique_tournament_id": 44},
    {"key": "primeira-liga", "name": "Primeira Liga", "unique_tournament_id": 238},
    {"key": "super-lig", "name": "Süper Lig", "unique_tournament_id": 52},
    {"key": "mls", "name": "Major League Soccer", "unique_tournament_id": 242},
    {"key": "brasileirao", "name": "Brasileirão Série A", "unique_tournament_id": 325},
    {"key": "argentine-primera", "name": "Argentine Primera División", "unique_tournament_id": 390},
    {"key": "liga-mx", "name": "Liga MX", "unique_tournament_id": 406},
    {"key": "saudi-pro-league", "name": "Saudi Pro League", "unique_tournament_id": 203},
    {"key": "j1-league", "name": "J1 League", "unique_tournament_id": 98},
    {"key": "k-league-1", "name": "K League 1", "unique_tournament_id": 116},
    {"key": "liga-profesional", "name": "Liga Profesional Argentina", "unique_tournament_id": 155},
    {"key": "copa-libertadores", "name": "Copa Libertadores", "unique_tournament_id": 384},
    {"key": "copa-sudamericana", "name": "Copa Sudamericana", "unique_tournament_id": 480},
    {"key": "belgian-pro-league", "name": "Belgian Pro League", "unique_tournament_id": 38},
    {"key": "scottish-premiership", "name": "Scottish Premiership", "unique_tournament_id": 36},
    {"key": "swiss-super-league", "name": "Swiss Super League", "unique_tournament_id": 215},
    {"key": "austrian-bundesliga", "name": "Austrian Bundesliga", "unique_tournament_id": 45},
    {"key": "danish-superliga", "name": "Danish Superliga", "unique_tournament_id": 39},
    {"key": "eliteserien", "name": "Eliteserien", "unique_tournament_id": 20},
    {"key": "allsvenskan", "name": "Allsvenskan", "unique_tournament_id": 67},
    {"key": "colombia-primera-a", "name": "Categoría Primera A", "unique_tournament_id": 11539},
)
_CATALOGUE_BY_KEY = {entry["key"]: entry for entry in TOP_30_COMPETITIONS}


def apply_known_competition_context(doc: dict[str, Any]) -> dict[str, Any]:
    """Attach competition-special intelligence to any ordinary match document.

    This keeps manual match matching and DeepSeek analysis on the same known-league
    footing as fixtures that entered through the dedicated competition lane.
    """
    event = doc.get("sofascore_event") if isinstance(doc.get("sofascore_event"), dict) else {}
    detail = doc.get("sofascore_detail") if isinstance(doc.get("sofascore_detail"), dict) else {}
    tournament = event.get("tournament") if isinstance(event.get("tournament"), dict) else {}
    tournament_id = tournament.get("tournament_id") or doc.get("unique_tournament_id")
    name = str(tournament.get("name") or doc.get("tournament") or "")
    entry = next((item for item in TOP_30_COMPETITIONS if str(item["unique_tournament_id"]) == str(tournament_id)), None)
    if not entry:
        normalized = _normalise_competition_name(name)
        entry = next((item for item in TOP_30_COMPETITIONS if _normalise_competition_name(item["name"]) == normalized), None)
    if not entry:
        doc["known_competition"] = {"known": False, "provider": "sofascore", "tournament": name or None}
        return doc

    intelligence = _competition_intelligence_context(entry["key"], event, detail, doc)
    context = {
        "known": True,
        "provider": "sofascore",
        "key": entry["key"],
        "name": entry["name"],
        "unique_tournament_id": entry["unique_tournament_id"],
        "importance": _match_importance_context(entry["key"], event),
        "intelligence": intelligence,
    }
    doc["known_competition"] = context
    doc["competition_special"] = {"key": entry["key"], "name": entry["name"], "source": "known_competition_match"}
    doc["competition_intelligence"] = intelligence
    doc["team_strength_context"] = intelligence.get("team_strength")
    doc["table_context"] = intelligence.get("table")
    return doc


def _normalise_competition_name(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


def list_top_competitions() -> list[dict[str, Any]]:
    """Return the complete curated catalogue with persisted enablement."""
    return [get_competition_settings(entry["key"]) for entry in TOP_30_COMPETITIONS]


def _catalogue_default(key: str) -> dict[str, Any]:
    if key == DEFAULT_WORLD_CUP["key"]:
        return DEFAULT_WORLD_CUP
    entry = _CATALOGUE_BY_KEY.get(key)
    if entry:
        # League fixtures are rolling, so do not inherit the World Cup dates.
        return {**entry, "season_id": None, "start_date": "", "end_date": ""}
    return {"key": key, "name": key, "unique_tournament_id": 0, "season_id": None, "start_date": "", "end_date": ""}


def init_competition_tables(conn: sqlite3.Connection) -> None:
    conn.execute("pragma busy_timeout = 30000")
    try:
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma synchronous = normal")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        create table if not exists competition_special_settings (
            key text primary key,
            name text not null,
            enabled integer not null default 0,
            unique_tournament_id integer not null,
            season_id integer,
            start_date text,
            end_date text,
            metadata_json text not null default '{}',
            updated_at text not null default current_timestamp
        )
        """
    )
    conn.execute(
        """
        create table if not exists competition_special_buffer (
            competition_key text not null,
            match_id text not null,
            match_date text,
            group_name text,
            round_name text,
            name text,
            start_time integer,
            status text,
            score_home text,
            score_away text,
            enriched_at text,
            predicted_at text,
            raw_event text not null,
            raw_detail text,
            prediction_json text,
            importance_context_json text not null default '{}',
            primary key (competition_key, match_id)
        )
        """
    )
    _ensure_column(conn, "competition_special_buffer", "importance_context_json", "text not null default '{}'")
    conn.execute("create index if not exists idx_comp_special_date on competition_special_buffer(competition_key, match_date)")
    conn.execute("create index if not exists idx_comp_special_start on competition_special_buffer(competition_key, start_time)")


def get_competition_settings(key: str = "world-cup-2026") -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        _ensure_catalogue_settings(conn)
        row = conn.execute(
            "select * from competition_special_settings where key = ?",
            (key,),
        ).fetchone()
    return _settings_row(row) if row else {**_catalogue_default(key), "enabled": False, "metadata": {}}


def update_competition_settings(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_competition_settings(key)
    default = _catalogue_default(key)
    updated = {
        **current,
        "enabled": bool(payload.get("enabled", current.get("enabled"))),
        "name": str(payload.get("name") or current.get("name") or default["name"]),
        "unique_tournament_id": int(payload.get("unique_tournament_id") or current.get("unique_tournament_id") or default["unique_tournament_id"]),
        "season_id": _optional_int(payload.get("season_id", current.get("season_id"))),
        "start_date": str(payload.get("start_date") if payload.get("start_date") is not None else current.get("start_date") or ""),
        "end_date": str(payload.get("end_date") if payload.get("end_date") is not None else current.get("end_date") or ""),
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else current.get("metadata") or {},
    }
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        init_competition_tables(conn)
        conn.execute(
            """
            insert into competition_special_settings
                (key, name, enabled, unique_tournament_id, season_id, start_date, end_date, metadata_json, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(key) do update set
                name = excluded.name,
                enabled = excluded.enabled,
                unique_tournament_id = excluded.unique_tournament_id,
                season_id = excluded.season_id,
                start_date = excluded.start_date,
                end_date = excluded.end_date,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                key,
                updated["name"],
                1 if updated["enabled"] else 0,
                updated["unique_tournament_id"],
                updated["season_id"],
                updated["start_date"],
                updated["end_date"],
                json.dumps(updated["metadata"]),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    return get_competition_settings(key)


def sync_competition_fixtures(
    key: str = "world-cup-2026",
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    limit_days: int = 60,
) -> dict[str, Any]:
    from app.sofascore_client import fetch_scheduled_events

    settings = get_competition_settings(key)
    tournament_id = int(settings.get("unique_tournament_id") or _catalogue_default(key)["unique_tournament_id"])
    start = _parse_date(start_date or settings.get("start_date") or date.today().isoformat())
    # For leagues (no end_date configured), default to 7 days ahead so future
    # fixtures are always pulled in automatically.
    configured_end = settings.get("end_date") or ""
    end = _parse_date(end_date or configured_end or (date.today() + timedelta(days=7)).isoformat())
    if end < start:
        end = start
    days = min(max((end - start).days + 1, 1), max(1, limit_days))
    # Only filter by season_id when one is explicitly configured.
    required_season_id = str(settings.get("season_id") or "").strip()

    stored = 0
    fetched = 0
    rejected = 0
    errors: list[dict[str, str]] = []
    cursor_candidate: str | None = None
    cursor_blocked = False
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        init_competition_tables(conn)
        _ensure_catalogue_settings(conn)
        for offset in range(days):
            match_date = (start + timedelta(days=offset)).isoformat()
            try:
                events = fetch_scheduled_events(match_date, tournament_id=tournament_id)
            except Exception as exc:
                errors.append({"date": match_date, "error": str(exc)})
                cursor_blocked = True
                continue
            if not cursor_blocked:
                cursor_candidate = match_date
            fetched += len(events)
            for event in events:
                # SofaScore occasionally returns an event list for a stale or
                # remapped tournament route. Never store it under the requested
                # competition unless its unique tournament identity agrees.
                if _event_unique_tournament_id(event) != tournament_id:
                    rejected += 1
                    continue
                # Skip season filter when no season_id is configured (rolling leagues)
                if required_season_id and str(event.get("season_id") or "").strip() != required_season_id:
                    continue
                _upsert_competition_event(conn, key, event, match_date)
                stored += 1
        conn.commit()
    scanned_end = (start + timedelta(days=days - 1)).isoformat()
    if cursor_candidate:
        _mark_sync_cursor(key, cursor_candidate)
    return {
        "status": "success",
        "competition": key,
        "date_range": {"start": start.isoformat(), "end": scanned_end},
        "fetched": fetched,
        "stored": stored,
        "rejected_wrong_tournament": rejected,
        "mirrored_to_main_buffer": stored,
        "cursor_advanced_to": cursor_candidate,
        "cursor_blocked": cursor_blocked,
        "errors": errors,
    }


def list_competition_buffer(key: str = "world-cup-2026", limit: int = 200) -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        _ensure_catalogue_settings(conn)
        rows = conn.execute(
            """
            select * from competition_special_buffer
            where competition_key = ?
            order by start_time asc
            limit ?
            """,
            (key, limit),
        ).fetchall()
    matches = [_buffer_row(row) for row in rows]
    return {
        "status": "success",
        "competition": get_competition_settings(key),
        "count": len(matches),
        "summary": _competition_summary(matches),
        "matches": matches,
    }


def competition_status(key: str = "world-cup-2026") -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        init_competition_tables(conn)
        _ensure_catalogue_settings(conn)
        row = conn.execute(
            """
            select
              count(*) as total,
              sum(case when raw_detail is not null then 1 else 0 end) as enriched,
              sum(case when prediction_json is not null then 1 else 0 end) as predicted,
              min(match_date) as first_match_date,
              max(match_date) as last_match_date,
              max(enriched_at) as last_enriched_at,
              max(predicted_at) as last_predicted_at
            from competition_special_buffer
            where competition_key = ?
            """,
            (key,),
        ).fetchone()
    return {
        "status": "success",
        "competition": get_competition_settings(key),
        "buffer": {
            "total": int(row[0] or 0),
            "enriched": int(row[1] or 0),
            "predicted": int(row[2] or 0),
            "first_match_date": row[3],
            "last_match_date": row[4],
            "last_enriched_at": row[5],
            "last_predicted_at": row[6],
        },
    }


def enrich_predict_competition(key: str = "world-cup-2026", limit: int = 12, allow_repeat: bool = False) -> dict[str, Any]:
    from app.enriched_prediction import prediction_readiness
    from app.prediction_flow import apply_prediction_state
    from app.sofascore_client import fetch_event_detail, fetch_event

    _init_db()
    mirrored = ensure_competition_main_buffer(key)
    processed = enriched = predicted = deferred = errors = 0
    items: list[dict[str, Any]] = []
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        rows = conn.execute(
            """
            select * from competition_special_buffer
            where competition_key = ?
              and (
                raw_detail is null
                or prediction_json is null
              or enriched_at < datetime('now', '-10 minutes')
              )
            order by
              case when status in ('inprogress', '1st half', '2nd half', 'halftime') then 0 else 1 end asc,
              case when prediction_json is null then 0 else 1 end asc,
              case when raw_detail is null then 0 else 1 end asc,
              start_time asc
            limit ?
            """,
            (key, limit),
        ).fetchall()

    for row in rows:
        processed += 1
        event = json.loads(row["raw_event"] or "{}")
        try:
            fresh_event = fetch_event(event.get("id")) or event
            detail = fetch_event_detail(fresh_event)
            doc = _competition_doc(key, fresh_event, detail)
            readiness = prediction_readiness(doc)
            state = apply_prediction_state(
                doc,
                match_id=f"competition:{key}:{fresh_event.get('id')}",
                match_date=doc.get("match_date"),
                source=f"competition_special:{key}",
                allow_repeat=allow_repeat,
            )
            prediction = state.get("prediction")
            if state.get("status") == "predicted":
                predicted += 1
            elif state.get("status") == "deferred":
                deferred += 1
            else:
                errors += 1 if state.get("status") == "error" else 0
            enriched += 1
            _save_competition_detail(key, fresh_event, detail, prediction, readiness)
            items.append({
                "match_id": fresh_event.get("id"),
                "match": fresh_event.get("name"),
                "readiness": readiness,
                "state": state.get("status"),
                "error": state.get("error") or state.get("message") if state.get("status") == "error" else None,
                "prediction": prediction,
            })
        except Exception as exc:
            errors += 1
            items.append({"match_id": event.get("id"), "match": event.get("name"), "state": "error", "error": str(exc)})
    return {
        "status": "success",
        "competition": key,
        "processed": processed,
        "mirrored_to_main_buffer": mirrored,
        "enriched": enriched,
        "predicted": predicted,
        "deferred": deferred,
        "errors": errors,
        "matches": items,
    }


def ensure_competition_main_buffer(key: str = "world-cup-2026") -> int:
    """Backfill/mirror dedicated competition rows into the normal enrichment buffer."""
    _init_db()
    mirrored = 0
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        rows = conn.execute(
            """
            select competition_key, raw_event, match_date, importance_context_json, raw_detail
            from competition_special_buffer
            where competition_key = ?
            """,
            (key,),
        ).fetchall()
        for row in rows:
            event = json.loads(row["raw_event"] or "{}")
            importance = json.loads(row["importance_context_json"] or "{}") or _match_importance_context(key, event)
            detail = json.loads(row["raw_detail"] or "{}") if row["raw_detail"] else {}
            enriched_doc = _competition_doc(key, event, detail) if detail else None
            _mirror_competition_event_to_main_buffer(
                conn,
                key,
                event,
                row["match_date"] or _event_match_date(event, date.today().isoformat()),
                importance,
                enriched_doc=enriched_doc,
            )
            mirrored += 1
        conn.commit()
    return mirrored


def run_competition_special_cycle(key: str = "world-cup-2026") -> dict[str, Any]:
    settings = get_competition_settings(key)
    if not settings.get("enabled"):
        return {"status": "idle", "competition": key, "enabled": False}
    configured_start = _parse_date(settings.get("start_date") or date.today().isoformat())
    # For rolling leagues end_date is empty — use a 14-day rolling horizon.
    configured_end_raw = settings.get("end_date") or ""
    configured_end = _parse_date(configured_end_raw) if configured_end_raw else date.today() + timedelta(days=14)
    loaded_until = _sync_cursor(key) or _loaded_until(key)
    if loaded_until and loaded_until < configured_end:
        window_start = loaded_until + timedelta(days=1)
    else:
        window_start = max(date.today(), configured_start)
    # Continuous lane: keep a rolling 7-day fixture window fresh, then enrich
    # and predict the highest-priority competition matches.
    window_end = min(configured_end, window_start + timedelta(days=7))
    sync = sync_competition_fixtures(
        key,
        start_date=window_start.isoformat(),
        end_date=window_end.isoformat(),
        limit_days=8,
    )
    mirrored = ensure_competition_main_buffer(key)
    enrich = enrich_predict_competition(key, limit=8)
    refresh = refresh_competition_context(key, limit=12)
    return {"status": "success", "competition": key, "sync": sync, "mirrored": mirrored, "enrich": enrich, "refresh": refresh}


def run_enabled_competition_cycles(limit: int = 31) -> dict[str, Any]:
    """Run the rolling lane for enabled top-30 entries and the existing World Cup special."""
    settings = [get_competition_settings(DEFAULT_WORLD_CUP["key"]), *list_top_competitions()]
    enabled = [item for item in settings if item.get("enabled")][:max(1, limit)]
    results = [run_competition_special_cycle(item["key"]) for item in enabled]
    return {"status": "success", "processed": len(results), "enabled": len(enabled), "competitions": results}


def refresh_competition_context(key: str = "world-cup-2026", limit: int = 12) -> dict[str, Any]:
    """Continuously refresh table/team-strength/odds context for competition rows."""
    from app.sofascore_client import fetch_event, fetch_event_detail

    _init_db()
    refreshed = errors = 0
    items: list[dict[str, Any]] = []
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        rows = conn.execute(
            """
            select *
            from competition_special_buffer
            where competition_key = ?
              and status not in ('finished', 'cancelled', 'postponed')
              and (
                enriched_at is null
                or enriched_at < datetime('now', '-10 minutes')
                or json_extract(raw_detail, '$.competition_intelligence') is null
              )
            order by
              case when status in ('inprogress', '1st half', '2nd half', 'halftime') then 0 else 1 end asc,
              start_time asc
            limit ?
            """,
            (key, limit),
        ).fetchall()

    for row in rows:
        event = json.loads(row["raw_event"] or "{}")
        try:
            fresh_event = fetch_event(event.get("id")) or event
            detail = fetch_event_detail(fresh_event)
            readiness = (json.loads(row["raw_detail"] or "{}").get("prediction_readiness") if row["raw_detail"] else {}) or {}
            _save_competition_detail(key, fresh_event, detail, None, readiness)
            refreshed += 1
            items.append({"match_id": fresh_event.get("id"), "match": fresh_event.get("name"), "status": "refreshed"})
        except Exception as exc:
            errors += 1
            items.append({"match_id": event.get("id"), "match": event.get("name"), "status": "error", "error": str(exc)})
    return {"status": "success", "competition": key, "refreshed": refreshed, "errors": errors, "matches": items}


def _ensure_catalogue_settings(conn: sqlite3.Connection) -> None:
    """Seed catalogue rows once, preserving all operator configuration."""
    for default in (DEFAULT_WORLD_CUP, *TOP_30_COMPETITIONS):
        conn.execute(
            """
            insert into competition_special_settings
                (key, name, enabled, unique_tournament_id, season_id, start_date, end_date, metadata_json)
            values (?, ?, 1, ?, ?, ?, ?, ?)
            on conflict(key) do nothing
            """,
            (
                default["key"], default["name"], default["unique_tournament_id"],
                default.get("season_id"), default.get("start_date", ""), default.get("end_date", ""),
                json.dumps({"source": "sofascore", "mode": "competition_special"}),
            ),
        )
    # Correct two historic catalogue IDs without overwriting any other operator
    # configuration. MLS was previously pointed at 955, while 242 is its
    # SofaScore unique-tournament ID; Brasileirão Série A is 325.
    conn.execute("""update competition_special_settings
                    set unique_tournament_id = 242, updated_at = current_timestamp
                    where key = 'mls' and unique_tournament_id = 955""")
    conn.execute("""update competition_special_settings
                    set unique_tournament_id = 325, updated_at = current_timestamp
                    where key = 'brasileirao' and unique_tournament_id = 242""")
    conn.execute("""update competition_special_settings
                    set unique_tournament_id = 52, updated_at = current_timestamp
                    where key = 'super-lig' and unique_tournament_id = 325""")


def _event_unique_tournament_id(event: dict[str, Any]) -> int | None:
    """Extract SofaScore's stable unique-tournament ID from parsed/raw events."""
    tournament = event.get("tournament") if isinstance(event.get("tournament"), dict) else {}
    try:
        if tournament.get("id") is not None:
            return int(tournament["id"])
    except (TypeError, ValueError):
        pass
    raw = event.get("raw_event") if isinstance(event.get("raw_event"), dict) else {}
    unique = ((raw.get("tournament") or {}).get("uniqueTournament") or {}) if raw else {}
    try:
        return int(unique.get("id")) if unique.get("id") is not None else None
    except (TypeError, ValueError):
        return None


def purge_misclassified_competition_rows() -> dict[str, int]:
    """Remove regenerable rows created by the historic Brasileirão→MLS mix-up."""
    _init_db()
    removed_special = removed_main = 0
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        rows = conn.execute("""select match_id, raw_event from competition_special_buffer
                               where competition_key = 'brasileirao'""").fetchall()
        wrong_ids = []
        for row in rows:
            try:
                event = json.loads(row["raw_event"] or "{}")
            except Exception:
                continue
            if _event_unique_tournament_id(event) == 242:
                wrong_ids.append(str(row["match_id"]))
        if wrong_ids:
            placeholders = ",".join("?" for _ in wrong_ids)
            removed_special = conn.execute(
                f"delete from competition_special_buffer where competition_key = 'brasileirao' and match_id in ({placeholders})",
                wrong_ids,
            ).rowcount
            prefixed = [f"competition:brasileirao:{item}" for item in wrong_ids]
            main_placeholders = ",".join("?" for _ in prefixed)
            for table in ("match_buffer", "future_match_buffer"):
                removed_main += conn.execute(
                    f"delete from {table} where match_id in ({main_placeholders})", prefixed
                ).rowcount
        conn.commit()
    return {"competition_rows": removed_special, "main_buffer_rows": removed_main}


def _loaded_until(key: str) -> date | None:
    try:
        _init_db()
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            init_competition_tables(conn)
            row = conn.execute(
                "select max(match_date) from competition_special_buffer where competition_key = ?",
                (key,),
            ).fetchone()
        return _parse_date(row[0]) if row and row[0] else None
    except Exception:
        return None


def _sync_cursor(key: str) -> date | None:
    settings = get_competition_settings(key)
    metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
    value = metadata.get("last_sync_end_date")
    if not value:
        return None
    try:
        return _parse_date(value)
    except Exception:
        return None


def _mark_sync_cursor(key: str, scanned_end: str) -> None:
    settings = get_competition_settings(key)
    metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
    metadata["last_sync_end_date"] = scanned_end
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        init_competition_tables(conn)
        conn.execute(
            """
            update competition_special_settings
            set metadata_json = ?, updated_at = ?
            where key = ?
            """,
            (json.dumps(metadata), datetime.now(timezone.utc).isoformat(), key),
        )
        conn.commit()


def _upsert_competition_event(conn: sqlite3.Connection, key: str, event: dict[str, Any], match_date: str) -> None:
    status = event.get("status") or {}
    score = event.get("score") or {}
    tournament = event.get("tournament") or {}
    event_match_date = _event_match_date(event, match_date)
    importance = _match_importance_context(key, event)
    conn.execute(
        """
        insert into competition_special_buffer (
            competition_key, match_id, match_date, group_name, round_name, name, start_time,
            status, score_home, score_away, raw_event, importance_context_json
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(competition_key, match_id) do update set
            match_date = excluded.match_date,
            group_name = excluded.group_name,
            round_name = excluded.round_name,
            name = excluded.name,
            start_time = excluded.start_time,
            status = excluded.status,
            score_home = excluded.score_home,
            score_away = excluded.score_away,
            raw_event = excluded.raw_event,
            importance_context_json = excluded.importance_context_json
        """,
        (
            key,
            str(event.get("id")),
            event_match_date,
            tournament.get("name"),
            str(event.get("round") or ""),
            event.get("name"),
            int((event.get("start_timestamp") or 0) * 1000),
            (status.get("type") or status.get("description") or "").lower(),
            _score_value(score.get("home")),
            _score_value(score.get("away")),
            json.dumps(event),
            json.dumps(importance),
        ),
    )
    _mirror_competition_event_to_main_buffer(conn, key, event, event_match_date, importance)


def _save_competition_detail(
    key: str,
    event: dict[str, Any],
    detail: dict[str, Any],
    prediction: dict[str, Any] | None,
    readiness: dict[str, Any],
) -> None:
    _init_db()
    status = (event.get("status") or {}).get("type") or (event.get("status") or {}).get("description") or ""
    score = event.get("score") or {}
    now = datetime.now(timezone.utc).isoformat()
    importance = _match_importance_context(key, event)
    doc = _competition_doc(key, event, detail)
    intelligence = _competition_intelligence_context(key, event, detail, doc)
    doc["competition_intelligence"] = intelligence
    doc["team_strength_context"] = intelligence.get("team_strength")
    doc["table_context"] = intelligence.get("table")
    try:
        from app.market import snapshot_odds, get_movement
        snapshot_odds(doc)
        doc["odds_movement"] = get_movement(doc.get("sportybet_id"))
        intelligence["odds_movement"] = doc["odds_movement"]
    except Exception as exc:
        intelligence["odds_movement"] = {"error": str(exc)}
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        init_competition_tables(conn)
        conn.execute(
            """
            update competition_special_buffer set
                status = ?,
                score_home = ?,
                score_away = ?,
                raw_event = ?,
                raw_detail = ?,
                prediction_json = coalesce(?, prediction_json),
                importance_context_json = ?,
                enriched_at = ?,
                predicted_at = case when ? is not null then ? else predicted_at end
            where competition_key = ? and match_id = ?
            """,
            (
                str(status).lower(),
                _score_value(score.get("home")),
                _score_value(score.get("away")),
                json.dumps(event),
                json.dumps({**detail, "prediction_readiness": readiness, "competition_intelligence": intelligence}),
                json.dumps(prediction) if prediction else None,
                json.dumps(importance),
                now,
                json.dumps(prediction) if prediction else None,
                now,
                key,
                str(event.get("id")),
            ),
        )
        _mirror_competition_event_to_main_buffer(conn, key, event, _event_match_date(event, date.today().isoformat()), importance, enriched_doc=doc)
        conn.commit()


def _competition_doc(key: str, event: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    start = event.get("start_timestamp") or detail.get("start_timestamp")
    match_date = datetime.fromtimestamp(float(start), tz=timezone.utc).date().isoformat() if start else date.today().isoformat()
    markets = _special_markets(detail)
    importance = _match_importance_context(key, event)
    intelligence = _competition_intelligence_context(key, event, detail, {})
    return {
        "sportybet_id": f"competition:{key}:{event.get('id')}",
        "id": f"competition:{key}:{event.get('id')}",
        "sofascore_id": event.get("id"),
        "sofascore_match_status": "matched",
        "sportybet_name": event.get("name"),
        "name": event.get("name"),
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "tournament": (event.get("tournament") or {}).get("name"),
        "category": "World",
        "match_date": match_date,
        "start_time": int(float(start or 0) * 1000) if start else None,
        "period": (event.get("status") or {}).get("description") or "Not started",
        "score": event.get("score") or {},
        "sofascore_event": event,
        "raw_sofascore_event": event.get("raw_event") or event,
        "sofascore_detail": detail,
        "sportybet_detail": {"provider": "competition_special", "markets": markets},
        "raw_sporty": {
            "id": f"competition:{key}:{event.get('id')}",
            "name": event.get("name"),
            "home_team": event.get("home_team"),
            "away_team": event.get("away_team"),
            "tournament": (event.get("tournament") or {}).get("name"),
            "category": "World",
            "period": (event.get("status") or {}).get("description") or "Not started",
            "start_time": int(float(start or 0) * 1000) if start else None,
            "markets": markets,
        },
        "markets": markets,
        "sportybet_markets": markets,
        "time_context": {
            "utc_date": match_date,
            "local_date": match_date,
            "match_local_time": datetime.fromtimestamp(float(start), tz=timezone.utc).strftime("%H:%M") if start else "",
        },
        "data_sources": {
            "sofascore": {
                "detail": bool(detail),
                "statistics": bool(detail.get("statistics") or detail.get("match_statistics")),
                "history": bool(detail.get("home_last_matches") and detail.get("away_last_matches")),
            },
            "sportybet": {
                "available": False,
                "markets": True,
                "market_count": len(markets),
                "competition_special_proxy": True,
            },
            "competition_special": {
                "available": True,
                "key": key,
                "provider": "sofascore",
            },
        },
        "competition_special": {"key": key, "name": DEFAULT_WORLD_CUP["name"] if key == DEFAULT_WORLD_CUP["key"] else key},
        "match_importance_context": importance,
        "importance_context": importance,
        "competition_intelligence": intelligence,
        "team_strength_context": intelligence.get("team_strength"),
        "table_context": intelligence.get("table"),
    }


def _special_markets(detail: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Expose SofaScore's featured 1X2 prices in the normal market shape."""
    featured = (detail or {}).get("odds_featured") or {}
    candidates = [featured.get("full_time"), featured.get("default")]
    for market in candidates:
        choices = (market or {}).get("choices") or []
        selections = [
            {"name": choice.get("name"), "odds": _decimal_odds(choice.get("fractional_value"))}
            for choice in choices
            if choice.get("name")
        ]
        if len(selections) >= 2:
            return [{
                "id": "sofascore_featured_1x2",
                "name": market.get("market_name") or "SofaScore Featured Odds",
                "selections": selections,
                "source": "sofascore",
            }]
    return [
        {
            "id": "competition_special_1x2",
            "name": "Competition Special Baseline",
            "selections": [
                {"name": "Home", "odds": None},
                {"name": "Draw", "odds": None},
                {"name": "Away", "odds": None},
            ],
        }
    ]


def _decimal_odds(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return round(1 + float(numerator) / float(denominator), 3)
        return round(float(text), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _settings_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": row["key"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "unique_tournament_id": row["unique_tournament_id"],
        "season_id": row["season_id"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "updated_at": row["updated_at"],
    }


def _buffer_row(row: sqlite3.Row) -> dict[str, Any]:
    event = json.loads(row["raw_event"] or "{}")
    detail = json.loads(row["raw_detail"] or "null") if row["raw_detail"] else None
    prediction = json.loads(row["prediction_json"] or "null") if row["prediction_json"] else None
    importance = json.loads(row["importance_context_json"] or "{}")
    doc = _competition_doc(row["competition_key"], event, detail or {}) if detail else {"sofascore_event": event, "period": row["status"], "start_time": row["start_time"]}
    intelligence = (detail or {}).get("competition_intelligence") if isinstance(detail, dict) else None
    if not intelligence:
        intelligence = doc.get("competition_intelligence") if isinstance(doc, dict) else {}
    state = classify_match_state(doc)
    return {
        "competition_key": row["competition_key"],
        "match_id": _prefixed_match_id(row["competition_key"], row["match_id"]),
        "competition_match_id": row["match_id"],
        "sofascore_id": row["match_id"],
        "match_date": row["match_date"],
        "group": row["group_name"],
        "round": row["round_name"],
        "match": row["name"],
        "start_time": row["start_time"],
        "status": row["status"],
        "score": {"home": row["score_home"], "away": row["score_away"]},
        "match_state": state,
        "enriched": bool(row["raw_detail"]),
        "predicted": bool(row["prediction_json"]),
        "enriched_at": row["enriched_at"],
        "predicted_at": row["predicted_at"],
        "prediction": prediction,
        "readiness": (detail or {}).get("prediction_readiness") if isinstance(detail, dict) else None,
        "importance_context": importance,
        "competition_intelligence": intelligence,
        "event": event,
    }


def _competition_summary(matches: list[dict[str, Any]]) -> dict[str, Any]:
    importance_scores = [
        int(((match.get("importance_context") or {}).get("importance_score")) or 0)
        for match in matches
    ]
    return {
        "total": len(matches),
        "enriched": sum(1 for match in matches if match.get("enriched")),
        "predicted": sum(1 for match in matches if match.get("predicted")),
        "live": sum(1 for match in matches if (match.get("match_state") or {}).get("is_live")),
        "finished": sum(1 for match in matches if (match.get("match_state") or {}).get("is_finished")),
        "high_importance": sum(1 for score in importance_scores if score >= 78),
        "critical_importance": sum(1 for score in importance_scores if score >= 90),
        "groups": sorted({str(match.get("group") or "") for match in matches if match.get("group")}),
    }


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _event_match_date(event: dict[str, Any], fallback: str) -> str:
    try:
        return datetime.fromtimestamp(float(event.get("start_timestamp")), tz=timezone.utc).date().isoformat()
    except Exception:
        return fallback


def _score_value(value: Any) -> str:
    return "" if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _prefixed_match_id(key: str, event_id: Any) -> str:
    return f"competition:{key}:{event_id}"


def _mirror_competition_event_to_main_buffer(
    conn: sqlite3.Connection,
    key: str,
    event: dict[str, Any],
    match_date: str,
    importance: dict[str, Any],
    *,
    enriched_doc: dict[str, Any] | None = None,
) -> None:
    from app.buffer import _buffer_table_for, _init_buffer_table

    _init_buffer_table(conn)
    status = event.get("status") or {}
    score = event.get("score") or {}
    state = classify_match_state({
        "period": status.get("description") or status.get("type") or "Not start",
        "status": status,
        "start_time": int((event.get("start_timestamp") or 0) * 1000),
        "score": score,
    })
    is_live = 1 if state.get("is_live") else 0
    is_finished = 1 if (state.get("is_finished") or state.get("state") in {"postponed", "cancelled"}) else 0
    table = _buffer_table_for(match_date, is_live)
    other_table = "future_match_buffer" if table == "match_buffer" else "match_buffer"
    match_id = _prefixed_match_id(key, event.get("id"))
    raw_sporty = _competition_raw_sporty(key, event, match_date, importance)
    conn.execute(f"delete from {other_table} where match_id = ?", (match_id,))
    conn.execute(
        f"""
        insert into {table} (
            match_id, match_date, tournament, category, name, start_time, period,
            score_home, score_away, is_live, is_finished, ingested_at,
            sofascore_id, raw_sporty, raw_enriched
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(match_id) do update set
            match_date = excluded.match_date,
            tournament = excluded.tournament,
            category = excluded.category,
            name = excluded.name,
            start_time = excluded.start_time,
            period = excluded.period,
            score_home = excluded.score_home,
            score_away = excluded.score_away,
            is_live = excluded.is_live,
            is_finished = excluded.is_finished,
            ingested_at = excluded.ingested_at,
            sofascore_id = excluded.sofascore_id,
            raw_sporty = excluded.raw_sporty,
            raw_enriched = coalesce(excluded.raw_enriched, {table}.raw_enriched)
        """,
        (
            match_id,
            match_date,
            (event.get("tournament") or {}).get("name"),
            "World",
            event.get("name"),
            int((event.get("start_timestamp") or 0) * 1000),
            status.get("description") or status.get("type") or "Not start",
            _score_value(score.get("home")),
            _score_value(score.get("away")),
            is_live,
            is_finished,
            datetime.now(timezone.utc).isoformat(),
            str(event.get("id") or ""),
            json.dumps(raw_sporty),
            json.dumps(enriched_doc) if enriched_doc else None,
        ),
    )


def _competition_raw_sporty(key: str, event: dict[str, Any], match_date: str, importance: dict[str, Any]) -> dict[str, Any]:
    status = event.get("status") or {}
    score = event.get("score") or {}
    return {
        "id": _prefixed_match_id(key, event.get("id")),
        "competition_source_id": str(event.get("id") or ""),
        "name": event.get("name"),
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "tournament": (event.get("tournament") or {}).get("name"),
        "category": "World",
        "match_date": match_date,
        "start_time": int((event.get("start_timestamp") or 0) * 1000),
        "period": status.get("description") or status.get("type") or "Not start",
        "score": {"home": _score_value(score.get("home")), "away": _score_value(score.get("away"))},
        "markets": _special_markets(),
        "competition_special": {"key": key, "source": "sofascore"},
        "match_importance_context": importance,
        "importance_context": importance,
        "sofascore_event": event,
    }


def _competition_intelligence_context(
    key: str,
    event: dict[str, Any],
    detail: dict[str, Any],
    doc: dict[str, Any],
) -> dict[str, Any]:
    standings = detail.get("standings") or doc.get("standings") or []
    home = event.get("home_team") or {}
    away = event.get("away_team") or {}
    home_table = _team_standing_context(standings, home)
    away_table = _team_standing_context(standings, away)
    home_strength = _recent_play_strength(detail.get("home_last_matches") or [], home)
    away_strength = _recent_play_strength(detail.get("away_last_matches") or [], away)
    table_edge = _safe_num((home_table or {}).get("points_per_game")) - _safe_num((away_table or {}).get("points_per_game"))
    strength_edge = _safe_num(home_strength.get("strength_score")) - _safe_num(away_strength.get("strength_score"))
    return {
        "competition_key": key,
        "table": {
            "available": bool(home_table or away_table),
            "home": home_table,
            "away": away_table,
            "edge_ppg": round(table_edge, 3),
            "leader": "home" if table_edge > 0.15 else "away" if table_edge < -0.15 else "even",
        },
        "team_strength": {
            "home": home_strength,
            "away": away_strength,
            "edge": round(strength_edge, 2),
            "leader": "home" if strength_edge > 5 else "away" if strength_edge < -5 else "even",
            "basis": "recent_play_results_goal_difference_and_opponent_quality",
        },
        "readiness_notes": _competition_readiness_notes(home_table, away_table, home_strength, away_strength),
    }


def _team_standing_context(standings: list[dict[str, Any]], team: dict[str, Any]) -> dict[str, Any] | None:
    team_id = str(team.get("id") or "")
    team_name = str(team.get("name") or "").lower()
    for row in standings or []:
        row_team = row.get("team") or {}
        if team_id and str(row_team.get("id") or "") == team_id:
            return _standing_summary(row)
        if team_name and str(row_team.get("name") or "").lower() == team_name:
            return _standing_summary(row)
    return None


def _standing_summary(row: dict[str, Any]) -> dict[str, Any]:
    played = int(_safe_num(row.get("played")) or 0)
    points = int(_safe_num(row.get("points")) or 0)
    gf = int(_safe_num(row.get("goals_for")) or 0)
    ga = int(_safe_num(row.get("goals_against")) or 0)
    return {
        "position": row.get("position"),
        "team": row.get("team"),
        "played": played,
        "points": points,
        "points_per_game": round(points / played, 3) if played else 0,
        "goal_difference": _goal_diff(row.get("goal_diff"), gf - ga),
        "promotion": row.get("promotion"),
    }


def _recent_play_strength(matches: list[dict[str, Any]], team: dict[str, Any], limit: int = 8) -> dict[str, Any]:
    team_id = str(team.get("id") or "")
    team_name = str(team.get("name") or "").lower()
    sample = 0
    points = 0
    goal_diff = 0
    goals_for = 0
    goals_against = 0
    opponent_quality = 0.0
    for match in (matches or [])[:limit]:
        score = match.get("score") or {}
        home = match.get("home_team") or {}
        away = match.get("away_team") or {}
        is_home = (team_id and str(home.get("id") or "") == team_id) or (team_name and str(home.get("name") or "").lower() == team_name)
        is_away = (team_id and str(away.get("id") or "") == team_id) or (team_name and str(away.get("name") or "").lower() == team_name)
        if not is_home and not is_away:
            continue
        hg = _optional_score(score.get("home"))
        ag = _optional_score(score.get("away"))
        if hg is None or ag is None:
            continue
        sample += 1
        own = hg if is_home else ag
        opp = ag if is_home else hg
        goals_for += own
        goals_against += opp
        goal_diff += own - opp
        points += 3 if own > opp else 1 if own == opp else 0
        opponent = away if is_home else home
        opponent_quality += _name_quality_score(str(opponent.get("name") or ""))
    ppg = points / sample if sample else 0.0
    gd_per_match = goal_diff / sample if sample else 0.0
    quality = opponent_quality / sample if sample else 50.0
    strength_score = max(1, min(99, 45 + ppg * 12 + gd_per_match * 6 + (quality - 50) * 0.25))
    return {
        "sample_size": sample,
        "points": points,
        "points_per_game": round(ppg, 3),
        "goals_for": goals_for,
        "goals_against": goals_against,
        "goal_difference": goal_diff,
        "opponent_quality_avg": round(quality, 2),
        "strength_score": round(strength_score, 2),
        "trend": "strong" if strength_score >= 68 else "weak" if strength_score <= 45 else "stable",
    }


def _competition_readiness_notes(
    home_table: dict[str, Any] | None,
    away_table: dict[str, Any] | None,
    home_strength: dict[str, Any],
    away_strength: dict[str, Any],
) -> list[str]:
    notes: list[str] = []
    if not home_table or not away_table:
        notes.append("table_context_missing_or_incomplete")
    if int(home_strength.get("sample_size") or 0) < 3 or int(away_strength.get("sample_size") or 0) < 3:
        notes.append("recent_play_sample_low")
    if abs(float(home_strength.get("strength_score") or 0) - float(away_strength.get("strength_score") or 0)) >= 12:
        notes.append("team_strength_gap_detected")
    return notes


def _goal_diff(value: Any, fallback: int) -> int:
    try:
        return int(str(value).replace("+", ""))
    except Exception:
        return fallback


def _optional_score(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _safe_num(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _name_quality_score(name: str) -> float:
    lower = name.lower()
    elite = ("brazil", "argentina", "france", "spain", "england", "germany", "portugal", "netherlands", "italy")
    strong = ("belgium", "croatia", "uruguay", "colombia", "mexico", "usa", "morocco", "japan", "switzerland")
    if any(token in lower for token in elite):
        return 72.0
    if any(token in lower for token in strong):
        return 62.0
    return 50.0


def _match_importance_context(key: str, event: dict[str, Any]) -> dict[str, Any]:
    name = str(event.get("name") or "")
    tournament = event.get("tournament") or {}
    round_value = str(event.get("round") or tournament.get("round") or "")
    round_lower = f"{round_value} {name}".lower()
    status_type = str((event.get("status") or {}).get("type") or "").lower()
    reasons: list[str] = []
    score = 60
    stage = "group"

    if "final" in round_lower and "semi" not in round_lower:
        stage, score = "final", 100
        reasons.append("trophy_decider")
    elif "semi" in round_lower:
        stage, score = "semi_final", 92
        reasons.append("final_place_at_stake")
    elif "quarter" in round_lower:
        stage, score = "quarter_final", 85
        reasons.append("knockout_elimination")
    elif "round of 16" in round_lower or "last 16" in round_lower:
        stage, score = "round_of_16", 78
        reasons.append("knockout_elimination")
    elif "knockout" in round_lower or "playoff" in round_lower:
        stage, score = "knockout", 82
        reasons.append("single_game_elimination")
    elif "group" in round_lower:
        stage, score = "group_stage", 62
        reasons.append("group_table_points")
        if any(token in round_lower for token in ("round 3", "matchday 3", "3")):
            score = 70
            reasons.append("final_group_round_pressure")

    if status_type == "inprogress":
        score = min(100, score + 8)
        reasons.append("live_state_priority")
    if "opening" in round_lower or "opening" in name.lower():
        score = max(score, 75)
        reasons.append("opening_match_pressure")

    if key == DEFAULT_WORLD_CUP["key"]:
        reasons.append("world_cup_special_context")

    tier = "critical" if score >= 90 else "high" if score >= 78 else "medium" if score >= 62 else "normal"
    return {
        "competition_key": key,
        "stage": stage,
        "tier": tier,
        "importance_score": score,
        "round": round_value,
        "reasons": reasons,
        "prediction_focus": _importance_prediction_focus(stage),
    }


def _importance_prediction_focus(stage: str) -> list[str]:
    if stage in {"final", "semi_final", "quarter_final", "round_of_16", "knockout"}:
        return ["lineup_strength", "game_state", "extra_time_risk", "defensive_caution", "late_goal_pressure"]
    if stage == "group_stage":
        return ["table_pressure", "goal_difference", "rotation_risk", "qualification_scenario"]
    return ["freshness", "team_strength", "market_context"]


def list_all_competition_summaries(
    buffer_limit: int = 50,
    analysis_limit: int = 1,
) -> dict[str, Any]:
    """Return a lightweight summary for every tracked competition.

    This is the production-ready data source for the unified competition
    dashboard page.  It aggregates settings, buffer health, and latest
    analysis for all 30 curated competitions plus the World Cup special.
    """
    _init_db()
    all_keys = [DEFAULT_WORLD_CUP["key"], *[entry["key"] for entry in TOP_30_COMPETITIONS]]
    summaries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for key in all_keys:
        try:
            settings = get_competition_settings(key)
            buffer = list_competition_buffer(key, limit=buffer_limit)
            status = competition_status(key)

            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                init_competition_tables(conn)
                from app.competition_analyser import get_latest_analysis, init_competition_analysis_table
                init_competition_analysis_table(conn)
                latest_analysis = get_latest_analysis(key, conn)

            summary = {
                "key": key,
                "name": settings.get("name") or key,
                "enabled": bool(settings.get("enabled")),
                "unique_tournament_id": settings.get("unique_tournament_id"),
                "settings": settings,
                "buffer_summary": buffer.get("summary", {}),
                "buffer_status": status.get("buffer", {}),
                "latest_analysis": latest_analysis,
                "match_count": buffer.get("count", 0),
                "error": None,
            }
            summaries.append(summary)
        except Exception as exc:
            errors.append({"key": key, "error": str(exc)})
            summaries.append({
                "key": key,
                "name": key,
                "enabled": False,
                "unique_tournament_id": 0,
                "settings": {"key": key, "name": key, "enabled": False},
                "buffer_summary": {},
                "buffer_status": {},
                "latest_analysis": None,
                "match_count": 0,
                "error": str(exc),
            })

    # Sort: enabled first, then by match count descending
    summaries.sort(key=lambda s: (not s["enabled"], -s.get("match_count", 0)))

    return {
        "status": "success",
        "total_tracked": len(all_keys),
        "enabled_count": sum(1 for s in summaries if s["enabled"]),
        "competitions": summaries,
        "errors": errors,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {ddl}")
