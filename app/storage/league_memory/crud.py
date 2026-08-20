"""
league_memory.crud
~~~~~~~~~~~~~~~~~~
Direct SQLite read / write operations for the league memory store.
Higher-level analytical queries live in queries.py.
"""
from __future__ import annotations

import re
import os
import sqlite3
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.storage.db import (
    DB_PATH, _conn, close_db, connect_readonly_db, db_conn, get_db,
    _init_db, _init_db_unlocked, _ensure_column, _is_sqlite_lock,
    _DB_SCHEMA_READY, _DB_SCHEMA_LOCK, _run_schema_migrations,
    _run_legacy_backfills, _existing_schema_can_be_trusted,
    _ensure_prediction_history_columns,
)
from app.config.config import get_settings
from app.match_facts import enrich_match_facts
from app.signal_combinations import build_signal_combination, live_context_from_prediction
from app.market.market_intent import classify_market_intent, grade_market_intent
from app.monitoring.prediction_audit import build_pick_audit, build_prediction_audit, grading_reason
from app.competition.competition_registry import init_competition_registry_tables, ensure_competition
from .schema import _ensure_signal_outcomes_table
from ._helpers import (
    normalize_league, _league_from_match, _country_from_match, _team_name,
    _match_fingerprint, _match_minute, _minute_bucket, _bucket_bounds,
    _score_state, _red_card_state, _favorite_from_match,
    _to_int, _to_float, _safe_json, _safe_float,
    _memory_row, _match_row, _snapshot_row, _prediction_row, _decision_row,
    _snapshot_memory_row, _get_passed_models, _standings_from_matches,
    _match_1x2_odds_profile, _normalise_start_seconds, _datetime_to_seconds,
    _date_from_start, _sofa_ids_from_raw,
    _same_team, _close_match_row, _team_form_from_rows,
    _grade_pick_for_match, _grade_pick, _side_from_selection_and_match,
    _betbuilder_leg_key, _odds_band, _infer_betbuilder_pick_type,
    _decorate_betbuilder_selections, _betbuilder_learning_summary,
    _TEAM_HISTORY_CACHE_DAYS,
    _country_from_league, _is_country_like,
    grade_prediction_row,
)

logger = logging.getLogger(__name__)

def observe_match(source: str, match: dict[str, Any]) -> dict[str, Any]:
    _init_db()
    match = enrich_match_facts(match)
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
        from app.utils.match_state import classify_match_state

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
        init_competition_registry_tables(conn)
        # ── Auto-verify / auto-create the competition ────────────────────────
        try:
            ensure_competition(
                conn,
                name=league,
                category=str(match.get("category") or ""),
                country=str(match.get("country_name") or ""),
            )
        except Exception as _exc:
            logger.debug("ensure_competition failed for league=%s: %s", league, _exc)

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
        from app.storage.mongo_store import save_finished_match

        archived = {
            **match,
            "league_key": normalize_league(league),
            "league_name": league,
        }
        return save_finished_match(source, archived)
    except Exception:
        return False


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
    # VACUUM requires an exclusive lock — run it outside the write transaction
    # so concurrent readers/writers are not blocked.
    with _conn() as conn:
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
    live_context = live_context_from_prediction(prediction)
    combination = build_signal_combination(
        signals=prediction.get("signals") or [],
        pick_type=best_pick.get("type"),
        selection=best_pick.get("selection"),
        prediction_mode=prediction.get("prediction_mode") or live_context.get("prediction_mode"),
        live_context=live_context,
    )
    _record_prediction_decision(prediction, source, match_id, league_name, country_name, audit)
    _record_prediction_candidates(prediction, source, match_id, league_name, country_name)

    # ── Fix 3: skip junk predictions ───────────────────────────────────────────
    # Don't pollute prediction_history with no_bet or sub-55% confidence picks.
    if best_pick.get("type") == "no_bet":
        return
    if (best_pick.get("confidence") or 0) < 55:
        return
    with _conn() as conn:
        match_name_key = _prediction_text_key(prediction.get("name"))
        league_name_key = _prediction_text_key(league_name)
        existing = conn.execute(
            """
            select id
            from prediction_history
            where coalesce(prediction_mode, 'prematch') = ?
              and pick_type = ?
              and selection = ?
              and (
                    match_id = ?
                 or (
                    ? != ''
                    and lower(trim(coalesce(match_name, ''))) = ?
                    and lower(trim(coalesce(league_name, ''))) = ?
                    and datetime(created_at) >= datetime('now', '-180 days')
                 )
              )
            order by created_at desc
            limit 1
            """,
            (
                prediction.get("prediction_mode") or "prematch",
                best_pick.get("type"),
                best_pick.get("selection"),
                match_id,
                match_name_key,
                match_name_key,
                league_name_key,
            ),
        ).fetchone()
        if existing:
            return
        conn.execute(
            """
            insert into prediction_history (
                source, match_id, match_name, league_name, pick_type, selection,
                confidence, reason, signals_json, picks_json, audit_json,
                country_name, sofascore_id, sportybet_id, prediction_mode,
                data_source, live_data_sources_json, models_json,
                signal_combination_key, signal_combination_json, live_context_json, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
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
                combination.get("key"),
                json.dumps(combination.get("payload") or {}),
                json.dumps(live_context),
            ),
        )
        conn.commit()

    # ── Record CLV entry: capture entry odds at prediction time ────────────────
    try:
        from app.risk.clv import record_clv_entry
        record_clv_entry(
            match_id=match_id,
            pick_type=best_pick.get("type") or "match_result",
            selection=best_pick.get("selection") or "",
            confidence=int(best_pick.get("confidence") or 0),
            match_name=prediction.get("name"),
            match_date=prediction.get("match_date"),
        )
    except Exception as exc:
        from app.utils.health_counters import record_health_event

        record_health_event("league_memory", "clv_entry_record_failed", exc, match_id=match_id)


def _prediction_text_key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


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


def _backfill_local_signal_outcomes_from_history(limit: int = 5000) -> int:
    """Populate local signal analytics from already graded rows once."""
    try:
        from app.monitoring.self_learner import _decision_signals_for_row
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


def _ensure_buffer_tables(conn: sqlite3.Connection) -> None:
    """Ensure match_buffer and future_match_buffer exist without holding a long lock."""
    for table in ("match_buffer", "future_match_buffer"):
        try:
            conn.execute(
                f"""
                create table if not exists {table} (
                    match_id     text primary key,
                    match_date   text,
                    tournament   text,
                    category     text,
                    name         text,
                    start_time   integer,
                    period       text,
                    score_home   text,
                    score_away   text,
                    is_live      integer not null default 0,
                    is_finished  integer not null default 0,
                    ingested_at  text not null default current_timestamp,
                    enriched_at  text,
                    data_source  text not null default 'sportybet',
                    sportybet_id text,
                    sofascore_id text,
                    raw_sporty   text not null default '{{}}',
                    raw_enriched text
                )
                """
            )
        except Exception:
            pass


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
    from .queries import get_league_memory, get_snapshot_memory
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
    result = _grade_pick_for_match(pick_type, selection, final_home, final_away, None)
    grade_info = grading_reason(pick_type, selection, final_home, final_away)
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
    with db_conn(timeout=5) as conn:
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
        from app.storage.mongo_store import store_enriched_matches as store_mongo_enriched_matches

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
        from app.storage.mongo_store import get_enriched_matches as get_mongo_enriched_matches

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
        from app.storage.mongo_store import get_enriched_match as get_mongo_enriched_match

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
    half_time = match.get("half_time_score") or {}
    goal_timing = match.get("goal_timing") or {}
    conn.execute(
        """
        insert into matches (
            source, match_id, league_key, league_name, match_fingerprint, home_team, away_team,
            start_time, half_time_home_goals, half_time_away_goals,
            final_home_goals, final_away_goals, goal_times_json, average_goal_interval_minutes,
            live_statistics_json, provider_capabilities_json, is_finished, country_name, last_seen_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
        on conflict(source, match_id) do update set
            league_key = excluded.league_key,
            league_name = excluded.league_name,
            match_fingerprint = excluded.match_fingerprint,
            home_team = excluded.home_team,
            away_team = excluded.away_team,
            start_time = excluded.start_time,
            half_time_home_goals = coalesce(excluded.half_time_home_goals, matches.half_time_home_goals),
            half_time_away_goals = coalesce(excluded.half_time_away_goals, matches.half_time_away_goals),
            final_home_goals = case when excluded.is_finished = 1 then excluded.final_home_goals else matches.final_home_goals end,
            final_away_goals = case when excluded.is_finished = 1 then excluded.final_away_goals else matches.final_away_goals end,
            goal_times_json = case when excluded.goal_times_json != '[]' then excluded.goal_times_json else matches.goal_times_json end,
            average_goal_interval_minutes = coalesce(excluded.average_goal_interval_minutes, matches.average_goal_interval_minutes),
            live_statistics_json = case when excluded.live_statistics_json != '{}' then excluded.live_statistics_json else matches.live_statistics_json end,
            provider_capabilities_json = case when excluded.provider_capabilities_json != '{}' then excluded.provider_capabilities_json else matches.provider_capabilities_json end,
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
            half_time.get("home"),
            half_time.get("away"),
            home_goals if is_finished else None,
            away_goals if is_finished else None,
            json.dumps(goal_timing.get("goal_minutes") or []),
            goal_timing.get("average_interval_minutes"),
            json.dumps(match.get("live_statistics") or {}),
            json.dumps(match.get("provider_live_capabilities") or {}),
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


def _safe_mark_buffer_finished(match_id: str, final_home: Any, final_away: Any) -> None:
    try:
        _mark_buffer_finished(match_id, final_home, final_away)
    except Exception:
        pass


def _mark_buffer_finished(match_id: str, final_home: Any, final_away: Any) -> None:
    try:
        from app.storage.mongo_store import archive_finished_match_from_buffer
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



