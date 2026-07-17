from __future__ import annotations

import logging
from datetime import date as dt
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from app.buffer import (
    get_buffered_matches,
    get_buffered_match,
    get_live_buffered_matches,
    get_buffer_stats,
)
from app.league_memory import list_prediction_history
from app.market import get_movement
from app.match_state import classify_match_state
from app.scheduler import scheduler_status

try:
    from cachetools import TTLCache
    _similar_cache: TTLCache = TTLCache(maxsize=200, ttl=300)
except ImportError:
    _similar_cache = {}  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["frontend"])


@router.get("/matches/today")
def get_matches_today():
    target_date = dt.today().isoformat()
    docs = get_buffered_matches(target_date)
    return {
        "status": "success",
        "date": target_date,
        "count": len(docs),
        "matches": [_match_summary(doc) for doc in docs],
    }


@router.get("/matches/live")
def get_live_matches():
    """All currently live matches from the buffer regardless of date."""
    docs = get_live_buffered_matches()
    return {
        "status": "success",
        "count": len(docs),
        "matches": [_match_summary(doc) for doc in docs],
    }


@router.get("/matches/by-date/{match_date}")
def get_matches_by_date(match_date: str, limit: int = Query(default=500, ge=1, le=1000)):
    docs = get_buffered_matches(match_date, limit=limit)
    return {
        "status": "success",
        "date": match_date,
        "count": len(docs),
        "matches": [_match_summary(doc) for doc in docs],
    }


@router.get("/matches/today/{sportybet_id}")
def get_today_match_detail(sportybet_id: str):
    sportybet_id = _resolve_buffer_match_id(sportybet_id)
    doc = get_buffered_match(sportybet_id)
    if not doc:
        archived = _archived_match_detail(sportybet_id)
        if archived:
            return archived
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found")
    return _match_detail(doc)


@router.get("/matches/{sportybet_id}/similar")
def get_similar_matches(
    sportybet_id: str,
    limit: int = Query(default=10, ge=1, le=25),
):
    """
    Return up to `limit` historical matches most similar to the given match,
    ranked by a composite similarity score (ELO proximity + odds proximity + league bonus).

    Results are cached for 5 minutes per sportybet_id.
    """
    from app.similar_matches import find_similar_matches

    sportybet_id = _resolve_buffer_match_id(sportybet_id)
    cache_key = (sportybet_id, limit)

    # ── Cache hit ─────────────────────────────────────────────────────────────
    if cache_key in _similar_cache:
        return _similar_cache[cache_key]

    # ── Fetch buffered match ──────────────────────────────────────────────────
    doc = get_buffered_match(sportybet_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found")

    # ── Compute similarity ────────────────────────────────────────────────────
    try:
        from app.similar_matches import find_similar_matches, _extract_target_odds_implied
        target_implied = _extract_target_odds_implied(doc)
        target_odds_context = None
        if target_implied:
            try:
                target_odds_context = {
                    "home": round(1.0 / target_implied[0], 2),
                    "draw": round(1.0 / target_implied[1], 2),
                    "away": round(1.0 / target_implied[2], 2),
                    "home_implied_pct": round(target_implied[0] * 100, 1),
                    "draw_implied_pct": round(target_implied[1] * 100, 1),
                    "away_implied_pct": round(target_implied[2] * 100, 1),
                }
            except Exception:
                pass
        matches = find_similar_matches(doc, limit=limit)
    except Exception as exc:
        _logger.exception("Similar matches computation failed for %s", sportybet_id)
        raise HTTPException(
            status_code=500,
            detail=f"Similar matches computation failed: {exc}",
        )

    result = {
        "status": "success",
        "sportybet_id": sportybet_id,
        "match_name": doc.get("sportybet_name") or doc.get("name") or sportybet_id,
        "target_odds": target_odds_context,
        "odds_filter_applied": target_odds_context is not None,
        "count": len(matches),
        "matches": matches,
    }

    _similar_cache[cache_key] = result
    return result


@router.get("/matches/{sportybet_id}/sporty-info")
def get_match_sporty_info(sportybet_id: str):
    """Fetch fresh SportyBet endpoint data for one match and merge it into buffer."""
    from app.buffer import refresh_sporty_match_state
    from app.sportybet_client import fetch_match_info

    sportybet_id = _resolve_buffer_match_id(sportybet_id)
    try:
        info = fetch_match_info(sportybet_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not info.get("found"):
        raise HTTPException(status_code=404, detail=info)

    refresh = {}
    try:
        refresh = refresh_sporty_match_state(sportybet_id)
    except Exception as exc:
        refresh = {"status": "error", "detail": str(exc)}

    return {
        "status": "success",
        "sportybet_id": sportybet_id,
        "endpoint": info.get("api_endpoint"),
        "scope": info.get("scope"),
        "request_payload": info.get("request_payload"),
        "match": info.get("match"),
        "buffer_refresh": refresh,
    }


@router.get("/matches/{sportybet_id}/sportradar-info")
def get_match_sportradar_info(sportybet_id: str):
    """Fetch fresh Sportradar/SIR widget stats for one buffered SportyBet match."""
    from app.buffer import _data_sources, store_enriched
    from app.sportradar_client import fetch_match_intelligence

    sportybet_id = _resolve_buffer_match_id(sportybet_id)
    doc = get_buffered_match(sportybet_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found in buffer")
    raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
    sportradar = fetch_match_intelligence(raw_sporty.get("id") or sportybet_id)
    merged = {**doc, "sportradar_detail": sportradar}
    merged["data_sources"] = _data_sources(
        merged.get("sofascore_event"),
        merged.get("sofascore_detail"),
        merged.get("raw_sporty") or raw_sporty,
        sportradar,
    )
    store_enriched(sportybet_id, merged)
    return {
        "status": "success" if sportradar.get("available") else "unavailable",
        "sportybet_id": sportybet_id,
        "has_sportradar": bool(sportradar.get("available")),
        "sportradar": sportradar,
    }


@router.post("/matches/sportradar-enrich")
def post_sportradar_enrich_all(limit: int = Query(default=500, ge=1, le=2000)):
    """Backfill Sportradar/SIR widget stats for buffered matches without running full prediction."""
    from app.buffer import _data_sources, store_enriched
    from app.sportradar_client import fetch_match_intelligence

    docs = get_buffered_matches(limit=limit)
    processed = available = stored = 0
    errors: list[dict[str, Any]] = []
    for doc in docs:
        sportybet_id = str(doc.get("sportybet_id") or doc.get("id") or "")
        if not sportybet_id:
            continue
        raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
        sportradar = fetch_match_intelligence(raw_sporty.get("id") or sportybet_id)
        processed += 1
        available += 1 if sportradar.get("available") else 0
        if not sportradar.get("available"):
            errors.append({"sportybet_id": sportybet_id, "error": sportradar.get("error")})
        merged = {**doc, "sportradar_detail": sportradar}
        merged["data_sources"] = _data_sources(
            merged.get("sofascore_event"),
            merged.get("sofascore_detail"),
            merged.get("raw_sporty") or raw_sporty,
            sportradar,
        )
        store_enriched(sportybet_id, merged)
        stored += 1
    return {
        "status": "success",
        "processed": processed,
        "available": available,
        "stored": stored,
        "errors": errors[:25],
    }


@router.get("/matches/{sportybet_id}/sofascore-candidates")
def get_sofascore_candidates(sportybet_id: str):
    """Return the full SofaScore pool for this SportyBet match state."""
    from app.buffer import _is_ghost_match, _sofascore_date_candidates
    from app.sofascore_client import fetch_all_scheduled_events, fetch_live_events, is_usable_event_for_mode

    sportybet_id = _resolve_buffer_match_id(sportybet_id)
    doc = get_buffered_match(sportybet_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found in buffer")

    target_date = doc.get("match_date") or dt.today().isoformat()
    live = _is_live_doc(doc)
    raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else {}
    period = doc.get("period") or raw_sporty.get("period")
    if not live and _is_ghost_match(doc.get("start_time"), period):
        return {
            "status": "success",
            "sportybet_id": sportybet_id,
            "match_date": target_date,
            "dates_scanned": [],
            "mode": "prematch",
            "count": 0,
            "candidates": [],
            "reason": "sportybet_match_start_time_is_stale",
        }

    scan_dates = [target_date]
    if not live:
        sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
        scan_dates = _sofascore_date_candidates(sporty, target_date)
        from app.buffer import _with_search_fallback_candidates

        search_filtered = _with_search_fallback_candidates(sporty, [], live=False)
        search_pool = sorted(
            (_candidate_summary(event, doc, target_date) for event in search_filtered),
            key=lambda item: item["score"],
            reverse=True,
        )
        confident_search = [item for item in search_pool if float(item.get("score") or 0) >= 0.70]
        if confident_search:
            return {
                "status": "success",
                "sportybet_id": sportybet_id,
                "match_date": target_date,
                "dates_scanned": scan_dates,
                "mode": "prematch",
                "count": len(confident_search),
                "candidates": confident_search,
                "best_score": float(search_pool[0].get("score") or 0),
                "reason": "sofascore_search_fallback",
            }

    try:
        if live:
            events = fetch_live_events()
        else:
            events = []
            seen_ids: set[str] = set()
            for scan_date in scan_dates:
                for event in fetch_all_scheduled_events(scan_date):
                    eid = str(event.get("id") or "")
                    if eid and eid not in seen_ids:
                        events.append(event)
                        seen_ids.add(eid)
    except Exception as exc:
        # SofaScore unreachable / rate-limited — return empty list with a clear message
        return {
            "status": "error",
            "sportybet_id": sportybet_id,
            "match_date": target_date,
            "mode": "live" if live else "prematch",
            "count": 0,
            "candidates": [],
            "error": f"SofaScore scan failed: {exc}",
        }

    filtered = []
    for event in events:
        if not is_usable_event_for_mode(event, live=live):
            continue
        filtered.append(event)
    if not live:
        from app.buffer import _with_search_fallback_candidates

        sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
        filtered = _with_search_fallback_candidates(sporty, filtered, live=False)

    candidate_pool = sorted(
        (_candidate_summary(event, doc, target_date) for event in filtered),
        key=lambda item: item["score"],
        reverse=True,
    )
    min_score = 0.58 if live else 0.70
    candidates = [item for item in candidate_pool if float(item.get("score") or 0) >= min_score]
    best_score = float(candidate_pool[0].get("score") or 0) if candidate_pool else 0.0
    reason = None
    if not candidates:
        reason = (
            "no_usable_sofascore_events_found"
            if not candidate_pool
            else "best_candidate_below_prematch_confidence_threshold"
        )
    return {
        "status": "success",
        "sportybet_id": sportybet_id,
        "match_date": target_date,
        "dates_scanned": scan_dates,
        "mode": "live" if live else "prematch",
        "count": len(candidates),
        "rejected_low_score": len(candidate_pool) - len(candidates),
        "candidate_pool": len(candidate_pool),
        "best_score": round(best_score, 3),
        "min_score": min_score,
        "reason": reason,
        "candidates": candidates,
    }


@router.post("/matches/{sportybet_id}/sofascore-match")
def match_sofascore_candidate(
    sportybet_id: str,
    payload: dict[str, Any] = Body(...),
):
    """Manually attach a SofaScore event and persist the correction."""
    from app.buffer import store_enriched
    from app.sofascore_client import fetch_all_scheduled_events, fetch_live_events, is_usable_event_for_mode

    sofa_id = payload.get("sofascore_id")
    if sofa_id is None:
        raise HTTPException(status_code=400, detail="sofascore_id is required")

    sportybet_id = _resolve_buffer_match_id(sportybet_id)
    doc = get_buffered_match(sportybet_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {sportybet_id} not found")

    dates = []
    for value in (payload.get("match_date"), doc.get("match_date"), dt.today().isoformat()):
        if value and value not in dates:
            dates.append(value)

    sofa = None
    if _is_live_doc(doc):
        try:
            sofa = next((event for event in fetch_live_events() if str(event.get("id")) == str(sofa_id)), None)
        except Exception:
            sofa = None
    for target_date in dates:
        if sofa:
            break
        try:
            events = fetch_all_scheduled_events(target_date)
        except Exception:
            continue
        sofa = next((event for event in events if str(event.get("id")) == str(sofa_id)), None)
        if sofa:
            break
    if not sofa and isinstance(payload.get("event"), dict):
        sofa = payload["event"]
    if not sofa:
        raise HTTPException(status_code=404, detail=f"SofaScore event {sofa_id} was not found")
    if not is_usable_event_for_mode(sofa, live=_is_live_doc(doc)):
        status = sofa.get("status") or {}
        raise HTTPException(
            status_code=400,
            detail=f"SofaScore event {sofa_id} is not usable for this match state: {status.get('description') or status.get('type')}",
        )

    now = datetime.now(timezone.utc).isoformat()
    enriched_doc = {
        **doc,
        "sportybet_id": sportybet_id,
        "sportybet_name": doc.get("sportybet_name") or doc.get("name"),
        "match_date": doc.get("match_date") or payload.get("match_date") or dt.today().isoformat(),
        "sofascore_id": sofa.get("id"),
        "sofascore_name": sofa.get("name"),
        "sofascore_event": sofa,
        "sofascore_detail": None,
        "web_context": {},
        "match_score": _candidate_score(sofa, doc),
        "manual_match": True,
        "manual_matched_at": now,
        "raw_sporty": doc.get("raw_sporty") or doc,
        "raw_sofascore_event": sofa.get("raw_event"),
        "enriched_at": now,
    }

    store_enriched(sportybet_id, enriched_doc)
    return {
        "status": "success",
        "sportybet_id": sportybet_id,
        "sofascore_id": sofa.get("id"),
        "matched_sofascore": True,
        "saved": True,
        "needs_enrichment": True,
        "match": _match_detail(enriched_doc),
    }


@router.post("/predictions/refresh")
def refresh_predictions_today():
    """
    Re-enrich all buffered matches and append fresh predictions.
    Prediction history is immutable because grading/learning depends on what the
    engine actually said at the time.
    """
    from datetime import date
    from app.league_memory import _init_db
    from app.buffer import get_buffered_matches, refresh_sporty_buffer_scope
    from app.agentic_prediction import AgentExecutionError, run_agentic_match_prediction

    today = date.today().isoformat()
    _init_db()

    # 1. Refresh SportyBet state before rebuilding predictions.
    refresh_state = {"live": None, "upcoming": None}
    for scope in ("live", "upcoming"):
        try:
            refresh_state[scope] = refresh_sporty_buffer_scope(scope)
        except Exception as exc:
            refresh_state[scope] = {"status": "error", "error": str(exc)}

    # 2. Re-enrich active buffered matches before predicting so odds/Sofa/detail stay in sync.
    docs = get_buffered_matches(today, limit=500)
    predicted = 0
    errors = 0
    enriched = 0
    skipped_already_predicted = 0
    skipped_inactive = 0
    deferred = 0
    agent_runs = []
    for doc in docs:
        match_id = str(doc.get("sportybet_id") or doc.get("id") or "")
        try:
            if not match_id:
                errors += 1
                continue
            result = run_agentic_match_prediction(match_id)
            state = result.get("prediction_state") or {}
            completed_keys = {
                item.get("key")
                for item in (result.get("agent") or {}).get("completed", [])
                if isinstance(item, dict)
            }
            if "enrich_context" in completed_keys:
                enriched += 1
            if state.get("status") == "predicted":
                predicted += 1
            elif state.get("status") == "skipped":
                skipped_already_predicted += 1
            elif state.get("status") == "deferred":
                deferred += 1
            else:
                errors += 1
            agent_runs.append({
                "match_id": match_id,
                "status": result.get("status"),
                "prediction_status": state.get("status"),
                "completed": list(completed_keys),
            })
        except AgentExecutionError as exc:
            skipped_inactive += 1 if "inactive" in str(exc).lower() else 0
            errors += 0 if "inactive" in str(exc).lower() else 1
            agent_runs.append({"match_id": match_id, "status": "failed", "message": str(exc)})
        except Exception:
            errors += 1

    return {
        "status": "success",
        "date": today,
        "sporty_refresh": refresh_state,
        "deleted_old": 0,
        "history_policy": "append_only",
        "enriched": enriched,
        "predicted": predicted,
        "deferred": deferred,
        "skipped_already_predicted": skipped_already_predicted,
        "errors": errors,
        "skipped_inactive": skipped_inactive,
        "total_matches": len(docs),
        "agentic_execution": {
            "mode": "plan_before_action",
            "runs": agent_runs[:50],
            "note": "Each match was validated, freshness-checked, readiness-checked, enriched only if needed, then predicted or skipped.",
        },
    }


@router.get("/predictions/today")
def get_predictions_today():
    """Return today's latest predictions per match, sorted by confidence desc. Excludes no_bet."""
    from datetime import date
    from app.league_memory import DB_PATH, _init_db
    from app.buffer import _init_buffer_table
    import sqlite3 as _sqlite3

    today = date.today().isoformat()
    today_preds = _list_recent_dashboard_predictions(hours=36, limit=800)

    # Build a lookup of match_id → buffer status (period, is_live, is_finished)
    _init_db()
    buffer_status: dict[str, dict] = {}
    try:
        with _sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.row_factory = _sqlite3.Row
            _init_buffer_table(conn)
            rows = conn.execute(
                """
                select match_id, period, is_live, is_finished, raw_sporty, raw_enriched from match_buffer
                union all
                select match_id, period, is_live, is_finished, raw_sporty, raw_enriched from future_match_buffer
                """
            ).fetchall()
            for row in rows:
                doc = {}
                try:
                    import json as _json
                    doc = _json.loads(row["raw_enriched"] or row["raw_sporty"] or "{}")
                except Exception:
                    doc = {"period": row["period"], "is_finished": bool(row["is_finished"])}
                state = classify_match_state(doc)
                buffer_status[str(row["match_id"])] = {
                    "period":      row["period"],
                    "is_live":     bool(state.get("is_live")),
                    "is_finished": bool(row["is_finished"] or state.get("is_finished")),
                    "match_state": state,
                }
    except Exception:
        pass

    role_rows = _load_role_memory_rows()

    # Keep latest prediction per match_id (list is already ordered by created_at desc)
    seen: dict[str, dict] = {}
    for p in today_preds:
        mid = str(p.get("match_id") or "")
        if not mid or mid in seen:
            continue
        if mid not in buffer_status:
            continue
        if p.get("result") or p.get("graded_at"):
            continue
        # Skip no_bet picks
        picks = p.get("picks") or []
        real_picks = [pk for pk in picks if pk.get("type") != "no_bet"]
        if not real_picks:
            continue
        p = {**p, "picks": real_picks}
        _backfill_role_learning(p, real_picks, mid, role_rows)
        # Prefer the backend learned primary/secondary decision, then confidence.
        best = _learned_best_pick(real_picks)
        p["best_pick"] = best
        # Attach live/period status from buffer
        status = buffer_status.get(mid, {})
        p["period"]      = status.get("period") or p.get("period")
        p["is_live"]     = status.get("is_live", False)
        p["is_finished"] = status.get("is_finished", False)
        if p["is_finished"] or _is_finished_doc(p):
            continue

        # Attach regime info + filter picks that don't meet regime threshold
        try:
            from app.regime import get_regime, passes_regime_gate
            tournament = p.get("league_name") or p.get("tournament") or ""
            regime = get_regime(tournament)
            p["regime"] = {"tier": regime.tier, "name": regime.name}
            # Filter picks below regime minimum confidence
            regime_picks = [
                pk for pk in real_picks
                if int(pk.get("confidence") or 0) >= regime.min_confidence
            ]
            if regime_picks:
                p["picks"] = regime_picks
                _backfill_role_learning(p, regime_picks, mid, role_rows)
                best = _learned_best_pick(regime_picks)
                p["best_pick"] = best
        except Exception:
            pass

        seen[mid] = p
    sorted_preds = sorted(
        seen.values(),
        key=lambda x: str(x.get("created_at") or ""),
        reverse=True,
    )

    # ── Portfolio correlation filter ──────────────────────────────────────────
    try:
        from app.portfolio import filter_correlated, portfolio_summary
        sorted_preds = filter_correlated(sorted_preds)
        summary = portfolio_summary(sorted_preds)
    except Exception:
        summary = {}

    return {
        "status": "success",
        "date": today,
        "count": len(sorted_preds),
        "predictions": sorted_preds,
        "portfolio": summary,
    }


def _list_recent_dashboard_predictions(hours: int = 36, limit: int = 800) -> list[dict[str, Any]]:
    """Compatibility wrapper for older local imports."""
    from app.current_predictions import list_recent_dashboard_predictions

    return list_recent_dashboard_predictions(hours=hours, limit=limit)


@router.get("/matches/upcoming-enriched-predicted")
def get_upcoming_enriched_predicted(limit: int = Query(default=500, ge=1, le=1000)):
    """Upcoming buffered matches with enrichment and latest prediction status."""
    from datetime import date
    from app.buffer import get_buffered_matches, refresh_sporty_buffer_scope
    from app.league_memory import list_prediction_history
    from app.enriched_prediction import prediction_readiness

    today = date.today().isoformat()
    docs = get_buffered_matches(limit=limit)
    history = list_prediction_history(limit=3000).get("predictions") or []
    latest_by_match: dict[str, dict[str, Any]] = {}
    for prediction in history:
        match_id = str(prediction.get("match_id") or "")
        if match_id and match_id not in latest_by_match:
            latest_by_match[match_id] = prediction

    rows = []
    for doc in docs:
        time_context = doc.get("time_context") or {}
        match_date = time_context.get("local_date") or doc.get("match_date") or today
        if str(match_date) < today:
            continue
        if _is_live_doc(doc) or _is_finished_doc(doc):
            continue
        match_id = str(doc.get("sportybet_id") or doc.get("id") or "")
        prediction = latest_by_match.get(match_id)
        readiness = prediction_readiness(doc)
        if prediction and not readiness["ready"]:
            prediction = None
        best_pick = (prediction or {}).get("best_pick") or ((prediction or {}).get("picks") or [{}])[0]
        summary = _match_summary(doc)
        rows.append(
            {
                **summary,
                "match_date": match_date,
                "time_context": doc.get("time_context"),
                "lifecycle": doc.get("lifecycle"),
                "enriched": bool(readiness.get("has_detail")),
                "manual_match": bool(doc.get("manual_match")),
                "predicted": bool(prediction),
                "prediction_readiness": readiness,
                "prediction": prediction,
                "best_pick": best_pick if best_pick else None,
                "data_quality": {
                    "has_sofascore_detail": bool(doc.get("sofascore_detail")),
                    "has_web_context": bool((doc.get("web_context") or {}).get("snippets")),
                    "has_raw_sporty": bool(doc.get("raw_sporty")),
                    "has_raw_sofascore": bool(doc.get("raw_sofascore_event") or doc.get("sofascore_event")),
                },
            }
        )

    rows.sort(
        key=lambda item: (
            int((item.get("best_pick") or {}).get("confidence") or 0),
            1 if item.get("predicted") else 0,
            1 if item.get("enriched") else 0,
            -_sort_start(item.get("start_time")),
        ),
        reverse=True,
    )
    return {
        "status": "success",
        "date": today,
        "count": len(rows),
        "summary": {
            "upcoming": len(rows),
            "enriched": sum(1 for item in rows if item["enriched"]),
            "predicted": sum(1 for item in rows if item["predicted"]),
            "matched_sofascore": sum(1 for item in rows if item.get("sofascore_id")),
        },
        "matches": rows,
    }


@router.post("/matches/purge-ghosts")
def purge_ghost_matches_endpoint():
    """Delete stale not-started matches from the buffer whose kick-off has passed."""
    from app.buffer import purge_ghost_matches
    from app.mongo_store import cleanup_buffer
    purged = purge_ghost_matches()
    cleanup = cleanup_buffer()
    return {
        "status": "success",
        "purged_ghosts": purged,
        "deleted_finished": cleanup.get("deleted_finished", 0),
        "deleted_90_plus": cleanup.get("deleted_90_plus", 0),
        "deleted_stale": cleanup.get("deleted_stale_unenriched", 0),
    }


@router.post("/matches/cleanup")
def cleanup_finished_matches():
    """Immediately remove all finished, 90+, and ghost matches from the buffer."""
    from app.mongo_store import cleanup_buffer
    from app.buffer import purge_ghost_matches
    from app.league_memory import DB_PATH, _init_db
    import sqlite3 as _sqlite3

    # First pass: archive any is_finished=1 rows that weren't cleaned up
    _init_db()
    archived = 0
    with _sqlite3.connect(DB_PATH, timeout=30) as conn:
        rows = conn.execute(
            "select match_id from match_buffer where is_finished = 1"
        ).fetchall()
    for (match_id,) in rows:
        try:
            from app.buffer import _archive_finished_locally
            _archive_finished_locally(match_id)
            archived += 1
        except Exception:
            pass

    # Second pass: cleanup by period string + stale rows + 90+ + ghost
    result = cleanup_buffer()
    ghost_deleted = purge_ghost_matches()

    return {
        "status": "success",
        "archived_locally":  archived,
        "deleted_finished":  result.get("deleted_finished", 0),
        "deleted_90_plus":   result.get("deleted_90_plus", 0),
        "deleted_ghost":     result.get("deleted_ghost", 0) + ghost_deleted,
        "deleted_stale":     result.get("deleted_stale_unenriched", 0),
    }


@router.get("/buffer/status")
def get_buffer_status():
    """Scheduler health + buffer counts. Use to verify background jobs are running."""
    from app.activity_log import get_activity

    return {
        "status": "success",
        "scheduler": scheduler_status(),
        "buffer": get_buffer_stats(),
        "activity": get_activity(limit=12),
    }


@router.get("/system/activity")
def get_system_activity(limit: int = Query(default=30, ge=1, le=120)):
    """Small live activity log for the Settings page."""
    from app.activity_log import get_activity

    return {"status": "success", **get_activity(limit=limit)}


@router.get("/system/audit")
def get_system_audit(limit: int = Query(default=200, ge=20, le=1000)):
    """Operational contract audit for ingest, enrichment, prediction, grading, and jobs."""
    from app.system_audit import prediction_system_audit

    return prediction_system_audit(limit=limit)


@router.get("/system/authority")
def get_system_authority():
    """Current correction authority leases across self-healing loops."""
    from app.loop_authority import authority_snapshot

    return {
        "status": "success",
        "source_of_truth": "scheduler jobs plus scoped correction_authority_leases",
        "scopes": {
            "operational": "buffer/job cleanup only; held by system_supervisor",
            "learning": "calibration/weight refresh only; held by prediction_monitor",
            "flow": "ingest/enrich/grading catch-up only; held by scheduler/job guards",
        },
        "leases": authority_snapshot(),
    }


@router.post("/system/supervisor")
def run_system_supervisor(auto_correct: bool = True, deep_audit: bool = False):
    """Run the safe operational supervisor once."""
    from app.system_supervisor import run_system_supervisor as _run_system_supervisor

    return _run_system_supervisor(auto_correct=auto_correct, deep_audit=deep_audit)


@router.get("/system/supervisor")
def get_system_supervisor_snapshots(limit: int = Query(default=50, ge=1, le=300)):
    """Recent supervisor observations, actions, and recurring operational issues."""
    from app.system_supervisor import latest_supervisor_snapshots

    return latest_supervisor_snapshots(limit=limit)


@router.post("/system/prediction-monitor")
def run_prediction_monitor(auto_correct: bool = True):
    """Run the hourly prediction-quality monitor once."""
    from app.prediction_monitor import run_prediction_monitor as _run_prediction_monitor

    return _run_prediction_monitor(auto_correct=auto_correct)


@router.get("/system/prediction-monitor")
def get_prediction_monitor_snapshots(limit: int = Query(default=50, ge=1, le=300)):
    """Recent prediction-quality snapshots, mismatch patterns, and accuracy trends."""
    from app.prediction_monitor import latest_prediction_monitor_snapshots

    return latest_prediction_monitor_snapshots(limit=limit)


@router.get("/system/desk")
def get_desk_observability(limit: int = Query(default=200, ge=20, le=1000)):
    """Trading-desk style operational view: breaks, stale work, decision/risk log."""
    from app.desk_analytics import desk_observability

    return desk_observability(limit=limit)


@router.get("/competition-special/world-cup/settings")
def get_world_cup_special_settings():
    """Settings-tab contract for the dedicated World Cup competition mode."""
    from app.competition_special import get_competition_settings

    return {"status": "success", "settings": get_competition_settings("world-cup-2026")}


@router.post("/competition-special/world-cup/settings")
def post_world_cup_special_settings(payload: dict[str, Any] = Body(...)):
    """Enable/disable the World Cup special lane and adjust tournament ids/dates."""
    from app.competition_special import update_competition_settings

    return {"status": "success", "settings": update_competition_settings("world-cup-2026", payload)}


@router.post("/competition-special/world-cup/sync")
def post_world_cup_special_sync(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit_days: int = Query(default=60, ge=1, le=90),
):
    """Pull the World Cup fixture list into the dedicated competition buffer."""
    from app.competition_special import sync_competition_fixtures

    return sync_competition_fixtures(
        "world-cup-2026",
        start_date=start_date,
        end_date=end_date,
        limit_days=limit_days,
    )


@router.post("/competition-special/world-cup/enrich-predict")
def post_world_cup_special_enrich_predict(
    limit: int = Query(default=12, ge=1, le=80),
    allow_repeat: bool = False,
):
    """Enrich World Cup rows from SofaScore and run the special prediction path."""
    from app.competition_special import enrich_predict_competition

    return enrich_predict_competition("world-cup-2026", limit=limit, allow_repeat=allow_repeat)


@router.get("/competition-special/world-cup/buffer")
def get_world_cup_special_buffer(limit: int = Query(default=200, ge=1, le=500)):
    """Dedicated World Cup buffer: fixtures, groups, match state, enrichment, predictions."""
    from app.competition_special import list_competition_buffer

    return list_competition_buffer("world-cup-2026", limit=limit)


@router.get("/competition-special/world-cup/status")
def get_world_cup_special_status():
    """Small health summary for the Settings tab and World Cup page."""
    from app.competition_special import competition_status

    return competition_status("world-cup-2026")


@router.get("/analytics/clv")
def get_clv_analytics(days: int = Query(default=30, ge=1, le=365)):
    """
    Closing Line Value analytics.
    avg_clv > 0 means you're consistently beating the closing price = real edge.
    """
    from app.clv import get_clv_summary
    return {"status": "success", **get_clv_summary(days=days)}


@router.post("/analytics/clv/compute")
def compute_clv_now(match_date: Optional[str] = None):
    """Force-compute CLV for a specific date (defaults to today)."""
    from datetime import date
    from app.clv import compute_clv_for_date
    target = match_date or date.today().isoformat()
    result = compute_clv_for_date(target)
    return {"status": "success", **result}


@router.get("/analytics/calibration")
def get_calibration():
    """Historical win rate per pick_type × confidence band. Used to size stakes."""
    from app.confidence_calibrator import get_calibration_table
    return {"status": "success", "calibration": get_calibration_table()}


@router.get("/analytics/backtest-gate")
def get_backtest_gate(limit: int = Query(default=1000, ge=50, le=10000), min_samples: int = Query(default=50, ge=10, le=1000)):
    """Stored-decision replay gate used before trusting model/rule changes."""
    from app.desk_analytics import backtest_gate

    return backtest_gate(limit=limit, min_samples=min_samples)


@router.get("/analytics/signal-attribution")
def get_signal_attribution(min_samples: int = Query(default=5, ge=1, le=100), limit: int = Query(default=5000, ge=100, le=20000)):
    """Signal attribution from graded primary and candidate prediction rows."""
    from app.desk_analytics import signal_attribution_report

    return signal_attribution_report(min_samples=min_samples, limit=limit)


@router.post("/analytics/calibration/rebuild")
def rebuild_calibration_endpoint():
    """Force-rebuild the calibration table from all graded predictions."""
    from app.confidence_calibrator import rebuild_calibration
    result = rebuild_calibration()
    return {"status": "success", **result}


@router.get("/analytics/odds-patterns")
def get_odds_pattern_stats():
    """Aggregate stats on odds movement patterns and their historical win rates."""
    from app.odds_pattern import pattern_stats
    return {"status": "success", **pattern_stats()}


@router.get("/analytics/odds-patterns/{match_id}")
def get_match_pattern_signal(match_id: str):
    """Get the odds pattern signal for a specific match."""
    from app.odds_pattern import pattern_signal
    from urllib.parse import unquote
    signal = pattern_signal(unquote(match_id))
    return {"status": "success", "match_id": match_id, "signal": signal}


@router.get("/analytics/regime")
def get_regime_info(tournament: str = Query(default=""), category: str = Query(default="")):
    """Look up the liquidity regime for a tournament name."""
    from app.regime import get_regime, TIER_1, TIER_2, TIER_3, TIER_4
    regime = get_regime(tournament, category)
    return {
        "status":    "success",
        "tournament": tournament,
        "tier":       regime.tier,
        "name":       regime.name,
        "min_confidence":  regime.min_confidence,
        "edge_threshold":  regime.edge_threshold,
        "stake_cap":       regime.stake_cap,
        "description":     regime.description,
    }


@router.get("/analytics/regime/tiers")
def get_all_tiers():
    """Return all regime tier definitions."""
    from app.regime import TIER_1, TIER_2, TIER_3, TIER_4
    return {
        "status": "success",
        "tiers": [
            {k: getattr(t, k) for k in
             ["tier", "name", "min_confidence", "edge_threshold", "stake_cap", "description"]}
            for t in [TIER_1, TIER_2, TIER_3, TIER_4]
        ],
    }


@router.post("/results/grade")
def grade_results(hours_back: int = 24):
    """
    Fetch finished results from SportyBet and SofaScore, grade pending predictions,
    and archive matched buffer rows to MongoDB.
    """
    import time as _time
    import json
    from datetime import date as _date, timedelta as _timedelta
    from app.sportybet_client import fetch_results
    from app.league_memory import DB_PATH, _init_db, _grade_pick_for_match, grade_overdue_predictions, grade_predictions_for_date, store_local_signal_outcomes
    from app.prediction_audit import grading_reason
    from app.mongo_store import archive_finished_match_from_buffer
    from app.sofascore_client import fetch_all_scheduled_events
    import sqlite3

    now_ms   = int(_time.time() * 1000)
    start_ms = now_ms - (hours_back * 3_600_000)

    try:
        results = fetch_results(start_ms, now_ms, count=500)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SportyBet results fetch failed: {exc}")

    result_map = {
        str(r["id"]): r
        for r in results
        if r.get("score", {}).get("home") is not None
    }

    _init_db()
    graded = archived = skipped = 0

    # grade pending predictions
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        pending = conn.execute(
            "select id, match_id, match_name, league_name, country_name, pick_type, selection, "
            "confidence, signals_json, audit_json, "
            "(select match_date from match_buffer where match_id=ph.match_id limit 1) as match_date "
            "from prediction_history ph "
            "where graded_at is null and pick_type != 'no_bet'"
        ).fetchall()

    for row in pending:
        result = result_map.get(str(row["match_id"]))
        if not result:
            skipped += 1
            continue
        score = result["score"]
        grade_info = grading_reason(row["pick_type"], row["selection"], score["home"], score["away"], row["match_name"])
        outcome = grade_info["result"] if grade_info.get("result") != "void" else _grade_pick_for_match(row["pick_type"], row["selection"], score["home"], score["away"], row["match_name"])
        grade_info["result"] = outcome
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute(
                "update prediction_history set result=?, final_home=?, final_away=?, "
                "grading_reason_json=?, graded_at=current_timestamp where id=?",
                (outcome, score["home"], score["away"], json.dumps(grade_info), row["id"]),
            )
            conn.commit()
        # Store decision-signal outcomes in local device memory; Mongo is only an optional mirror.
        try:
            from app.self_learner import _decision_signals_for_row

            signals = _decision_signals_for_row(row)
        except Exception:
            signals = json.loads(row["signals_json"]) if row["signals_json"] else []
        try:
            tournament = row["league_name"] or ""
            country = row["country_name"] or (tournament.split(" - ")[0].strip() if " - " in tournament else "")
            store_local_signal_outcomes(
                match_id=str(row["match_id"]),
                match_name=row["match_name"],
                tournament=tournament,
                country=country,
                match_date=row["match_date"],
                signals=signals,
                result=outcome,
                pick_type=row["pick_type"],
                selection=row["selection"],
                confidence=row["confidence"],
            )
            try:
                from app.mongo_store import store_signal_outcomes, is_configured
                if is_configured():
                    store_signal_outcomes(
                        match_id=str(row["match_id"]),
                        match_name=row["match_name"],
                        tournament=tournament,
                        country=country,
                        match_date=row["match_date"],
                        signals=signals,
                        result=outcome,
                        pick_type=row["pick_type"],
                        selection=row["selection"],
                        confidence=row["confidence"],
                    )
            except Exception:
                pass
        except Exception:
            pass
        graded += 1

    # archive buffer rows that now have a result
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        buffer_rows = conn.execute(
            "select match_id from match_buffer where is_finished=0"
        ).fetchall()

    for row in buffer_rows:
        if str(row["match_id"]) not in result_map:
            continue
        score = result_map[str(row["match_id"])]["score"]
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute(
                "update match_buffer set period='Ended', score_home=?, score_away=?, "
                "is_live=0, is_finished=1 where match_id=?",
                (str(score["home"]), str(score["away"]), row["match_id"]),
            )
            conn.commit()
        try:
            archive_finished_match_from_buffer(str(row["match_id"]))
            archived += 1
        except Exception:
            pass

    sofascore = {"graded": 0, "skipped": 0, "dates": [], "errors": {}}
    days = max(1, min(7, (hours_back + 23) // 24 + 1))
    for offset in range(days):
        target_date = (_date.today() - _timedelta(days=offset)).isoformat()
        try:
            events = fetch_all_scheduled_events(target_date)
            result = grade_predictions_for_date(target_date, events)
            sofascore["graded"] += int(result.get("graded") or 0)
            sofascore["skipped"] += int(result.get("skipped") or 0)
            sofascore["dates"].append({"date": target_date, **result})
        except Exception as exc:
            sofascore["errors"][target_date] = str(exc)

    overdue = grade_overdue_predictions(hours_after_kickoff=2, limit=500)

    return {
        "status": "ok",
        "results_fetched": len(results),
        "predictions_graded": graded,
        "predictions_skipped": skipped,
        "sofascore_predictions_graded": sofascore["graded"],
        "sofascore_predictions_skipped": sofascore["skipped"],
        "sofascore": sofascore,
        "overdue": overdue,
        "matches_archived": archived,
        "signal_stats": _grade_signal_stats(graded + sofascore["graded"] + int(overdue.get("graded") or 0)),
    }


@router.post("/results/grade-overdue")
def grade_overdue_results(hours_after_kickoff: float = 2.0, limit: int = 500):
    """Grade all pending matches that are past kickoff+N hours, skipping true live matches."""
    from app.league_memory import grade_overdue_predictions

    return grade_overdue_predictions(hours_after_kickoff=hours_after_kickoff, limit=limit)


@router.post("/results/grade-match/{match_id:path}")
def grade_one_match_result(match_id: str, hours_back: int = 72):
    """Check SportyBet/SofaScore for one match result and grade its prediction rows."""
    from app.league_memory import check_and_grade_match_result

    return check_and_grade_match_result(match_id, hours_back=hours_back)


@router.get("/predictions/odds-only")
def get_odds_only_predictions():
    """
    Fast predictions using only 1x2 odds — no SofaScore required.
    Runs on every buffered match that has odds, including unenriched ones.
    Useful as a baseline when enrichment hasn't run yet.
    """
    from app.odds_predictor import odds_only_prediction
    from datetime import date

    today = date.today().isoformat()
    docs = get_buffered_matches(today, limit=500)
    predictions = []
    for doc in docs:
        pred = odds_only_prediction(doc)
        if pred:
            predictions.append(pred)

    predictions.sort(
        key=lambda p: (p.get("picks") or [{}])[0].get("confidence", 0),
        reverse=True,
    )
    return {
        "status": "success",
        "date": today,
        "count": len(predictions),
        "predictions": predictions,
    }


@router.get("/analytics/signals")
def get_signal_analytics(
    country: str = "",
    tournament: str = "",
    min_samples: int = 5,
):
    """
    Signal win rate analytics.
    Scope: whole DB (default), filtered by country, or filtered by tournament.
    """
    from app.league_memory import get_local_signal_stats
    return {
        "status": "success",
        **get_local_signal_stats(
            country=country or None,
            tournament=tournament or None,
            min_samples=min_samples,
        ),
    }


@router.get("/analytics/signal-matches")
def get_signal_matches(
    signal_name: str = Query(default="consensus_longshot_value"),
    result: str = Query(default=""),
    limit: int = Query(default=300, ge=1, le=1000),
):
    """List matches carrying a specific learned signal, with grading summary."""
    import json
    import sqlite3
    from app.league_memory import DB_PATH, _init_db

    signal_name = str(signal_name or "consensus_longshot_value")
    result = result if isinstance(result, str) else ""
    limit = int(limit) if isinstance(limit, int) else 300
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        if signal_name == "consensus_longshot_value":
            rows = conn.execute(
                """
                select id, match_id, match_name, league_name, country_name, pick_type,
                       selection, confidence, reason, result, final_home, final_away,
                       graded_at, created_at, context_json, signals_json
                from (
                    select
                        pch.*,
                        row_number() over (
                            partition by match_id, pick_type, selection, coalesce(role, 'candidate')
                            order by
                                case when graded_at is not null then 0 else 1 end,
                                datetime(coalesce(graded_at, created_at)) desc,
                                id desc
                        ) as rn
                    from prediction_candidate_history pch
                    where pick_type = 'consensus_longshot_value'
                      and (? = '' or result = ?)
                )
                where rn = 1
                order by created_at desc
                limit ?
                """,
                (result, result, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                select id, match_id, match_name, league_name, country_name, pick_type,
                       selection, confidence, reason, result, final_home, final_away,
                       graded_at, created_at, '{}' as context_json, signals_json
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
                    where signals_json like ?
                      and (? = '' or result = ?)
                )
                where rn = 1
                order by created_at desc
                limit ?
                """,
                (f'%"{signal_name}"%', result, result, limit),
            ).fetchall()

    items = []
    for row in rows:
        try:
            context = json.loads(row["context_json"] or "{}")
        except Exception:
            context = {}
        signal_value = context.get("signal") if isinstance(context.get("signal"), dict) else {}
        market_intent = context.get("market_intent") if isinstance(context.get("market_intent"), dict) else signal_value.get("market_intent") if isinstance(signal_value, dict) else {}
        items.append({
            "id": row["id"],
            "match_id": row["match_id"],
            "match_name": row["match_name"],
            "league_name": row["league_name"],
            "country_name": row["country_name"],
            "pick_type": row["pick_type"],
            "selection": row["selection"],
            "confidence": row["confidence"],
            "reason": row["reason"],
            "result": row["result"],
            "final_home": row["final_home"],
            "final_away": row["final_away"],
            "graded": bool(row["graded_at"]),
            "created_at": row["created_at"],
            "signal": signal_value,
            "market_intent": market_intent or {},
            "market_family": (market_intent or {}).get("family") or signal_value.get("market_family"),
            "market": (market_intent or {}).get("market") or signal_value.get("market"),
        })
    graded = [item for item in items if item["result"] in {"win", "loss"}]
    wins = sum(1 for item in graded if item["result"] == "win")
    losses = sum(1 for item in graded if item["result"] == "loss")
    return {
        "status": "success",
        "signal_name": signal_name,
        "label": "Consensus longshot value" if signal_name == "consensus_longshot_value" else signal_name.replace("_", " ").title(),
        "count": len(items),
        "graded": len(graded),
        "wins": wins,
        "losses": losses,
        "accuracy": round(wins / len(graded) * 100, 1) if graded else None,
        "items": items,
    }


@router.get("/analytics/model-explorer")
def get_model_explorer(
    preset: str = Query(default="all"),
    model: str = Query(default="all"),
    pick_type: str = Query(default=""),
    selection_key: str = Query(default=""),
    min_samples: int = Query(default=1, ge=1, le=100),
    limit: int = Query(default=2000, ge=1, le=5000),
):
    """Prediction history grouped by pick/model so the UI can rank by accuracy."""
    import json
    import sqlite3
    from app.market_intent import classify_market_intent
    from app.league_memory import DB_PATH, _init_db

    def _pick_family(pick_type: str, selection: str, market_intent: dict[str, Any] | None = None) -> str:
        intent = market_intent or classify_market_intent(pick_type, selection)
        market = str(intent.get("market") or "")
        direction = str(intent.get("direction") or "")
        text = f"{pick_type} {selection}".lower()
        if str(pick_type or "").lower() == "consensus_longshot_value":
            return "longshot_value"
        if market == "live_match_winner":
            return "live_match_winner"
        if market == "live_team_to_score":
            return "live_team_to_score"
        if market == "btts":
            return "low_scoring" if direction == "no" else "goals"
        if market in {"total_goals", "live_total_goals", "live_next_goal"}:
            return "low_scoring" if direction == "under" else "goals"
        if market == "double_chance":
            return "double_chance"
        if market == "1x2":
            if direction in {"home", "away", "draw"}:
                return direction
        if "live_match_winner" in text or "live winner" in text:
            return "live_match_winner"
        if "live_team_to_score" in text or "next team to score" in text:
            return "live_team_to_score"
        if "under" in text or "btts no" in text or "both teams to score - no" in text:
            return "low_scoring"
        if "over" in text or "both teams to score" in text or "goal" in text:
            return "goals"
        if "draw" in text and ("home" in text or "away" in text):
            return "double_chance"
        if "home" in text:
            return "home"
        if "away" in text:
            return "away"
        if "draw" in text:
            return "draw"
        if "value" in text:
            return "value"
        return pick_type or "other"

    def _norm_text(value: str) -> str:
        return " ".join(
            "".join(ch.lower() if ch.isalnum() else " " for ch in str(value or "")).split()
        )

    def _match_sides(match_name: str) -> tuple[str, str]:
        raw = str(match_name or "")
        if " vs " in raw:
            home, away = raw.split(" vs ", 1)
        elif " v " in raw:
            home, away = raw.split(" v ", 1)
        else:
            return "", ""
        return _norm_text(home), _norm_text(away)

    def _side_from_team_selection(selection: str, match_name: str) -> str:
        text = _norm_text(selection)
        home, away = _match_sides(match_name)
        if home and home in text:
            return "home"
        if away and away in text:
            return "away"
        return ""

    def _normalise_selection(selection: str, match_name: str = "", pick_type: str = "") -> str:
        text = " ".join(str(selection or "").lower().replace("-", " ").split())
        pick_type = str(pick_type or "").lower()
        if pick_type == "live_match_winner" or "live winner" in text:
            side = _side_from_team_selection(selection, match_name)
            suffix = "_lean" if "lean" in text else ""
            if side:
                return f"{side}_live_winner{suffix}"
            if "draw protection" in text:
                return "live_draw_protection"
            return f"live_winner{suffix}"
        if pick_type == "live_team_to_score" or "next team to score" in text:
            side = _side_from_team_selection(selection, match_name)
            if side:
                return f"{side}_next_team_to_score"
            return "next_team_to_score"
        if "or draw protection" in text or "double chance" in text:
            side = _side_from_team_selection(selection, match_name)
            if side == "home":
                return "home_or_draw"
            if side == "away":
                return "away_or_draw"
            return "team_or_draw"
        aliases = {
            "home win": "home",
            "home": "home",
            "1": "home",
            "away win": "away",
            "away": "away",
            "2": "away",
            "draw": "draw",
            "x": "draw",
            "home or draw": "home_or_draw",
            "draw or home": "home_or_draw",
            "1x": "home_or_draw",
            "away or draw": "away_or_draw",
            "draw or away": "away_or_draw",
            "x2": "away_or_draw",
            "home or away": "home_or_away",
            "away or home": "home_or_away",
            "12": "home_or_away",
            "both teams to score": "btts_yes",
            "btts yes": "btts_yes",
            "both teams to score no": "btts_no",
            "both teams to score - no": "btts_no",
            "btts no": "btts_no",
        }
        return aliases.get(text, text)

    def _display_selection(selection: str, match_name: str = "", pick_type: str = "") -> str:
        normal = _normalise_selection(selection, match_name, pick_type)
        labels = {
            "home": "Home Win",
            "away": "Away Win",
            "draw": "Draw",
            "home_or_draw": "Home or Draw",
            "away_or_draw": "Away or Draw",
            "home_or_away": "Home or Away",
            "btts_yes": "Both Teams To Score",
            "btts_no": "Both Teams To Score - No",
            "home_next_team_to_score": "Home Next Team To Score",
            "away_next_team_to_score": "Away Next Team To Score",
            "next_team_to_score": "Next Team To Score",
            "home_live_winner": "Home Live Winner",
            "away_live_winner": "Away Live Winner",
            "home_live_winner_lean": "Home Live Winner Lean",
            "away_live_winner_lean": "Away Live Winner Lean",
            "live_winner": "Live Winner",
            "live_winner_lean": "Live Winner Lean",
            "live_draw_protection": "Live Draw Protection",
            "team_or_draw": "Team or Draw",
        }
        return labels.get(normal, selection or "Pick")

    def _uses_model(signals: list[dict[str, Any]], model_name: str) -> bool:
        if model_name == "all":
            return True
        names = {str(signal.get("name") or "") for signal in signals}
        aliases = {
            "poisson": {"poisson_model"},
            "dixon_coles": {"dixon_coles_model"},
            "elo": {"elo_model"},
            "ensemble": {"ensemble_model"},
            "database": {"finished_database_memory", "prediction_memory"},
            "odds": {"odds_edge", "odds_progression", "odds_pattern"},
            "rules": {"goal_pressure", "h2h_edge", "league_position_edge", "recent_history_edge", "common_opponent_edge"},
            "longshot": {"consensus_longshot_value"},
        }
        return bool(names & aliases.get(model_name, {model_name}))

    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select *
            from (
                select id, match_id, match_name, league_name, country_name, pick_type,
                       selection, confidence, reason, result, graded_at, created_at,
                       signals_json, '{}' as context_json, 'primary' as role, 'prediction_history' as source_table
                from prediction_history
                where pick_type != 'no_bet'
                union all
                select id, match_id, match_name, league_name, country_name, pick_type,
                       selection, confidence, reason, result, graded_at, created_at,
                       signals_json, context_json, coalesce(role, 'candidate') as role, 'candidate_history' as source_table
                from prediction_candidate_history
                where pick_type != 'no_bet'
            )
            order by created_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()

    filtered_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    raw_count = 0
    for row in rows:
        signals = json.loads(row["signals_json"] or "[]")
        try:
            context = json.loads(row["context_json"] or "{}")
        except Exception:
            context = {}
        market_intent = context.get("market_intent") if isinstance(context.get("market_intent"), dict) else classify_market_intent(row["pick_type"] or "", row["selection"] or "")
        raw_count += 1
        normal_selection = _normalise_selection(row["selection"] or "", row["match_name"] or "", row["pick_type"] or "")
        display_selection = _display_selection(row["selection"] or "", row["match_name"] or "", row["pick_type"] or "")
        family = _pick_family(row["pick_type"] or "", display_selection, market_intent)
        if preset != "all" and family != preset:
            continue
        if pick_type and str(row["pick_type"] or "") != pick_type:
            continue
        if selection_key and normal_selection != selection_key:
            continue
        if not _uses_model(signals, model):
            continue
        item = {
            "id": row["id"],
            "match_id": row["match_id"],
            "match_name": row["match_name"],
            "league_name": row["league_name"],
            "country_name": row["country_name"],
            "pick_type": row["pick_type"],
            "selection": display_selection,
            "raw_selection": row["selection"],
            "selection_key": normal_selection,
            "confidence": row["confidence"],
            "reason": row["reason"],
            "result": row["result"],
            "graded": bool(row["graded_at"]),
            "role": row["role"] or "candidate",
            "source_table": row["source_table"],
            "created_at": row["created_at"],
            "family": family,
            "market_intent": market_intent,
            "market_family": market_intent.get("family"),
            "market": market_intent.get("market"),
            "models_used": sorted({str(s.get("name") or "") for s in signals if s.get("name")}),
        }
        dedupe_key = (
            str(row["match_id"] or ""),
            str(row["pick_type"] or ""),
            normal_selection,
            str(row["role"] or "candidate"),
        )
        existing = filtered_by_key.get(dedupe_key)
        if not existing:
            filtered_by_key[dedupe_key] = item
        elif item["graded"] and not existing["graded"]:
            filtered_by_key[dedupe_key] = item

    filtered = sorted(filtered_by_key.values(), key=lambda item: item.get("created_at") or "", reverse=True)

    groups: dict[str, dict[str, Any]] = {}
    for item in filtered:
        key = f"{item['family']}::{item['pick_type']}::{item['selection_key']}"
        group = groups.setdefault(key, {
            "family": item["family"],
            "pick_type": item["pick_type"],
            "selection": item["selection"],
            "selection_key": item["selection_key"],
            "total": 0,
            "graded": 0,
            "wins": 0,
            "losses": 0,
            "pending": 0,
            "avg_confidence": 0.0,
            "models_used": set(),
            "roles": {},
            "recent": [],
            "previous": [],
            "upcoming": [],
        })
        group["total"] += 1
        group["avg_confidence"] += float(item["confidence"] or 0)
        group["models_used"].update(item["models_used"])
        if item["graded"]:
            group["graded"] += 1
            if item["result"] == "win":
                group["wins"] += 1
            elif item["result"] == "loss":
                group["losses"] += 1
            if len(group["previous"]) < 20:
                group["previous"].append(item)
        else:
            group["pending"] += 1
            if len(group["upcoming"]) < 20:
                group["upcoming"].append(item)
        role = item.get("role") or "candidate"
        role_bucket = group["roles"].setdefault(role, {"total": 0, "graded": 0, "wins": 0, "losses": 0, "pending": 0})
        role_bucket["total"] += 1
        if item["graded"]:
            role_bucket["graded"] += 1
            if item["result"] == "win":
                role_bucket["wins"] += 1
            elif item["result"] == "loss":
                role_bucket["losses"] += 1
        else:
            role_bucket["pending"] += 1
        if len(group["recent"]) < 8:
            group["recent"].append(item)

    summary = []
    for group in groups.values():
        group["sample_ready"] = group["graded"] >= min_samples
        group["accuracy"] = round(group["wins"] / group["graded"] * 100, 1) if group["graded"] else None
        group["avg_confidence"] = round(group["avg_confidence"] / group["total"], 1) if group["total"] else 0
        group["models_used"] = sorted(group["models_used"])
        for role_stats in group["roles"].values():
            role_stats["accuracy"] = round(role_stats["wins"] / role_stats["graded"] * 100, 1) if role_stats["graded"] else None
        primary_acc = (group["roles"].get("primary") or {}).get("accuracy")
        secondary_acc = (group["roles"].get("secondary") or group["roles"].get("alternative") or {}).get("accuracy")
        group["role_signal"] = {
            "primary_accuracy": primary_acc,
            "secondary_accuracy": secondary_acc,
            "primary_edge": round(primary_acc - secondary_acc, 1) if primary_acc is not None and secondary_acc is not None else None,
            "guidance": (
                "promote_primary" if primary_acc is not None and secondary_acc is not None and primary_acc - secondary_acc >= 8
                else "secondary_caution" if secondary_acc is not None and secondary_acc < 48
                else "neutral"
            ),
        }
        summary.append(group)

    summary.sort(key=lambda g: (g["sample_ready"], g["accuracy"] or -1, g["graded"], g["avg_confidence"]), reverse=True)
    ready = [group for group in summary if group["sample_ready"]]
    best = ready[0] if ready else (summary[0] if summary else None)
    return {
        "status": "success",
        "preset": preset,
        "model": model,
        "pick_type": pick_type,
        "selection_key": selection_key,
        "min_samples": min_samples,
        "count": len(filtered),
        "summary": {
            "raw_rows": raw_count,
            "unique_picks": len(filtered),
            "duplicates_removed": max(0, raw_count - len(filtered)),
            "groups": len(summary),
            "sample_ready": len(ready),
            "best_accuracy": best.get("accuracy") if best else None,
            "best_selection": best.get("selection") if best else None,
            "graded": sum(group["graded"] for group in summary),
            "pending": sum(group["pending"] for group in summary),
        },
        "groups": summary,
        "recent": filtered[:50],
        "previous": [item for item in filtered if item.get("graded")][:80],
        "upcoming": [item for item in filtered if not item.get("graded")][:80],
        "presets": ["all", "low_scoring", "goals", "home", "away", "draw", "double_chance", "value", "longshot_value", "live_team_to_score", "live_match_winner"],
        "models": ["all", "poisson", "dixon_coles", "elo", "ensemble", "database", "odds", "rules", "longshot"],
    }


@router.post("/matches/{sportybet_id}/enrich")
def enrich_match_endpoint(sportybet_id: str):
    """Force-enrich a single match using saved manual SofaScore match when present."""
    try:
        from app.match_enrichment import MatchEnrichmentError, enrich_buffered_match

        sportybet_id = _resolve_buffer_match_id(sportybet_id)
        return enrich_buffered_match(sportybet_id)
    except MatchEnrichmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/matches/{sportybet_id}/predict")
def predict_single_match(
    sportybet_id: str,
    force_enrich: bool = False,
    allow_repeat: bool = False,
    dry_run: bool = False,
):
    """Plan, enrich only when needed, then predict with bounded agentic execution."""
    from app.agentic_prediction import AgentExecutionError, run_agentic_match_prediction

    try:
        sportybet_id = _resolve_buffer_match_id(sportybet_id)
        result = run_agentic_match_prediction(
            sportybet_id,
            force_enrich=force_enrich,
            allow_repeat=allow_repeat,
            dry_run=dry_run,
        )
    except AgentExecutionError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "agent": exc.trace})

    state = result.get("prediction_state") or {}
    if result.get("status") == "deferred":
        raise HTTPException(
            status_code=409,
            detail={
                "message": state.get("message") or "Prediction deferred until full signal is ready",
                "readiness": result.get("readiness"),
                "agent": result.get("agent"),
            },
        )
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail={"message": state.get("message") or "Prediction failed", "agent": result.get("agent")})
    if result.get("status") == "skipped":
        return {
            "status": "success",
            "sportybet_id": sportybet_id,
            "sporty_refresh": result.get("sporty_refresh"),
            "prediction": result.get("prediction") or _latest_prediction(sportybet_id),
            "skipped": True,
            "skip_reason": state.get("skip_reason"),
            "agent": result.get("agent"),
        }

    return {
        "status": "success",
        "sportybet_id": sportybet_id,
        "sporty_refresh": result.get("sporty_refresh"),
        "prediction": result.get("prediction"),
        "agent": result.get("agent"),
    }


def _match_detail(doc: dict[str, Any]) -> dict[str, Any]:
    from app.enriched_prediction import prediction_readiness
    from app.match_intelligence import build_match_intelligence

    detail = doc.get("sofascore_detail") or {}
    managers = detail.get("managers") or {}
    form = detail.get("pregameForm") or detail.get("pregame_form") or {}
    home_form = form.get("homeTeam") or form.get("home_team") or {}
    away_form = form.get("awayTeam") or form.get("away_team") or {}
    web = doc.get("web_context") or {}
    sportradar = doc.get("sportradar_detail") or {}
    sportybet_id = str(doc.get("sportybet_id") or doc.get("id") or "")
    readiness = prediction_readiness(doc)
    match_state = classify_match_state(doc)
    current_prediction = _current_prediction_for_detail(sportybet_id, readiness, doc)
    intelligence_doc = {**doc, "prediction": current_prediction, "prediction_readiness": readiness}

    return {
        "sportybet_id": sportybet_id,
        "sofascore_id": doc.get("sofascore_id"),
        "name": doc.get("sportybet_name") or doc.get("name"),
        "home_team": _home_team(doc),
        "away_team": _away_team(doc),
        "tournament": doc.get("tournament"),
        "category": doc.get("category"),
        "match_date": doc.get("match_date") or (doc.get("time_context") or {}).get("local_date"),
        "start_time": doc.get("start_time"),
        "period": doc.get("period"),
        "played_seconds": doc.get("played_seconds"),
        "is_live": bool(match_state.get("is_live")),
        "is_finished": bool(match_state.get("is_finished") or _is_finished_doc(doc)),
        "match_state": match_state,
        "score": doc.get("score"),
        "venue": doc.get("venue"),
        "enriched_at": doc.get("enriched_at"),
        "data_sources": doc.get("data_sources") or {},
        "prediction_readiness": readiness,
        "source_summary": {
            "sportybet": {
                "available": bool(doc.get("sportybet_detail") or doc.get("raw_sporty")),
                "markets": len(doc.get("sportybet_markets") or doc.get("markets") or []),
                "active": doc.get("sporty_active", True),
            },
            "sofascore": {
                "matched": bool(doc.get("sofascore_id") or doc.get("sofascore_event")),
                "detail": bool(detail),
                "status": doc.get("sofascore_match_status"),
            },
            "sportradar": {
                "available": bool(sportradar.get("available")),
                "detail": bool(sportradar.get("match")),
                "standings": bool(sportradar.get("standings")),
                "error": sportradar.get("error") or sportradar.get("standings_error"),
            },
            "ready_for_prediction": bool(readiness.get("ready")),
            "missing": readiness.get("missing") or [],
        },
        "sportybet_detail": doc.get("sportybet_detail") or {},
        "sportybet_data_status": doc.get("sportybet_data_status"),
        "has_sportybet_detail": bool(doc.get("sportybet_detail") or doc.get("raw_sporty")),
        "has_sofascore": bool(detail),
        "has_sportradar": bool(sportradar.get("available")),
        "sportradar_detail": sportradar,
        "odds_1x2": _extract_1x2(doc.get("sportybet_markets") or doc.get("markets") or []),
        "all_markets": doc.get("sportybet_markets") or doc.get("markets") or [],
        "home_manager": _manager_name(managers, "home"),
        "away_manager": _manager_name(managers, "away"),
        "home_form": home_form.get("form"),
        "away_form": away_form.get("form"),
        "home_position": home_form.get("position"),
        "away_position": away_form.get("position"),
        "home_avg_rating": home_form.get("avgRating") or home_form.get("avg_rating"),
        "away_avg_rating": away_form.get("avgRating") or away_form.get("avg_rating"),
        "h2h": detail.get("h2h"),
        "statistics": detail.get("statistics") or [],
        "standings": detail.get("standings"),
        "home_last_matches": detail.get("home_last_matches") or [],
        "away_last_matches": detail.get("away_last_matches") or [],
        "lineups": detail.get("lineups"),
        "home_players": detail.get("homeFeaturedPlayers") or detail.get("home_featured_players"),
        "away_players": detail.get("awayFeaturedPlayers") or detail.get("away_featured_players"),
        "incidents": detail.get("incidents"),
        "web_context": {
            "query": web.get("query"),
            "snippets": web.get("snippets", []),
            "articles": web.get("scraped", []) or web.get("articles", []),
            "error": web.get("error"),
            "disabled": web.get("disabled"),
            "diagnostics": web.get("diagnostics"),
        },
        "time_context": doc.get("time_context"),
        "lifecycle": doc.get("lifecycle"),
        "manual_match": bool(doc.get("manual_match")),
        "raw_sporty": doc.get("raw_sporty"),
        "raw_sofascore_event": doc.get("raw_sofascore_event"),
        "odds_movement": get_movement(sportybet_id) if sportybet_id else {"snapshots": 0, "movement": None},
        "prediction": current_prediction,
        "intelligence": build_match_intelligence(intelligence_doc),
        "prediction_error": doc.get("prediction_error") or (
            "Prediction deferred until full signal is ready"
            + (f": {', '.join(readiness.get('missing') or [])}" if readiness.get("missing") else "")
            if not readiness.get("ready")
            else None
        ),
        "stale_prediction": _latest_prediction(sportybet_id) if not readiness.get("ready") else None,
        "raw": doc,
    }


def _archived_match_detail(match_id: str) -> dict[str, Any] | None:
    """Read a finished match that has already left the hot SQL buffer."""
    archived: dict[str, Any] | None = None
    try:
        from app.mongo_store import get_finished_match
        archived = get_finished_match(match_id)
    except Exception:
        archived = None
    if not archived:
        archived = _local_finished_match(match_id)
    if not archived:
        return _history_match_detail(match_id)
    if not archived:
        return None

    detail = archived.get("sofascore_detail") or {}
    score = archived.get("score") or {}
    doc = {
        "sportybet_id": archived.get("sportybet_id") or archived.get("_id") or match_id,
        "id": archived.get("_id") or match_id,
        "sportybet_name": archived.get("name") or archived.get("match_name"),
        "home_team": archived.get("home_team"),
        "away_team": archived.get("away_team"),
        "tournament": archived.get("tournament"),
        "category": archived.get("country") or archived.get("category"),
        "match_date": archived.get("match_date"),
        "period": "Finished",
        "is_finished": True,
        "score": score,
        "sofascore_id": archived.get("sofascore_id") or detail.get("id"),
        "sofascore_detail": detail,
        "sportybet_detail": archived.get("sportybet_detail") or {},
        "sportybet_data_status": archived.get("sportybet_data_status"),
        "data_sources": archived.get("data_sources") or {},
        "sportybet_markets": archived.get("sportybet_markets") or [],
        "web_context": archived.get("web_context") or {},
        "time_context": archived.get("time_context"),
        "lifecycle": {"state": "archived", "stages": {"finished": True, "archived": True}, "missing": []},
        "raw_sporty": archived.get("raw_sporty"),
        "raw_sofascore_event": archived.get("raw_sofascore_event"),
        "archived_at": archived.get("finished_at") or archived.get("archived_at"),
        "raw": archived,
    }
    out = _match_detail(doc)
    out["archive_source"] = archived.get("archive_source") or "finished_store"
    out["is_finished"] = True
    out["period"] = "Finished"
    return out


def _local_finished_match(match_id: str) -> dict[str, Any] | None:
    try:
        import json
        import sqlite3
        from app.league_memory import DB_PATH, _init_db

        _init_db()
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select coalesce(raw_doc, raw_json) as raw_doc from finished_matches where match_id = ?",
                (str(match_id),),
            ).fetchone()
        if not row or not row["raw_doc"]:
            return None
        doc = json.loads(row["raw_doc"])
        doc["_id"] = str(match_id)
        doc["archive_source"] = "local_sqlite"
        return doc
    except Exception:
        return None


def _history_match_detail(match_id: str) -> dict[str, Any] | None:
    """Fallback for a match that has left buffers but still has prediction history."""
    try:
        import json
        import sqlite3
        from app.league_memory import DB_PATH, _init_db

        _init_db()
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select match_id, sportybet_id, sofascore_id, match_name, league_name,
                       country_name, pick_type, selection, confidence, reason,
                       signals_json, picks_json, result, final_home, final_away, created_at
                from prediction_history
                where match_id = ? or sportybet_id = ? or sofascore_id = ?
                order by created_at desc
                limit 1
                """,
                (str(match_id), str(match_id), str(match_id)),
            ).fetchone()
            if not row:
                row = conn.execute(
                    """
                    select match_id, sportybet_id, sofascore_id, match_name, league_name,
                           country_name, pick_type, selection, confidence, reason,
                           signals_json, '[]' as picks_json, result, final_home, final_away, created_at
                    from prediction_candidate_history
                    where match_id = ? or sportybet_id = ? or sofascore_id = ?
                    order by created_at desc
                    limit 1
                    """,
                    (str(match_id), str(match_id), str(match_id)),
                ).fetchone()
        if not row:
            return None
        match_name = row["match_name"] or str(match_id)
        teams = [part.strip() for part in match_name.replace(" v ", " vs ").split(" vs ", 1)]
        home = teams[0] if teams else None
        away = teams[1] if len(teams) > 1 else None
        score = None
        if row["final_home"] is not None and row["final_away"] is not None:
            score = {"home": row["final_home"], "away": row["final_away"]}
        prediction = {
            "match_id": row["match_id"],
            "pick_type": row["pick_type"],
            "selection": row["selection"],
            "confidence": row["confidence"],
            "reason": row["reason"],
            "signals": json.loads(row["signals_json"] or "[]"),
            "picks": json.loads(row["picks_json"] or "[]"),
            "result": row["result"],
            "created_at": row["created_at"],
        }
        return {
            "sportybet_id": row["sportybet_id"] or row["match_id"] or str(match_id),
            "sofascore_id": row["sofascore_id"],
            "id": row["match_id"] or str(match_id),
            "name": match_name,
            "sportybet_name": match_name,
            "home_team": home,
            "away_team": away,
            "tournament": row["league_name"],
            "category": row["country_name"],
            "period": "Archived history",
            "is_finished": bool(score),
            "score": score,
            "prediction": prediction,
            "lifecycle": {"state": "history", "stages": {"predicted": True}, "missing": ["buffer", "full_archive"]},
            "archive_source": "prediction_history",
            "raw": {"prediction_history": prediction},
        }
    except Exception:
        return None


def _find_sofascore_event(
    sofa_id: str,
    match_date: str,
    live: bool,
    fetch_live_events,
    fetch_all_scheduled_events,
) -> dict[str, Any] | None:
    from app.sofascore_client import is_usable_event_for_mode

    if live:
        try:
            match = next((event for event in fetch_live_events() if str(event.get("id")) == sofa_id), None)
            if match and is_usable_event_for_mode(match, live=True):
                return match
        except Exception:
            pass

    dates = []
    for value in (match_date, dt.today().isoformat()):
        if value and value not in dates:
            dates.append(value)
    for target_date in dates:
        try:
            events = fetch_all_scheduled_events(target_date)
        except Exception:
            continue
        match = next((event for event in events if str(event.get("id")) == sofa_id), None)
        if match and is_usable_event_for_mode(match, live=False):
            return match
    return None


def _candidate_summary(event: dict[str, Any], doc: dict[str, Any], match_date: str) -> dict[str, Any]:
    status = event.get("status") or {}
    score = event.get("score") or {}
    return {
        "id": event.get("id"),
        "name": event.get("name"),
        "home_team": (event.get("home_team") or {}).get("name"),
        "away_team": (event.get("away_team") or {}).get("name"),
        "tournament": (event.get("tournament") or {}).get("name"),
        "status": status.get("description") or status.get("type"),
        "status_type": status.get("type"),
        "scoreline": _scoreline(score),
        "start_timestamp": event.get("start_timestamp"),
        "match_date": match_date,
        "score": _candidate_score(event, doc),
        "event": event,
    }


def _learned_best_pick(picks: list[dict[str, Any]]) -> dict[str, Any]:
    from app.pick_roles import learned_best_pick

    return learned_best_pick(picks)


def _load_role_memory_rows() -> dict[tuple[str, str], list[dict[str, Any]]]:
    from app.pick_roles import load_role_memory_rows

    return load_role_memory_rows()


def _backfill_role_learning(
    prediction: dict[str, Any],
    picks: list[dict[str, Any]],
    match_id: str,
    role_rows: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
) -> None:
    from app.pick_roles import backfill_role_learning

    backfill_role_learning(prediction, picks, match_id, role_rows)


def _fast_role_memory(
    league: str,
    country: str,
    pick_type: str,
    selection: str,
    role_rows: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    from app.pick_roles import fast_role_memory

    return fast_role_memory(league, country, pick_type, selection, role_rows)


def _attach_fast_learned_decision(picks: list[dict[str, Any]]) -> None:
    from app.pick_roles import attach_fast_learned_decision

    attach_fast_learned_decision(picks)


def _candidate_score(event: dict[str, Any], doc: dict[str, Any]) -> float:
    try:
        from app.enrichment import _event_score
        sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
        return _event_score(
            {
                **sporty,
                "name": sporty.get("name") or doc.get("sportybet_name") or doc.get("name"),
                "home_team": sporty.get("home_team") or _home_team(doc),
                "away_team": sporty.get("away_team") or _away_team(doc),
                "tournament": sporty.get("tournament") or doc.get("tournament"),
                "category": sporty.get("category") or doc.get("category"),
                "start_time": sporty.get("start_time") or doc.get("start_time"),
            },
            event,
        )
    except Exception:
        sofa_name = event.get("name") or ""
        sporty_name = doc.get("sportybet_name") or doc.get("name") or ""
        direct = SequenceMatcher(None, sofa_name.lower(), sporty_name.lower()).ratio()
        home = SequenceMatcher(None, ((event.get("home_team") or {}).get("name") or "").lower(), _home_team(doc).lower()).ratio()
        away = SequenceMatcher(None, ((event.get("away_team") or {}).get("name") or "").lower(), _away_team(doc).lower()).ratio()
        return round(max(direct, (home + away) / 2), 3)


def _scoreline(score: dict[str, Any]) -> str | None:
    home = score.get("home")
    away = score.get("away")
    if home is None or away is None:
        return None
    return f"{home}-{away}"


def _is_live_doc(doc: dict[str, Any]) -> bool:
    return bool(classify_match_state(doc).get("is_live"))


def _is_finished_doc(doc: dict[str, Any]) -> bool:
    state = classify_match_state(doc)
    return bool(doc.get("is_finished") or state.get("is_finished") or state.get("state") in {"postponed", "cancelled"})


def _sort_start(value: Any) -> int:
    try:
        number = int(value)
        return number if number > 1e10 else number * 1000
    except (TypeError, ValueError):
        return 0


def _match_summary(doc: dict[str, Any]) -> dict[str, Any]:
    from app.match_view import match_summary

    return match_summary(doc)


def _extract_1x2(markets: list[dict[str, Any]]) -> dict[str, Any]:
    from app.match_view import extract_1x2

    return extract_1x2(markets)


def _home_team(doc: dict[str, Any]) -> str:
    from app.match_view import home_team

    return home_team(doc)


def _away_team(doc: dict[str, Any]) -> str:
    from app.match_view import away_team

    return away_team(doc)


def _team_from_name(doc: dict[str, Any], index: int) -> str:
    from app.match_view import team_from_name

    return team_from_name(doc, index)


def _manager_name(managers: dict[str, Any], side: str) -> str | None:
    key = "homeTeam" if side == "home" else "awayTeam"
    alt_key = "home_manager" if side == "home" else "away_manager"
    manager = managers.get(key) or managers.get(alt_key) or {}
    return manager.get("name") if isinstance(manager, dict) else None


def _current_prediction_for_detail(match_id: str, readiness: dict[str, Any], doc: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not readiness.get("ready"):
        return None
    current_state = classify_match_state(doc or {})
    current = (doc or {}).get("prediction")
    if isinstance(current, dict) and current.get("picks"):
        if _prediction_mode_conflicts(current, current_state):
            return None
        return current
    latest = _latest_prediction(match_id)
    if latest and _prediction_mode_conflicts(latest, current_state):
        return None
    return latest


def _prediction_mode_conflicts(prediction: dict[str, Any], match_state: dict[str, Any]) -> bool:
    if match_state.get("is_live"):
        return False
    picks = prediction.get("picks") or []
    if prediction.get("live_inplay"):
        return True
    return any(str(pick.get("type") or "").startswith("live_") for pick in picks if isinstance(pick, dict))


def _latest_prediction(match_id: str) -> dict[str, Any] | None:
    if not match_id:
        return None
    history = list_prediction_history(limit=1, match_id=match_id).get("predictions") or []
    return history[0] if history else None


def _grade_signal_stats(graded: int) -> dict[str, Any]:
    """Quick signal win rate summary from local device memory after grading."""
    if graded == 0:
        return {}
    try:
        from app.league_memory import get_local_signal_stats
        stats = get_local_signal_stats(min_samples=3)
        top = stats.get("signals", [])[:5]
        return {"top_signals": top, "scope": "device"}
    except Exception:
        return {}


# ── Brain / Self-Learning Analytics ──────────────────────────────────────────

@router.get("/analytics/brain/summary")
def get_brain_summary():
    """
    Full summary of what the system has learned from its own prediction history.
    Shows signal win rates, league accuracy, auto-tuned model weights, and CLV trend.
    This is the 'consciousness' view — what the system knows about itself.
    """
    from app.self_learner import get_learning_summary
    summary = get_learning_summary()
    return {"status": "success", **summary}


@router.post("/analytics/brain/learn")
def trigger_learning_cycle():
    """
    Manually trigger a self-learning cycle.
    Normally runs automatically after every grading cycle (every 6 hours).
    Use this to force an immediate update after manual grading.
    """
    from app.self_learner import run_learning_cycle
    result = run_learning_cycle()
    return {"status": "success", **result}


def _resolve_buffer_match_id(match_id: str) -> str:
    if not match_id or ":" in match_id:
        return match_id
    prefixed = f"competition:world-cup-2026:{match_id}"
    if get_buffered_match(prefixed):
        return prefixed
    return match_id


@router.get("/analytics/brain/signals")
def get_brain_signal_weights(league: str = Query(default="")):
    """
    Return learned signal weights — which signals are hot/cold.
    Optionally filter by league to get league-specific weights.
    """
    from app.self_learner import get_top_signals, get_signal_weights
    top = get_top_signals(limit=30)
    league_weights = get_signal_weights(league) if league else {}
    return {
        "status": "success",
        "league": league or "global",
        "top_signals": top,
        "league_weights": league_weights,
    }


@router.get("/analytics/brain/league/{league_name}")
def get_brain_league_profile(league_name: str):
    """
    Return the system's learned accuracy profile for a specific league.
    Shows win rate per pick type, calibration gap, and whether the system
    is over/under-confident in this league.
    """
    from app.self_learner import get_league_accuracy
    from urllib.parse import unquote
    result = get_league_accuracy(unquote(league_name))
    return {"status": "success", **result}


@router.get("/analytics/brain/model-weights")
def get_brain_model_weights():
    """
    Return the current auto-tuned ensemble model weights.
    Shows how much each model (Poisson, Dixon-Coles, ELO, Rules, Groq)
    has been adjusted based on historical accuracy.
    """
    from app.self_learner import get_learned_weights
    weights = get_learned_weights()
    return {
        "status": "success",
        "weights": weights,
        "note": "These weights are auto-tuned from graded prediction history. "
                "Default weights are used until enough data is available.",
    }


@router.get("/analytics/brain/grades/{team_id}")
def get_team_sofascore_grades(team_id: str, last_n: int = Query(default=5, ge=1, le=20)):
    """
    Return SofaScore rating trend for a team.
    Shows whether the team is improving or declining based on player ratings.
    """
    from app.sofascore_grades import get_team_rating_trend
    result = get_team_rating_trend(team_id, last_n=last_n)
    return {"status": "success", **result}


@router.get("/analytics/brain/health")
def get_brain_health():
    """
    Full brain health check — shows all learning systems and their status.
    Use this to verify the self-learning pipeline is working end-to-end.
    """
    from app.self_learner import get_learning_summary, get_learned_weights
    from app.clv import get_clv_summary
    from app.confidence_calibrator import get_calibration_table

    health: dict[str, Any] = {"status": "success"}

    try:
        summary = get_learning_summary()
        health["self_learner"] = {
            "status": "ok",
            "signals_learned": summary.get("signals_learned", 0),
            "leagues_profiled": summary.get("leagues_profiled", 0),
            "model_weights_tuned": len(summary.get("model_weights", [])),
        }
    except Exception as exc:
        health["self_learner"] = {"status": "error", "detail": str(exc)}

    try:
        weights = get_learned_weights()
        defaults = {"dixon_coles": 0.30, "elo": 0.25, "poisson": 0.15, "rules": 0.20, "groq": 0.10}
        health["ensemble_weights"] = {
            "status": "ok",
            "weights": weights,
            "source": "learned" if any(abs(float(weights.get(k, 0)) - v) > 0.001 for k, v in defaults.items()) else "default",
        }
    except Exception as exc:
        health["ensemble_weights"] = {"status": "error", "detail": str(exc)}

    try:
        clv = get_clv_summary(days=14)
        health["clv"] = {
            "status": "ok",
            "avg_clv_14d": clv.get("avg_clv_percent"),
            "edge_quality": clv.get("edge_quality"),
            "total_entries": clv.get("total_entries"),
        }
    except Exception as exc:
        health["clv"] = {"status": "error", "detail": str(exc)}

    try:
        cal = get_calibration_table()
        health["calibration"] = {
            "status": "ok",
            "bands": len(cal) if isinstance(cal, list) else 0,
        }
    except Exception as exc:
        health["calibration"] = {"status": "error", "detail": str(exc)}

    # Overall brain score
    ok_count = sum(1 for v in health.values() if isinstance(v, dict) and v.get("status") == "ok")
    total = sum(1 for v in health.values() if isinstance(v, dict) and "status" in v)
    health["brain_score"] = f"{ok_count}/{total} systems healthy"

    return health
