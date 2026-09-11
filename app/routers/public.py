"""
Public, SEO-facing read API.

Deliberately separate from app.routers.frontend (which is the internal ops
dashboard's API — admin actions, raw provider payloads, proprietary signal
internals). Everything here is:

  - read-only (no mutation, no admin actions)
  - curated (only the fields a public match/league/team page needs — no
    raw_sporty, no per-signal breakdowns, no analyst notes; see the
    "protect the pipeline, share the analysis" strategy in the project's
    own notes)
  - cached briefly (TTLCache) so a Google crawler or a traffic spike can't
    hammer SQLite directly on every request

Consumed by the separate public site (Next.js), not by the internal
dashboard.
"""
from __future__ import annotations

import sqlite3
from datetime import date as dt
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.config.config import get_settings
from app.storage.buffer import get_buffered_matches, get_buffered_match
from app.storage.db import db_conn
from app.storage.league_memory import _init_db
from app.utils.match_state import classify_match_state, is_finished_match
from app.utils.match_view import home_team, away_team
from app.utils.match_helpers import _extract_1x2

import logging

logger = logging.getLogger(__name__)

try:
    from cachetools import TTLCache
except ImportError:  # pragma: no cover
    TTLCache = None  # type: ignore[assignment]

router = APIRouter(prefix="/public", tags=["public"])

_settings = get_settings()
_cache_ttl = max(15, _settings.public_site_cache_ttl_seconds)
_cache: Any = TTLCache(maxsize=500, ttl=_cache_ttl) if TTLCache else {}


def _cached(key: str, builder):
    if key in _cache:
        return _cache[key]
    value = builder()
    _cache[key] = value
    return value


# ---------------------------------------------------------------------------
# Curation helpers — these are the only place doc/profile internals are
# translated into public shape. Nothing upstream of these should be
# returned as-is.
# ---------------------------------------------------------------------------

def _public_prediction(doc: dict[str, Any]) -> dict[str, Any] | None:
    prediction = doc.get("prediction")
    if not isinstance(prediction, dict):
        return None
    picks = prediction.get("picks") or []
    best = picks[0] if picks else prediction
    selection = best.get("selection")
    if not selection or best.get("type") == "no_bet":
        return None
    return {
        "pick": selection,
        "confidence": best.get("confidence"),
        "reason": best.get("reason"),
    }


def _public_match(doc: dict[str, Any]) -> dict[str, Any]:
    state = classify_match_state(doc)
    return {
        "match_id": str(doc.get("sportybet_id") or doc.get("id") or ""),
        "name": doc.get("sportybet_name") or doc.get("name"),
        "home_team": home_team(doc),
        "away_team": away_team(doc),
        "league": doc.get("tournament"),
        "country": doc.get("category"),
        "start_time": doc.get("start_time"),
        "score": doc.get("score"),
        "is_live": bool(state.get("is_live")),
        "is_finished": bool(state.get("is_finished") or is_finished_match(doc)),
        "odds_1x2": _extract_1x2(doc.get("sportybet_markets") or doc.get("markets") or []),
        "prediction": _public_prediction(doc),
    }


def _public_watcher(watcher: dict[str, Any]) -> dict[str, Any]:
    profile = watcher.get("profile") or {}
    record = profile.get("record") or {}
    goals = profile.get("goals") or {}
    return {
        "team_key": watcher.get("team_key"),
        "team_name": watcher.get("team_name"),
        "league": watcher.get("league_name"),
        "position": watcher.get("position"),
        "form": record.get("form"),
        "record": {
            "wins": record.get("wins"),
            "draws": record.get("draws"),
            "losses": record.get("losses"),
        },
        "over_2_5_rate": goals.get("over_2_5_rate"),
        "btts_rate": goals.get("btts_rate"),
        "clean_sheet_rate": goals.get("clean_sheet_rate"),
        "matches_tracked": watcher.get("match_count"),
    }


# ---------------------------------------------------------------------------
# Matches
# ---------------------------------------------------------------------------

@router.get("/matches/today")
def public_matches_today():
    def build():
        target_date = dt.today().isoformat()
        docs = get_buffered_matches(target_date)
        matches = [_public_match(doc) for doc in docs]
        return {"status": "success", "date": target_date, "count": len(matches), "matches": matches}

    return _cached("matches_today", build)


@router.get("/matches/{match_id}")
def public_match_detail(match_id: str):
    doc = get_buffered_match(match_id)
    if not doc:
        try:
            from app.storage.mongo_store import get_finished_match

            doc = get_finished_match(match_id)
        except Exception as exc:
            logger.debug("public match lookup: mongo fallback failed for %s: %s", match_id, exc)
            doc = None
    if not doc:
        raise HTTPException(status_code=404, detail=f"Match {match_id} not found")
    return {"status": "success", "match": _public_match(doc)}


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------

@router.get("/predictions/today")
def public_predictions_today(limit: int = Query(default=100, ge=1, le=300)):
    def build():
        from app.utils.current_predictions import list_recent_dashboard_predictions

        rows = list_recent_dashboard_predictions(hours=36, limit=800)
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for row in rows:
            match_id = str(row.get("match_id") or "")
            if not match_id or match_id in seen:
                continue
            best = row.get("best_pick") or {}
            selection = best.get("selection")
            if not selection or best.get("type") == "no_bet":
                continue
            seen.add(match_id)
            out.append({
                "match_id": match_id,
                "match": row.get("match_name"),
                "league": row.get("league_name"),
                "country": row.get("country_name"),
                "pick": selection,
                "confidence": best.get("confidence"),
                "reason": best.get("reason"),
            })
            if len(out) >= limit:
                break
        out.sort(key=lambda item: item.get("confidence") or 0, reverse=True)
        return {"status": "success", "count": len(out), "predictions": out}

    return _cached(f"predictions_today_{limit}", build)


# ---------------------------------------------------------------------------
# Leagues
# ---------------------------------------------------------------------------

@router.get("/leagues")
def public_leagues():
    def build():
        from app.competition.competition_registry import list_competitions

        _init_db()
        with db_conn(timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            rows = list_competitions(conn, enabled=True)
        leagues = [
            {"key": row.get("key"), "name": row.get("name"), "country": row.get("country"), "tier": row.get("tier")}
            for row in rows
        ]
        return {"status": "success", "count": len(leagues), "leagues": leagues}

    return _cached("leagues", build)


@router.get("/leagues/{competition_key}")
def public_league_detail(competition_key: str):
    def build():
        from app.competition.competition_registry import get_competition
        from app.competition.competition_special import list_competition_buffer
        from app.team_watcher.team_watcher import list_watchers

        _init_db()
        with db_conn(timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            competition = get_competition(conn, competition_key)
        if not competition:
            return None

        buffer = list_competition_buffer(competition_key, limit=100, skip_mirror=True)
        matches = [_public_match(doc) for doc in (buffer.get("matches") or [])]
        watchers = list_watchers(limit=40, league_name=competition.get("name"))
        teams = [_public_watcher(w) for w in (watchers.get("watchers") or [])]

        return {
            "status": "success",
            "league": {
                "key": competition.get("key"),
                "name": competition.get("name"),
                "country": competition.get("country"),
                "tier": competition.get("tier"),
            },
            "matches": matches,
            "teams": teams,
        }

    result = _cached(f"league_{competition_key}", build)
    if not result:
        raise HTTPException(status_code=404, detail=f"League {competition_key} not found")
    return result


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

@router.get("/teams/{team_key}")
def public_team_detail(team_key: str):
    def build():
        from app.team_watcher.team_watcher import get_watcher

        result = get_watcher(team_key, limit=20)
        watcher = result.get("watcher") if isinstance(result, dict) else None
        if not watcher:
            return None
        matches = result.get("matches") or []
        recent = [
            {
                "opponent": m.get("opponent"),
                "venue": m.get("venue"),
                "date": m.get("match_date"),
                "score": f"{m.get('goals_for')}-{m.get('goals_against')}" if m.get("goals_for") is not None else None,
                "result": m.get("result"),
            }
            for m in matches[:10]
        ]
        return {
            "status": "success",
            "team": _public_watcher(watcher),
            "prediction_accuracy": result.get("prediction_accuracy"),
            "recent_matches": recent,
        }

    result = _cached(f"team_{team_key}", build)
    if not result:
        raise HTTPException(status_code=404, detail=f"Team {team_key} not found")
    return result


# ---------------------------------------------------------------------------
# Track record — aggregate accuracy only, no per-signal breakdown.
# ---------------------------------------------------------------------------

@router.get("/track-record")
def public_track_record():
    def build():
        from app.storage.league_memory import get_grading_metrics

        metrics = get_grading_metrics()
        return {"status": "success", "track_record": metrics}

    return _cached("track_record", build)


# ---------------------------------------------------------------------------
# Booking codes — what's already been (or would be) posted to X. Safe to
# expose as-is: this is public-facing copy the moment it's posted anyway.
# ---------------------------------------------------------------------------

@router.get("/booking-codes/latest")
def public_booking_codes(limit: int = Query(default=10, ge=1, le=50)):
    def build():
        from app.storage.social_posts import list_recent_posts

        posts = list_recent_posts(limit=limit)
        out = [
            {
                "status": p.get("status"),
                "share_code": p.get("share_code"),
                "thread": p.get("thread"),
                "posted_at": p.get("posted_at"),
                "created_at": p.get("created_at"),
            }
            for p in posts
        ]
        return {"status": "success", "count": len(out), "posts": out}

    return _cached(f"booking_codes_{limit}", build)


# ---------------------------------------------------------------------------
# Sitemap data — lets the Next.js site build sitemap.xml without needing
# direct DB access.
# ---------------------------------------------------------------------------

@router.get("/sitemap-data")
def public_sitemap_data():
    def build():
        from app.competition.competition_registry import list_competitions
        from app.team_watcher.team_watcher import list_watchers

        _init_db()
        with db_conn(timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            leagues = [row.get("key") for row in list_competitions(conn, enabled=True)]
        watchers = list_watchers(limit=200)
        teams = [w.get("team_key") for w in (watchers.get("watchers") or [])]
        today_docs = get_buffered_matches(dt.today().isoformat())
        match_ids = [str(doc.get("sportybet_id") or doc.get("id") or "") for doc in today_docs]
        return {
            "status": "success",
            "leagues": leagues,
            "teams": teams,
            "match_ids": [m for m in match_ids if m],
        }

    return _cached("sitemap_data", build)
