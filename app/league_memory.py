from __future__ import annotations

import re
import sqlite3
import json
from pathlib import Path
from typing import Any

from app.config import get_settings


DB_PATH = get_settings().database_path


def observe_match(source: str, match: dict[str, Any]) -> dict[str, Any]:
    _init_db()
    league = _league_from_match(match)
    match_id = str(match.get("id") or match.get("match_id") or "")
    if not match_id or not league:
        return {"recorded": False, "reason": "missing match_id or league"}

    score = match.get("score") or {}
    home_goals = _to_int(score.get("home"), 0)
    away_goals = _to_int(score.get("away"), 0)
    total_goals = home_goals + away_goals
    minute = _match_minute(match)
    raw_status = match.get("status") or {}
    status_type = raw_status.get("type") if isinstance(raw_status, dict) else raw_status
    status_text = str(status_type or "").lower()
    status_code = raw_status.get("code") if isinstance(raw_status, dict) else raw_status
    is_finished = status_text in {"finished", "ended", "100"} or status_code == 100
    is_live = status_text in {"inprogress", "live"} or minute > 0

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        duplicate_info = _detect_duplicate_or_replay(conn, source, match_id, league, match, home_goals, away_goals, minute, is_finished)
        _upsert_match(conn, source, match_id, league, match, home_goals, away_goals, is_finished)
        timeline_snapshot_recorded = False
        late_snapshot_recorded = False
        resolved = 0
        if is_live and not duplicate_info.get("is_duplicate"):
            timeline_snapshot_recorded = _insert_match_snapshot(
                conn,
                source,
                match_id,
                league,
                match,
                minute,
                home_goals,
                away_goals,
            )
            if minute >= 70 and abs(home_goals - away_goals) <= 1:
                late_snapshot_recorded = _insert_late_snapshot(conn, source, match_id, league, minute, total_goals, home_goals - away_goals)
        if is_finished:
            resolved = _resolve_snapshots(conn, source, match_id, home_goals, away_goals)
            _aggregate_resolved_snapshots(conn)
        conn.commit()

    mongo_archived = False
    if is_finished:
        mongo_archived = _archive_finished_match(source, match, league)

    return {
        "recorded": True,
        "source": source,
        "match_id": match_id,
        "league": league,
        "mongo_archived": mongo_archived,
        "snapshot_recorded": timeline_snapshot_recorded or late_snapshot_recorded,
        "timeline_snapshot_recorded": timeline_snapshot_recorded,
        "late_snapshot_recorded": late_snapshot_recorded,
        "snapshots_resolved": resolved,
        "duplicate": duplicate_info,
    }


def observe_matches(source: str, matches: list[dict[str, Any]]) -> dict[str, Any]:
    results = [observe_match(source, match) for match in matches]
    return {
        "observed": len(results),
        "recorded": sum(1 for item in results if item.get("recorded")),
        "mongo_archived": sum(1 for item in results if item.get("mongo_archived")),
        "snapshots_recorded": sum(1 for item in results if item.get("snapshot_recorded")),
        "timeline_snapshots_recorded": sum(1 for item in results if item.get("timeline_snapshot_recorded")),
        "late_snapshots_recorded": sum(1 for item in results if item.get("late_snapshot_recorded")),
        "snapshots_resolved": sum(item.get("snapshots_resolved", 0) for item in results),
    }


def _archive_finished_match(source: str, match: dict[str, Any], league: str) -> bool:
    try:
        from app.mongo_store import save_finished_match

        archived = {
            **match,
            "league_key": normalize_league(league),
            "league_name": league,
        }
        return save_finished_match(source, archived)
    except Exception:
        return False


def league_memory_for_match(match: dict[str, Any]) -> dict[str, Any]:
    return get_league_memory(_league_from_match(match))


def get_league_memory(league: str | None = None) -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if league:
            key = normalize_league(league)
            row = conn.execute(
                """
                select
                    league_key,
                    max(league_name) as league_name,
                    count(*) as samples,
                    sum(case when had_late_goal = 1 then 1 else 0 end) as late_goals,
                    avg(had_late_goal) as late_goal_rate
                from late_goal_snapshots
                where league_key = ? and had_late_goal is not null
                group by league_key
                """,
                (key,),
            ).fetchone()
            return _memory_row(row, key, league)

        rows = conn.execute(
            """
            select
                league_key,
                max(league_name) as league_name,
                count(*) as samples,
                sum(case when had_late_goal = 1 then 1 else 0 end) as late_goals,
                avg(had_late_goal) as late_goal_rate
            from late_goal_snapshots
            where had_late_goal is not null
            group by league_key
            order by samples desc, late_goal_rate desc
            """
        ).fetchall()
        return {"leagues": [_memory_row(row) for row in rows]}


def get_snapshot_memory(
    league: str | None = None,
    minute_bucket: str | None = None,
    score_state: str | None = None,
    min_samples: int = 1,
) -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        _aggregate_resolved_snapshots(conn)
        conn.commit()

    clauses = ["1 = 1"]
    params: list[Any] = []
    if league:
        clauses.append("league_key = ?")
        params.append(normalize_league(league))
    if minute_bucket:
        clauses.append("minute_bucket = ?")
        params.append(minute_bucket)
    if score_state:
        clauses.append("score_state = ?")
        params.append(score_state)
    where = " and ".join(clauses)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select
                league_key,
                max(league_name) as league_name,
                minute_bucket,
                score_state,
                sum(samples) as samples,
                sum(next_goal_hits) as next_goal_hits,
                sum(over_1_5_hits) as over_1_5_hits,
                sum(over_2_5_hits) as over_2_5_hits,
                sum(favorite_recovered_hits) as favorite_recovered_hits,
                sum(red_card_team_conceded_hits) as red_card_team_conceded_hits
            from snapshot_aggregates
            where {where}
            group by league_key, minute_bucket, score_state
            having sum(samples) >= ?
            order by samples desc, (sum(next_goal_hits) * 1.0 / sum(samples)) desc
            limit 200
            """,
            (*params, min_samples),
        ).fetchall()
    return {"snapshots": [_snapshot_memory_row(row) for row in rows]}


def list_memory_matches(limit: int = 200, league: str | None = None, source: str | None = None) -> dict[str, Any]:
    _init_db()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if league:
        clauses.append("league_key = ?")
        params.append(normalize_league(league))
    if source:
        clauses.append("source = ?")
        params.append(source)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select source, match_id, league_key, league_name, home_team, away_team,
                   final_home_goals, final_away_goals, is_finished, last_seen_at
            from matches
            where {" and ".join(clauses)}
            order by last_seen_at desc
            limit ?
            """,
            (*params, limit),
        ).fetchall()
    return {"matches": [_match_row(row) for row in rows]}


def list_duplicate_matches(limit: int = 200) -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select *
            from match_duplicates
            order by detected_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return {
        "duplicates": [
            {
                "id": row["id"],
                "source": row["source"],
                "match_id": row["match_id"],
                "duplicate_of_source": row["duplicate_of_source"],
                "duplicate_of_match_id": row["duplicate_of_match_id"],
                "reason": row["reason"],
                "confidence": row["confidence"],
                "detected_at": row["detected_at"],
            }
            for row in rows
        ]
    }


def get_memory_match(match_id: str, source: str | None = None) -> dict[str, Any] | None:
    _init_db()
    clauses = ["match_id = ?"]
    params: list[Any] = [match_id]
    if source:
        clauses.append("source = ?")
        params.append(source)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        match = conn.execute(
            f"""
            select source, match_id, league_key, league_name, home_team, away_team,
                   final_home_goals, final_away_goals, is_finished, last_seen_at
            from matches
            where {" and ".join(clauses)}
            order by last_seen_at desc
            limit 1
            """,
            params,
        ).fetchone()
        if not match:
            return None
        snapshots = conn.execute(
            """
            select *
            from match_snapshots
            where source = ? and match_id = ?
            order by minute asc, observed_at asc
            """,
            (match["source"], match["match_id"]),
        ).fetchall()
        predictions = conn.execute(
            """
            select *
            from prediction_history
            where source = ? and match_id = ?
            order by created_at desc
            """,
            (match["source"], match["match_id"]),
        ).fetchall()
    return {
        **_match_row(match),
        "snapshots": [_snapshot_row(row) for row in snapshots],
        "predictions": [_prediction_row(row) for row in predictions],
    }


def list_countries_from_memory() -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select league_key, max(league_name) as league_name, count(*) as matches
            from matches
            group by league_key
            order by league_name
            """
        ).fetchall()
    countries: dict[str, dict[str, Any]] = {}
    for row in rows:
        country = _country_from_league(row["league_name"])
        key = normalize_league(country)
        countries.setdefault(key, {"id": key, "name": country, "leagues": [], "match_count": 0})
        countries[key]["leagues"].append({
            "id": row["league_key"],
            "name": row["league_name"],
            "match_count": row["matches"],
        })
        countries[key]["match_count"] += row["matches"]
    return {"countries": list(countries.values())}


def get_country_from_memory(country_id: str) -> dict[str, Any]:
    countries = list_countries_from_memory()["countries"]
    key = normalize_league(country_id)
    country = next((item for item in countries if item["id"] == key), None)
    return country or {"id": key, "name": country_id, "leagues": [], "match_count": 0}


def get_league_detail_from_memory(league_id: str) -> dict[str, Any]:
    memory = get_league_memory(league_id)
    snapshots = get_snapshot_memory(league=league_id, min_samples=1)["snapshots"]
    matches = list_memory_matches(limit=100, league=league_id)["matches"]
    standings = _standings_from_matches(matches)
    return {
        "id": normalize_league(league_id),
        "name": memory.get("league_name") or league_id,
        "memory": memory,
        "snapshot_groups": snapshots,
        "standings": standings,
        "recent_matches": matches,
    }


def run_memory_maintenance(raw_retention_days: int = 30, odds_retention_days: int = 60) -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        before = conn.total_changes
        _aggregate_resolved_snapshots(conn)
        conn.execute(
            """
            delete from match_snapshots
            where resolved_at is not null
              and datetime(resolved_at) < datetime('now', ?)
            """,
            (f"-{raw_retention_days} days",),
        )
        conn.execute(
            """
            delete from late_goal_snapshots
            where resolved_at is not null
              and datetime(resolved_at) < datetime('now', ?)
            """,
            (f"-{raw_retention_days} days",),
        )
        conn.execute(
            """
            delete from odds_snapshots
            where datetime(snapshot_time) < datetime('now', ?)
            """,
            (f"-{odds_retention_days} days",),
        )
        conn.commit()
        changed = conn.total_changes - before
        conn.execute("vacuum")
    return {
        "status": "success",
        "rows_changed": changed,
        "raw_retention_days": raw_retention_days,
        "odds_retention_days": odds_retention_days,
    }


def record_prediction(prediction: dict[str, Any]) -> None:
    _init_db()
    match_id = str(prediction.get("match_id") or "")
    source = prediction.get("source") or "unknown"
    if not match_id:
        return
    best_pick = (prediction.get("picks") or [{}])[0]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            insert into prediction_history (
                source, match_id, match_name, league_name, pick_type, selection,
                confidence, reason, signals_json, picks_json, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
            """,
            (
                source,
                match_id,
                prediction.get("name"),
                _league_from_match(prediction),
                best_pick.get("type"),
                best_pick.get("selection"),
                best_pick.get("confidence"),
                best_pick.get("reason"),
                json.dumps(prediction.get("signals") or []),
                json.dumps(prediction.get("picks") or []),
            ),
        )
        conn.commit()


def grade_prediction(prediction_id: int, final_home: int, final_away: int) -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select * from prediction_history where id = ?", (prediction_id,)).fetchone()
        if not row:
            return {"graded": False, "reason": "not found"}
        result = _grade_pick(row["pick_type"], row["selection"], final_home, final_away)
        conn.execute(
            "update prediction_history set result = ?, final_home = ?, final_away = ?, graded_at = current_timestamp where id = ?",
            (result, final_home, final_away, prediction_id),
        )
        conn.commit()
    return {"graded": True, "id": prediction_id, "result": result, "final_home": final_home, "final_away": final_away}


def grade_predictions_for_date(match_date: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    _init_db()
    finished = {str(e["id"]): e for e in events if (e.get("status") or {}).get("type") == "finished"}
    if not finished:
        return {"graded": 0, "skipped": 0, "no_finished_events": True}

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select id, match_id, pick_type, selection
            from prediction_history
            where graded_at is null
              and date(created_at) = ?
            """,
            (match_date,),
        ).fetchall()

    graded = skipped = 0
    for row in rows:
        event = finished.get(str(row["match_id"]))
        if not event:
            skipped += 1
            continue
        score = event.get("score") or {}
        final_home = _to_int(score.get("home"), 0)
        final_away = _to_int(score.get("away"), 0)
        result = _grade_pick(row["pick_type"], row["selection"], final_home, final_away)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "update prediction_history set result = ?, final_home = ?, final_away = ?, graded_at = current_timestamp where id = ?",
                (result, final_home, final_away, row["id"]),
            )
            conn.commit()
        graded += 1

    return {"graded": graded, "skipped": skipped, "date": match_date}


def get_grading_metrics() -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        totals = conn.execute(
            """
            select
                count(*) as total,
                sum(case when graded_at is not null then 1 else 0 end) as graded,
                sum(case when result = 'win' then 1 else 0 end) as wins,
                sum(case when result = 'loss' then 1 else 0 end) as losses,
                sum(case when result = 'void' then 1 else 0 end) as voids
            from prediction_history
            where pick_type != 'no_bet'
            """
        ).fetchone()
        by_type = conn.execute(
            """
            select pick_type,
                   count(*) as total,
                   sum(case when result = 'win' then 1 else 0 end) as wins,
                   sum(case when result = 'loss' then 1 else 0 end) as losses
            from prediction_history
            where graded_at is not null and pick_type != 'no_bet'
            group by pick_type
            order by total desc
            """
        ).fetchall()
        recent = conn.execute(
            """
            select result, confidence, match_name, selection, created_at
            from prediction_history
            where graded_at is not null and pick_type != 'no_bet'
            order by graded_at desc
            limit 20
            """
        ).fetchall()

    graded = totals["graded"] or 0
    wins = totals["wins"] or 0
    losses = totals["losses"] or 0
    win_rate = round(wins / graded, 3) if graded else None
    return {
        "total_predictions": totals["total"] or 0,
        "graded": graded,
        "pending": (totals["total"] or 0) - graded,
        "wins": wins,
        "losses": losses,
        "voids": totals["voids"] or 0,
        "win_rate": win_rate,
        "win_percent": round(win_rate * 100, 1) if win_rate is not None else None,
        "by_type": [
            {
                "pick_type": r["pick_type"],
                "total": r["total"],
                "wins": r["wins"] or 0,
                "losses": r["losses"] or 0,
                "win_rate": round((r["wins"] or 0) / r["total"], 3) if r["total"] else None,
            }
            for r in by_type
        ],
        "recent": [
            {
                "result": r["result"],
                "confidence": r["confidence"],
                "match": r["match_name"],
                "selection": r["selection"],
                "created_at": r["created_at"],
            }
            for r in recent
        ],
    }


def _grade_pick(pick_type: str | None, selection: str | None, home: int, away: int) -> str:
    total = home + away
    sel = (selection or "").lower()
    pt = (pick_type or "").lower()

    if pt == "no_bet":
        return "void"

    if pt == "goals":
        if "over 2.5" in sel:
            return "win" if total > 2 else "loss"
        if "over 1.5" in sel:
            return "win" if total > 1 else "loss"
        if "over 0.5" in sel:
            return "win" if total > 0 else "loss"
        if "both teams to score" in sel or "btts" in sel:
            return "win" if home > 0 and away > 0 else "loss"
        return "void"

    if pt == "live_goals":
        if "over 0.5" in sel or "next goal" in sel or "late goal" in sel:
            return "win" if total > 0 else "loss"
        return "void"

    if pt == "match_result":
        if " or draw" in sel:
            return "win" if home > away or home == away else "loss"
        if "home" in sel:
            return "win" if home > away else "loss"
        if "away" in sel:
            return "win" if away > home else "loss"
        return "void"

    if pt == "double_chance":
        if "double chance" in sel:
            team_part = sel.replace("double chance", "").strip()
            if "home" in team_part:
                return "win" if home >= away else "loss"
            if "away" in team_part:
                return "win" if away >= home else "loss"
        return "void"

    return "void"


def list_prediction_history(limit: int = 200, match_id: str | None = None) -> dict[str, Any]:
    _init_db()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if match_id:
        clauses.append("match_id = ?")
        params.append(str(match_id))
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select *
            from prediction_history
            where {" and ".join(clauses)}
            order by created_at desc
            limit ?
            """,
            (*params, limit),
        ).fetchall()
    return {"predictions": [_prediction_row(row) for row in rows]}


def save_betbuilder(selections: list[dict[str, Any]], combined_odds: float, confidence: int) -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            insert into betbuilder_history (selections_json, combined_odds, confidence, created_at)
            values (?, ?, ?, current_timestamp)
            """,
            (json.dumps(selections), combined_odds, confidence),
        )
        conn.commit()
        bet_id = cursor.lastrowid
    return {"id": bet_id, "selections": selections, "combined_odds": combined_odds, "confidence": confidence}


def list_betbuilder_history(limit: int = 100) -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select *
            from betbuilder_history
            order by created_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return {
        "bets": [
            {
                "id": row["id"],
                "selections": json.loads(row["selections_json"] or "[]"),
                "combined_odds": row["combined_odds"],
                "confidence": row["confidence"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


def set_engine_status(engine_id: str, status: str) -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            insert into engine_state (id, status, updated_at)
            values (?, ?, current_timestamp)
            on conflict(id) do update set status = excluded.status, updated_at = current_timestamp
            """,
            (engine_id, status),
        )
        conn.commit()
    return {"id": engine_id, "status": status}


def get_engine_states() -> dict[str, str]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("select id, status from engine_state").fetchall()
    return {row[0]: row[1] for row in rows}


def store_enriched_matches(documents: list[dict[str, Any]]) -> int:
    _init_db()
    if not documents:
        return 0
    with sqlite3.connect(DB_PATH) as conn:
        for doc in documents:
            match_id = str(doc.get("sportybet_id") or doc.get("id") or "")
            if not match_id:
                continue
            conn.execute(
                """
                create table if not exists enriched_matches (
                    match_id text primary key,
                    match_date text,
                    tournament text,
                    category text,
                    name text,
                    sofascore_id text,
                    start_time text,
                    enriched_at text not null default current_timestamp,
                    raw_json text not null
                )
                """
            )
            conn.execute(
                """
                insert into enriched_matches (
                    match_id, match_date, tournament, category, name,
                    sofascore_id, start_time, raw_json, enriched_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
                on conflict(match_id) do update set
                    match_date = excluded.match_date,
                    tournament = excluded.tournament,
                    category = excluded.category,
                    name = excluded.name,
                    sofascore_id = excluded.sofascore_id,
                    start_time = excluded.start_time,
                    raw_json = excluded.raw_json,
                    enriched_at = current_timestamp
                """,
                (
                    match_id,
                    doc.get("match_date"),
                    doc.get("tournament"),
                    doc.get("category"),
                    doc.get("sportybet_name") or doc.get("name"),
                    str(doc.get("sofascore_id") or ""),
                    str(doc.get("start_time") or ""),
                    json.dumps(doc),
                ),
            )
        conn.commit()
    try:
        from app.mongo_store import store_enriched_matches as store_mongo_enriched_matches

        store_mongo_enriched_matches(documents)
    except Exception:
        pass
    return len(documents)


def get_enriched_matches(match_date: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    _init_db()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if match_date:
        clauses.append("match_date = ?")
        params.append(match_date)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select raw_json
            from enriched_matches
            where {" and ".join(clauses)}
            order by enriched_at desc
            limit ?
            """,
            (*params, limit),
        ).fetchall()
    docs = [json.loads(row["raw_json"]) for row in rows]
    if docs:
        return docs
    try:
        from app.mongo_store import get_enriched_matches as get_mongo_enriched_matches

        return get_mongo_enriched_matches(match_date=match_date, limit=limit)
    except Exception:
        return []


def get_enriched_match(match_id: str) -> dict[str, Any] | None:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("select raw_json from enriched_matches where match_id = ?", (str(match_id),)).fetchone()
    if row:
        return json.loads(row["raw_json"])
    try:
        from app.mongo_store import get_enriched_match as get_mongo_enriched_match

        return get_mongo_enriched_match(match_id)
    except Exception:
        return None


def patch_enriched_match_live(
    match_id: str,
    period: str | None,
    score: dict[str, Any] | None,
    played_seconds: Any = None,
) -> bool:
    """
    Fast in-place update of period + score for a live match already in the buffer.
    Avoids a full re-enrichment cycle just to show the current score.
    Returns True if the row existed and was updated.
    """
    if not match_id:
        return False
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select raw_json from enriched_matches where match_id = ?", (match_id,)
        ).fetchone()
        if not row:
            return False
        doc = json.loads(row["raw_json"])
        doc["period"] = period
        doc["score"] = score
        if played_seconds is not None:
            doc["played_seconds"] = played_seconds
        conn.execute(
            "update enriched_matches set raw_json = ? where match_id = ?",
            (json.dumps(doc), match_id),
        )
        conn.commit()
    return True


def get_live_matches_from_buffer(limit: int = 200) -> list[dict[str, Any]]:
    """
    Returns all matches currently marked as live (period != 'Not start' and not finished)
    from the enriched_matches buffer, regardless of match_date.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select raw_json from enriched_matches
            where json_extract(raw_json, '$.period') is not null
              and json_extract(raw_json, '$.period') != 'Not start'
              and json_extract(raw_json, '$.period') != ''
            order by enriched_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return [json.loads(row["raw_json"]) for row in rows]


def late_goal_memory_signal(match: dict[str, Any]) -> dict[str, Any] | None:
    memory = league_memory_for_match(match)
    samples = memory.get("samples", 0)
    if samples < 2:
        return None
    rate = memory.get("late_goal_rate", 0)
    # Smoothed around 50%, so tiny samples help but do not dominate.
    adjusted_rate = ((rate * samples) + 1.0) / (samples + 2)
    impact = round((adjusted_rate - 0.5) * 24, 2)
    if abs(impact) < 1.5:
        return None
    return {
        "name": "league_memory_late_goal",
        "value": {
            "league": memory.get("league_name"),
            "samples": samples,
            "late_goal_rate": round(rate, 3),
            "smoothed_rate": round(adjusted_rate, 3),
        },
        "impact": impact,
    }


def normalize_league(value: str | None) -> str:
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split()).strip()


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            create table if not exists matches (
                source text not null,
                match_id text not null,
                league_key text not null,
                league_name text not null,
                match_fingerprint text,
                home_team text,
                away_team text,
                start_time text,
                final_home_goals integer,
                final_away_goals integer,
                is_finished integer not null default 0,
                last_seen_at text not null default current_timestamp,
                primary key (source, match_id)
            )
            """
        )
        conn.execute(
            """
            create table if not exists late_goal_snapshots (
                id integer primary key autoincrement,
                source text not null,
                match_id text not null,
                league_key text not null,
                league_name text not null,
                minute integer not null,
                score_total_at_snapshot integer not null,
                score_diff_at_snapshot integer not null,
                had_late_goal integer,
                final_total_goals integer,
                observed_at text not null default current_timestamp,
                resolved_at text,
                unique (source, match_id, minute, score_total_at_snapshot)
            )
            """
        )
        conn.execute(
            """
            create table if not exists match_snapshots (
                id integer primary key autoincrement,
                source text not null,
                match_id text not null,
                league_key text not null,
                league_name text not null,
                minute integer not null,
                minute_bucket text not null,
                home_goals integer not null,
                away_goals integer not null,
                total_goals integer not null,
                score_diff integer not null,
                score_state text not null,
                favorite_side text,
                favorite_probability real,
                home_red_cards integer not null default 0,
                away_red_cards integer not null default 0,
                red_card_state text not null,
                final_home_goals integer,
                final_away_goals integer,
                final_total_goals integer,
                next_goal_happened integer,
                over_0_5_hit integer,
                over_1_5_hit integer,
                over_2_5_hit integer,
                over_3_5_hit integer,
                home_win_hit integer,
                away_win_hit integer,
                draw_hit integer,
                favorite_won integer,
                favorite_recovered integer,
                red_card_team_conceded integer,
                observed_at text not null default current_timestamp,
                resolved_at text,
                unique (source, match_id, minute_bucket, home_goals, away_goals, home_red_cards, away_red_cards)
            )
            """
        )
        conn.execute(
            """
            create table if not exists snapshot_aggregates (
                league_key text not null,
                league_name text not null,
                minute_bucket text not null,
                score_state text not null,
                red_card_state text not null,
                favorite_side text not null default 'none',
                samples integer not null default 0,
                next_goal_hits integer not null default 0,
                over_1_5_hits integer not null default 0,
                over_2_5_hits integer not null default 0,
                favorite_recovered_hits integer not null default 0,
                red_card_team_conceded_hits integer not null default 0,
                updated_at text not null default current_timestamp,
                primary key (league_key, minute_bucket, score_state, red_card_state, favorite_side)
            )
            """
        )
        conn.execute(
            """
            create table if not exists aggregated_snapshot_ids (
                snapshot_id integer primary key
            )
            """
        )
        conn.execute(
            """
            create table if not exists match_duplicates (
                id integer primary key autoincrement,
                source text not null,
                match_id text not null,
                duplicate_of_source text,
                duplicate_of_match_id text,
                reason text not null,
                confidence real not null,
                detected_at text not null default current_timestamp,
                unique (source, match_id, duplicate_of_source, duplicate_of_match_id, reason)
            )
            """
        )
        conn.execute(
            """
            create table if not exists prediction_history (
                id integer primary key autoincrement,
                source text not null,
                match_id text not null,
                match_name text,
                league_name text,
                pick_type text,
                selection text,
                confidence integer,
                reason text,
                signals_json text not null,
                picks_json text not null,
                created_at text not null default current_timestamp
            )
            """
        )
        conn.execute(
            """
            create table if not exists betbuilder_history (
                id integer primary key autoincrement,
                selections_json text not null,
                combined_odds real,
                confidence integer,
                created_at text not null default current_timestamp
            )
            """
        )
        conn.execute(
            """
            create table if not exists engine_state (
                id text primary key,
                status text not null,
                updated_at text not null default current_timestamp
            )
            """
        )
        conn.execute(
            """
            create table if not exists enriched_matches (
                match_id text primary key,
                match_date text,
                tournament text,
                category text,
                name text,
                sofascore_id text,
                start_time text,
                enriched_at text not null default current_timestamp,
                raw_json text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists odds_snapshots (
                id integer primary key autoincrement,
                match_id text not null,
                match_name text,
                match_date text,
                home_odds real,
                draw_odds real,
                away_odds real,
                home_implied real,
                draw_implied real,
                away_implied real,
                source text,
                snapshot_time text not null default current_timestamp
            )
            """
        )
        _ensure_column(conn, "matches", "match_fingerprint", "text")
        _ensure_column(conn, "matches", "start_time", "text")
        conn.execute("create index if not exists idx_matches_league on matches(league_key)")
        conn.execute("create index if not exists idx_matches_last_seen on matches(last_seen_at)")
        conn.execute("create index if not exists idx_matches_fingerprint on matches(match_fingerprint)")
        conn.execute("create index if not exists idx_snapshots_group on match_snapshots(league_key, minute_bucket, score_state)")
        conn.execute("create index if not exists idx_snapshots_resolved on match_snapshots(resolved_at)")
        conn.execute("create index if not exists idx_predictions_match on prediction_history(match_id)")
        conn.execute("create index if not exists idx_predictions_created on prediction_history(created_at)")
        conn.execute("create index if not exists idx_odds_match on odds_snapshots(match_id)")
        conn.execute("create index if not exists idx_odds_date on odds_snapshots(match_date)")
        _ensure_column(conn, "prediction_history", "result", "text")
        _ensure_column(conn, "prediction_history", "final_home", "integer")
        _ensure_column(conn, "prediction_history", "final_away", "integer")
        _ensure_column(conn, "prediction_history", "graded_at", "text")
        conn.execute("create index if not exists idx_predictions_graded on prediction_history(graded_at)")
        conn.commit()


def _upsert_match(
    conn: sqlite3.Connection,
    source: str,
    match_id: str,
    league: str,
    match: dict[str, Any],
    home_goals: int,
    away_goals: int,
    is_finished: bool,
) -> None:
    league_key = normalize_league(league)
    conn.execute(
        """
        insert into matches (
            source, match_id, league_key, league_name, match_fingerprint, home_team, away_team,
            start_time, final_home_goals, final_away_goals, is_finished, last_seen_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
        on conflict(source, match_id) do update set
            league_key = excluded.league_key,
            league_name = excluded.league_name,
            match_fingerprint = excluded.match_fingerprint,
            home_team = excluded.home_team,
            away_team = excluded.away_team,
            start_time = excluded.start_time,
            final_home_goals = case when excluded.is_finished = 1 then excluded.final_home_goals else matches.final_home_goals end,
            final_away_goals = case when excluded.is_finished = 1 then excluded.final_away_goals else matches.final_away_goals end,
            is_finished = max(matches.is_finished, excluded.is_finished),
            last_seen_at = current_timestamp
        """,
        (
            source,
            match_id,
            league_key,
            league,
            _match_fingerprint(league, match),
            _team_name(match, "home"),
            _team_name(match, "away"),
            str(match.get("start_time") or match.get("start_timestamp") or ""),
            home_goals if is_finished else None,
            away_goals if is_finished else None,
            1 if is_finished else 0,
        ),
    )


def _insert_late_snapshot(
    conn: sqlite3.Connection,
    source: str,
    match_id: str,
    league: str,
    minute: int,
    total_goals: int,
    score_diff: int,
) -> bool:
    league_key = normalize_league(league)
    before = conn.total_changes
    lower, upper = _bucket_bounds(_minute_bucket(minute))
    exists = conn.execute(
        """
        select 1
        from late_goal_snapshots
        where source = ?
          and match_id = ?
          and minute between ? and ?
          and score_total_at_snapshot = ?
          and score_diff_at_snapshot = ?
        limit 1
        """,
        (source, match_id, lower, upper, total_goals, score_diff),
    ).fetchone()
    if exists:
        return False
    conn.execute(
        """
        insert or ignore into late_goal_snapshots (
            source, match_id, league_key, league_name, minute,
            score_total_at_snapshot, score_diff_at_snapshot
        )
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        (source, match_id, league_key, league, minute, total_goals, score_diff),
    )
    return conn.total_changes > before


def _insert_match_snapshot(
    conn: sqlite3.Connection,
    source: str,
    match_id: str,
    league: str,
    match: dict[str, Any],
    minute: int,
    home_goals: int,
    away_goals: int,
) -> bool:
    league_key = normalize_league(league)
    favorite = _favorite_from_match(match)
    home_red_cards = _to_int(match.get("home_red_cards") or match.get("homeRedCards"), 0)
    away_red_cards = _to_int(match.get("away_red_cards") or match.get("awayRedCards"), 0)
    before = conn.total_changes
    exists = conn.execute(
        """
        select 1
        from match_snapshots
        where source = ?
          and match_id = ?
          and minute_bucket = ?
          and home_goals = ?
          and away_goals = ?
          and home_red_cards = ?
          and away_red_cards = ?
        limit 1
        """,
        (source, match_id, _minute_bucket(minute), home_goals, away_goals, home_red_cards, away_red_cards),
    ).fetchone()
    if exists:
        return False
    conn.execute(
        """
        insert or ignore into match_snapshots (
            source, match_id, league_key, league_name, minute, minute_bucket,
            home_goals, away_goals, total_goals, score_diff, score_state,
            favorite_side, favorite_probability, home_red_cards, away_red_cards, red_card_state
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            match_id,
            league_key,
            league,
            minute,
            _minute_bucket(minute),
            home_goals,
            away_goals,
            home_goals + away_goals,
            home_goals - away_goals,
            _score_state(home_goals, away_goals, favorite.get("side")),
            favorite.get("side"),
            favorite.get("probability"),
            home_red_cards,
            away_red_cards,
            _red_card_state(home_red_cards, away_red_cards),
        ),
    )
    return conn.total_changes > before


def _resolve_snapshots(conn: sqlite3.Connection, source: str, match_id: str, final_home_goals: int, final_away_goals: int) -> int:
    final_total_goals = final_home_goals + final_away_goals
    before = conn.total_changes
    conn.execute(
        """
        update late_goal_snapshots
        set
            final_total_goals = ?,
            had_late_goal = case when ? > score_total_at_snapshot then 1 else 0 end,
            resolved_at = current_timestamp
        where source = ?
          and match_id = ?
          and had_late_goal is null
        """,
        (final_total_goals, final_total_goals, source, match_id),
    )
    conn.execute(
        """
        update match_snapshots
        set
            final_home_goals = ?,
            final_away_goals = ?,
            final_total_goals = ?,
            next_goal_happened = case when ? > total_goals then 1 else 0 end,
            over_0_5_hit = case when ? >= 1 then 1 else 0 end,
            over_1_5_hit = case when ? >= 2 then 1 else 0 end,
            over_2_5_hit = case when ? >= 3 then 1 else 0 end,
            over_3_5_hit = case when ? >= 4 then 1 else 0 end,
            home_win_hit = case when ? > ? then 1 else 0 end,
            away_win_hit = case when ? > ? then 1 else 0 end,
            draw_hit = case when ? = ? then 1 else 0 end,
            favorite_won = case
                when favorite_side = 'home' and ? > ? then 1
                when favorite_side = 'away' and ? > ? then 1
                when favorite_side is not null then 0
                else null
            end,
            favorite_recovered = case
                when score_state in ('favorite_losing', 'favorite_drawing') and favorite_side = 'home' and ? >= ? then 1
                when score_state in ('favorite_losing', 'favorite_drawing') and favorite_side = 'away' and ? >= ? then 1
                when score_state in ('favorite_losing', 'favorite_drawing') then 0
                else null
            end,
            red_card_team_conceded = case
                when home_red_cards > away_red_cards and ? > away_goals then 1
                when away_red_cards > home_red_cards and ? > home_goals then 1
                when home_red_cards != away_red_cards then 0
                else null
            end,
            resolved_at = current_timestamp
        where source = ?
          and match_id = ?
          and resolved_at is null
        """,
        (
            final_home_goals,
            final_away_goals,
            final_total_goals,
            final_total_goals,
            final_total_goals,
            final_total_goals,
            final_total_goals,
            final_total_goals,
            final_home_goals,
            final_away_goals,
            final_away_goals,
            final_home_goals,
            final_home_goals,
            final_away_goals,
            final_home_goals,
            final_away_goals,
            final_away_goals,
            final_home_goals,
            final_home_goals,
            final_away_goals,
            final_away_goals,
            final_home_goals,
            final_away_goals,
            final_home_goals,
            source,
            match_id,
        ),
    )
    return conn.total_changes - before


def _aggregate_resolved_snapshots(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        select *
        from match_snapshots
        where resolved_at is not null
          and id not in (select snapshot_id from aggregated_snapshot_ids)
        """
    ).fetchall()
    for row in rows:
        favorite_side = row["favorite_side"] or "none"
        conn.execute(
            """
            insert into snapshot_aggregates (
                league_key, league_name, minute_bucket, score_state, red_card_state, favorite_side,
                samples, next_goal_hits, over_1_5_hits, over_2_5_hits,
                favorite_recovered_hits, red_card_team_conceded_hits, updated_at
            )
            values (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, current_timestamp)
            on conflict(league_key, minute_bucket, score_state, red_card_state, favorite_side) do update set
                league_name = excluded.league_name,
                samples = samples + 1,
                next_goal_hits = next_goal_hits + excluded.next_goal_hits,
                over_1_5_hits = over_1_5_hits + excluded.over_1_5_hits,
                over_2_5_hits = over_2_5_hits + excluded.over_2_5_hits,
                favorite_recovered_hits = favorite_recovered_hits + excluded.favorite_recovered_hits,
                red_card_team_conceded_hits = red_card_team_conceded_hits + excluded.red_card_team_conceded_hits,
                updated_at = current_timestamp
            """,
            (
                row["league_key"],
                row["league_name"],
                row["minute_bucket"],
                row["score_state"],
                row["red_card_state"],
                favorite_side,
                row["next_goal_happened"] or 0,
                row["over_1_5_hit"] or 0,
                row["over_2_5_hit"] or 0,
                row["favorite_recovered"] or 0,
                row["red_card_team_conceded"] or 0,
            ),
        )
        conn.execute("insert or ignore into aggregated_snapshot_ids (snapshot_id) values (?)", (row["id"],))


def _detect_duplicate_or_replay(
    conn: sqlite3.Connection,
    source: str,
    match_id: str,
    league: str,
    match: dict[str, Any],
    home_goals: int,
    away_goals: int,
    minute: int,
    is_finished: bool,
) -> dict[str, Any]:
    fingerprint = _match_fingerprint(league, match)
    existing_same_id = conn.execute(
        """
        select source, match_id, is_finished, final_home_goals, final_away_goals, last_seen_at
        from matches
        where source = ? and match_id = ?
        """,
        (source, match_id),
    ).fetchone()
    if existing_same_id and existing_same_id["is_finished"] and not is_finished:
        reason = "replayed_finished_match_as_live"
        _record_duplicate(conn, source, match_id, existing_same_id["source"], existing_same_id["match_id"], reason, 0.95)
        return {"is_duplicate": True, "reason": reason, "confidence": 0.95}

    existing_fingerprint = conn.execute(
        """
        select source, match_id, is_finished, final_home_goals, final_away_goals
        from matches
        where match_fingerprint = ?
          and not (source = ? and match_id = ?)
        order by last_seen_at desc
        limit 1
        """,
        (fingerprint, source, match_id),
    ).fetchone()
    if existing_fingerprint:
        reason = "same_match_different_source_or_id"
        confidence = 0.9
        if existing_fingerprint["is_finished"] and not is_finished:
            reason = "possible_sportybet_replay"
            confidence = 0.97
        _record_duplicate(conn, source, match_id, existing_fingerprint["source"], existing_fingerprint["match_id"], reason, confidence)
        return {
            "is_duplicate": True,
            "reason": reason,
            "confidence": confidence,
            "duplicate_of": {"source": existing_fingerprint["source"], "match_id": existing_fingerprint["match_id"]},
        }

    return {"is_duplicate": False}


def _record_duplicate(
    conn: sqlite3.Connection,
    source: str,
    match_id: str,
    duplicate_of_source: str | None,
    duplicate_of_match_id: str | None,
    reason: str,
    confidence: float,
) -> None:
    conn.execute(
        """
        insert or ignore into match_duplicates (
            source, match_id, duplicate_of_source, duplicate_of_match_id, reason, confidence
        )
        values (?, ?, ?, ?, ?, ?)
        """,
        (source, match_id, duplicate_of_source, duplicate_of_match_id, reason, confidence),
    )


def _memory_row(row: sqlite3.Row | None, fallback_key: str | None = None, fallback_name: str | None = None) -> dict[str, Any]:
    if not row:
        return {
            "league_key": fallback_key,
            "league_name": fallback_name,
            "samples": 0,
            "late_goals": 0,
            "late_goal_rate": 0,
        }
    samples = row["samples"] or 0
    late_goals = row["late_goals"] or 0
    return {
        "league_key": row["league_key"],
        "league_name": row["league_name"],
        "samples": samples,
        "late_goals": late_goals,
        "late_goal_rate": round(late_goals / samples, 3) if samples else 0,
    }


def _snapshot_memory_row(row: sqlite3.Row) -> dict[str, Any]:
    samples = row["samples"] or 0
    return {
        "league_key": row["league_key"],
        "league_name": row["league_name"],
        "minute_bucket": row["minute_bucket"],
        "score_state": row["score_state"],
        "samples": samples,
        "next_goal_rate": _rate(row["next_goal_hits"], samples),
        "over_1_5_rate": _rate(row["over_1_5_hits"], samples),
        "over_2_5_rate": _rate(row["over_2_5_hits"], samples),
        "favorite_recovered_rate": _rate(row["favorite_recovered_hits"], samples),
        "red_card_team_conceded_rate": _rate(row["red_card_team_conceded_hits"], samples),
    }


def _league_from_match(match: dict[str, Any]) -> str:
    tournament = match.get("tournament")
    if isinstance(tournament, dict):
        return tournament.get("name") or tournament.get("uniqueTournament", {}).get("name") or ""
    if match.get("league_name"):
        return str(match.get("league_name"))
    category = match.get("category")
    return " ".join(part for part in [str(category or ""), str(tournament or "")] if part).strip()


def _match_fingerprint(league: str, match: dict[str, Any]) -> str:
    home = _team_name(match, "home") or ""
    away = _team_name(match, "away") or ""
    start = str(match.get("start_time") or match.get("start_timestamp") or "")[:10]
    parts = [normalize_league(league), normalize_league(home), normalize_league(away), start]
    return "|".join(parts)


def _match_minute(match: dict[str, Any]) -> int:
    if match.get("minute"):
        return _to_int(match.get("minute"), 0)
    played_seconds = match.get("played_seconds")
    if isinstance(played_seconds, str) and ":" in played_seconds:
        return _to_int(played_seconds.split(":", 1)[0], 0)
    if played_seconds:
        return int(_to_int(played_seconds, 0) / 60)
    status = match.get("status") or {}
    description = str(status.get("description") or "")
    digits = "".join(ch for ch in description if ch.isdigit())
    return _to_int(digits, 0)


def _minute_bucket(minute: int) -> str:
    if minute <= 15:
        return "00-15"
    if minute <= 30:
        return "16-30"
    if minute <= 45:
        return "31-45"
    if minute <= 60:
        return "46-60"
    if minute <= 70:
        return "61-70"
    if minute <= 80:
        return "71-80"
    return "81-90+"


def _bucket_bounds(bucket: str) -> tuple[int, int]:
    if bucket == "00-15":
        return 0, 15
    if bucket == "16-30":
        return 16, 30
    if bucket == "31-45":
        return 31, 45
    if bucket == "46-60":
        return 46, 60
    if bucket == "61-70":
        return 61, 70
    if bucket == "71-80":
        return 71, 80
    return 81, 130


def _score_state(home_goals: int, away_goals: int, favorite_side: str | None) -> str:
    score_diff = home_goals - away_goals
    if score_diff == 0:
        return "favorite_drawing" if favorite_side else "draw"
    leading_side = "home" if score_diff > 0 else "away"
    if not favorite_side:
        return "home_leading" if leading_side == "home" else "away_leading"
    if leading_side == favorite_side:
        return "favorite_leading"
    return "favorite_losing"


def _red_card_state(home_red_cards: int, away_red_cards: int) -> str:
    if home_red_cards == away_red_cards:
        return "even"
    return "home_red" if home_red_cards > away_red_cards else "away_red"


def _favorite_from_match(match: dict[str, Any]) -> dict[str, Any]:
    odds = _main_decimal_odds(match)
    if len(odds) < 2:
        return {}
    home = odds[0]
    away = odds[-1]
    if home["probability"] == away["probability"]:
        return {}
    favorite = home if home["probability"] > away["probability"] else away
    return {"side": favorite["side"], "probability": favorite["probability"]}


def _main_decimal_odds(match: dict[str, Any]) -> list[dict[str, Any]]:
    sporty_markets = match.get("markets") or []
    for market in sporty_markets:
        name = (market.get("name") or "").lower()
        if "1x2" in name or "winner" in name or name in {"3 way", "match result"}:
            odds = []
            for index, selection in enumerate(market.get("selections", [])):
                decimal = _to_float(selection.get("odds"))
                if decimal and decimal > 1:
                    odds.append({
                        "side": "home" if index == 0 else "away" if index == len(market.get("selections", [])) - 1 else "draw",
                        "probability": 1 / decimal,
                    })
            return odds

    choices = (((match.get("odds_featured") or {}).get("default") or {}).get("choices") or [])
    odds = []
    for choice in choices:
        probability = _fraction_to_probability(choice.get("fractional_value"))
        name = choice.get("name")
        if probability is None:
            continue
        side = "home" if name in ("1", "Home") else "away" if name in ("2", "Away") else "draw"
        odds.append({"side": side, "probability": probability})
    return odds


def _fraction_to_probability(value: Any) -> float | None:
    if not value or "/" not in str(value):
        return None
    top, bottom = str(value).split("/", 1)
    numerator = _to_float(top)
    denominator = _to_float(bottom)
    if numerator is None or denominator in (None, 0):
        return None
    decimal = numerator / denominator + 1
    return 1 / decimal


def _rate(hits: Any, samples: int) -> float | None:
    if hits is None or not samples:
        return None
    return round((hits or 0) / samples, 3)


def _team_name(match: dict[str, Any], side: str) -> str | None:
    team = match.get(f"{side}_team")
    if isinstance(team, dict):
        return team.get("name")
    return team


def _match_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source": row["source"],
        "id": row["match_id"],
        "league": {"id": row["league_key"], "name": row["league_name"]},
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "score": {"home": row["final_home_goals"], "away": row["final_away_goals"]},
        "is_finished": bool(row["is_finished"]),
        "last_seen_at": row["last_seen_at"],
    }


def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "minute": row["minute"],
        "minute_bucket": row["minute_bucket"],
        "score": {"home": row["home_goals"], "away": row["away_goals"]},
        "total_goals": row["total_goals"],
        "score_state": row["score_state"],
        "favorite_side": row["favorite_side"],
        "favorite_probability": row["favorite_probability"],
        "red_card_state": row["red_card_state"],
        "outcomes": {
            "next_goal_happened": row["next_goal_happened"],
            "over_1_5_hit": row["over_1_5_hit"],
            "over_2_5_hit": row["over_2_5_hit"],
            "favorite_recovered": row["favorite_recovered"],
            "red_card_team_conceded": row["red_card_team_conceded"],
        },
        "observed_at": row["observed_at"],
        "resolved_at": row["resolved_at"],
    }


def _prediction_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source": row["source"],
        "match_id": row["match_id"],
        "match_name": row["match_name"],
        "league_name": row["league_name"],
        "best_pick": {
            "type": row["pick_type"],
            "selection": row["selection"],
            "confidence": row["confidence"],
            "reason": row["reason"],
        },
        "signals": json.loads(row["signals_json"] or "[]"),
        "picks": json.loads(row["picks_json"] or "[]"),
        "created_at": row["created_at"],
    }


def _country_from_league(league_name: str | None) -> str:
    text = league_name or "Unknown"
    if " " in text:
        first = text.split(" ", 1)[0]
        if first.lower() in {"england", "spain", "germany", "italy", "france", "netherlands", "nigeria", "brazil"}:
            return first
    return "Global"


def _standings_from_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}
    for match in matches:
        if not match.get("is_finished"):
            continue
        home = match.get("home_team")
        away = match.get("away_team")
        home_goals = match.get("score", {}).get("home")
        away_goals = match.get("score", {}).get("away")
        if home is None or away is None or home_goals is None or away_goals is None:
            continue
        for team in (home, away):
            table.setdefault(team, {"team": team, "played": 0, "wins": 0, "draws": 0, "losses": 0, "goals_for": 0, "goals_against": 0, "points": 0})
        table[home]["played"] += 1
        table[away]["played"] += 1
        table[home]["goals_for"] += home_goals
        table[home]["goals_against"] += away_goals
        table[away]["goals_for"] += away_goals
        table[away]["goals_against"] += home_goals
        if home_goals > away_goals:
            table[home]["wins"] += 1
            table[away]["losses"] += 1
            table[home]["points"] += 3
        elif away_goals > home_goals:
            table[away]["wins"] += 1
            table[home]["losses"] += 1
            table[away]["points"] += 3
        else:
            table[home]["draws"] += 1
            table[away]["draws"] += 1
            table[home]["points"] += 1
            table[away]["points"] += 1
    rows = sorted(table.values(), key=lambda item: (item["points"], item["goals_for"] - item["goals_against"], item["goals_for"]), reverse=True)
    for index, row in enumerate(rows, start=1):
        row["position"] = index
        row["goal_diff"] = row["goals_for"] - row["goals_against"]
    return rows


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")
