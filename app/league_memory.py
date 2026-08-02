from __future__ import annotations

import re
import os
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db import (DB_PATH, _conn, close_db, connect_readonly_db, db_conn, get_db, _init_db, _init_db_unlocked, _ensure_column, _is_sqlite_lock, _DB_SCHEMA_READY, _DB_SCHEMA_LOCK, _run_schema_migrations, _run_legacy_backfills, _existing_schema_can_be_trusted, _ensure_prediction_history_columns)
from app.market_intent import classify_market_intent, grade_market_intent
from app.prediction_audit import build_pick_audit, build_prediction_audit, grading_reason



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
    # Use the shared match-state classifier so SportyBet period/status variants
    # (e.g. "FT") resolve snapshots instead of leaving them permanently open.
    try:
        from app.match_state import classify_match_state

        state = classify_match_state(match)
        is_finished = bool(state.get("is_finished"))
        is_live = bool(state.get("is_live"))
    except Exception:
        is_finished = False
        is_live = False
    # Minute is still a useful live hint for sources that omit explicit status.
    if minute > 0:
        is_live = True

    with _conn(timeout=15) as conn:
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
    with _conn(timeout=15) as conn:
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
    with _conn() as conn:
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

    with _conn() as conn:
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
    with _conn() as conn:
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
    with _conn() as conn:
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
    with _conn() as conn:
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
    with _conn() as conn:
        rows = conn.execute(
            """
            select league_key, max(league_name) as league_name, max(country_name) as country_name, count(*) as matches
            from matches
            group by league_key
            order by league_name
            """
        ).fetchall()
    countries: dict[str, dict[str, Any]] = {}
    for row in rows:
        country = row["country_name"] or _country_from_league(row["league_name"])
        if not _is_country_like(country):
            country = _country_from_league(row["league_name"])
        if not _is_country_like(country):
            country = "Global"
        key = normalize_league(country)
        countries.setdefault(key, {"id": key, "name": country, "leagues": [], "match_count": 0})
        countries[key]["leagues"].append({
            "id": row["league_key"],
            "name": row["league_name"],
            "match_count": row["matches"],
        })
        countries[key]["match_count"] += row["matches"]
    ordered = sorted(
        countries.values(),
        key=lambda item: (item["name"] in {"Global", "International"}, item["name"]),
    )
    return {"countries": ordered}


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
    if get_settings().disable_pruning:
        return {
            "status": "skipped",
            "reason": "pruning disabled",
            "raw_retention_days": raw_retention_days,
            "odds_retention_days": odds_retention_days,
        }
    _init_db()
    with _conn() as conn:
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
        # Odds market snapshots can grow extremely large; prune them with the same retention window.
        for table in ("odds_market_snapshots", "odds_market_changes"):
            try:
                exists = conn.execute(
                    "select 1 from sqlite_master where type='table' and name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue
                conn.execute(
                    f"""
                    delete from {table}
                    where datetime(snapshot_time) < datetime('now', ?)
                    """,
                    (f"-{odds_retention_days} days",),
                )
            except Exception:
                pass
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
    league_name = _league_from_match(prediction)
    country_name = _country_from_match(prediction, league_name)
    audit = prediction.get("audit") if isinstance(prediction.get("audit"), dict) else build_prediction_audit(prediction)
    _record_prediction_decision(prediction, source, match_id, league_name, country_name, audit)
    _record_prediction_candidates(prediction, source, match_id, league_name, country_name)

    # ── Fix 3: skip junk predictions ───────────────────────────────────────────
    # Don't pollute prediction_history with no_bet or sub-55% confidence picks.
    if best_pick.get("type") == "no_bet":
        return
    if (best_pick.get("confidence") or 0) < 55:
        return
    with _conn() as conn:
        existing = conn.execute(
            """
            select id
            from prediction_history
            where match_id = ?
              and coalesce(prediction_mode, 'prematch') = ?
              and pick_type = ?
              and selection = ?
              and result is null
              and datetime(created_at) >= datetime('now', '-4 hours')
            order by created_at desc
            limit 1
            """,
            (match_id, prediction.get("prediction_mode") or "prematch", best_pick.get("type"), best_pick.get("selection")),
        ).fetchone()
        if existing:
            return
        conn.execute(
            """
            insert into prediction_history (
                source, match_id, match_name, league_name, pick_type, selection,
                confidence, reason, signals_json, picks_json, audit_json,
                country_name, sofascore_id, sportybet_id, prediction_mode,
                data_source, live_data_sources_json, models_json, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
            """,
            (
                source,
                match_id,
                prediction.get("name"),
                league_name,
                best_pick.get("type"),
                best_pick.get("selection"),
                best_pick.get("confidence"),
                best_pick.get("reason"),
                json.dumps(prediction.get("signals") or []),
                json.dumps(prediction.get("picks") or []),
                json.dumps(audit),
                country_name,
                prediction.get("sofascore_id"),
                prediction.get("sportybet_id") or match_id,
                prediction.get("prediction_mode") or "prematch",
                prediction.get("data_source") or ((prediction.get("data_quality") or {}).get("prediction_readiness") or {}).get("data_source"),
                json.dumps(prediction.get("live_data_sources") or []),
                json.dumps(prediction.get("models") or {}),
            ),
        )
        conn.commit()

    # ── Record CLV entry: capture entry odds at prediction time ────────────────
    try:
        from app.clv import record_clv_entry
        record_clv_entry(
            match_id=match_id,
            pick_type=best_pick.get("type") or "match_result",
            selection=best_pick.get("selection") or "",
            confidence=int(best_pick.get("confidence") or 0),
            match_name=prediction.get("name"),
            match_date=prediction.get("match_date"),
        )
    except Exception as exc:
        from app.health_counters import record_health_event

        record_health_event("league_memory", "clv_entry_record_failed", exc, match_id=match_id)


def record_deferred_prediction_decision(
    *,
    doc: dict[str, Any],
    readiness: dict[str, Any],
    audit: dict[str, Any],
    source: str = "deferred",
    reason: str | None = None,
) -> None:
    """Persist deferred/no-prediction decisions without polluting betting history."""
    _init_db()
    match_id = str(doc.get("sportybet_id") or doc.get("id") or doc.get("match_id") or "")
    if not match_id:
        return
    league_name = _league_from_match(doc)
    country_name = _country_from_match(doc, league_name)
    prediction = {
        "match_id": match_id,
        "sportybet_id": match_id,
        "sofascore_id": doc.get("sofascore_id") or ((doc.get("sofascore_detail") or {}).get("id")),
        "name": doc.get("sportybet_name") or doc.get("name"),
        "match_date": doc.get("match_date"),
        "source": source,
        "signals": [],
        "picks": [{
            "type": "no_bet",
            "selection": "Prediction deferred",
            "confidence": 0,
            "reason": reason or "Prediction data contract is not ready",
        }],
        "data_quality": {"prediction_readiness": readiness},
        "audit": audit,
    }
    _record_prediction_decision(prediction, source, match_id, league_name, country_name, audit, decision_type="deferred")


def _record_prediction_decision(
    prediction: dict[str, Any],
    source: str,
    match_id: str,
    league_name: str,
    country_name: str,
    audit: dict[str, Any],
    *,
    decision_type: str | None = None,
) -> None:
    picks = prediction.get("picks") or []
    best_pick = picks[0] if picks else {}
    pick_type = best_pick.get("type") or "no_bet"
    selection = best_pick.get("selection") or best_pick.get("pick") or "No decision"
    confidence = int(best_pick.get("confidence") or 0)
    resolved_decision_type = decision_type or ("no_bet" if pick_type == "no_bet" else "published")
    readiness = (
        (prediction.get("data_quality") or {}).get("prediction_readiness")
        or prediction.get("prediction_readiness")
        or {}
    )
    with _conn() as conn:
        existing = conn.execute(
            """
            select id
            from prediction_decision_log
            where match_id = ?
              and decision_type = ?
              and pick_type = ?
              and selection = ?
              and datetime(created_at) >= datetime('now', '-4 hours')
            order by created_at desc
            limit 1
            """,
            (match_id, resolved_decision_type, pick_type, selection),
        ).fetchone()
        if existing:
            return
        conn.execute(
            """
            insert into prediction_decision_log (
                source, match_id, sofascore_id, sportybet_id, match_name, league_name, country_name,
                decision_type, pick_type, selection, confidence, reason,
                readiness_json, signals_json, picks_json, audit_json, contextual_json, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
            """,
            (
                source,
                match_id,
                prediction.get("sofascore_id"),
                prediction.get("sportybet_id") or match_id,
                prediction.get("name"),
                league_name,
                country_name,
                resolved_decision_type,
                pick_type,
                selection,
                confidence,
                best_pick.get("reason") or best_pick.get("reasoning"),
                json.dumps(readiness),
                json.dumps(prediction.get("signals") or []),
                json.dumps(picks),
                json.dumps(audit),
                json.dumps(prediction.get("contextual_intelligence") or best_pick.get("contextual_intelligence") or {}),
            ),
        )
        conn.commit()


def _record_prediction_candidates(
    prediction: dict[str, Any],
    source: str,
    match_id: str,
    league_name: str,
    country_name: str,
) -> None:
    picks = prediction.get("picks") or []
    if not picks:
        return
    score = ((prediction.get("rules") or {}).get("score") or {})
    context = {
        "score_home": _to_int(score.get("home"), 0),
        "score_away": _to_int(score.get("away"), 0),
        "minute": (prediction.get("rules") or {}).get("minute"),
        "value_overlay": picks[0].get("value_overlay") if picks else None,
        "data_sources": prediction.get("data_sources") or {},
        "source_assurance": ((prediction.get("data_quality") or {}).get("prediction_readiness") or {}).get("assurance"),
        "has_sportybet_detail": bool(prediction.get("sportybet_detail")),
    }
    audit_json = json.dumps(prediction.get("audit") if isinstance(prediction.get("audit"), dict) else build_prediction_audit(prediction))
    rows: list[tuple[Any, ...]] = []
    for index, pick in enumerate(picks):
        pick_type = pick.get("type")
        selection = pick.get("selection")
        confidence = int(pick.get("confidence") or 0)
        if not pick_type or not selection or pick_type == "no_bet" or confidence < 50:
            continue
        market_intent = classify_market_intent(str(pick_type), str(selection), pick)
        pick_context = {**context, "market_intent": market_intent}
        rows.append((
            source,
            match_id,
            prediction.get("sofascore_id"),
            prediction.get("sportybet_id") or match_id,
            prediction.get("name"),
            league_name,
            country_name,
            pick_type,
            selection,
            confidence,
            pick.get("reason"),
            "primary" if index == 0 else "secondary",
            json.dumps(pick_context),
            json.dumps(prediction.get("signals") or []),
            json.dumps(build_pick_audit(prediction, pick)),
        ))
        overlay = pick.get("value_overlay") or {}
        if overlay.get("selection"):
            overlay_intent = classify_market_intent("value_bet", str(overlay.get("selection") or ""), overlay)
            rows.append((
                source,
                match_id,
                prediction.get("sofascore_id"),
                prediction.get("sportybet_id") or match_id,
                prediction.get("name"),
                league_name,
                country_name,
                "value_bet",
                overlay.get("selection"),
                confidence,
                f"value overlay edge {overlay.get('edge_percent')} stake {overlay.get('stake_per_100')}",
                "overlay",
                json.dumps({**context, "market_intent": overlay_intent}),
                json.dumps(prediction.get("signals") or []),
                json.dumps(build_pick_audit(prediction, {
                    **pick,
                    "type": "value_bet",
                    "selection": overlay.get("selection"),
                    "reason": f"value overlay edge {overlay.get('edge_percent')} stake {overlay.get('stake_per_100')}",
                })),
            ))
    ensemble = ((prediction.get("models") or {}).get("ensemble") or {})
    if ensemble and not ensemble.get("error") and ensemble.get("prediction"):
        rows.append((
            source,
            match_id,
            prediction.get("sofascore_id"),
            prediction.get("sportybet_id") or match_id,
            prediction.get("name"),
            league_name,
            country_name,
            "ensemble_1x2",
            ensemble.get("prediction"),
            int(float(ensemble.get("confidence") or 0)),
            f"ensemble evidence using {', '.join(ensemble.get('models_used') or [])}",
            "evidence",
            json.dumps({**context, "market_intent": classify_market_intent("ensemble_1x2", str(ensemble.get("prediction") or ""))}),
            json.dumps(prediction.get("signals") or []),
            json.dumps(build_pick_audit(prediction, {
                "type": "ensemble_1x2",
                "selection": ensemble.get("prediction"),
                "confidence": int(float(ensemble.get("confidence") or 0)),
                "role": "evidence",
                "reason": f"ensemble evidence using {', '.join(ensemble.get('models_used') or [])}",
            })),
        ))
    for signal in prediction.get("signals") or []:
        if signal.get("name") != "consensus_longshot_value":
            continue
        value = signal.get("value") if isinstance(signal.get("value"), dict) else {}
        selection = value.get("selection")
        if not selection:
            continue
        market_intent = classify_market_intent("consensus_longshot_value", str(selection), value)
        confidence = int(max(50, min(99, float(value.get("model_probability") or 0))))
        market_label = market_intent.get("market") or market_intent.get("family") or "market"
        rows.append((
            source,
            match_id,
            prediction.get("sofascore_id"),
            prediction.get("sportybet_id") or match_id,
            prediction.get("name"),
            league_name,
            country_name,
            "consensus_longshot_value",
            selection,
            confidence,
            f"Consensus longshot {market_label} value at {value.get('decimal_odds')} with {value.get('edge_percent')}% edge",
            "signal",
            json.dumps({**context, "signal": value, "market_intent": market_intent}),
            json.dumps(prediction.get("signals") or []),
            json.dumps(build_pick_audit(prediction, {
                "type": "consensus_longshot_value",
                "selection": selection,
                "confidence": confidence,
                "role": "signal",
                "reason": f"Consensus longshot value at {value.get('decimal_odds')} with {value.get('edge_percent')}% edge",
            })),
        ))
    if not rows:
        return
    with _conn() as conn:
        fresh_rows = []
        for row in rows:
            existing = conn.execute(
                """
                select id
                from prediction_candidate_history
                where match_id = ?
                  and pick_type = ?
                  and selection = ?
                  and role = ?
                  and result is null
                  and datetime(created_at) >= datetime('now', '-4 hours')
                order by created_at desc
                limit 1
                """,
                (row[1], row[7], row[8], row[11]),
            ).fetchone()
            if not existing:
                fresh_rows.append(row)
        if fresh_rows:
            conn.executemany(
                """
                insert into prediction_candidate_history (
                    source, match_id, sofascore_id, sportybet_id, match_name, league_name, country_name,
                    pick_type, selection, confidence, reason, role,
                    context_json, signals_json, audit_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
                """,
                [row if len(row) == 15 else (*row, audit_json) for row in fresh_rows],
            )
        conn.commit()


def weighted_prediction_memory(
    match: dict[str, Any],
    pick_type: str | None,
    selection: str | None,
) -> dict[str, Any]:
    """Blend graded prediction performance: tournament first, then country, then global."""
    _init_db()
    league = _league_from_match(match)
    country = _country_from_match(match, league)
    pick_type = pick_type or ""
    selection = selection or ""

    with _conn() as conn:
        scopes = {
            "tournament": _prediction_scope_stats(
                conn,
                "league_name = ? and pick_type = ? and selection = ?",
                (league, pick_type, selection),
            ),
            "country": _prediction_scope_stats(
                conn,
                "country_name = ? and pick_type = ? and selection = ?",
                (country, pick_type, selection),
            ),
            "global": _prediction_scope_stats(
                conn,
                "pick_type = ? and selection = ?",
                (pick_type, selection),
            ),
        }

    base_weights = {"tournament": 0.55, "country": 0.30, "global": 0.15}
    weighted = 0.0
    total_weight = 0.0
    for scope, stats in scopes.items():
        samples = stats["samples"]
        if samples <= 0:
            continue
        sample_factor = min(1.0, samples / 30)
        weight = base_weights[scope] * sample_factor
        weighted += stats["win_rate"] * weight
        total_weight += weight

    blended = round(weighted / total_weight * 100, 1) if total_weight else None
    impact = 0
    if blended is not None:
        impact = max(-8, min(8, round((blended - 52) / 4)))

    return {
        "league": league,
        "country": country,
        "pick_type": pick_type,
        "selection": selection,
        "weights": base_weights,
        "scopes": scopes,
        "blended_win_rate": blended,
        "confidence_adjustment": impact,
    }


def weighted_candidate_memory(
    match: dict[str, Any],
    pick_type: str | None,
    selection: str | None = None,
) -> dict[str, Any]:
    """Learning memory for secondary/overlay candidates by tournament, country, global."""
    _init_db()
    league = _league_from_match(match)
    country = _country_from_match(match, league)
    pick_type = pick_type or ""
    selection = selection or ""
    with _conn() as conn:
        scopes = {
            "tournament": _candidate_scope_stats(conn, "league_name = ? and pick_type = ?", (league, pick_type)),
            "country": _candidate_scope_stats(conn, "country_name = ? and pick_type = ?", (country, pick_type)),
            "global": _candidate_scope_stats(conn, "pick_type = ?", (pick_type,)),
        }
        if selection:
            scopes["selection_global"] = _candidate_scope_stats(
                conn,
                "pick_type = ? and selection = ?",
                (pick_type, selection),
            )

    base_weights = {"tournament": 0.50, "country": 0.30, "global": 0.20, "selection_global": 0.20}
    weighted = 0.0
    total_weight = 0.0
    for scope, stats in scopes.items():
        samples = stats["samples"]
        if samples <= 0:
            continue
        sample_factor = min(1.0, samples / 20)
        weight = base_weights[scope] * sample_factor
        weighted += stats["win_rate"] * weight
        total_weight += weight
    blended = round(weighted / total_weight * 100, 1) if total_weight else None
    return {
        "league": league,
        "country": country,
        "pick_type": pick_type,
        "selection": selection,
        "scopes": scopes,
        "blended_win_rate": blended,
        "allow": blended is None or blended >= 50,
        "confidence_adjustment": 0 if blended is None else max(-10, min(8, round((blended - 52) / 4))),
    }


def _candidate_scope_stats(conn: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> dict[str, Any]:
    row = conn.execute(
        f"""
        select
            count(*) as samples,
            sum(case when result = 'win' then 1 else 0 end) as wins,
            sum(case when result = 'loss' then 1 else 0 end) as losses
        from (
            select *
            from (
                select
                    pch.*,
                    row_number() over (
                        partition by match_id, pick_type, selection, role
                        order by datetime(coalesce(graded_at, created_at)) desc, id desc
                    ) as rn
                from prediction_candidate_history pch
                where graded_at is not null and result in ('win', 'loss')
            )
            where rn = 1
        )
        where {where}
        """,
        params,
    ).fetchone()
    samples = int(row["samples"] or 0) if row else 0
    wins = int(row["wins"] or 0) if row else 0
    losses = int(row["losses"] or 0) if row else 0
    graded = wins + losses
    return {
        "samples": graded,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / graded, 3) if graded else 0.0,
    }


def weighted_finished_match_memory(match: dict[str, Any]) -> dict[str, Any]:
    """Blend finished outcomes by tournament, country, global, and similar 1X2 odds."""
    _init_db()
    league = _league_from_match(match)
    country = _country_from_match(match, league)
    odds_profile = _match_1x2_odds_profile(match)
    with _conn() as conn:
        scopes = _finished_memory_scopes(conn, league, country, odds_profile)

    base_weights = {
        "tournament_odds": 0.50,
        "country_odds": 0.30,
        "global_odds": 0.20,
        "tournament": 0.25,
        "country": 0.15,
        "global": 0.10,
    }
    totals = {
        "home_win_rate": 0.0,
        "draw_rate": 0.0,
        "away_win_rate": 0.0,
        "over_1_5_rate": 0.0,
        "over_2_5_rate": 0.0,
        "over_3_5_rate": 0.0,
        "btts_rate": 0.0,
        "avg_goals": 0.0,
    }
    total_weight = 0.0
    effective_weights: dict[str, float] = {}
    for scope, stats in scopes.items():
        samples = stats["samples"]
        if samples <= 0:
            continue
        sample_factor = min(1.0, samples / 50)
        weight = base_weights[scope] * sample_factor
        effective_weights[scope] = round(weight, 3)
        total_weight += weight
        for key in totals:
            totals[key] += float(stats[key] or 0) * weight

    blended = {
        key: round(value / total_weight, 3)
        for key, value in totals.items()
    } if total_weight else {}

    return {
        "league": league,
        "country": country,
        "odds_profile": odds_profile,
        "weights": base_weights,
        "effective_weights": effective_weights,
        "scopes": scopes,
        "blended": blended,
        "samples": sum(stats["samples"] for stats in scopes.values()),
    }


def close_match_strength_context(match: dict[str, Any], limit: int = 8) -> dict[str, Any]:
    """
    Compact historical context for matches close to this one.
    Uses our own DB first: same teams, same league/country, and similar 1X2 odds.
    """
    _init_db()
    league = _league_from_match(match)
    country = _country_from_match(match, league)
    home = _team_name(match, "home") or ""
    away = _team_name(match, "away") or ""
    odds_profile = _match_1x2_odds_profile(match)
    with _conn() as conn:
        team_rows = conn.execute(
            """
            select home_team, away_team, final_home_goals, final_away_goals, league_name, country_name, start_time
            from matches
            where is_finished = 1
              and final_home_goals is not null
              and final_away_goals is not null
              and (
                    lower(home_team) in (?, ?)
                 or lower(away_team) in (?, ?)
              )
            order by last_seen_at desc
            limit ?
            """,
            (home.lower(), away.lower(), home.lower(), away.lower(), max(limit * 2, 12)),
        ).fetchall()
        league_stats = _finished_scope_stats(conn, "m.league_name = ?", (league,))
        country_stats = _finished_scope_stats(conn, "m.country_name = ?", (country,))
        odds_stats = _finished_scope_stats(conn, "{odds_filter}", (), odds_profile) if odds_profile else {
            "samples": 0,
            "home_win_rate": 0.0,
            "draw_rate": 0.0,
            "away_win_rate": 0.0,
            "over_1_5_rate": 0.0,
            "over_2_5_rate": 0.0,
            "over_3_5_rate": 0.0,
            "btts_rate": 0.0,
            "avg_goals": 0.0,
            "odds_filtered": False,
        }

    team_matches = [_close_match_row(row, home, away) for row in team_rows[:limit]]
    home_form = _team_form_from_rows(team_rows, home)
    away_form = _team_form_from_rows(team_rows, away)
    strength_delta = round((home_form.get("points_per_game", 0.0) - away_form.get("points_per_game", 0.0)), 3)
    return {
        "league": league,
        "country": country,
        "home_team": home,
        "away_team": away,
        "odds_profile": odds_profile,
        "team_matches": team_matches,
        "team_samples": len(team_matches),
        "home_recent_record": home_form,
        "away_recent_record": away_form,
        "strength_delta_ppg": strength_delta,
        "league_outcomes": league_stats,
        "country_outcomes": country_stats,
        "similar_odds_outcomes": odds_stats,
        "samples": int(league_stats.get("samples") or 0) + int(country_stats.get("samples") or 0) + int(odds_stats.get("samples") or 0) + len(team_matches),
    }


def _finished_memory_scopes(
    conn: sqlite3.Connection,
    league: str,
    country: str,
    odds_profile: dict[str, float | str] | None,
) -> dict[str, dict[str, Any]]:
    scopes: dict[str, dict[str, Any]] = {}
    if odds_profile:
        scopes["tournament_odds"] = _finished_scope_stats(
            conn,
            "m.league_name = ? and {odds_filter}",
            (league,),
            odds_profile,
        )
        scopes["country_odds"] = _finished_scope_stats(
            conn,
            "m.country_name = ? and {odds_filter}",
            (country,),
            odds_profile,
        )
        scopes["global_odds"] = _finished_scope_stats(conn, "{odds_filter}", (), odds_profile)

    plain = {
        "tournament": _finished_scope_stats(conn, "m.league_name = ?", (league,)),
        "country": _finished_scope_stats(conn, "m.country_name = ?", (country,)),
        "global": _finished_scope_stats(conn, "1 = 1", ()),
    }
    # Only let broad memory help when the odds-similar scope is thin.
    fallback_pairs = (
        ("tournament_odds", "tournament"),
        ("country_odds", "country"),
        ("global_odds", "global"),
    )
    for odds_key, plain_key in fallback_pairs:
        if (scopes.get(odds_key) or {}).get("samples", 0) < 12:
            scopes[plain_key] = plain[plain_key]
    if not odds_profile:
        scopes.update(plain)
    return scopes


def _close_match_row(row: sqlite3.Row, home: str, away: str) -> dict[str, Any]:
    home_goals = int(row["final_home_goals"] or 0)
    away_goals = int(row["final_away_goals"] or 0)
    return {
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "score": {"home": home_goals, "away": away_goals},
        "total_goals": home_goals + away_goals,
        "league": row["league_name"],
        "country": row["country_name"],
        "start_time": row["start_time"],
        "involves_home": _same_team(row["home_team"], home) or _same_team(row["away_team"], home),
        "involves_away": _same_team(row["home_team"], away) or _same_team(row["away_team"], away),
    }


def _team_form_from_rows(rows: list[sqlite3.Row], team: str) -> dict[str, Any]:
    if not team:
        return {"samples": 0, "points_per_game": 0.0, "avg_goals_for": 0.0, "avg_goals_against": 0.0}
    samples = points = goals_for = goals_against = 0
    for row in rows:
        is_home = _same_team(row["home_team"], team)
        is_away = _same_team(row["away_team"], team)
        if not is_home and not is_away:
            continue
        hg = int(row["final_home_goals"] or 0)
        ag = int(row["final_away_goals"] or 0)
        gf, ga = (hg, ag) if is_home else (ag, hg)
        samples += 1
        goals_for += gf
        goals_against += ga
        points += 3 if gf > ga else 1 if gf == ga else 0
    return {
        "samples": samples,
        "points_per_game": round(points / samples, 3) if samples else 0.0,
        "avg_goals_for": round(goals_for / samples, 3) if samples else 0.0,
        "avg_goals_against": round(goals_against / samples, 3) if samples else 0.0,
    }


def _same_team(left: Any, right: Any) -> bool:
    return normalize_league(str(left or "")) == normalize_league(str(right or ""))


def grade_prediction(prediction_id: int, final_home: int, final_away: int) -> dict[str, Any]:
    _init_db()
    with _conn() as conn:
        row = conn.execute("select * from prediction_history where id = ?", (prediction_id,)).fetchone()
        if not row:
            return {"graded": False, "reason": "not found"}
        grade_info = grading_reason(row["pick_type"], row["selection"], final_home, final_away, row["match_name"])
        result = grade_info["result"] if grade_info.get("result") != "void" else _grade_pick_for_match(row["pick_type"], row["selection"], final_home, final_away, row["match_name"])
        grade_info["result"] = result
        models = _safe_json(row["models_json"] if "models_json" in row.keys() else "{}", {})
        grade_info["passed_models"] = _get_passed_models(models, result)
        conn.execute(
            """
            update prediction_history
            set result = ?, final_home = ?, final_away = ?, grading_reason_json = ?, graded_at = current_timestamp
            where id = ?
            """,
            (result, final_home, final_away, json.dumps(grade_info), prediction_id),
        )
        conn.commit()
    _grade_decision_logs_by_ids([str(row["match_id"])], final_home, final_away)
    _store_signal_outcome_for_row(row, result)
    return {"graded": True, "id": prediction_id, "result": result, "final_home": final_home, "final_away": final_away}


def _grade_candidate_row(row: sqlite3.Row, final_home: int, final_away: int) -> str:
    pick_type = row["pick_type"]
    selection = row["selection"]
    pt = str(pick_type or "").lower()
    sel = str(selection or "").lower()
    if pt == "live_team_to_score":
        try:
            context = json.loads(row["context_json"] or "{}")
        except Exception:
            context = {}
        start_home = _to_int(context.get("score_home"), 0)
        start_away = _to_int(context.get("score_away"), 0)
        home_delta = final_home - start_home
        away_delta = final_away - start_away
        side = _side_from_selection_and_match(str(selection or "").lower(), row["match_name"])
        if not side:
            return "void"
        picked_delta = home_delta if side == "home" else away_delta
        other_delta = away_delta if side == "home" else home_delta
        if picked_delta > 0 and other_delta == 0:
            return "win"
        if picked_delta == 0 and other_delta > 0:
            return "loss"
        return "void"
    if pt in {"live_next_goal", "live_total_goals"}:
        try:
            context = json.loads(row["context_json"] or "{}")
        except Exception:
            context = {}
        start_total = _to_int(context.get("score_home"), 0) + _to_int(context.get("score_away"), 0)
        final_total = final_home + final_away
        if "next goal" in sel:
            return "win" if final_total > start_total else "loss"
        match = re.search(r"(over|under)\s+(\d+(?:\.\d+)?)", sel)
        if match:
            line = float(match.group(2))
            if match.group(1) == "over":
                return "win" if final_total > line else "loss"
            return "win" if final_total < line else "loss"
        return "void"
    if pt == "live_match_winner":
        return _grade_pick_for_match("match_result", selection, final_home, final_away, row["match_name"])
    if pt == "consensus_longshot_value":
        try:
            context = json.loads(row["context_json"] or "{}")
        except Exception:
            context = {}
        intent = context.get("market_intent") if isinstance(context.get("market_intent"), dict) else {}
        graded = grade_market_intent(intent, selection, final_home, final_away, row["match_name"])
        if graded != "void":
            return graded
        return _grade_pick_for_match("match_result", selection, final_home, final_away, row["match_name"])
    if pt == "value_overlay":
        pick_type = "value_bet"
    return _grade_pick_for_match(pick_type, selection, final_home, final_away, row["match_name"])


def _grading_reason_for_candidate_row(row: sqlite3.Row, final_home: int, final_away: int, result: str | None = None) -> dict[str, Any]:
    context = _safe_json(row["context_json"] if "context_json" in row.keys() else "{}", {})
    intent = context.get("market_intent") if isinstance(context.get("market_intent"), dict) else None
    info = grading_reason(row["pick_type"], row["selection"], final_home, final_away, row["match_name"], intent)
    if result and info.get("result") != result:
        info["result"] = result
        info["reason"] = f"{row['selection']} graded {result} with candidate-specific live/context rule on final score {final_home}-{final_away}."
    return info


def _store_signal_outcome_for_row(row: sqlite3.Row, result: str) -> None:
    """Store graded decision-signal outcomes in local memory, with Mongo as optional mirror."""
    try:
        payload = _signal_outcome_payload_for_row(row, result)
        if not payload:
            return
        _store_signal_outcome_payload(payload)
    except Exception:
        pass


def _signal_outcome_payload_for_row(row: sqlite3.Row, result: str) -> dict[str, Any] | None:
    """Build signal-outcome payload without opening another SQLite writer."""
    if result not in {"win", "loss"}:
        return None
    try:
        try:
            from app.self_learner import _decision_signals_for_row

            signals = _decision_signals_for_row(row)
        except Exception:
            signals = _safe_json(row["signals_json"] if "signals_json" in row.keys() else "[]", [])
        if not signals:
            return None
        return {
            "match_id": str(row["match_id"]),
            "match_name": row["match_name"] if "match_name" in row.keys() else None,
            "tournament": row["league_name"] if "league_name" in row.keys() else None,
            "country": row["country_name"] if "country_name" in row.keys() else None,
            "match_date": str(row["created_at"] or "")[:10] if "created_at" in row.keys() else None,
            "signals": signals,
            "result": result,
            "pick_type": row["pick_type"] if "pick_type" in row.keys() else None,
            "selection": row["selection"] if "selection" in row.keys() else None,
            "confidence": int(row["confidence"] or 0) if "confidence" in row.keys() else None,
        }
    except Exception:
        return None


def _store_signal_outcome_payload(payload: dict[str, Any]) -> None:
    """Store signal outcome payload after the grading write transaction commits."""
    store_local_signal_outcomes(**payload)
    try:
        from app.mongo_store import is_configured, store_signal_outcomes

        if is_configured():
            store_signal_outcomes(**payload)
    except Exception:
        pass


def store_local_signal_outcomes(
    match_id: str,
    match_name: str | None,
    tournament: str | None,
    country: str | None,
    match_date: str | None,
    signals: list[dict[str, Any]],
    result: str,
    pick_type: str | None,
    selection: str | None,
    confidence: int | None,
) -> int:
    """Persist signal outcome analytics in the device SQLite memory."""
    if result not in {"win", "loss"} or not signals:
        return 0
    _init_db()
    rows = []
    for signal in signals:
        name = signal.get("name") if isinstance(signal, dict) else None
        if not name:
            continue
        rows.append((
            match_id,
            match_name,
            tournament,
            country,
            match_date,
            str(name),
            json.dumps(signal.get("value") if isinstance(signal, dict) else {}, default=str),
            _to_float(signal.get("impact")) if isinstance(signal, dict) else None,
            result,
            pick_type,
            selection,
            confidence,
        ))
    if not rows:
        return 0
    with _conn() as conn:
        _ensure_signal_outcomes_table(conn)
        conn.executemany(
            """
            insert into signal_outcomes (
                match_id, match_name, tournament, country, match_date,
                signal_name, signal_value_json, signal_impact, result,
                pick_type, selection, confidence
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(match_id, pick_type, selection, signal_name) do update set
                match_name = excluded.match_name,
                tournament = excluded.tournament,
                country = excluded.country,
                match_date = excluded.match_date,
                signal_value_json = excluded.signal_value_json,
                signal_impact = excluded.signal_impact,
                result = excluded.result,
                confidence = excluded.confidence,
                recorded_at = current_timestamp
            """,
            rows,
        )
        conn.commit()
    return len(rows)


def get_local_signal_stats(
    country: str | None = None,
    tournament: str | None = None,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Aggregate signal win rates from local SQLite device memory."""
    _init_db()
    clauses = ["result in ('win', 'loss')"]
    params: list[Any] = []
    scope = "device"
    if tournament:
        clauses.append("lower(coalesce(tournament, '')) like ?")
        params.append(f"%{tournament.lower()}%")
        scope = f"device:tournament:{tournament}"
    elif country:
        clauses.append("lower(coalesce(country, '')) like ?")
        params.append(f"%{country.lower()}%")
        scope = f"device:country:{country}"
    params.append(max(1, int(min_samples or 5)))
    with _conn() as conn:
        _ensure_signal_outcomes_table(conn)
        existing = conn.execute("select count(*) from signal_outcomes").fetchone()[0]
    if not existing:
        _backfill_local_signal_outcomes_from_history()
    with _conn() as conn:
        _ensure_signal_outcomes_table(conn)
        rows = conn.execute(
            f"""
            select signal_name,
                   count(*) as total,
                   sum(case when result = 'win' then 1 else 0 end) as wins,
                   sum(case when result = 'loss' then 1 else 0 end) as losses,
                   avg(signal_impact) as avg_impact,
                   avg(confidence) as avg_confidence
            from signal_outcomes
            where {' and '.join(clauses)}
            group by signal_name
            having count(*) >= ?
            order by (1.0 * wins / nullif(total, 0)) desc, total desc
            """,
            params,
        ).fetchall()
    return {
        "configured": True,
        "storage": "device",
        "scope": scope,
        "min_samples": min_samples,
        "signals": [
            {
                "signal": row["signal_name"],
                "total": row["total"],
                "wins": row["wins"],
                "losses": row["losses"],
                "win_rate": round((float(row["wins"] or 0) / max(1, int(row["total"] or 0))) * 100, 1),
                "avg_impact": round(float(row["avg_impact"] or 0), 2),
                "avg_confidence": round(float(row["avg_confidence"] or 0), 1),
            }
            for row in rows
        ],
    }


def _ensure_signal_outcomes_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists signal_outcomes (
            id integer primary key autoincrement,
            match_id text not null,
            match_name text,
            tournament text,
            country text,
            match_date text,
            signal_name text not null,
            signal_value_json text not null default '{}',
            signal_impact real,
            result text not null,
            pick_type text,
            selection text,
            confidence integer,
            recorded_at text not null default current_timestamp,
            unique (match_id, pick_type, selection, signal_name)
        )
        """
    )
    conn.execute("create index if not exists idx_signal_outcomes_signal on signal_outcomes(signal_name)")
    conn.execute("create index if not exists idx_signal_outcomes_scope on signal_outcomes(country, tournament, result)")


def _backfill_local_signal_outcomes_from_history(limit: int = 5000) -> int:
    """Populate local signal analytics from already graded rows once."""
    try:
        from app.self_learner import _decision_signals_for_row
    except Exception:
        _decision_signals_for_row = None
    with _conn() as conn:
        rows = conn.execute(
            """
            select *
            from (
                select id, match_id, match_name, league_name, country_name,
                       pick_type, selection, confidence, result, signals_json,
                       audit_json, '{}' as context_json, created_at, graded_at
                from prediction_history ph
                where result in ('win', 'loss')
                  and pick_type != 'no_bet'
                union all
                select id, match_id, match_name, league_name, country_name,
                       pick_type, selection, confidence, result, signals_json,
                       audit_json, context_json, created_at, graded_at
                from prediction_candidate_history pch
                where result in ('win', 'loss')
                  and pick_type != 'no_bet'
            )
            order by datetime(coalesce(graded_at, created_at)) desc
            limit ?
            """,
            (max(1, int(limit or 5000)),),
        ).fetchall()
    stored = 0
    for row in rows:
        try:
            signals = _decision_signals_for_row(row) if _decision_signals_for_row else _safe_json(row["signals_json"], [])
            stored += store_local_signal_outcomes(
                match_id=str(row["match_id"]),
                match_name=row["match_name"] if "match_name" in row.keys() else None,
                tournament=row["league_name"] if "league_name" in row.keys() else None,
                country=row["country_name"] if "country_name" in row.keys() else None,
                match_date=str(row["created_at"] or "")[:10] if "created_at" in row.keys() else None,
                signals=signals,
                result=row["result"],
                pick_type=row["pick_type"] if "pick_type" in row.keys() else None,
                selection=row["selection"] if "selection" in row.keys() else None,
                confidence=int(row["confidence"] or 0) if "confidence" in row.keys() else None,
            )
        except Exception:
            continue
    return stored


def _grade_candidate_predictions_for_match(
    match_id: str,
    sofa_ids: list[str],
    final_home: int,
    final_away: int,
) -> int:
    keys = [match_id, *sofa_ids]
    placeholders = ",".join("?" for _ in keys)
    signal_payloads: list[dict[str, Any]] = []
    with _conn() as conn:
        rows = conn.execute(
            f"""
            select *
            from prediction_candidate_history
            where graded_at is null and match_id in ({placeholders})
            """,
            tuple(keys),
        ).fetchall()
        for row in rows:
            result = _grade_candidate_row(row, final_home, final_away)
            grade_info = _grading_reason_for_candidate_row(row, final_home, final_away, result)
            conn.execute(
                """
                update prediction_candidate_history
                set result = ?, final_home = ?, final_away = ?, grading_reason_json = ?, graded_at = current_timestamp
                where id = ?
                """,
                (result, final_home, final_away, json.dumps(grade_info), row["id"]),
            )
            payload = _signal_outcome_payload_for_row(row, result)
            if payload:
                signal_payloads.append(payload)
        conn.commit()
    for payload in signal_payloads:
        _store_signal_outcome_payload(payload)
    return len(rows)


def grade_predictions_for_date(match_date: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    _init_db()
    finished = {str(e["id"]): e for e in events if (e.get("status") or {}).get("type") == "finished"}
    if not finished:
        return {"graded": 0, "skipped": 0, "no_finished_events": True}

    with _conn() as conn:
        rows = conn.execute(
            """
            select *
            from prediction_history
            where graded_at is null
              and date(created_at) = ?
            """,
            (match_date,),
        ).fetchall()
        candidate_rows = conn.execute(
            """
            select distinct match_id
            from prediction_candidate_history
            where graded_at is null
              and date(created_at) = ?
            """,
            (match_date,),
        ).fetchall()
        all_match_ids = list({str(row["match_id"]) for row in rows} | {str(row["match_id"]) for row in candidate_rows})
        sofa_ids_by_match = _sofascore_ids_for_predictions(conn, all_match_ids)

    graded = skipped = candidate_graded = 0
    for row in rows:
        match_id = str(row["match_id"])
        event = finished.get(match_id)
        sofa_ids = sofa_ids_by_match.get(match_id, [])
        if not event:
            event = next((finished.get(sofa_id) for sofa_id in sofa_ids if finished.get(sofa_id)), None)
        if not event:
            skipped += 1
            continue
        score = event.get("score") or {}
        final_home = _to_int(score.get("home"), 0)
        final_away = _to_int(score.get("away"), 0)
        candidate_graded += _grade_candidate_predictions_for_match(match_id, sofa_ids, final_home, final_away)
        _grade_decision_logs_by_ids([match_id, *sofa_ids], final_home, final_away)
        grade_info = grading_reason(row["pick_type"], row["selection"], final_home, final_away, row["match_name"])
        result = grade_info["result"] if grade_info.get("result") != "void" else _grade_pick_for_match(row["pick_type"], row["selection"], final_home, final_away, row["match_name"])
        grade_info["result"] = result
        models = _safe_json(row["models_json"] if "models_json" in row.keys() else "{}", {})
        grade_info["passed_models"] = _get_passed_models(models, result)
        with _conn() as conn:
            conn.execute(
                """
                update prediction_history
                set result = ?, final_home = ?, final_away = ?, grading_reason_json = ?, graded_at = current_timestamp
                where id = ?
                """,
                (result, final_home, final_away, json.dumps(grade_info), row["id"]),
            )
            conn.commit()
        _store_signal_outcome_for_row(row, result)

        # Record outcome for the probability learner
        try:
            from app.probability_learner import learn_probabilities
            from app.signal_aggregator import normalize_signal

            signals_json = row.get("signals_json") or "[]"
            signals = json.loads(signals_json) if signals_json else []
            normalized_signals = []
            for sig in signals:
                name = sig.get("name") or sig.get("signal_name") or ""
                value = sig.get("value") or sig.get("signal_value") or 0
                normalized = normalize_signal(name, value)
                normalized_signals.append(normalized)

            learn_probabilities(
                signals=normalized_signals,
                result=result,
                pick_type=row.get("pick_type") or "match_result",
                league_key="__global__",
                confidence=float(row.get("confidence") or 0.5),
            )
        except Exception:
            pass

        graded += 1

    primary_ids = {str(row["match_id"]) for row in rows}
    for match_id in all_match_ids:
        if match_id in primary_ids:
            continue
        event = finished.get(match_id)
        sofa_ids = sofa_ids_by_match.get(match_id, [])
        if not event:
            event = next((finished.get(sofa_id) for sofa_id in sofa_ids if finished.get(sofa_id)), None)
        if not event:
            continue
        score = event.get("score") or {}
        final_home = _to_int(score.get("home"), 0)
        final_away = _to_int(score.get("away"), 0)
        candidate_graded += _grade_candidate_predictions_for_match(match_id, sofa_ids, final_home, final_away)
        _grade_decision_logs_by_ids([match_id, *sofa_ids], final_home, final_away)

    return {"graded": graded, "candidate_graded": candidate_graded, "skipped": skipped, "date": match_date}


def grade_overdue_predictions(hours_after_kickoff: float = 2.0, limit: int = 300) -> dict[str, Any]:
    """Grade any pending match once it is 2+ hours past kickoff, unless still live.

    SportyBet is tried first because its result endpoint is keyed by the same
    `sr:match:*` id used in our prediction history. SofaScore is the fallback
    for matches where Sporty does not return a result or the match was archived
    after enrichment with only a SofaScore id.
    """
    _init_db()
    import time as _time
    from collections import defaultdict

    now_seconds = _time.time()
    cutoff_seconds = now_seconds - hours_after_kickoff * 3600
    rows = _pending_matches_for_grading(cutoff_seconds, limit)
    if not rows:
        return {
            "status": "ok",
            "checked": 0,
            "eligible": 0,
            "graded": 0,
            "candidate_graded": 0,
            "decision_graded": 0,
            "still_live": 0,
            "not_found": 0,
            "skipped": 0,
            "errors": [],
        }

    graded = candidate_graded = decision_graded = still_live = not_found = skipped = 0
    sporty_graded = sofa_graded = 0
    errors: list[str] = []
    already_graded: set[str] = set()

    # 1) Sporty result checker. It can grade even when buffer/Mongo state was
    # lost, because the result id matches our Sporty match id.
    try:
        from app.sportybet_client import fetch_results

        known_starts = [
            _normalise_start_seconds(row["start_time"]) or _datetime_to_seconds(row["first_seen"])
            for row in rows
        ]
        known_starts = [ts for ts in known_starts if ts]
        start_ms = int((min(known_starts) - 3 * 3600) * 1000) if known_starts else int((now_seconds - 72 * 3600) * 1000)
        end_ms = int((now_seconds + 30 * 60) * 1000)
        sporty_results = fetch_results(start_ms, end_ms, count=max(500, limit))
        sporty_by_id = {
            str(result.get("id")): result
            for result in sporty_results
            if (result.get("score") or {}).get("home") is not None
            and (result.get("score") or {}).get("away") is not None
        }
        for row in rows:
            match_id = str(row["match_id"])
            result = sporty_by_id.get(match_id)
            if not result:
                continue
            score = result.get("score") or {}
            counts = _grade_match_predictions_by_ids(
                match_id=match_id,
                linked_ids=_sofa_ids_from_raw(row["raw_enriched"]),
                final_home=_to_int(score.get("home"), 0),
                final_away=_to_int(score.get("away"), 0),
            )
            if counts["primary"] or counts["candidate"] or counts.get("decisions"):
                graded += counts["primary"]
                candidate_graded += counts["candidate"]
                decision_graded += counts.get("decisions", 0)
                sporty_graded += counts["primary"]
                already_graded.add(match_id)
                _safe_mark_buffer_finished(match_id, score.get("home"), score.get("away"))
    except Exception as exc:
        errors.append(f"sporty_results: {exc}")

    # 2) SofaScore fallback and live guard. If SofaScore says the match is live,
    # do not grade it even if kickoff+2h has passed.
    try:
        from app.sofascore_client import fetch_all_scheduled_events, fetch_event, fetch_live_events

        try:
            live_events = fetch_live_events()
        except Exception:
            live_events = []
        live_ids = {str(event.get("id")) for event in live_events}
        by_date: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            match_id = str(row["match_id"])
            if match_id in already_graded:
                continue
            match_date = row["match_date"] or _date_from_start(row["start_time"]) or str(row["first_seen"] or "")[:10]
            if match_date:
                by_date[str(match_date)].append(row)

        for match_date, date_rows in by_date.items():
            try:
                events = fetch_all_scheduled_events(match_date)
            except Exception as exc:
                errors.append(f"sofascore {match_date}: {exc}")
                skipped += len(date_rows)
                continue
            event_by_id = {str(event.get("id")): event for event in events}
            for row in date_rows:
                match_id = str(row["match_id"])
                sofa_ids = _sofa_ids_from_raw(row["raw_enriched"])
                _row_sofa = str(row["sofascore_id"]) if row["sofascore_id"] else None
                if _row_sofa and _row_sofa not in sofa_ids:
                    sofa_ids.append(_row_sofa)
                event = event_by_id.get(match_id)
                if not event:
                    event = next((event_by_id.get(sofa_id) for sofa_id in sofa_ids if event_by_id.get(sofa_id)), None)
                if not event and sofa_ids:
                    for sofa_id in sofa_ids:
                        try:
                            direct_event = fetch_event(sofa_id)
                        except Exception:
                            direct_event = {}
                        if direct_event:
                            event = direct_event
                            break
                if not event:
                    not_found += 1
                    continue
                status_type = str(((event.get("status") or {}).get("type")) or "").lower()
                event_id = str(event.get("id"))
                if status_type in {"inprogress", "live"} or event_id in live_ids:
                    still_live += 1
                    continue
                if status_type != "finished":
                    skipped += 1
                    continue
                score = event.get("score") or {}
                counts = _grade_match_predictions_by_ids(
                    match_id=match_id,
                    linked_ids=sofa_ids,
                    final_home=_to_int(score.get("home"), 0),
                    final_away=_to_int(score.get("away"), 0),
                )
                graded += counts["primary"]
                candidate_graded += counts["candidate"]
                decision_graded += counts.get("decisions", 0)
                sofa_graded += counts["primary"]
                _safe_mark_buffer_finished(match_id, score.get("home"), score.get("away"))
    except Exception as exc:
        errors.append(f"sofascore_results: {exc}")

    return {
        "status": "ok",
        "checked": len(rows),
        "eligible": len(rows),
        "graded": graded,
        "candidate_graded": candidate_graded,
        "decision_graded": decision_graded,
        "sporty_graded": sporty_graded,
        "sofascore_graded": sofa_graded,
        "still_live": still_live,
        "not_found": not_found,
        "skipped": skipped,
        "errors": errors[:5],
    }


def check_and_grade_match_result(match_id: str, hours_back: int = 72) -> dict[str, Any]:
    """Check SportyBet/SofaScore for one match result and grade all related rows."""
    _init_db()
    import time as _time

    match_id = str(match_id)
    with _conn() as conn:
        _ensure_buffer_tables(conn)
        row = conn.execute(
            """
            select ? as match_id,
                   coalesce(mb.match_date, fb.match_date) as match_date,
                   coalesce(mb.start_time, fb.start_time) as start_time,
                   coalesce(mb.raw_enriched, fb.raw_enriched) as raw_enriched
            from (select 1) seed
            left join match_buffer mb on mb.match_id = ?
            left join future_match_buffer fb on fb.match_id = ?
            """,
            (match_id, match_id, match_id),
        ).fetchone()

    sofa_ids = _sofa_ids_from_raw(row["raw_enriched"] if row else None)
    now_ms = int(_time.time() * 1000)
    start_ms = now_ms - max(1, hours_back) * 3_600_000
    try:
        from app.sportybet_client import fetch_results

        for result in fetch_results(start_ms, now_ms, count=500):
            if str(result.get("id")) != match_id:
                continue
            score = result.get("score") or {}
            if score.get("home") is None or score.get("away") is None:
                break
            counts = _grade_match_predictions_by_ids(match_id, sofa_ids, _to_int(score.get("home"), 0), _to_int(score.get("away"), 0))
            _safe_mark_buffer_finished(match_id, score.get("home"), score.get("away"))
            return {"status": "graded", "source": "sportybet", "match_id": match_id, "score": score, **counts}
    except Exception as exc:
        sporty_error = str(exc)
    else:
        sporty_error = None

    match_date = (row["match_date"] if row else None) or _date_from_start(row["start_time"] if row else None)
    if not match_date:
        return {"status": "not_found", "match_id": match_id, "sporty_error": sporty_error, "reason": "no match date for SofaScore fallback"}
    try:
        from app.sofascore_client import fetch_all_scheduled_events, fetch_event

        events = fetch_all_scheduled_events(str(match_date))
        event_by_id = {str(event.get("id")): event for event in events}
        event = next((event_by_id.get(sofa_id) for sofa_id in sofa_ids if event_by_id.get(sofa_id)), None)
        if not event and sofa_ids:
            for sofa_id in sofa_ids:
                direct_event = fetch_event(sofa_id)
                if direct_event:
                    event = direct_event
                    break
        if not event:
            return {"status": "not_found", "match_id": match_id, "sporty_error": sporty_error, "source": "sofascore"}
        status_type = str(((event.get("status") or {}).get("type")) or "").lower()
        if status_type in {"inprogress", "live"}:
            return {"status": "still_live", "match_id": match_id, "source": "sofascore", "sofascore_id": event.get("id")}
        if status_type != "finished":
            return {"status": "not_finished", "match_id": match_id, "source": "sofascore", "status_type": status_type}
        score = event.get("score") or {}
        counts = _grade_match_predictions_by_ids(match_id, sofa_ids, _to_int(score.get("home"), 0), _to_int(score.get("away"), 0))
        _safe_mark_buffer_finished(match_id, score.get("home"), score.get("away"))
        return {"status": "graded", "source": "sofascore", "match_id": match_id, "score": score, **counts}
    except Exception as exc:
        return {"status": "error", "match_id": match_id, "sporty_error": sporty_error, "error": str(exc)}


def _pending_matches_for_grading(cutoff_seconds: float, limit: int) -> list[sqlite3.Row]:
    with _conn() as conn:
        _ensure_buffer_tables(conn)
        rows = conn.execute(
            """
            with pending as (
                select match_id, min(created_at) as first_seen
                from prediction_history
                where graded_at is null and pick_type != 'no_bet'
                group by match_id
                union all
                select match_id, min(created_at) as first_seen
                from prediction_candidate_history
                where graded_at is null
                group by match_id
                union all
                select match_id, min(created_at) as first_seen
                from prediction_decision_log
                where graded_at is null
                group by match_id
            ),
            grouped as (
                select match_id, min(first_seen) as first_seen
                from pending
                group by match_id
            )
            select g.match_id, g.first_seen,
                   coalesce(mb.match_date, fb.match_date) as match_date,
                   coalesce(mb.start_time, fb.start_time) as start_time,
                   coalesce(mb.is_live, fb.is_live, 0) as is_live,
                   coalesce(mb.raw_enriched, fb.raw_enriched) as raw_enriched,
                   coalesce(mb.raw_sporty, fb.raw_sporty) as raw_sporty,
                   coalesce(mb.sofascore_id, fb.sofascore_id, ph_ids.sofascore_id) as sofascore_id,
                   coalesce(mb.sportybet_id, fb.sportybet_id) as sportybet_id
            from grouped g
            left join (
                select match_id, max(sofascore_id) as sofascore_id
                from prediction_history
                where sofascore_id is not null
                group by match_id
            ) ph_ids on ph_ids.match_id = g.match_id
            left join match_buffer mb on mb.match_id = g.match_id
            left join future_match_buffer fb on fb.match_id = g.match_id
            order by coalesce(mb.start_time, fb.start_time, strftime('%s', g.first_seen) * 1000) asc
            limit ?
            """,
            (limit,),
        ).fetchall()
    eligible: list[sqlite3.Row] = []
    for row in rows:
        kickoff = _normalise_start_seconds(row["start_time"])
        if kickoff is None:
            # Without kickoff we cannot assert overdue, but Sporty can still
            # confirm by id. Use first_seen only to allow result lookup, never
            # to mark not-found as a final state.
            kickoff = _datetime_to_seconds(row["first_seen"])
        if kickoff is not None and kickoff <= cutoff_seconds:
            eligible.append(row)
    return eligible


def _grade_match_predictions_by_ids(match_id: str, linked_ids: list[str], final_home: int, final_away: int) -> dict[str, int]:
    keys = []
    for value in [match_id, *linked_ids]:
        if value is not None and str(value) not in keys:
            keys.append(str(value))
    placeholders = ",".join("?" for _ in keys)
    primary = candidate = 0
    signal_payloads: list[dict[str, Any]] = []
    with _conn(timeout=15) as conn:
        rows = conn.execute(
            f"""
            select *
            from prediction_history
            where graded_at is null and match_id in ({placeholders})
            """,
            tuple(keys),
        ).fetchall()
        for row in rows:
            grade_info = grading_reason(row["pick_type"], row["selection"], final_home, final_away, row["match_name"])
            result = grade_info["result"] if grade_info.get("result") != "void" else _grade_pick_for_match(row["pick_type"], row["selection"], final_home, final_away, row["match_name"])
            grade_info["result"] = result
            models = _safe_json(row["models_json"] if "models_json" in row.keys() else "{}", {})
            grade_info["passed_models"] = _get_passed_models(models, result)
            conn.execute(
                """
                update prediction_history
                set result = ?, final_home = ?, final_away = ?, grading_reason_json = ?, graded_at = current_timestamp
                where id = ?
                """,
                (result, final_home, final_away, json.dumps(grade_info), row["id"]),
            )
            payload = _signal_outcome_payload_for_row(row, result)
            if payload:
                signal_payloads.append(payload)
            primary += 1

        candidate_rows = conn.execute(
            f"""
            select *
            from prediction_candidate_history
            where graded_at is null and match_id in ({placeholders})
            """,
            tuple(keys),
        ).fetchall()
        for row in candidate_rows:
            result = _grade_candidate_row(row, final_home, final_away)
            grade_info = _grading_reason_for_candidate_row(row, final_home, final_away, result)
            conn.execute(
                """
                update prediction_candidate_history
                set result = ?, final_home = ?, final_away = ?, grading_reason_json = ?, graded_at = current_timestamp
                where id = ?
                """,
                (result, final_home, final_away, json.dumps(grade_info), row["id"]),
            )
            payload = _signal_outcome_payload_for_row(row, result)
            if payload:
                signal_payloads.append(payload)
            candidate += 1
        conn.commit()
    for payload in signal_payloads:
        _store_signal_outcome_payload(payload)
    decisions = _grade_decision_logs_by_ids(keys, final_home, final_away)
    return {"primary": primary, "candidate": candidate, "decisions": decisions}


def _grade_decision_logs_by_ids(keys: list[str], final_home: int, final_away: int) -> int:
    clean_keys = []
    for value in keys:
        if value is not None and str(value) not in clean_keys:
            clean_keys.append(str(value))
    if not clean_keys:
        return 0
    placeholders = ",".join("?" for _ in clean_keys)
    graded = 0
    with _conn(timeout=15) as conn:
        rows = conn.execute(
            f"""
            select *
            from prediction_decision_log
            where graded_at is null and match_id in ({placeholders})
            """,
            tuple(clean_keys),
        ).fetchall()
        for row in rows:
            grade_info = _grade_decision_row(row, final_home, final_away)
            conn.execute(
                """
                update prediction_decision_log
                set result = ?, final_home = ?, final_away = ?, grading_reason_json = ?, graded_at = current_timestamp
                where id = ?
                """,
                (grade_info.get("result"), final_home, final_away, json.dumps(grade_info), row["id"]),
            )
            graded += 1
        conn.commit()
    return graded


def _grade_decision_row(row: sqlite3.Row, final_home: int, final_away: int) -> dict[str, Any]:
    pick_type = row["pick_type"]
    selection = row["selection"]
    if row["decision_type"] == "published" and pick_type != "no_bet":
        info = grading_reason(pick_type, selection, final_home, final_away, row["match_name"])
        if info.get("result") == "void":
            info["result"] = _grade_pick_for_match(pick_type, selection, final_home, final_away, row["match_name"])
        info["decision_type"] = row["decision_type"]
        return info

    audit = _safe_json(row["audit_json"], {})
    rejected = (((audit.get("signals") or {}).get("rejected")) or []) if isinstance(audit, dict) else []
    rejected_outcome = None
    rejected_pick = None
    for item in rejected:
        rejected_type = item.get("type") or item.get("pick_type")
        rejected_selection = item.get("selection")
        if not rejected_type or not rejected_selection:
            continue
        result = grading_reason(rejected_type, rejected_selection, final_home, final_away, row["match_name"]).get("result")
        if result in {"win", "loss"}:
            rejected_outcome = result
            rejected_pick = item
            break

    if row["decision_type"] in {"no_bet", "deferred"}:
        if rejected_outcome == "loss":
            result = "correct_avoid"
            reason = "Avoid/deferred decision protected against a rejected pick that would have lost."
        elif rejected_outcome == "win":
            result = "missed_opportunity"
            reason = "Avoid/deferred decision skipped a rejected pick that would have won."
        else:
            result = "avoided"
            reason = "No graded rejected pick was available, so the no-prediction decision is recorded as avoided."
    else:
        result = "observed"
        reason = "Decision was observed after match completion."

    return {
        "version": "decision_grading_v1",
        "result": result,
        "decision_type": row["decision_type"],
        "final_score": {"home": final_home, "away": final_away, "total": final_home + final_away},
        "rejected_pick_checked": rejected_pick,
        "reason": reason,
    }


def _safe_mark_buffer_finished(match_id: str, final_home: Any, final_away: Any) -> None:
    try:
        _mark_buffer_finished(match_id, final_home, final_away)
    except Exception:
        pass


def _mark_buffer_finished(match_id: str, final_home: Any, final_away: Any) -> None:
    try:
        from app.mongo_store import archive_finished_match_from_buffer
    except Exception:
        archive_finished_match_from_buffer = None

    with _conn(timeout=15) as conn:
        _ensure_buffer_tables(conn)
        for table in ("match_buffer", "future_match_buffer"):
            try:
                conn.execute(
                    f"""
                    update {table}
                    set period = 'Ended', score_home = ?, score_away = ?, is_live = 0, is_finished = 1
                    where match_id = ?
                    """,
                    (str(final_home), str(final_away), match_id),
                )
            except Exception:
                continue
        conn.commit()
    if archive_finished_match_from_buffer:
        try:
            archive_finished_match_from_buffer(match_id)
        except Exception:
            pass


def _normalise_start_seconds(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        ts = float(value)
    except Exception:
        return None
    if ts > 10_000_000_000:
        ts = ts / 1000
    return ts


def _datetime_to_seconds(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        try:
            dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _date_from_start(value: Any) -> str | None:
    ts = _normalise_start_seconds(value)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def _sofa_ids_from_raw(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        doc = json.loads(raw)
    except Exception:
        return []
    candidates = [
        doc.get("sofascore_id"),
        (doc.get("sofascore_event") or {}).get("id") if isinstance(doc.get("sofascore_event"), dict) else None,
        (doc.get("sofascore_detail") or {}).get("id") if isinstance(doc.get("sofascore_detail"), dict) else None,
        ((doc.get("sofascore_detail") or {}).get("raw_event") or {}).get("id") if isinstance(doc.get("sofascore_detail"), dict) else None,
    ]
    out: list[str] = []
    for value in candidates:
        if value is not None and str(value) not in out:
            out.append(str(value))
    return out


def _ensure_buffer_tables(conn: sqlite3.Connection) -> None:
    try:
        from app.buffer import _init_buffer_table

        _init_buffer_table(conn)
    except Exception:
        pass


def _sofascore_ids_for_predictions(conn: sqlite3.Connection, match_ids: list[str]) -> dict[str, list[str]]:
    """Map prediction SportyBet ids back to saved SofaScore ids from the buffer."""
    if not match_ids:
        return {}
    placeholders = ",".join("?" for _ in match_ids)
    mapping: dict[str, list[str]] = {match_id: [] for match_id in match_ids}
    try:
        rows = []
        for table in ("match_buffer", "future_match_buffer"):
            try:
                rows.extend(conn.execute(
                    f"""
                    select match_id, raw_enriched
                    from {table}
                    where match_id in ({placeholders})
                    """,
                    tuple(match_ids),
                ).fetchall())
            except Exception:
                continue
    except Exception:
        return mapping

    for row in rows:
        match_id = str(row["match_id"])
        try:
            doc = json.loads(row["raw_enriched"] or "{}")
        except Exception:
            doc = {}
        candidates = [
            doc.get("sofascore_id"),
            (doc.get("sofascore_event") or {}).get("id") if isinstance(doc.get("sofascore_event"), dict) else None,
            (doc.get("sofascore_detail") or {}).get("id") if isinstance(doc.get("sofascore_detail"), dict) else None,
            ((doc.get("sofascore_detail") or {}).get("raw_event") or {}).get("id") if isinstance(doc.get("sofascore_detail"), dict) else None,
        ]
        for value in candidates:
            if value is not None:
                sofa_id = str(value)
                if sofa_id not in mapping.setdefault(match_id, []):
                    mapping[match_id].append(sofa_id)
    return mapping


def get_grading_metrics() -> dict[str, Any]:
    _init_db()
    with _conn() as conn:
        totals = conn.execute(
            """
            select
                count(*) as total,
                sum(case when graded_at is not null then 1 else 0 end) as graded,
                sum(case when result = 'win' then 1 else 0 end) as wins,
                sum(case when result = 'loss' then 1 else 0 end) as losses,
                sum(case when result = 'void' then 1 else 0 end) as voids
            from (
                select *
                from (
                    select
                        ph.*,
                        row_number() over (
                            partition by match_id, pick_type, selection
                            order by
                                case when graded_at is not null then 0 else 1 end,
                                datetime(coalesce(graded_at, created_at)) desc,
                                id desc
                        ) as rn
                    from prediction_history ph
                    where pick_type != 'no_bet'
                )
                where rn = 1
            )
            """
        ).fetchone()
        by_type = conn.execute(
            """
            select pick_type,
                   count(*) as total,
                   sum(case when result = 'win' then 1 else 0 end) as wins,
                   sum(case when result = 'loss' then 1 else 0 end) as losses
            from (
                select *
                from (
                    select
                        ph.*,
                        row_number() over (
                            partition by match_id, pick_type, selection
                            order by
                                case when graded_at is not null then 0 else 1 end,
                                datetime(coalesce(graded_at, created_at)) desc,
                                id desc
                        ) as rn
                    from prediction_history ph
                    where graded_at is not null and pick_type != 'no_bet'
                )
                where rn = 1
            )
            group by pick_type
            order by total desc
            """
        ).fetchall()
        recent = conn.execute(
            """
            select result, confidence, match_name, selection, created_at
            from (
                select *
                from (
                    select
                        ph.*,
                        row_number() over (
                            partition by match_id, pick_type, selection
                            order by datetime(coalesce(graded_at, created_at)) desc, id desc
                        ) as rn
                    from prediction_history ph
                    where graded_at is not null and pick_type != 'no_bet'
                )
                where rn = 1
            )
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
    return _grade_pick_for_match(pick_type, selection, home, away, None)


def _grade_pick_for_match(pick_type: str | None, selection: str | None, home: int, away: int, match_name: str | None = None) -> str:
    total = home + away
    sel = (selection or "").lower()
    pt = (pick_type or "").lower()

    if pt == "no_bet":
        return "void"

    if pt == "goals":
        if "under 3.5" in sel:
            return "win" if total < 4 else "loss"
        if "under 2.5" in sel:
            return "win" if total < 3 else "loss"
        if "under 1.5" in sel:
            return "win" if total < 2 else "loss"
        if "over 2.5" in sel:
            return "win" if total > 2 else "loss"
        if "over 1.5" in sel:
            return "win" if total > 1 else "loss"
        if "over 0.5" in sel:
            return "win" if total > 0 else "loss"
        if ("both teams to score" in sel or "btts" in sel) and (" no" in sel or "- no" in sel):
            return "win" if not (home > 0 and away > 0) else "loss"
        if "both teams to score" in sel or "btts" in sel:
            return "win" if home > 0 and away > 0 else "loss"
        return "void"

    if pt == "live_goals":
        if "over 0.5" in sel or "next goal" in sel or "late goal" in sel:
            return "win" if total > 0 else "loss"
        return "void"

    if pt == "live_team_to_score":
        return "void"

    if pt in ("match_result", "double_chance", "market_value", "ensemble_1x2", "value_bet"):
        sel_lower = sel
        if "home or draw" in sel_lower or "draw or home" in sel_lower or sel_lower.strip() == "1x":
            return "win" if home >= away else "loss"
        if "away or draw" in sel_lower or "draw or away" in sel_lower or sel_lower.strip() == "x2":
            return "win" if away >= home else "loss"
        if "home or away" in sel_lower or "away or home" in sel_lower or sel_lower.strip() == "12":
            return "win" if home != away else "loss"
        # Handle "{Team} or draw protection" and "{Team} double chance"
        if "or draw" in sel_lower or "double chance" in sel_lower:
            picked_side = _side_from_selection_and_match(sel_lower, match_name)
            if picked_side == "home":
                return "win" if home >= away else "loss"
            if picked_side == "away":
                return "win" if away >= home else "loss"
            # These are home-or-draw / away-or-draw style picks
            # Try to detect which side from context
            if "home" in sel_lower:
                return "win" if home >= away else "loss"
            if "away" in sel_lower:
                return "win" if away >= home else "loss"
            # Generic "or draw protection" — treat as favourite side wins or draws
            return "win" if home == away or home > away else "loss"
        if "home" in sel_lower:
            return "win" if home > away else "loss"
        if "away" in sel_lower:
            return "win" if away > home else "loss"
        if "draw" in sel_lower:
            return "win" if home == away else "loss"
        return "void"

    return "void"


def _side_from_selection_and_match(selection: str, match_name: str | None) -> str | None:
    if not match_name or " vs " not in match_name:
        return None
    home_name, away_name = [part.strip().lower() for part in match_name.split(" vs ", 1)]
    sel = selection.lower()
    if home_name and home_name in sel:
        return "home"
    if away_name and away_name in sel:
        return "away"
    home_tokens = [part for part in re.split(r"\W+", home_name) if len(part) >= 4]
    away_tokens = [part for part in re.split(r"\W+", away_name) if len(part) >= 4]
    home_hits = sum(1 for token in home_tokens if token in sel)
    away_hits = sum(1 for token in away_tokens if token in sel)
    if home_hits > away_hits:
        return "home"
    if away_hits > home_hits:
        return "away"
    return None


def list_prediction_history(limit: int = 200, match_id: str | None = None) -> dict[str, Any]:
    _init_db()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if match_id:
        clauses.append("match_id = ?")
        params.append(str(match_id))
    with _conn() as conn:
        order_clause = "datetime(created_at) asc, id asc" if match_id else "datetime(created_at) desc, id desc"
        rows = conn.execute(
            f"""
            select *
            from prediction_history
            where {" and ".join(clauses)}
            order by {order_clause}
            limit ?
            """,
            (*params, limit),
        ).fetchall()
    return {"predictions": [_prediction_row(row) for row in rows]}


def list_prediction_decisions(limit: int = 200, match_id: str | None = None) -> dict[str, Any]:
    _init_db()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if match_id:
        clauses.append("match_id = ?")
        params.append(match_id)
    with _conn() as conn:
        rows = conn.execute(
            f"""
            select *
            from prediction_decision_log
            where {" and ".join(clauses)}
            order by datetime(created_at) desc, id desc
            limit ?
            """,
            (*params, max(1, min(int(limit or 200), 1000))),
        ).fetchall()
    return {"decisions": [_decision_row(row) for row in rows]}


def save_betbuilder(
    selections: list[dict[str, Any]],
    combined_odds: float,
    confidence: int,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _init_db()
    with _conn() as conn:
        cursor = conn.execute(
            """
            insert into betbuilder_history (selections_json, combined_odds, confidence, request_json, created_at)
            values (?, ?, ?, ?, current_timestamp)
            """,
            (json.dumps(selections), combined_odds, confidence, json.dumps(request or {})),
        )
        conn.commit()
        bet_id = cursor.lastrowid
        _record_betbuilder_legs(conn, bet_id, selections)
        conn.commit()
    return {"id": bet_id, "selections": _decorate_betbuilder_selections(selections), "combined_odds": combined_odds, "confidence": confidence, "request": request or {}}


def list_betbuilder_history(limit: int = 100, auto_grade: bool = True) -> dict[str, Any]:
    _init_db()
    grade_summary = grade_betbuilder_history(limit=min(max(limit, 1), 1000)) if auto_grade else {"graded": 0, "pending": 0}
    with _conn() as conn:
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
        "grading": grade_summary,
        "bets": [
            {
                "id": row["id"],
                "selections": _decorate_betbuilder_selections(_safe_json(row["selections_json"], []), _betbuilder_leg_results(row["id"])),
                "combined_odds": row["combined_odds"],
                "confidence": row["confidence"],
                "request": _safe_json(row["request_json"] if "request_json" in row.keys() else "{}", {}),
                "result": row["result"] if "result" in row.keys() else None,
                "leg_results": _safe_json(row["leg_results_json"] if "leg_results_json" in row.keys() else "[]", []),
                "learning": _safe_json(row["learning_json"] if "learning_json" in row.keys() else "{}", {}),
                "graded_at": row["graded_at"] if "graded_at" in row.keys() else None,
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


def grade_betbuilder_history(limit: int = 200) -> dict[str, Any]:
    """Resolve saved betbuilder slips from graded leg predictions."""
    _init_db()
    with _conn() as conn:
        rows = conn.execute(
            """
            select id, selections_json
            from betbuilder_history
            where graded_at is null
            order by created_at asc
            limit ?
            """,
            (limit,),
        ).fetchall()
        graded = pending = 0
        for row in rows:
            selections = _safe_json(row["selections_json"], [])
            leg_results = []
            unresolved = False
            for selection in selections:
                match_id = str(selection.get("match_id") or "")
                pick_type = str(selection.get("type") or selection.get("pick_type") or "")
                pick_selection = str(selection.get("selection") or "")
                if not pick_type and pick_selection:
                    pick_type = _infer_betbuilder_pick_type(pick_selection)
                if not match_id or not pick_type or not pick_selection:
                    unresolved = True
                    continue
                grade = _latest_leg_grade(conn, match_id, pick_type, pick_selection)
                if not grade:
                    unresolved = True
                else:
                    leg = {
                        **selection,
                        "result": grade["result"],
                        "graded_at": grade.get("graded_at"),
                        "grading_reason": grade.get("grading_reason"),
                    }
                    leg_results.append(leg)
                    _upsert_betbuilder_leg_grade(conn, row["id"], leg)
            if unresolved:
                pending += 1
                continue
            results = [str(item.get("result") or "") for item in leg_results]
            if any(result == "loss" for result in results):
                slip_result = "loss"
            elif results and all(result == "win" for result in results):
                slip_result = "win"
            else:
                slip_result = "void"
            learning = _betbuilder_learning_summary(leg_results, slip_result)
            conn.execute(
                """
                update betbuilder_history
                set result = ?, leg_results_json = ?, learning_json = ?, graded_at = current_timestamp
                where id = ?
                """,
                (slip_result, json.dumps(leg_results), json.dumps(learning), row["id"]),
            )
            graded += 1
        conn.commit()
    return {"graded": graded, "pending": pending}


def _latest_leg_result(conn: sqlite3.Connection, match_id: str, pick_type: str, selection: str) -> str | None:
    grade = _latest_leg_grade(conn, match_id, pick_type, selection)
    return str(grade["result"]) if grade else None


def _latest_leg_grade(conn: sqlite3.Connection, match_id: str, pick_type: str, selection: str) -> dict[str, Any] | None:
    for table in ("prediction_candidate_history", "prediction_history"):
        context_expr = "context_json" if table == "prediction_candidate_history" else "'{}' as context_json"
        row = conn.execute(
            f"""
            select result, graded_at, grading_reason_json, signals_json, {context_expr}, league_name, country_name
            from {table}
            where match_id = ?
              and pick_type = ?
              and selection = ?
              and graded_at is not null
            order by datetime(graded_at) desc, id desc
            limit 1
            """,
            (match_id, pick_type, selection),
        ).fetchone()
        if row and row["result"]:
            return {
                "result": str(row["result"]),
                "graded_at": row["graded_at"],
                "grading_reason": _safe_json(row["grading_reason_json"] if "grading_reason_json" in row.keys() else "{}", {}),
                "signals": _safe_json(row["signals_json"] if "signals_json" in row.keys() else "[]", []),
                "context": _safe_json(row["context_json"] if "context_json" in row.keys() else "{}", {}),
                "league": row["league_name"],
                "country": row["country_name"] if "country_name" in row.keys() else None,
            }
    # Fallback: if we never recorded a leg prediction but we have a final score,
    # grade the leg directly from stored match results.
    score = _latest_finished_score(conn, match_id)
    if score is None:
        return None
    final_home, final_away = score
    grade_info = grading_reason(pick_type, selection, final_home, final_away)
    result = grade_info.get("result")
    if result == "void":
        result = _grade_pick_for_match(pick_type, selection, final_home, final_away, None)
        grade_info["result"] = result
    return {
        "result": str(result or "void"),
        "graded_at": None,
        "grading_reason": grade_info,
        "signals": [],
        "context": {},
        "league": None,
        "country": None,
    }


def _latest_finished_score(conn: sqlite3.Connection, match_id: str) -> tuple[int, int] | None:
    row = conn.execute(
        """
        select final_home_goals, final_away_goals
        from matches
        where match_id = ?
          and is_finished = 1
          and final_home_goals is not null
          and final_away_goals is not null
        order by datetime(last_seen_at) desc
        limit 1
        """,
        (match_id,),
    ).fetchone()
    if row and row[0] is not None and row[1] is not None:
        return (_to_int(row[0], 0), _to_int(row[1], 0))
    row = conn.execute(
        """
        select score_home, score_away
        from finished_matches
        where match_id = ?
        order by datetime(finished_at) desc
        limit 1
        """,
        (match_id,),
    ).fetchone()
    if row and row[0] is not None and row[1] is not None:
        return (_to_int(row[0], 0), _to_int(row[1], 0))
    return None


def _infer_betbuilder_pick_type(selection: str) -> str:
    """Infer a pick_type for betbuilder selections that omitted it."""
    try:
        from app.market_intent import classify_market_intent
    except Exception:
        classify_market_intent = None
    text = str(selection or "")
    if classify_market_intent:
        intent = classify_market_intent("", text, {})
        market = str(intent.get("market") or "")
        if market in {"total_goals", "btts"}:
            return "goals"
        if market == "double_chance":
            return "double_chance"
        if market == "1x2":
            return "match_result"
    lower = text.lower()
    if "over" in lower or "under" in lower or "both teams" in lower or "btts" in lower:
        return "goals"
    if " or draw" in lower or "home or away" in lower or lower.strip() in {"1x", "x2", "12"}:
        return "double_chance"
    if lower.strip() in {"home win", "away win", "draw", "home", "away"}:
        return "match_result"
    return ""


def betbuilder_pick_memory(
    pick_type: str | None,
    selection: str | None,
    league: str | None = None,
    country: str | None = None,
    odds: float | None = None,
) -> dict[str, Any]:
    """Read betbuilder-specific leg memory for future combination selection."""
    _init_db()
    pick_type = str(pick_type or "")
    selection = str(selection or "")
    if not pick_type or not selection:
        return {"samples": 0, "win_rate": None, "adjustment": 0}
    odds_band = _odds_band(odds)
    scopes = []
    with _conn() as conn:
        for label, where, params, weight in [
            ("league_odds", "pick_type=? and selection=? and league_name=? and odds_band=?", (pick_type, selection, league or "", odds_band), 0.45),
            ("country_odds", "pick_type=? and selection=? and country_name=? and odds_band=?", (pick_type, selection, country or "", odds_band), 0.30),
            ("global", "pick_type=? and selection=?", (pick_type, selection), 0.25),
        ]:
            row = conn.execute(
                f"""
                select count(*) samples,
                       sum(case when result='win' then 1 else 0 end) wins,
                       sum(case when result='loss' then 1 else 0 end) losses
                from betbuilder_leg_history
                where graded_at is not null and result in ('win','loss') and {where}
                """,
                params,
            ).fetchone()
            samples = int(row["samples"] or 0)
            wins = int(row["wins"] or 0)
            losses = int(row["losses"] or 0)
            scopes.append({
                "scope": label,
                "weight": weight,
                "samples": samples,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / samples, 3) if samples else None,
            })
    usable = [scope for scope in scopes if scope["samples"]]
    if not usable:
        return {"samples": 0, "win_rate": None, "adjustment": 0, "odds_band": odds_band, "scopes": scopes}
    weight_sum = sum(scope["weight"] for scope in usable)
    blended = sum((scope["win_rate"] or 0) * scope["weight"] for scope in usable) / max(weight_sum, 0.01)
    samples = sum(scope["samples"] for scope in usable)
    adjustment = round(max(-8, min(8, (blended - 0.55) * 28)))
    return {
        "samples": samples,
        "win_rate": round(blended, 3),
        "adjustment": adjustment,
        "odds_band": odds_band,
        "scopes": scopes,
    }


def _record_betbuilder_legs(conn: sqlite3.Connection, bet_id: int, selections: list[dict[str, Any]]) -> None:
    for index, selection in enumerate(selections):
        pick_type = str(selection.get("type") or selection.get("pick_type") or "")
        pick_selection = str(selection.get("selection") or "")
        match_id = str(selection.get("match_id") or "")
        if not pick_type and pick_selection:
            pick_type = _infer_betbuilder_pick_type(pick_selection)
        if not match_id or not pick_type or not pick_selection:
            continue
        odds = _to_float(selection.get("odds") or selection.get("decimal_odds"))
        confidence = _to_int(selection.get("confidence"), 0)
        conn.execute(
            """
            insert or ignore into betbuilder_leg_history (
                bet_id, leg_index, match_id, match_name, league_name, country_name,
                pick_type, selection, odds, odds_band, confidence, role,
                signals_json, context_json, market_intent_json, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
            """,
            (
                bet_id,
                index,
                match_id,
                selection.get("match") or selection.get("match_name"),
                selection.get("league") or selection.get("league_name") or selection.get("tournament"),
                selection.get("country") or selection.get("country_name"),
                pick_type,
                pick_selection,
                odds,
                _odds_band(odds),
                confidence,
                selection.get("role") or ((selection.get("learning") or {}).get("role") if isinstance(selection.get("learning"), dict) else None),
                json.dumps(selection.get("signals") or []),
                json.dumps(selection.get("learning") or selection.get("context") or {}),
                json.dumps(selection.get("market_intent") or {}),
            ),
        )


def _upsert_betbuilder_leg_grade(conn: sqlite3.Connection, bet_id: int, leg: dict[str, Any]) -> None:
    match_id = str(leg.get("match_id") or "")
    pick_type = str(leg.get("type") or leg.get("pick_type") or "")
    selection = str(leg.get("selection") or "")
    if not match_id or not pick_type or not selection:
        return
    conn.execute(
        """
        update betbuilder_leg_history
        set result = ?, grading_reason_json = ?, signals_json = coalesce(nullif(?, '[]'), signals_json),
            graded_at = coalesce(?, current_timestamp)
        where bet_id = ? and match_id = ? and pick_type = ? and selection = ?
        """,
        (
            leg.get("result"),
            json.dumps(leg.get("grading_reason") or {}),
            json.dumps(leg.get("signals") or []),
            leg.get("graded_at"),
            bet_id,
            match_id,
            pick_type,
            selection,
        ),
    )


def _betbuilder_leg_results(bet_id: int) -> dict[str, dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """
            select *
            from betbuilder_leg_history
            where bet_id = ?
            order by leg_index asc, id asc
            """,
            (bet_id,),
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _betbuilder_leg_key(row["match_id"], row["pick_type"], row["selection"])
        out[key] = {
            "result": row["result"],
            "graded_at": row["graded_at"],
            "grading_reason": _safe_json(row["grading_reason_json"], {}),
            "odds_band": row["odds_band"],
            "analysis_url": f"/match/{row['match_id']}",
        }
    return out


def _decorate_betbuilder_selections(selections: list[dict[str, Any]], leg_results: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    leg_results = leg_results or {}
    decorated = []
    for selection in selections:
        match_id = str(selection.get("match_id") or "")
        pick_type = str(selection.get("type") or selection.get("pick_type") or "")
        pick_selection = str(selection.get("selection") or "")
        result = leg_results.get(_betbuilder_leg_key(match_id, pick_type, pick_selection), {})
        decorated.append({
            **selection,
            "match_url": f"/match/{match_id}" if match_id else None,
            "analysis_url": f"/match/{match_id}" if match_id else None,
            "leg_result": result.get("result"),
            "graded_at": result.get("graded_at"),
            "grading_reason": result.get("grading_reason"),
            "odds_band": result.get("odds_band") or _odds_band(_to_float(selection.get("odds"))),
        })
    return decorated


def _betbuilder_learning_summary(legs: list[dict[str, Any]], slip_result: str) -> dict[str, Any]:
    wins = [leg for leg in legs if leg.get("result") == "win"]
    losses = [leg for leg in legs if leg.get("result") == "loss"]
    by_market: dict[str, dict[str, int]] = {}
    by_league: dict[str, dict[str, int]] = {}
    for leg in legs:
        result = str(leg.get("result") or "void")
        for bucket, key in ((by_market, str(leg.get("type") or leg.get("pick_type") or "unknown")), (by_league, str(leg.get("league") or leg.get("league_name") or "Global"))):
            bucket.setdefault(key, {"wins": 0, "losses": 0, "voids": 0})
            if result == "win":
                bucket[key]["wins"] += 1
            elif result == "loss":
                bucket[key]["losses"] += 1
            else:
                bucket[key]["voids"] += 1
    return {
        "slip_result": slip_result,
        "legs": len(legs),
        "wins": len(wins),
        "losses": len(losses),
        "voids": len([leg for leg in legs if leg.get("result") == "void"]),
        "by_market": by_market,
        "by_league": by_league,
        "failure_points": [
            {
                "match_id": leg.get("match_id"),
                "selection": leg.get("selection"),
                "type": leg.get("type") or leg.get("pick_type"),
                "reason": (leg.get("grading_reason") or {}).get("reason"),
            }
            for leg in losses
        ],
    }


def _betbuilder_leg_key(match_id: Any, pick_type: Any, selection: Any) -> str:
    return f"{match_id}|{pick_type}|{selection}"


def _odds_band(odds: float | None) -> str:
    if odds is None:
        return "unknown"
    if odds < 1.30:
        return "1.01-1.29"
    if odds < 1.60:
        return "1.30-1.59"
    if odds < 2.00:
        return "1.60-1.99"
    if odds < 3.00:
        return "2.00-2.99"
    return "3.00+"


# ── Fix 1: Team history cache ─────────────────────────────────────────────────────────────────────────────
# Store team history in SQLite so Poisson/Dixon/ELO read from local cache
# instead of hitting SofaScore on every prediction. Refresh weekly.
_TEAM_HISTORY_CACHE_DAYS = 7


def get_cached_team_history(team_id: int) -> list[dict[str, Any]] | None:
    """Return cached team history if fresh enough, else None."""
    _init_db()
    with _conn() as conn:
        row = conn.execute(
            "select events_json, cached_at from team_history_cache where team_id = ?",
            (str(team_id),),
        ).fetchone()
    if not row:
        return None
    cached_at = row["cached_at"]
    if cached_at:
        from datetime import datetime, timezone, timedelta
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
            if age > timedelta(days=_TEAM_HISTORY_CACHE_DAYS):
                return None
        except Exception:
            pass
    return json.loads(row["events_json"])


def store_team_history(team_id: int, events: list[dict[str, Any]]) -> None:
    """Persist team history to SQLite cache."""
    _init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """
            insert into team_history_cache (team_id, events_json, cached_at)
            values (?, ?, ?)
            on conflict(team_id) do update set events_json=excluded.events_json, cached_at=excluded.cached_at
            """,
            (str(team_id), json.dumps(events), now),
        )
        conn.commit()


def set_engine_status(engine_id: str, status: str) -> dict[str, Any]:
    _init_db()
    with _conn() as conn:
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
    conn = get_db()
    rows = conn.execute("select id, status from engine_state").fetchall()
    return {row[0]: row[1] for row in rows}


def store_enriched_matches(documents: list[dict[str, Any]]) -> int:
    _init_db()
    if not documents:
        return 0
    with _conn() as conn:
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
    with _conn() as conn:
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
    with _conn() as conn:
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
    with _conn() as conn:
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
    with _conn() as conn:
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
            start_time, final_home_goals, final_away_goals, is_finished, country_name, last_seen_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
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
            country_name = excluded.country_name,
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
            _country_from_match(match, league),
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
        tournament_name = tournament.get("name") or tournament.get("uniqueTournament", {}).get("name") or ""
        category = tournament.get("category") or {}
        category_text = category.get("name") if isinstance(category, dict) else ""
        if category_text and tournament_name and not tournament_name.lower().startswith(str(category_text).lower() + " "):
            return f"{category_text} {tournament_name}".strip()
        return tournament_name
    if match.get("league_name"):
        return str(match.get("league_name"))
    category = match.get("category") or match.get("country")
    if not category:
        raw_sporty = match.get("raw_sporty") or {}
        raw_event = raw_sporty.get("raw_event") or match.get("raw_event") or {}
        category = ((raw_event.get("sport") or {}).get("category") or {}).get("name")
    category_text = str(category or "").strip()
    tournament_text = str(tournament or "").strip()
    if category_text and tournament_text.lower().startswith(category_text.lower() + " "):
        return tournament_text
    return " ".join(part for part in [category_text, tournament_text] if part).strip()


def _country_from_match(match: dict[str, Any], league_name: str | None = None) -> str:
    for value in (match.get("country"), match.get("country_name"), match.get("category")):
        if value:
            return str(value)
    tournament = match.get("tournament")
    if isinstance(tournament, dict):
        category = tournament.get("category") or {}
        if isinstance(category, dict) and category.get("name"):
            return str(category.get("name"))
    raw_sporty = match.get("raw_sporty") or {}
    raw_event = raw_sporty.get("raw_event") or match.get("raw_event") or {}
    category = ((raw_event.get("sport") or {}).get("category") or {}).get("name")
    if category:
        return str(category)
    return _country_from_league(league_name)


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


def _get_passed_models(models: dict[str, Any], result: str) -> list[str]:
    """Return the list of model names whose prediction matched the graded result."""
    if not models or result not in ("win", "loss"):
        return []
    passed = []
    for name, model in models.items():
        if not isinstance(model, dict):
            continue
        probs = model.get("probabilities") if model else None
        if probs and isinstance(probs, dict):
            predicted = max(probs, key=probs.get)
            matched = False
            if result == "win" and predicted == "home_win":
                matched = True
            elif result == "loss" and predicted == "away_win":
                matched = True
            elif result == "draw" and predicted == "draw":
                matched = True
            if matched and name not in passed:
                passed.append(name)
            continue
        prediction = model.get("prediction")
        if prediction and isinstance(prediction, str):
            pred_lower = prediction.lower()
            matched = False
            if result == "win" and ("home" in pred_lower or "home win" in pred_lower):
                matched = True
            elif result == "loss" and ("away" in pred_lower or "away win" in pred_lower):
                matched = True
            elif result == "draw" and "draw" in pred_lower:
                matched = True
            if matched and name not in passed:
                passed.append(name)
    return passed


def _prediction_row(row: sqlite3.Row) -> dict[str, Any]:
    picks = _safe_json(row["picks_json"] if "picks_json" in row.keys() else "[]", [])
    stored_best = picks[0] if picks else {}
    models = _safe_json(row["models_json"] if "models_json" in row.keys() else "{}", {})
    result = row["result"] if "result" in row.keys() else None
    passed_models = _get_passed_models(models, result) if result in ("win", "loss") else []
    return {
        "id": row["id"],
        "source": row["source"],
        "match_id": row["match_id"],
        "match_name": row["match_name"],
        "league_name": row["league_name"],
        "best_pick": {
            **stored_best,
            "type": stored_best.get("type") or row["pick_type"],
            "selection": stored_best.get("selection") or row["selection"],
            "confidence": stored_best.get("confidence") or row["confidence"],
            "reason": stored_best.get("reason") or row["reason"],
        },
        "signals": _safe_json(row["signals_json"] if "signals_json" in row.keys() else "[]", []),
        "picks": picks,
        "models": models,
        "passed_models": passed_models,
        "audit": _safe_json(row["audit_json"] if "audit_json" in row.keys() else "{}", {}),
        "prediction_mode": row["prediction_mode"] if "prediction_mode" in row.keys() else "prematch",
        "data_source": row["data_source"] if "data_source" in row.keys() else None,
        "live_data_sources": _safe_json(row["live_data_sources_json"] if "live_data_sources_json" in row.keys() else "[]", []),
        "grading_reason": _safe_json(row["grading_reason_json"] if "grading_reason_json" in row.keys() else "{}", {}),
        "created_at": row["created_at"],
    }


def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
    pick = {
        "type": row["pick_type"],
        "selection": row["selection"],
        "confidence": row["confidence"],
        "reason": row["reason"],
    }
    return {
        "id": row["id"],
        "source": row["source"],
        "match_id": row["match_id"],
        "match_url": f"/match/{row['match_id']}",
        "match_name": row["match_name"],
        "league_name": row["league_name"],
        "country_name": row["country_name"],
        "decision_type": row["decision_type"],
        "best_pick": pick,
        "readiness": _safe_json(row["readiness_json"], {}),
        "signals": _safe_json(row["signals_json"], []),
        "picks": _safe_json(row["picks_json"], []),
        "audit": _safe_json(row["audit_json"], {}),
        "contextual_intelligence": _safe_json(row["contextual_json"], {}),
        "result": row["result"],
        "final_home": row["final_home"],
        "final_away": row["final_away"],
        "grading_reason": _safe_json(row["grading_reason_json"], {}),
        "graded_at": row["graded_at"],
        "created_at": row["created_at"],
    }


def _safe_json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except Exception:
        return fallback


def _country_from_league(league_name: str | None) -> str:
    text = (league_name or "Unknown").strip()
    countries = {
        "argentina", "australia", "austria", "belgium", "brazil", "bulgaria",
        "canada", "chile", "china", "colombia", "croatia", "czech republic",
        "denmark", "ecuador", "egypt", "england", "finland", "france", "germany",
        "ghana", "greece", "india", "indonesia", "ireland", "israel",
        "international", "italy", "japan", "kenya", "kuwait", "liberia", "mexico", "morocco", "netherlands",
        "nigeria", "norway", "oman", "paraguay", "peru", "poland", "portugal",
        "romania", "russia", "saudi arabia", "scotland", "serbia",
        "senegal", "south africa", "south korea", "spain", "sweden", "switzerland",
        "togo", "turkey", "ukraine", "uruguay", "usa", "united states", "wales",
    }
    lower = text.lower()
    for country in sorted(countries, key=len, reverse=True):
        if lower == country or lower.startswith(country + " ") or f" {country} " in f" {lower} ":
            return "USA" if country in {"usa", "united states"} else country.title()
    return "Global"


def _is_country_like(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    if normalized in {"global", "international"}:
        return True
    countries = {
        "argentina", "australia", "austria", "belgium", "brazil", "bulgaria",
        "canada", "chile", "china", "colombia", "croatia", "czech republic",
        "denmark", "ecuador", "egypt", "england", "finland", "france", "germany",
        "ghana", "greece", "india", "indonesia", "ireland", "israel",
        "italy", "japan", "kenya", "kuwait", "liberia", "mexico", "morocco",
        "netherlands", "nigeria", "norway", "oman", "paraguay", "peru",
        "poland", "portugal", "romania", "russia", "saudi arabia", "scotland",
        "senegal", "serbia", "south africa", "south korea", "spain", "sweden",
        "switzerland", "togo", "turkey", "ukraine", "uruguay", "usa",
        "united states", "wales",
    }
    return normalized in countries


def _prediction_scope_stats(conn: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> dict[str, Any]:
    row = conn.execute(
        f"""
        select
            count(*) as samples,
            sum(case when result = 'win' then 1 else 0 end) as wins,
            sum(case when result = 'loss' then 1 else 0 end) as losses
        from (
            select *
            from (
                select
                    ph.*,
                    row_number() over (
                        partition by match_id, pick_type, selection
                        order by datetime(coalesce(graded_at, created_at)) desc, id desc
                    ) as rn
                from prediction_history ph
                where graded_at is not null
                  and result in ('win', 'loss')
            )
            where rn = 1
        )
        where {where}
        """,
        params,
    ).fetchone()
    samples = row["samples"] or 0
    wins = row["wins"] or 0
    return {
        "samples": samples,
        "wins": wins,
        "losses": row["losses"] or 0,
        "win_rate": round(wins / samples, 3) if samples else 0.0,
    }


def _finished_scope_stats(
    conn: sqlite3.Connection,
    where: str,
    params: tuple[Any, ...],
    odds_profile: dict[str, float | str] | None = None,
) -> dict[str, Any]:
    odds_params: tuple[Any, ...] = ()
    if "{odds_filter}" in where:
        if not odds_profile:
            where = where.replace("{odds_filter}", "0 = 1")
        else:
            fav = str(odds_profile.get("favorite_side") or "")
            fav_odds = float(odds_profile.get("favorite_odds") or 0)
            home = float(odds_profile.get("home_odds") or 0)
            draw = float(odds_profile.get("draw_odds") or 0)
            away = float(odds_profile.get("away_odds") or 0)
            where = where.replace(
                "{odds_filter}",
                """
                lo.home_odds is not null and lo.draw_odds is not null and lo.away_odds is not null
                and case
                    when lo.home_odds <= lo.draw_odds and lo.home_odds <= lo.away_odds then 'home'
                    when lo.away_odds <= lo.home_odds and lo.away_odds <= lo.draw_odds then 'away'
                    else 'draw'
                end = ?
                and abs(
                    case
                        when lo.home_odds <= lo.draw_odds and lo.home_odds <= lo.away_odds then lo.home_odds
                        when lo.away_odds <= lo.home_odds and lo.away_odds <= lo.draw_odds then lo.away_odds
                        else lo.draw_odds
                    end - ?
                ) <= 0.45
                and ((abs(lo.home_odds - ?) + abs(lo.draw_odds - ?) + abs(lo.away_odds - ?)) / 3.0) <= 0.75
                """,
            )
            odds_params = (fav, fav_odds, home, draw, away)
    row = conn.execute(
        f"""
        with latest_odds as (
            select os.*
            from odds_snapshots os
            join (
                select match_id, max(snapshot_time) as snapshot_time
                from odds_snapshots
                group by match_id
            ) latest
              on latest.match_id = os.match_id
             and latest.snapshot_time = os.snapshot_time
        )
        select
            count(*) as samples,
            sum(case when m.final_home_goals > m.final_away_goals then 1 else 0 end) as home_wins,
            sum(case when m.final_home_goals = m.final_away_goals then 1 else 0 end) as draws,
            sum(case when m.final_away_goals > m.final_home_goals then 1 else 0 end) as away_wins,
            sum(case when m.final_home_goals + m.final_away_goals > 1 then 1 else 0 end) as over_1_5,
            sum(case when m.final_home_goals + m.final_away_goals > 2 then 1 else 0 end) as over_2_5,
            sum(case when m.final_home_goals + m.final_away_goals > 3 then 1 else 0 end) as over_3_5,
            sum(case when m.final_home_goals > 0 and m.final_away_goals > 0 then 1 else 0 end) as btts,
            avg(m.final_home_goals + m.final_away_goals) as avg_goals
        from matches m
        left join latest_odds lo on lo.match_id = m.match_id
        where m.is_finished = 1
          and m.final_home_goals is not null
          and m.final_away_goals is not null
          and {where}
        """,
        params + odds_params,
    ).fetchone()
    samples = row["samples"] or 0

    def rate(key: str) -> float:
        return round((row[key] or 0) / samples, 3) if samples else 0.0

    return {
        "samples": samples,
        "home_win_rate": rate("home_wins"),
        "draw_rate": rate("draws"),
        "away_win_rate": rate("away_wins"),
        "over_1_5_rate": rate("over_1_5"),
        "over_2_5_rate": rate("over_2_5"),
        "over_3_5_rate": rate("over_3_5"),
        "btts_rate": rate("btts"),
        "avg_goals": round(float(row["avg_goals"] or 0), 3),
        "odds_filtered": bool(odds_profile),
    }


def _match_1x2_odds_profile(match: dict[str, Any]) -> dict[str, float | str] | None:
    markets = match.get("sportybet_markets") or match.get("markets") or []
    odds: dict[str, float] = {}
    for market in markets or []:
        name = str(market.get("name") or "").lower()
        if not (market.get("id") == "1" or "1x2" in name or name == "match result"):
            continue
        for selection in market.get("selections") or market.get("choices") or []:
            sel = str(selection.get("name") or selection.get("label") or "").lower()
            odd = _safe_float(selection.get("odds") or selection.get("decimalOdds") or selection.get("decimal_odds"))
            if not odd or odd <= 1:
                continue
            if sel in {"home", "1"}:
                odds["home_odds"] = odd
            elif sel in {"draw", "x"}:
                odds["draw_odds"] = odd
            elif sel in {"away", "2"}:
                odds["away_odds"] = odd
    if not {"home_odds", "draw_odds", "away_odds"} <= set(odds):
        return None
    favorite_side, favorite_odds = min(
        (("home", odds["home_odds"]), ("draw", odds["draw_odds"]), ("away", odds["away_odds"])),
        key=lambda item: item[1],
    )
    return {**odds, "favorite_side": favorite_side, "favorite_odds": favorite_odds}


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


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


def track_user_behavior(
    match_id: str,
    user_action: str,
    pick_type: str | None = None,
    selection: str | None = None,
    confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Track user interaction with a prediction for self-learning."""
    _init_db()
    if not match_id or not user_action:
        return
    with _conn() as conn:
        conn.execute(
            """
            insert into user_behavior (match_id, user_action, pick_type, selection, confidence, metadata_json)
            values (?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                user_action,
                pick_type,
                selection,
                confidence,
                json.dumps(metadata or {}),
            ),
        )


def get_user_behavior_summary(match_id: str | None = None, days: int = 30) -> dict[str, Any]:
    """Get aggregated user behavior for self-learning."""
    _init_db()
    with _conn() as conn:
        if match_id:
            rows = conn.execute(
                """
                select user_action, pick_type, selection, confidence, metadata_json, created_at
                from user_behavior
                where match_id = ?
                order by created_at desc
                """,
                (match_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                select user_action, pick_type, selection, confidence, metadata_json, created_at
                from user_behavior
                where created_at >= datetime('now', ? || ' days')
                order by created_at desc
                """,
                (f"-{days}",),
            ).fetchall()

    actions = {}
    for row in rows:
        action = row["user_action"]
        if action not in actions:
            actions[action] = {"count": 0, "pick_types": {}, "selections": {}, "confidences": []}
        actions[action]["count"] += 1
        if row["pick_type"]:
            actions[action]["pick_types"][row["pick_type"]] = actions[action]["pick_types"].get(row["pick_type"], 0) + 1
        if row["selection"]:
            actions[action]["selections"][row["selection"]] = actions[action]["selections"].get(row["selection"], 0) + 1
        if row["confidence"] is not None:
            actions[action]["confidences"].append(row["confidence"])

    return {
        "total_interactions": len(rows),
        "by_action": actions,
        "match_id": match_id,
        "period_days": days,
    }


def get_behavior_weighted_picks(match_id: str) -> list[dict[str, Any]]:
    """Get user's past picks for this match to influence auto-bet suggestions."""
    _init_db()
    with _conn() as conn:
        rows = conn.execute(
            """
            select user_action, pick_type, selection, confidence, metadata_json, created_at
            from user_behavior
            where match_id = ?
            order by created_at desc
            """,
            (match_id,),
        ).fetchall()

    picks = []
    for row in rows:
        picks.append({
            "user_action": row["user_action"],
            "pick_type": row["pick_type"],
            "selection": row["selection"],
            "confidence": row["confidence"],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            "created_at": row["created_at"],
        })
    return picks
