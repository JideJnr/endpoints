"""
SofaScore-Only Pipeline
-----------------------
A self-contained ingest → enrich → predict pipeline that uses SofaScore as
the sole data source.  Designed for cloud deployments (Render, Railway, etc.)
where SportyBet's API blocks datacenter IPs.

Pipeline stages
~~~~~~~~~~~~~~~
1. INGEST   — Fetch today's SofaScore scheduled/live events for a date.
              Each SofaScore event is converted to a buffer-compatible document
              and written into `match_buffer` using `match_id = "sofa:{event_id}"`.
              Existing rows are updated (upsert), not duplicated.

2. ENRICH   — Fetch full SofaScore detail (H2H, team form, standings, lineups,
              incidents, statistics) for each buffered SofaScore-source match.
              Writes the enriched document back into `raw_enriched`.

3. PREDICT  — Run `apply_prediction_state` (the same prediction path as the
              normal pipeline) on each enriched SofaScore match.

Toggle
~~~~~~
A persistent engine-state flag `sofa_pipeline_mode` controls whether the
scheduled enrichment workers also process SofaScore-source matches. It can be
set via the API endpoint:

    POST /sofa-pipeline/toggle   body: {"enabled": true|false}

Manual one-shot runs:
    POST /sofa-pipeline/run      body: {"date": "YYYY-MM-DD", "limit": 50}
"""
from __future__ import annotations

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db, normalize_league
from app.utils.match_state import classify_match_state
from app.market.season_stage import detect_season_stage
from app.data_clients.sofascore_client import (
    fetch_all_scheduled_events,
    fetch_event_detail,
    fetch_live_events,
    is_usable_event_for_mode,
    is_terminal_event,
)
from app.utils.time_context import match_time_context
from app.storage.buffer import store_enriched, get_buffered_match, _sofa_live_data

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
SOFA_ID_PREFIX = "sofa:"
ENRICH_WORKERS = 1
ENGINE_STATE_ID = "sofa_pipeline"
LEGACY_ENGINE_STATE_ID = "sofa_pipeline_mode"


# ── Toggle helpers ─────────────────────────────────────────────────────────────

def get_sofa_pipeline_mode() -> dict[str, Any]:
    """Return current toggle state from the engine_states table."""
    try:
        from app.storage.league_memory import get_engine_states
        states = get_engine_states()
        enabled = states.get(ENGINE_STATE_ID, states.get(LEGACY_ENGINE_STATE_ID)) == "active"
    except Exception:
        enabled = False
    return {
        "enabled": enabled,
        "engine_id": ENGINE_STATE_ID,
        "description": "SofaScore-only enrichment and prediction pipeline",
    }


def set_sofa_pipeline_mode(enabled: bool) -> dict[str, Any]:
    """Persist toggle state."""
    from app.storage.league_memory import set_engine_status
    from app.utils.activity_log import record_activity

    status = "active" if enabled else "paused"
    set_engine_status(ENGINE_STATE_ID, status)
    set_engine_status(LEGACY_ENGINE_STATE_ID, status)
    record_activity(
        f"SofaScore pipeline {'enabled' if enabled else 'disabled'}",
        job="sofa_pipeline",
        status="ok",
        details={"enabled": enabled},
    )
    return get_sofa_pipeline_mode()


# ── Stage 1: Ingest from SofaScore ────────────────────────────────────────────

def _sofa_event_to_buffer_doc(event: dict[str, Any], match_date: str) -> dict[str, Any]:
    sofa_id = str(event.get("id") or "")
    match_id = f"{SOFA_ID_PREFIX}{sofa_id}"
    home = event.get("home_team") or {}
    away = event.get("away_team") or {}
    home_name = (home.get("name") or "") if isinstance(home, dict) else str(home or "")
    away_name = (away.get("name") or "") if isinstance(away, dict) else str(away or "")
    name = event.get("name") or f"{home_name} vs {away_name}"
    tournament = event.get("tournament") or {}
    tournament_name = (tournament.get("name") or "") if isinstance(tournament, dict) else str(tournament or "")
    raw_event = event.get("raw_event") or {}
    category_name = ""
    try:
        cat = (raw_event.get("tournament") or {}).get("category") or {}
        category_name = cat.get("name") or ""
    except Exception:
        pass

    start_ts = event.get("start_timestamp")
    start_time_ms = int(start_ts) * 1000 if start_ts else None

    status = event.get("status") or {}
    status_type = str(status.get("type") or "").lower()
    period = "Not start"
    if status_type == "inprogress":
        period = "1H"
    elif status_type in ("finished", "ended"):
        period = "FT"
    elif status_type == "halftime":
        period = "HT"

    score = event.get("score") or {}
    state = classify_match_state(event)

    raw_sporty = {
        "id": match_id,
        "name": name,
        "home_team": home_name,
        "away_team": away_name,
        "tournament": tournament_name,
        "category": category_name,
        "start_time": start_time_ms,
        "period": period,
        "score": score,
        "markets": [],
        "source": "sofascore",
        "sofascore_id": sofa_id,
    }

    return {
        "match_id": match_id,
        "sofascore_id": sofa_id,
        "match_date": match_date,
        "tournament": tournament_name,
        "category": category_name,
        "name": name,
        "home_team": home_name,
        "away_team": away_name,
        "start_time": start_time_ms,
        "period": period,
        "score": score,
        "is_live": bool(state.get("is_live")),
        "is_finished": bool(state.get("is_finished") or state.get("state") in {"postponed", "cancelled"}),
        "raw_sporty": raw_sporty,
        "sofascore_event": event,
        "sofascore_name": name,
        "source": "sofascore",
        "sportybet_id": match_id,
        "sportybet_name": name,
        "sofascore_match_status": "matched",
        "markets": [],
        "sportybet_markets": [],
    }


def ingest_from_sofascore(
    match_date: str | None = None,
    include_live: bool = True,
    limit: int = 300,
) -> dict[str, Any]:
    _init_db()
    target_date = match_date or date_cls.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()

    raw_events: list[dict] = []
    try:
        raw_events = fetch_all_scheduled_events(target_date)
    except Exception as exc:
        logger.error("sofa_pipeline.ingest: fetch_all_scheduled_events failed: %s", exc)

    if include_live:
        try:
            live_events = fetch_live_events()
            seen_ids = {str(e.get("id") or "") for e in raw_events}
            for e in live_events:
                eid = str(e.get("id") or "")
                if eid and eid not in seen_ids:
                    raw_events.append(e)
                    seen_ids.add(eid)
        except Exception as exc:
            logger.warning("sofa_pipeline.ingest: fetch_live_events failed: %s", exc)

    usable = [event for event in raw_events[:limit] if not is_terminal_event(event)]

    inserted = updated = skipped = 0

    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")

        for event in usable:
            sofa_id = str(event.get("id") or "")
            if not sofa_id:
                skipped += 1
                continue

            doc = _sofa_event_to_buffer_doc(event, target_date)
            match_id = doc["match_id"]
            is_live = 1 if doc["is_live"] else 0
            is_finished = 1 if doc["is_finished"] else 0

            if is_finished:
                skipped += 1
                continue

            exists = conn.execute(
                "select 1 from match_buffer where match_id = ?", (match_id,)
            ).fetchone()

            raw_sporty_json = json.dumps(doc["raw_sporty"])

            conn.execute(
                """
                insert into match_buffer (
                    match_id, match_date, tournament, category, name,
                    start_time, period, score_home, score_away,
                    is_live, is_finished, ingested_at,
                    data_source, sofascore_id, raw_sporty
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(match_id) do update set
                    match_date   = excluded.match_date,
                    tournament   = excluded.tournament,
                    category     = excluded.category,
                    name         = excluded.name,
                    start_time   = excluded.start_time,
                    period       = excluded.period,
                    score_home   = excluded.score_home,
                    score_away   = excluded.score_away,
                    is_live      = excluded.is_live,
                    is_finished  = excluded.is_finished,
                    data_source  = case
                        when match_buffer.sportybet_id is not null then 'both'
                        else excluded.data_source
                    end,
                    sofascore_id = excluded.sofascore_id,
                    raw_sporty   = excluded.raw_sporty,
                    ingested_at  = excluded.ingested_at
                """,
                (
                    match_id, target_date,
                    doc["tournament"], doc["category"], doc["name"],
                    doc["start_time"], doc["period"],
                    str((doc["score"] or {}).get("home") or ""),
                    str((doc["score"] or {}).get("away") or ""),
                    is_live, is_finished, now,
                    "sofascore",
                    doc["sofascore_id"], raw_sporty_json,
                ),
            )

            if exists:
                updated += 1
            else:
                inserted += 1

        conn.commit()

    logger.info(
        "sofa_pipeline.ingest: date=%s inserted=%d updated=%d skipped=%d total_fetched=%d",
        target_date, inserted, updated, skipped, len(raw_events),
    )
    return {
        "status": "ok",
        "date": target_date,
        "fetched": len(raw_events),
        "usable": len(usable),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


# ── Stage 2: Enrich with SofaScore detail ─────────────────────────────────────

def _get_sofa_buffer_matches(
    match_date: str | None = None,
    unenriched_only: bool = True,
    live_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return buffered matches that were ingested via SofaScore pipeline."""
    _init_db()
    clauses = [
        "is_finished = 0",
        "match_id like 'sofa:%'",
    ]
    params: list[Any] = []
    if match_date:
        clauses.append("match_date = ?")
        params.append(match_date)
    if unenriched_only:
        clauses.append(
            "(enriched_at is null or enriched_at < datetime('now', '-30 minutes') "
            "or (is_live = 1 and enriched_at < datetime('now', '-3 minutes')))"
        )
    if live_only:
        clauses.append("is_live = 1")

    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select match_id, sofascore_id, raw_sporty, raw_enriched, is_live, match_date
            from match_buffer
            where {" and ".join(clauses)}
            order by is_live desc, start_time asc
            limit ?
            """,
            (*params, limit),
        ).fetchall()

    return [
        {
            "match_id": row["match_id"],
            "sofascore_id": row["sofascore_id"],
            "is_live": bool(row["is_live"]),
            "match_date": row["match_date"],
            "raw_sporty": json.loads(row["raw_sporty"]) if row["raw_sporty"] else {},
            "existing": json.loads(row["raw_enriched"]) if row["raw_enriched"] else None,
        }
        for row in rows
    ]


def enrich_sofa_pipeline(
    match_date: str | None = None,
    batch_size: int = 10,
    live_only: bool = False,
) -> dict[str, Any]:
    """
    Stage 2: Enrich SofaScore-source buffer matches with full SofaScore detail.
    Uses parallel fetch for detail calls.
    Stage 3 (predict) runs after all enrichment writes are done — decoupled so
    a slow Ollama call cannot block the enrichment of the next match.
    """
    _init_db()
    target_date = match_date or date_cls.today().isoformat()
    batch = _get_sofa_buffer_matches(
        match_date=target_date,
        unenriched_only=True,
        live_only=live_only,
        limit=batch_size,
    )

    if not batch:
        return {"status": "idle", "pending": 0, "date": target_date}

    now = datetime.now(timezone.utc).isoformat()
    enriched_count = predicted_count = errors = 0

    def _fetch_detail(item: dict) -> tuple[dict, dict | None]:
        sofa_id = item.get("sofascore_id")
        existing = item.get("existing") or {}
        sofa_event = existing.get("sofascore_event") or item["raw_sporty"].get("sofascore_event") or {}
        if not sofa_event and sofa_id:
            sofa_event = {"id": int(sofa_id), "home_team": {}, "away_team": {}, "tournament": {}}
        if not sofa_event or not sofa_event.get("id"):
            return item, None
        try:
            return item, fetch_event_detail(sofa_event)
        except Exception as exc:
            logger.warning("sofa_pipeline.enrich: detail fetch failed for %s: %s", sofa_id, exc)
            return item, None

    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        futures = {pool.submit(_fetch_detail, item): item for item in batch}
        results: list[tuple[dict, dict | None]] = [
            future.result() for future in as_completed(futures)
        ]

    # ── Stage 2: store all enriched docs first ────────────────────────────────
    enriched_docs: list[tuple[str, dict]] = []
    for item, detail in results:
        match_id = item["match_id"]
        try:
            existing = item.get("existing") or {}
            raw_sporty = item["raw_sporty"]

            sofa_event = existing.get("sofascore_event") or {}
            if not sofa_event:
                sofa_event = {"id": int(item["sofascore_id"])} if item.get("sofascore_id") else {}

            home_name = (detail or sofa_event or {}).get("home_team", {})
            if isinstance(home_name, dict):
                home_name = home_name.get("name") or raw_sporty.get("home_team") or ""
            away_name = (detail or sofa_event or {}).get("away_team", {})
            if isinstance(away_name, dict):
                away_name = away_name.get("name") or raw_sporty.get("away_team") or ""

            match_state = classify_match_state(sofa_event or raw_sporty)
            time_ctx = match_time_context({**raw_sporty, "sofascore_event": sofa_event})

            doc = {
                **existing,
                "sportybet_id": match_id,
                "sofascore_id": item.get("sofascore_id"),
                "match_id": match_id,
                "source": "sofascore",
                "data_source": "sofascore",
                "name": raw_sporty.get("name"),
                "sportybet_name": raw_sporty.get("name"),
                "sofascore_name": raw_sporty.get("name"),
                "home_team": home_name,
                "away_team": away_name,
                "tournament": raw_sporty.get("tournament"),
                "category": raw_sporty.get("category"),
                "match_date": item["match_date"],
                "start_time": raw_sporty.get("start_time"),
                "period": raw_sporty.get("period"),
                "score": raw_sporty.get("score") or {},
                "sofascore_event": sofa_event,
                "sofascore_detail": detail,
                "live_data_sofascore": _sofa_live_data(detail),
                "live_data_sportybet": {},
                "home_last_matches": (detail or {}).get("home_last_matches") or [],
                "away_last_matches": (detail or {}).get("away_last_matches") or [],
                "standings": (detail or {}).get("standings") or [],
                "league_table": (detail or {}).get("standings") or [],
                "season_stage": detect_season_stage((detail or {}).get("standings") or []),
                "sofascore_match_status": "matched",
                "minimum_enrichment_status": "full_provider_match" if detail else "sofascore_only",
                "sportybet_markets": [],
                "markets": [],
                "sportybet_detail": {
                    "source": "sofascore",
                    "id": match_id,
                    "name": raw_sporty.get("name"),
                    "home_team": home_name,
                    "away_team": away_name,
                    "tournament": raw_sporty.get("tournament"),
                    "category": raw_sporty.get("category"),
                    "start_time": raw_sporty.get("start_time"),
                    "period": raw_sporty.get("period"),
                    "score": raw_sporty.get("score") or {},
                    "markets": [],
                    "market_count": 0,
                    "odds_1x2": {},
                    "refreshed_at": now,
                },
                "sportybet_data_status": "unavailable_cloud_mode",
                "time_context": time_ctx,
                "match_state": match_state,
                "web_context": existing.get("web_context") or {},
                "data_sources": {
                    "sportybet": {"available": False, "reason": "cloud_mode_no_sportybet"},
                    "sofascore": {
                        "available": True,
                        "matched": True,
                        "detail": bool(detail),
                        "statistics": bool((detail or {}).get("statistics")),
                        "history": bool((detail or {}).get("home_last_matches")),
                    },
                    "sportradar": {"available": False},
                },
                "data_source_detail": {
                    "sportybet": {"available": False, "reason": "cloud_mode_no_sportybet"},
                    "sofascore": {
                        "available": True,
                        "matched": True,
                        "detail": bool(detail),
                        "statistics": bool((detail or {}).get("statistics")),
                        "history": bool((detail or {}).get("home_last_matches")),
                        "live_data_available": bool(_sofa_live_data(detail)),
                        "live_data_fetched_at": (_sofa_live_data(detail) or {}).get("fetched_at"),
                    },
                    "sportradar": {"available": False},
                },
                "raw_sporty": raw_sporty,
                "raw_sofascore_event": sofa_event.get("raw_event") if isinstance(sofa_event, dict) else None,
                "enriched_at": now,
                "is_live": item["is_live"],
                "is_finished": False,
                "match_score": 1.0,
                "manual_match": False,
            }

            store_enriched(match_id, doc)
            enriched_count += 1
            enriched_docs.append((match_id, doc))

        except Exception as exc:
            logger.error("sofa_pipeline.enrich: failed for %s: %s", item.get("match_id"), exc)
            errors += 1

    # ── Stage 3: Predict after all enrichment writes are done (decoupled) ─────
    from app.utils.prediction_flow import apply_prediction_state
    for match_id, doc in enriched_docs:
        try:
            fresh_doc = get_buffered_match(match_id) or doc
            pred_result = apply_prediction_state(fresh_doc, match_id=match_id)
            if pred_result.get("status") == "predicted":
                predicted_count += 1
        except Exception as exc:
            logger.debug("sofa_pipeline: prediction failed for %s: %s", match_id, exc)

    logger.info(
        "sofa_pipeline.enrich: date=%s enriched=%d predicted=%d errors=%d",
        target_date, enriched_count, predicted_count, errors,
    )
    return {
        "status": "ok",
        "date": target_date,
        "batch": len(batch),
        "enriched": enriched_count,
        "predicted": predicted_count,
        "errors": errors,
    }


# ── Full cycle ─────────────────────────────────────────────────────────────────

def run_sofa_pipeline_cycle(
    match_date: str | None = None,
    ingest_limit: int = 300,
    enrich_batch: int = 20,
    include_live: bool = True,
) -> dict[str, Any]:
    from app.utils.activity_log import record_activity

    target_date = match_date or date_cls.today().isoformat()
    record_activity(
        f"SofaScore pipeline cycle starting for {target_date}",
        job="sofa_pipeline",
        status="running",
    )

    ingest_result = ingest_from_sofascore(
        match_date=target_date,
        include_live=include_live,
        limit=ingest_limit,
    )

    enrich_result = enrich_sofa_pipeline(
        match_date=target_date,
        batch_size=enrich_batch,
    )

    live_result = {}
    if ingest_result.get("fetched", 0) > 0:
        live_result = enrich_sofa_pipeline(
            match_date=target_date,
            batch_size=min(enrich_batch, 10),
            live_only=True,
        )

    record_activity(
        f"SofaScore pipeline cycle done: {enrich_result.get('enriched')} enriched, "
        f"{enrich_result.get('predicted')} predicted",
        job="sofa_pipeline",
        status="ok",
        details={"ingest": ingest_result, "enrich": enrich_result, "live": live_result},
    )

    return {
        "status": "ok",
        "date": target_date,
        "ingest": ingest_result,
        "enrich": enrich_result,
        "live_enrich": live_result,
    }

