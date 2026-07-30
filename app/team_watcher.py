from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.db import DB_PATH
from app.league_memory import _ensure_column, _init_db
from app.db import db_conn
from app.match_state import classify_match_state
from app.time_context import match_time_context
from app.web_context import search_team_context


def init_team_watcher_tables(conn: sqlite3.Connection) -> None:
    conn.execute("pragma busy_timeout = 30000")
    # Self-healing migration: drop stale NOT NULL columns added by a previous edit
    _stale = {r[1] for r in conn.execute("pragma table_info(ai_team_watchers)").fetchall()}
    if "primary_provider" in _stale or "provider_team_id" in _stale:
        conn.execute("drop table if exists _tw_mig")
        conn.execute("""
            create table _tw_mig (
                team_key text primary key,
                team_name text not null,
                sporty_team_id text,
                sofascore_team_id text,
                aliases_json text not null default '[]',
                analyst_name text not null default '',
                profile_json text not null default '{}',
                match_count integer not null default 0,
                last_match_id text,
                last_analysis_json text,
                league_name text,
                position text,
                table_json text not null default '{}',
                web_context_json text not null default '{}',
                overview_json text not null default '{}',
                last_web_context_at text,
                updated_at text not null default current_timestamp
            )
        """)
        conn.execute("""
            insert or ignore into _tw_mig
                (team_key, team_name, sporty_team_id, sofascore_team_id, aliases_json,
                 analyst_name, profile_json, match_count, last_match_id, last_analysis_json,
                 league_name, position, table_json, web_context_json, overview_json,
                 last_web_context_at, updated_at)
            select team_key, team_name,
                coalesce(sporty_team_id, provider_team_id),
                sofascore_team_id, aliases_json,
                coalesce(analyst_name, ''), coalesce(profile_json, '{}'),
                coalesce(match_count, 0), last_match_id, last_analysis_json,
                league_name, position,
                coalesce(table_json, '{}'), coalesce(web_context_json, '{}'),
                coalesce(overview_json, '{}'), last_web_context_at,
                coalesce(updated_at, current_timestamp)
            from ai_team_watchers
        """)
        conn.execute("drop table ai_team_watchers")
        conn.execute("alter table _tw_mig rename to ai_team_watchers")
        conn.commit()
    conn.execute(
        """
        create table if not exists ai_team_watchers (
            team_key text primary key,
            team_name text not null,
            sporty_team_id text,
            sofascore_team_id text,
            aliases_json text not null default '[]',
            analyst_name text not null default '',
            profile_json text not null default '{}',
            match_count integer not null default 0,
            last_match_id text,
            last_analysis_json text,
            updated_at text not null default current_timestamp
        )
        """
    )
    conn.execute(
        """
        create table if not exists ai_team_watcher_matches (
            team_key text not null,
            match_id text not null,
            match_date text,
            team_name text not null,
            team_side text,
            sporty_team_id text,
            sofascore_team_id text,
            opponent text,
            venue text,
            tournament text,
            goals_for integer,
            goals_against integer,
            result text,
            status text,
            prediction_json text,
            analysis_json text,
            brief text,
            raw_match_json text not null default '{}',
            created_at text not null default current_timestamp,
            primary key (team_key, match_id)
        )
        """
    )
    _ensure_column(conn, "ai_team_watchers", "league_name", "text")
    _ensure_column(conn, "ai_team_watchers", "position", "text")
    _ensure_column(conn, "ai_team_watchers", "sporty_team_id", "text")
    _ensure_column(conn, "ai_team_watchers", "sofascore_team_id", "text")
    _ensure_column(conn, "ai_team_watchers", "table_json", "text not null default '{}'")
    _ensure_column(conn, "ai_team_watchers", "web_context_json", "text not null default '{}'")
    _ensure_column(conn, "ai_team_watchers", "overview_json", "text not null default '{}'")
    _ensure_column(conn, "ai_team_watchers", "last_web_context_at", "text")
    _ensure_column(conn, "ai_team_watcher_matches", "league_name", "text")
    _ensure_column(conn, "ai_team_watcher_matches", "team_position", "text")
    _ensure_column(conn, "ai_team_watcher_matches", "opponent_position", "text")
    _ensure_column(conn, "ai_team_watcher_matches", "table_gap", "integer")
    _ensure_column(conn, "ai_team_watcher_matches", "team_side", "text")
    _ensure_column(conn, "ai_team_watcher_matches", "sporty_team_id", "text")
    _ensure_column(conn, "ai_team_watcher_matches", "sofascore_team_id", "text")
    _ensure_column(conn, "ai_team_watcher_matches", "web_context_json", "text not null default '{}'")
    conn.execute("create index if not exists idx_ai_team_watchers_updated on ai_team_watchers(updated_at desc)")
    conn.execute("create index if not exists idx_ai_team_watcher_matches_team on ai_team_watcher_matches(team_key, match_date desc)")


def list_watchers(limit: int = 100, league_name: str | None = None) -> dict[str, Any]:
    _init_db()
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_team_watcher_tables(conn)
        params: list[Any] = []
        league_clause = ""
        if league_name:
            league_clause = "where lower(coalesce(league_name, '')) like ?"
            params.append(f"%{league_name.strip().lower()}%")
        rows = conn.execute(
            """
            select *
            from ai_team_watchers
            {league_clause}
            order by match_count desc, updated_at desc, team_name asc
            limit ?
            """.format(league_clause=league_clause),
            (*params, limit),
        ).fetchall()
    watchers = [_watcher_row(row) for row in rows]
    return {"status": "success", "count": len(watchers), "watchers": watchers}


def get_watcher(team_key: str, limit: int = 30) -> dict[str, Any]:
    _init_db()
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_team_watcher_tables(conn)
        row = _resolve_watcher_row(conn, team_key)
        resolved_key = row["team_key"] if row else team_key
        matches = conn.execute(
            """
            select *
            from ai_team_watcher_matches
            where team_key = ?
            order by match_date desc, created_at desc
            limit ?
            """,
            (resolved_key, limit),
        ).fetchall()
    if row is None:
        return {"status": "not_found", "team_key": team_key}
    return {
        "status": "success",
        "watcher": _watcher_row(row),
        "matches": [_match_row(match) for match in matches],
    }


def inspect_sporty_team_ids(limit: int = 20) -> dict[str, Any]:
    _init_db()
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_team_watcher_tables(conn)
        for table in ("match_buffer", "future_match_buffer"):
            try:
                rows = conn.execute(
                    f"select match_id, raw_sporty from {table} where raw_sporty is not null limit ?",
                    (limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                sporty = _loads(row["raw_sporty"], {})
                team_ids = sporty.get("team_ids") if isinstance(sporty.get("team_ids"), dict) else {}
                examples.append({
                    "table": table,
                    "match_id": row["match_id"],
                    "home_team": sporty.get("home_team"),
                    "away_team": sporty.get("away_team"),
                    "home_team_id": team_ids.get("home"),
                    "away_team_id": team_ids.get("away"),
                })
                if len(examples) >= limit:
                    break
            if len(examples) >= limit:
                break
    has_ids = any(item.get("home_team_id") or item.get("away_team_id") for item in examples)
    return {"status": "success", "sporty_has_team_ids": has_ids, "examples": examples}


def observe_match(match_doc: dict[str, Any], analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    teams = _teams_for_doc(match_doc)
    if not teams:
        return {"status": "skipped", "reason": "no_team_identity"}
    match_id = str(match_doc.get("_id") or match_doc.get("sportybet_id") or match_doc.get("match_id") or match_doc.get("id") or "")
    if not match_id:
        return {"status": "skipped", "reason": "missing_match_id"}

    _init_db()
    updated: list[dict[str, Any]] = []
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_team_watcher_tables(conn)
        for team in teams:
            resolved_key = _resolve_watcher_key(conn, team)
            _upsert_watcher(conn, team, resolved_key=resolved_key)
            observation = _observation_for_team(match_doc, team, analysis)
            conn.execute(
                """
                insert into ai_team_watcher_matches
                    (team_key, match_id, match_date, team_name, team_side, sporty_team_id, sofascore_team_id, opponent, venue, tournament,
                     league_name, team_position, opponent_position, table_gap,
                     goals_for, goals_against, result, status, prediction_json, analysis_json,
                     brief, raw_match_json, web_context_json, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(team_key, match_id) do update set
                    match_date = excluded.match_date,
                    team_name = excluded.team_name,
                    team_side = excluded.team_side,
                    sporty_team_id = excluded.sporty_team_id,
                    sofascore_team_id = excluded.sofascore_team_id,
                    opponent = excluded.opponent,
                    venue = excluded.venue,
                    tournament = excluded.tournament,
                    league_name = excluded.league_name,
                    team_position = excluded.team_position,
                    opponent_position = excluded.opponent_position,
                    table_gap = excluded.table_gap,
                    goals_for = excluded.goals_for,
                    goals_against = excluded.goals_against,
                    result = excluded.result,
                    status = excluded.status,
                    prediction_json = excluded.prediction_json,
                    analysis_json = excluded.analysis_json,
                    brief = excluded.brief,
                    raw_match_json = excluded.raw_match_json,
                    web_context_json = excluded.web_context_json
                """,
                (
                    resolved_key,
                    match_id,
                    observation["match_date"],
                    team["team_name"],
                    observation["team_side"],
                    observation["sporty_team_id"],
                    observation["sofascore_team_id"],
                    observation["opponent"],
                    observation["venue"],
                    observation["tournament"],
                    observation["league_name"],
                    observation["team_position"],
                    observation["opponent_position"],
                    observation["table_gap"],
                    observation["goals_for"],
                    observation["goals_against"],
                    observation["result"],
                    observation["status"],
                    json.dumps(match_doc.get("prediction")) if match_doc.get("prediction") else None,
                    json.dumps(analysis) if analysis else None,
                    observation["brief"],
                    json.dumps(observation["raw_match"]),
                    json.dumps(observation.get("web_context") or {}),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            profile = _build_profile(conn, resolved_key, team)
            conn.execute(
                """
                update ai_team_watchers
                set profile_json = ?,
                    league_name = ?,
                    position = ?,
                    table_json = ?,
                    web_context_json = ?,
                    overview_json = ?,
                    last_web_context_at = ?,
                    match_count = ?,
                    last_match_id = ?,
                    last_analysis_json = ?,
                    updated_at = ?
                where team_key = ?
                """,
                (
                    json.dumps(profile),
                    profile.get("league_name"),
                    profile.get("position"),
                    json.dumps(profile.get("table") or {}),
                    json.dumps(profile.get("web_context") or {}),
                    json.dumps(profile.get("overview") or {}),
                    profile.get("last_web_context_at"),
                    int(profile.get("sample_size") or 0),
                    match_id,
                    json.dumps(analysis) if analysis else None,
                    datetime.now(timezone.utc).isoformat(),
                    resolved_key,
                ),
            )
            updated.append({"team_key": resolved_key, "team_name": team["team_name"], "profile": profile})
        conn.commit()
    return {"status": "success", "match_id": match_id, "updated": updated}


def observe_finished_match_by_id(match_id: str, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = _get_finished_or_buffered_match(match_id)
    if not doc:
        return {"status": "not_found", "match_id": match_id}
    return observe_match(doc, analysis=analysis)


def backfill_from_finished(limit: int = 200) -> dict[str, Any]:
    processed = updated = skipped = 0
    matches = _list_finished_matches_local_or_mongo(limit)
    for doc in matches:
        processed += 1
        result = observe_match(doc, analysis=doc.get("ai_analysis") or doc.get("ai_analysis_ollama"))
        if result.get("status") == "success":
            updated += len(result.get("updated") or [])
        else:
            skipped += 1
    return {"status": "success", "processed": processed, "team_updates": updated, "skipped": skipped, "source": "local" if not _mongo_configured() else "mongo"}


def _mongo_configured() -> bool:
    try:
        from app.mongo_store import is_configured
        return is_configured()
    except Exception:
        return False


def _list_finished_matches_local_or_mongo(limit: int) -> list[dict[str, Any]]:
    if _mongo_configured():
        try:
            from app.mongo_store import list_finished_matches
            return list_finished_matches(limit=limit)
        except Exception:
            pass
    # Local SQLite fallback (used when PREDICTX_LOCAL_STORAGE_ONLY=true)
    try:
        import json as _json
        from app.db import DB_PATH
        from app.league_memory import _init_db
        from app.db import db_conn
        _init_db()
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select coalesce(raw_doc, raw_json) as raw_doc from finished_matches order by finished_at desc limit ?",
                (limit,),
            ).fetchall()
        docs = []
        for row in rows:
            if not row["raw_doc"]:
                continue
            try:
                doc = _json.loads(row["raw_doc"])
                docs.append(doc)
            except Exception:
                pass
        return docs
    except Exception:
        return []


def team_context_for_match(match_doc: dict[str, Any]) -> dict[str, Any]:
    teams = _teams_for_doc(match_doc)
    if not teams:
        return {"available": False, "reason": "no_team_identity"}
    _init_db()
    out: dict[str, Any] = {"available": False}
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_team_watcher_tables(conn)
        for team in teams:
            row = _resolve_watcher_row(conn, team["team_key"], team=team)
            out[team["side"]] = _watcher_row(row) if row else {"available": False, **team}
            out["available"] = bool(out["available"] or row)
        if all(side in out for side in ("home", "away")):
            out["matchup"] = _matchup_context(out.get("home") or {}, out.get("away") or {}, match_doc)
    return out


def team_watchers_for_match(match_doc: dict[str, Any]) -> dict[str, Any]:
    teams = _teams_for_doc(match_doc)
    if not teams:
        return {"available": False, "reason": "no_team_identity"}
    _init_db()
    out: dict[str, Any] = {"available": False}
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_team_watcher_tables(conn)
        for team in teams:
            row = _resolve_watcher_row(conn, team["team_key"], team=team)
            out[team["side"]] = _watcher_row(row) if row else {"available": False, **team}
            out["available"] = bool(out["available"] or row)
    return out


def _upsert_watcher(conn: sqlite3.Connection, team: dict[str, Any], resolved_key: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    existing = _resolve_watcher_row(conn, team["team_key"]) or (_resolve_watcher_row(conn, resolved_key) if resolved_key else None)
    aliases = _merge_aliases(existing, team)
    db_key = resolved_key or team["team_key"]
    if existing:
        db_key = existing["team_key"]
    latest_profile = _loads(existing["profile_json"], {}) if existing else {}
    league_name = team.get("league_name") or latest_profile.get("league_name")
    position = team.get("position") or latest_profile.get("position")
    sporty_team_id = team.get("sporty_team_id") or latest_profile.get("sporty_team_id") or (existing["sporty_team_id"] if existing else None)
    sofascore_team_id = team.get("sofascore_team_id") or latest_profile.get("sofascore_team_id") or (existing["sofascore_team_id"] if existing else None)
    table_json = team.get("table_json") or latest_profile.get("table") or {}
    web_context_json = team.get("web_context") or latest_profile.get("web_context") or {}
    overview_json = team.get("overview") or latest_profile.get("overview") or {}
    conn.execute(
        """
        insert into ai_team_watchers
            (team_key, team_name, sporty_team_id, sofascore_team_id, aliases_json, analyst_name,
             league_name, position, table_json, web_context_json, overview_json, updated_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(team_key) do update set
            team_name = excluded.team_name,
            aliases_json = excluded.aliases_json,
            analyst_name = excluded.analyst_name,
            sporty_team_id = coalesce(excluded.sporty_team_id, ai_team_watchers.sporty_team_id),
            sofascore_team_id = coalesce(excluded.sofascore_team_id, ai_team_watchers.sofascore_team_id),
            league_name = coalesce(excluded.league_name, ai_team_watchers.league_name),
            position = coalesce(excluded.position, ai_team_watchers.position),
            table_json = coalesce(excluded.table_json, ai_team_watchers.table_json),
            web_context_json = coalesce(excluded.web_context_json, ai_team_watchers.web_context_json),
            overview_json = coalesce(excluded.overview_json, ai_team_watchers.overview_json),
            updated_at = excluded.updated_at
        """,
        (
            db_key,
            team["team_name"],
            sporty_team_id,
            sofascore_team_id,
            json.dumps(aliases),
            f"{team['team_name']} AI Watcher",
            league_name,
            position,
            json.dumps(table_json or {}),
            json.dumps(web_context_json or {}),
            json.dumps(overview_json or {}),
            now,
        ),
    )


def _teams_for_doc(doc: dict[str, Any]) -> list[dict[str, Any]]:
    raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
    sporty_ids = raw_sporty.get("team_ids") if isinstance(raw_sporty.get("team_ids"), dict) else {}
    sofa_detail = doc.get("sofascore_detail") if isinstance(doc.get("sofascore_detail"), dict) else {}
    sofa_home = sofa_detail.get("home_team") or sofa_detail.get("homeTeam") or {}
    sofa_away = sofa_detail.get("away_team") or sofa_detail.get("awayTeam") or {}
    league_name = _league_name_for_doc(doc)
    table_map = _table_lookup(doc)
    sides = [
        ("home", raw_sporty.get("home_team") or doc.get("home_team"), sporty_ids.get("home"), sofa_home),
        ("away", raw_sporty.get("away_team") or doc.get("away_team"), sporty_ids.get("away"), sofa_away),
    ]
    teams: list[dict[str, Any]] = []
    for side, name, sporty_id, sofa_team in sides:
        team_name = _team_name(name)
        sofa_id = (sofa_team or {}).get("id") if isinstance(sofa_team, dict) else None
        sporty_team_id = str(sporty_id or "").strip() or None
        sofascore_team_id = str(sofa_id or "").strip() or None
        team_key = _slug(team_name or sporty_team_id or sofascore_team_id or "")
        if not team_key or not team_name:
            continue
        aliases = [{"provider": "name", "id": _slug(team_name), "label": team_name}]
        if sporty_team_id:
            aliases.append({"provider": "sporty", "id": sporty_team_id})
        if sofascore_team_id:
            aliases.append({"provider": "sofascore", "id": sofascore_team_id})
        team_position = _team_position(table_map, team_name, sporty_id, sofa_id)
        teams.append({
            "side": side,
            "team_key": team_key,
            "team_name": team_name,
            "sporty_team_id": sporty_team_id,
            "sofascore_team_id": sofascore_team_id,
            "aliases": aliases,
            "league_name": league_name,
            "position": team_position,
            "table_json": table_map,
        })
    return teams


def _observation_for_team(doc: dict[str, Any], team: dict[str, Any], analysis: dict[str, Any] | None) -> dict[str, Any]:
    side = team["side"]
    opponent_side = "away" if side == "home" else "home"
    raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
    match_state = classify_match_state(doc)
    table_map = _table_lookup(doc)
    opponent_name = _team_name(raw_sporty.get(f"{opponent_side}_team") or doc.get(f"{opponent_side}_team"))
    opponent_row = _team_position(table_map, opponent_name, None, None, return_row=True)
    team_row = _team_position(table_map, team["team_name"], None, None, return_row=True)
    score = doc.get("score") if isinstance(doc.get("score"), dict) else raw_sporty.get("score") or {}
    own = _to_int(score.get(side))
    opp = _to_int(score.get(opponent_side))
    result = "win" if own is not None and opp is not None and own > opp else "loss" if own is not None and opp is not None and own < opp else "draw" if own is not None and opp is not None else None
    opponent = opponent_name
    analysis_text = _analysis_summary(analysis)
    score_text = f"{own}-{opp}" if own is not None and opp is not None else "score unavailable"
    brief = f"{team['team_name']} {result or 'played'} vs {opponent or 'opponent'} ({side}, {score_text})."
    if analysis_text:
        brief = f"{brief} Analysis: {analysis_text}"
    time_context = {}
    try:
        time_context = match_time_context(doc)
    except Exception:
        time_context = {}
    web_context = _team_web_context(team, doc, team_row, opponent_row)
    return {
        "match_date": doc.get("match_date"),
        "team_side": side,
        "opponent": opponent,
        "venue": doc.get("venue") or raw_sporty.get("venue"),
        "tournament": doc.get("tournament") or raw_sporty.get("tournament"),
        "league_name": team.get("league_name") or _league_name_for_doc(doc),
        "team_position": _position_value(team_row),
        "opponent_position": _position_value(opponent_row),
        "table_gap": _table_gap(team_row, opponent_row),
        "goals_for": own,
        "goals_against": opp,
        "result": result,
        "status": str(match_state.get("state") or doc.get("period") or raw_sporty.get("period") or doc.get("status") or ""),
        "brief": brief[:1200],
        "web_context": web_context,
        "raw_match": {
            "name": doc.get("name") or raw_sporty.get("name"),
            "sportybet_id": doc.get("sportybet_id") or raw_sporty.get("id"),
            "sofascore_id": doc.get("sofascore_id"),
            "home_team": raw_sporty.get("home_team") or doc.get("home_team"),
            "away_team": raw_sporty.get("away_team") or doc.get("away_team"),
            "home_team_id": (raw_sporty.get("team_ids") or {}).get("home") if isinstance(raw_sporty.get("team_ids"), dict) else None,
            "away_team_id": (raw_sporty.get("team_ids") or {}).get("away") if isinstance(raw_sporty.get("team_ids"), dict) else None,
            "team_side": side,
            "data_sources": doc.get("data_sources") or {},
            "time_context": time_context,
        },
    }


def _build_profile(conn: sqlite3.Connection, team_key: str, team: dict[str, Any] | None = None) -> dict[str, Any]:
    watcher = _resolve_watcher_row(conn, team_key)
    watcher_profile = _loads(watcher["profile_json"], {}) if watcher else {}
    rows = conn.execute(
        """
        select *
        from ai_team_watcher_matches
        where team_key = ?
        order by match_date desc, created_at desc
        limit 30
        """,
        (team_key,),
    ).fetchall()
    finished = [row for row in rows if row["goals_for"] is not None and row["goals_against"] is not None]
    sample = len(finished)
    wins = sum(1 for row in finished if row["result"] == "win")
    draws = sum(1 for row in finished if row["result"] == "draw")
    losses = sum(1 for row in finished if row["result"] == "loss")
    gf = sum(int(row["goals_for"] or 0) for row in finished)
    ga = sum(int(row["goals_against"] or 0) for row in finished)
    over_25 = sum(1 for row in finished if int(row["goals_for"] or 0) + int(row["goals_against"] or 0) >= 3)
    btts = sum(1 for row in finished if int(row["goals_for"] or 0) > 0 and int(row["goals_against"] or 0) > 0)
    clean_sheets = sum(1 for row in finished if int(row["goals_against"] or 0) == 0)
    blanks = sum(1 for row in finished if int(row["goals_for"] or 0) == 0)
    ppg = (wins * 3 + draws) / sample if sample else 0.0
    gf_avg = gf / sample if sample else 0.0
    ga_avg = ga / sample if sample else 0.0
    analyst_score = max(1, min(99, 42 + ppg * 13 + (gf_avg - ga_avg) * 7 + (clean_sheets / sample * 7 if sample else 0)))
    form = "".join("W" if row["result"] == "win" else "D" if row["result"] == "draw" else "L" for row in finished[:8])
    league_name = (
        (team or {}).get("league_name")
        or watcher_profile.get("league_name")
        or (watcher and watcher["league_name"])
        or _league_name_from_rows(rows)
    )
    position = (team or {}).get("position") or watcher_profile.get("position") or (watcher and watcher["position"])
    table = (team or {}).get("table_json") or watcher_profile.get("table") or _table_from_rows(rows)
    web_context = _loads(watcher["web_context_json"], {}) if watcher else {}
    last_web_context_at = watcher["last_web_context_at"] if watcher else None
    venue_split = _venue_split(rows)
    if _should_refresh_web_context({"web_context": web_context, "last_web_context_at": last_web_context_at}, sample):
        team_name = (team or {}).get("team_name") or (watcher and watcher["team_name"]) or "team"
        try:
            fresh_context = search_team_context(team_name, league_name or "", position)
            if isinstance(fresh_context, dict):
                web_context = fresh_context
                last_web_context_at = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            if not web_context:
                web_context = {"error": str(exc), "query": team_name}
    overview = _build_overview(
        team_name=(team or {}).get("team_name") or (watcher and watcher["team_name"]) or "Team",
        league_name=league_name,
        position=position,
        profile={
            "sample_size": sample,
            "record": {"wins": wins, "draws": draws, "losses": losses, "form": form},
            "goals": {
                "for_avg": round(gf_avg, 2),
                "against_avg": round(ga_avg, 2),
                "over_2_5_rate": round(over_25 / sample, 3) if sample else 0,
                "btts_rate": round(btts / sample, 3) if sample else 0,
                "clean_sheet_rate": round(clean_sheets / sample, 3) if sample else 0,
                "blank_rate": round(blanks / sample, 3) if sample else 0,
            },
            "preferred_markets": _market_leans(sample, wins, losses, over_25, btts, clean_sheets, blanks),
            "analyst_score": round(analyst_score, 2),
            "trend": "rising" if form[:3].count("W") >= 2 else "falling" if form[:3].count("L") >= 2 else "stable",
            "recent_briefs": [row["brief"] for row in rows[:6] if row["brief"]],
            "venue_split": venue_split,
        },
        table=table,
        web_context=web_context,
    )
    strengths: list[str] = []
    risks: list[str] = []
    if ppg >= 2:
        strengths.append("results_consistency")
    if gf_avg >= 1.6:
        strengths.append("attacking_output")
    if sample and clean_sheets / sample >= 0.38:
        strengths.append("defensive_clean_sheets")
    if ga_avg >= 1.6:
        risks.append("concedes_too_often")
    if sample and blanks / sample >= 0.35:
        risks.append("failed_to_score_risk")
    if ppg <= 1:
        risks.append("low_points_return")
    markets = _market_leans(sample, wins, losses, over_25, btts, clean_sheets, blanks)
    return {
        "sample_size": sample,
        "record": {"wins": wins, "draws": draws, "losses": losses, "form": form},
        "goals": {
            "for_avg": round(gf_avg, 2),
            "against_avg": round(ga_avg, 2),
            "over_2_5_rate": round(over_25 / sample, 3) if sample else 0,
            "btts_rate": round(btts / sample, 3) if sample else 0,
            "clean_sheet_rate": round(clean_sheets / sample, 3) if sample else 0,
            "blank_rate": round(blanks / sample, 3) if sample else 0,
        },
        "strengths": strengths or ["insufficient_clear_strength"],
        "risks": risks or ["no_major_risk_detected"],
        "preferred_markets": markets,
        "analyst_score": round(analyst_score, 2),
        "trend": "rising" if form[:3].count("W") >= 2 else "falling" if form[:3].count("L") >= 2 else "stable",
        "recent_briefs": [row["brief"] for row in rows[:6] if row["brief"]],
        "league_name": league_name,
        "position": position,
        "table": table,
        "web_context": web_context,
        "overview": overview,
        "last_web_context_at": last_web_context_at,
        "venue_split": venue_split,
        "prediction_context": {
            "usable": sample >= 3,
            "confidence": "high" if sample >= 8 else "medium" if sample >= 4 else "low",
            "market_focus": [item["market"] for item in markets[:3]],
            "boost_signals": strengths[:3],
            "risk_signals": risks[:3],
        },
    }


def _market_leans(sample: int, wins: int, losses: int, over_25: int, btts: int, clean_sheets: int, blanks: int) -> list[dict[str, Any]]:
    if not sample:
        return [{"market": "no_pick", "confidence": "low", "reason": "not_enough_matches"}]
    leans: list[dict[str, Any]] = []
    if wins / sample >= 0.55:
        leans.append({"market": "team_to_win_or_dnb", "confidence": "medium", "reason": "strong_win_rate"})
    if losses / sample <= 0.25:
        leans.append({"market": "double_chance", "confidence": "medium", "reason": "low_loss_rate"})
    if over_25 / sample >= 0.55:
        leans.append({"market": "over_2_5", "confidence": "medium", "reason": "open_games"})
    if btts / sample >= 0.55:
        leans.append({"market": "btts_yes", "confidence": "medium", "reason": "both_teams_scoring_pattern"})
    if clean_sheets / sample >= 0.38:
        leans.append({"market": "btts_no_or_opponent_under", "confidence": "medium", "reason": "clean_sheet_profile"})
    if blanks / sample >= 0.35:
        leans.append({"market": "team_under_goals", "confidence": "medium", "reason": "blank_risk"})
    return leans[:4] or [{"market": "context_only", "confidence": "low", "reason": "mixed_profile"}]


def _venue_split(rows: list[sqlite3.Row]) -> dict[str, Any]:
    buckets = {
        "home": {"played": 0, "wins": 0, "draws": 0, "losses": 0, "goals_for": 0, "goals_against": 0, "btts": 0, "over_25": 0, "clean_sheets": 0, "blanks": 0},
        "away": {"played": 0, "wins": 0, "draws": 0, "losses": 0, "goals_for": 0, "goals_against": 0, "btts": 0, "over_25": 0, "clean_sheets": 0, "blanks": 0},
    }
    for row in rows:
        side = str(row["team_side"] or "").strip().lower()
        if side not in buckets:
            side = _infer_team_side_from_row(row)
        if side not in buckets:
            continue
        gf = _to_int(row["goals_for"])
        ga = _to_int(row["goals_against"])
        if gf is None or ga is None:
            continue
        bucket = buckets[side]
        bucket["played"] += 1
        bucket["goals_for"] += gf
        bucket["goals_against"] += ga
        if gf > ga:
            bucket["wins"] += 1
        elif gf < ga:
            bucket["losses"] += 1
        else:
            bucket["draws"] += 1
        if gf > 0 and ga > 0:
            bucket["btts"] += 1
        if gf + ga >= 3:
            bucket["over_25"] += 1
        if ga == 0:
            bucket["clean_sheets"] += 1
        if gf == 0:
            bucket["blanks"] += 1
    for bucket in buckets.values():
        played = bucket["played"] or 0
        bucket["win_rate"] = round(bucket["wins"] / played, 3) if played else 0
        bucket["draw_rate"] = round(bucket["draws"] / played, 3) if played else 0
        bucket["loss_rate"] = round(bucket["losses"] / played, 3) if played else 0
        bucket["ppg"] = round((bucket["wins"] * 3 + bucket["draws"]) / played, 3) if played else 0
        bucket["gf_avg"] = round(bucket["goals_for"] / played, 2) if played else 0
        bucket["ga_avg"] = round(bucket["goals_against"] / played, 2) if played else 0
        bucket["btts_rate"] = round(bucket["btts"] / played, 3) if played else 0
        bucket["over_25_rate"] = round(bucket["over_25"] / played, 3) if played else 0
        bucket["clean_sheet_rate"] = round(bucket["clean_sheets"] / played, 3) if played else 0
        bucket["blank_rate"] = round(bucket["blanks"] / played, 3) if played else 0
    home = buckets["home"]
    away = buckets["away"]
    return {
        "home": home,
        "away": away,
        "preferred_side": "away" if away["ppg"] > home["ppg"] else "home" if home["ppg"] > away["ppg"] else "even",
        "away_bias": round(away["ppg"] - home["ppg"], 3),
        "note": "Away-side rows are treated as away by default; home-side rows are treated as home.",
    }


def _infer_team_side_from_row(row: sqlite3.Row) -> str:
    raw = _loads(row["raw_match_json"], {})
    team_name = str(row["team_name"] or "").strip().lower()
    home_name = str(raw.get("home_team") or "").strip().lower()
    away_name = str(raw.get("away_team") or "").strip().lower()
    if team_name and home_name and team_name == home_name:
        return "home"
    if team_name and away_name and team_name == away_name:
        return "away"
    return ""


def _watcher_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if not row:
        return {}
    profile = _loads(row["profile_json"], {})
    return {
        "team_key": row["team_key"],
        "team_name": row["team_name"],
        "sporty_team_id": row["sporty_team_id"],
        "sofascore_team_id": row["sofascore_team_id"],
        "aliases": _loads(row["aliases_json"], []),
        "analyst_name": row["analyst_name"],
        "profile": profile,
        "league_name": row["league_name"],
        "position": row["position"],
        "table": _loads(row["table_json"], {}),
        "web_context": _loads(row["web_context_json"], {}),
        "overview": _loads(row["overview_json"], {}),
        "venue_split": profile.get("venue_split") or {},
        "last_web_context_at": row["last_web_context_at"],
        "match_count": row["match_count"],
        "last_match_id": row["last_match_id"],
        "last_analysis": _loads(row["last_analysis_json"], None),
        "updated_at": row["updated_at"],
    }


def _match_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "match_id": row["match_id"],
        "match_date": row["match_date"],
        "team_name": row["team_name"],
        "team_side": row["team_side"],
        "sporty_team_id": row["sporty_team_id"],
        "sofascore_team_id": row["sofascore_team_id"],
        "opponent": row["opponent"],
        "venue": row["venue"],
        "tournament": row["tournament"],
        "league_name": row["league_name"],
        "team_position": row["team_position"],
        "opponent_position": row["opponent_position"],
        "table_gap": row["table_gap"],
        "goals_for": row["goals_for"],
        "goals_against": row["goals_against"],
        "result": row["result"],
        "status": row["status"],
        "prediction": _loads(row["prediction_json"], None),
        "analysis": _loads(row["analysis_json"], None),
        "brief": row["brief"],
        "web_context": _loads(row["web_context_json"], {}),
        "raw_match": _loads(row["raw_match_json"], {}),
        "created_at": row["created_at"],
    }


def _get_finished_or_buffered_match(match_id: str) -> dict[str, Any] | None:
    try:
        from app.mongo_store import get_finished_match
        doc = get_finished_match(match_id)
        if doc:
            return doc
    except Exception:
        pass
    try:
        from app.buffer import get_buffered_match
        return get_buffered_match(match_id)
    except Exception:
        return None


def _loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value) if isinstance(value, str) else value
    except Exception:
        return fallback


def _team_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("shortName") or value.get("short_name") or "").strip()
    return str(value or "").strip()


def _slug(value: str) -> str:
    return "-".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())


def _to_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _analysis_summary(analysis: dict[str, Any] | None) -> str:
    if not isinstance(analysis, dict):
        return ""
    for key in ("analysis", "summary", "recommendation", "consensus", "reasoning"):
        value = analysis.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    return ""


def _resolve_watcher_row(conn: sqlite3.Connection, identifier: str | None, team: dict[str, Any] | None = None) -> sqlite3.Row | None:
    if not identifier and not team:
        return None
    rows = conn.execute("select * from ai_team_watchers").fetchall()
    best_row: sqlite3.Row | None = None
    best_score = 0
    tokens = _watcher_tokens(team or {}, identifier)
    for row in rows:
        score = _watcher_match_score(row, tokens)
        if score > best_score:
            best_row = row
            best_score = score
    return best_row


def _resolve_watcher_key(conn: sqlite3.Connection, team: dict[str, Any]) -> str:
    row = _resolve_watcher_row(conn, team.get("team_key"), team=team)
    return row["team_key"] if row else team["team_key"]


def _watcher_tokens(team: dict[str, Any], identifier: str | None = None) -> set[str]:
    tokens: set[str] = set()
    if identifier:
        tokens.add(_norm_token(identifier))
    for key in ("team_key", "sporty_team_id", "sofascore_team_id", "team_name", "league_name", "position"):
        value = team.get(key)
        if value not in (None, ""):
            tokens.add(_norm_token(value))
    for alias in team.get("aliases") or []:
        if isinstance(alias, dict):
            for key in ("id", "provider", "label"):
                value = alias.get(key)
                if value not in (None, ""):
                    tokens.add(_norm_token(value))
        elif alias not in (None, ""):
            tokens.add(_norm_token(alias))
    return {token for token in tokens if token}


def _watcher_match_score(row: sqlite3.Row, tokens: set[str]) -> int:
    row_tokens = {
        _norm_token(row["team_key"]),
        _norm_token(row["sporty_team_id"]),
        _norm_token(row["sofascore_team_id"]),
        _norm_token(row["team_name"]),
        _norm_token(row["league_name"]),
        _norm_token(row["position"]),
    }
    for alias in _loads(row["aliases_json"], []):
        if isinstance(alias, dict):
            for key in ("id", "provider", "label"):
                value = alias.get(key)
                if value not in (None, ""):
                    row_tokens.add(_norm_token(value))
        elif alias not in (None, ""):
            row_tokens.add(_norm_token(alias))
    if not tokens.intersection(row_tokens):
        return 0
    score = 0
    if _norm_token(row["team_key"]) in tokens:
        score += 100
    if _norm_token(row["sporty_team_id"]) in tokens:
        score += 80
    if _norm_token(row["sofascore_team_id"]) in tokens:
        score += 76
    if _norm_token(row["team_name"]) in tokens:
        score += 60
    if tokens.intersection(row_tokens):
        score += 20
    return score


def _merge_aliases(existing: sqlite3.Row | None, team: dict[str, Any]) -> list[dict[str, Any]]:
    aliases: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(alias: Any) -> None:
        if isinstance(alias, dict):
            provider = str(alias.get("provider") or "").strip()
            alias_id = str(alias.get("id") or "").strip()
            if not provider or not alias_id:
                return
            key = (provider, alias_id)
            if key in seen:
                return
            seen.add(key)
            item = {"provider": provider, "id": alias_id}
            if alias.get("label"):
                item["label"] = alias.get("label")
            aliases.append(item)

    if existing:
        for alias in _loads(existing["aliases_json"], []):
            add(alias)
        add({"provider": "name", "id": _slug(existing["team_name"]), "label": existing["team_name"]})

    add({"provider": "name", "id": _slug(team.get("team_name") or ""), "label": team.get("team_name")})
    add({"provider": "sporty", "id": team.get("sporty_team_id") or "", "label": team.get("team_name")})
    add({"provider": "sofascore", "id": team.get("sofascore_team_id") or "", "label": team.get("team_name")})
    for alias in team.get("aliases") or []:
        add(alias)
    return aliases


def _norm_token(value: Any) -> str:
    return _slug(str(value or "").strip())


def _league_name_for_doc(doc: dict[str, Any]) -> str:
    tournament = doc.get("tournament")
    if isinstance(tournament, dict):
        value = tournament.get("name") or tournament.get("shortName") or tournament.get("short_name")
        if value:
            return str(value)
    if doc.get("league_name"):
        return str(doc.get("league_name"))
    raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else {}
    value = raw_sporty.get("tournament")
    if isinstance(value, dict):
        return str(value.get("name") or value.get("shortName") or value.get("short_name") or "")
    return str(value or "").strip()


def _league_name_from_rows(rows: list[sqlite3.Row]) -> str:
    for row in rows:
        value = row["league_name"] if "league_name" in row.keys() else None
        if value:
            return str(value)
        tournament = row["tournament"] if "tournament" in row.keys() else None
        if tournament:
            return str(tournament)
    return ""


def _table_lookup(doc: dict[str, Any]) -> dict[str, Any]:
    table = doc.get("standings") or doc.get("league_table") or ((doc.get("sofascore_detail") or {}).get("standings") if isinstance(doc.get("sofascore_detail"), dict) else None)
    return table if isinstance(table, dict) else table or {}


def _table_from_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return []


def _team_position(table: Any, team_name: str, sporty_id: Any, sofa_id: Any, return_row: bool = False) -> Any:
    if not isinstance(table, list):
        return None
    for row in table:
        if not isinstance(row, dict):
            continue
        team = row.get("team") if isinstance(row.get("team"), dict) else row.get("team") or {}
        row_name = _team_name(team)
        row_id = team.get("id") if isinstance(team, dict) else None
        if row_id and str(row_id) in {str(sporty_id or ""), str(sofa_id or "")}:
            return row if return_row else row.get("position")
        if team_name and row_name and (team_name.lower() in row_name.lower() or row_name.lower() in team_name.lower()):
            return row if return_row else row.get("position")
    return None


def _position_value(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    value = row.get("position")
    return str(value) if value not in (None, "") else None


def _table_gap(team_row: Any, opponent_row: Any) -> int | None:
    if not isinstance(team_row, dict) or not isinstance(opponent_row, dict):
        return None
    try:
        team_pos = int(team_row.get("position"))
        opp_pos = int(opponent_row.get("position"))
        return opp_pos - team_pos
    except Exception:
        return None


def _team_web_context(team: dict[str, Any], doc: dict[str, Any], team_row: Any, opponent_row: Any) -> dict[str, Any]:
    team_name = team.get("team_name") or "team"
    league_name = team.get("league_name") or _league_name_for_doc(doc)
    position = team.get("position") or _position_value(team_row)
    watcher_context = team.get("web_context")
    if isinstance(watcher_context, dict) and watcher_context:
        return watcher_context
    return {"team_name": team_name, "league_name": league_name, "position": position}


def _should_refresh_web_context(team: dict[str, Any] | sqlite3.Row | None, sample: int = 0) -> bool:
    if not team:
        return True
    if isinstance(team, sqlite3.Row):
        last = str(team["last_web_context_at"] or "").strip() if "last_web_context_at" in team.keys() else ""
        has_context = bool(team["web_context_json"]) if "web_context_json" in team.keys() else False
    else:
        last = str(team.get("last_web_context_at") or "").strip()
        has_context = bool(team.get("web_context"))
    if sample == 0 and not has_context:
        return True
    if not last:
        return not has_context or sample < 3
    try:
        ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
        return age.total_seconds() >= 60 * 60 * 24 * 3 and sample < 8
    except Exception:
        return True


def _build_overview(team_name: str, league_name: str | None, position: Any, profile: dict[str, Any], table: Any, web_context: dict[str, Any]) -> dict[str, Any]:
    record = profile.get("record") or {}
    goals = profile.get("goals") or {}
    sample_size = int(profile.get("sample_size") or 0)
    position_text = f"position {position}" if position not in (None, "") else "an unranked spot"
    form = str(record.get("form") or "")
    summary = f"{team_name} are {position_text} in {league_name or 'their league'} with form {form or 'n/a'}."
    if sample_size == 0:
        summary = f"{team_name} is a new watch entry in {league_name or 'their league'} and still building a meaningful history."
    web_summary = ""
    grok = web_context.get("grok_analysis") if isinstance(web_context, dict) else {}
    if isinstance(grok, dict):
        web_summary = str(grok.get("summary") or grok.get("analysis") or grok.get("result") or "").strip()
    if web_summary:
        summary = f"{summary} Online context: {web_summary[:280]}"
    return {
        "summary": summary[:1000],
        "team_goal": _infer_team_goal(position, goals, league_name),
        "table_snapshot": table if isinstance(table, list) else [],
        "learned_signals": {
            "strengths": profile.get("strengths") or [],
            "risks": profile.get("risks") or [],
            "preferred_markets": (profile.get("preferred_markets") or [])[:4],
        },
        "web_summary": web_summary[:500],
    }


def _infer_team_goal(position: Any, goals: dict[str, Any], league_name: str | None) -> str:
    try:
        pos = int(position) if position not in (None, "") else None
    except Exception:
        pos = None
    if pos is not None:
        if pos <= 4:
            return "challenge at the top of the table"
        if pos <= 8:
            return "push for Europe or a strong upper-half finish"
        if pos >= 16:
            return "protect league status and avoid relegation trouble"
    if (goals.get("for_avg") or 0) > (goals.get("against_avg") or 0):
        return "stay positive and keep scoring pressure on"
    return f"build stability in {league_name or 'the league'}"


def _matchup_context(home: dict[str, Any], away: dict[str, Any], match_doc: dict[str, Any]) -> dict[str, Any]:
    home_score = _profile_score(home, "home")
    away_score = _profile_score(away, "away")
    better = "home" if home_score > away_score else "away" if away_score > home_score else "even"
    clean_sheet_edge = "home" if _rate(home, "clean_sheet_rate") > _rate(away, "clean_sheet_rate") else "away" if _rate(away, "clean_sheet_rate") > _rate(home, "clean_sheet_rate") else "even"
    goal_edge = "home" if _rate(home, "for_avg") > _rate(away, "for_avg") else "away" if _rate(away, "for_avg") > _rate(home, "for_avg") else "even"
    return {
        "better_team": better,
        "better_team_reason": _matchup_reason(home, away, better),
        "clean_sheet_edge": clean_sheet_edge,
        "goal_edge": goal_edge,
        "home": {"team_name": home.get("team_name"), "score": home_score, "overview": home.get("overview") or home.get("profile", {}).get("overview")},
        "away": {"team_name": away.get("team_name"), "score": away_score, "overview": away.get("overview") or away.get("profile", {}).get("overview")},
        "match": {
            "match_id": str(match_doc.get("_id") or match_doc.get("sportybet_id") or match_doc.get("match_id") or match_doc.get("id") or ""),
            "league_name": _league_name_for_doc(match_doc),
        },
    }


def _profile_score(team: dict[str, Any], side: str | None = None) -> float:
    profile = team.get("profile") if isinstance(team.get("profile"), dict) else {}
    if not profile:
        profile = team.get("profile", {}) if isinstance(team.get("profile"), dict) else {}
    score = float(profile.get("analyst_score") or 0)
    record = profile.get("record") or {}
    goals = profile.get("goals") or {}
    score += float(record.get("wins") or 0) * 0.8
    score += float(goals.get("for_avg") or 0) * 1.5
    score -= float(goals.get("against_avg") or 0) * 1.2
    venue_split = profile.get("venue_split") if isinstance(profile.get("venue_split"), dict) else {}
    if side in {"home", "away"} and isinstance(venue_split.get(side), dict):
        split = venue_split.get(side) or {}
        score += float(split.get("ppg") or 0) * 3.5
        score += float(split.get("gf_avg") or 0) * 0.8
        score -= float(split.get("ga_avg") or 0) * 0.6
    return score


def _rate(team: dict[str, Any], key: str) -> float:
    profile = team.get("profile") if isinstance(team.get("profile"), dict) else {}
    goals = profile.get("goals") or {}
    value = goals.get(key)
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _matchup_reason(home: dict[str, Any], away: dict[str, Any], better: str) -> str:
    if better == "even":
        return "Both teams look evenly matched on the current memory profile."
    winner = home if better == "home" else away
    loser = away if better == "home" else home
    winner_name = winner.get("team_name") or better
    loser_name = loser.get("team_name") or ("away" if better == "home" else "home")
    return f"{winner_name} currently carry the stronger memory profile than {loser_name}."
