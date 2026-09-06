from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.storage.db import DB_PATH
from app.storage.db import _ensure_column, _init_db
from app.storage.db import db_conn
from app.utils.match_state import classify_match_state
from app.utils.time_context import match_time_context
from app.enrichment.web_context import search_team_context
from app.competition.competition_registry import (
    init_competition_registry_tables,
    ensure_competition,
    ensure_team_competition,
    update_team_competition_stats,
    add_performance_note,
)
from app.competition.league_strength import league_strength_score

from app.utils.doc_helpers import _band
from app.utils.primitives import _loads, _to_int
from app.utils.goal_timing import extract_goal_timing_from_doc as _extract_goal_timing_from_doc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile cache: avoid rebuilding the same profile multiple times in one
# observe_match() call or across consecutive calls in a batch.
# ---------------------------------------------------------------------------
_PROFILE_CACHE: dict[str, dict[str, Any]] = {}


def _cache_key(team_key: str, tournament_key: str | None = None) -> str:
    if tournament_key:
        return f"{team_key}::{tournament_key}"
    return team_key


def _get_cached_profile(cache_key: str) -> dict[str, Any] | None:
    return _PROFILE_CACHE.get(cache_key)


def _set_cached_profile(cache_key: str, profile: dict[str, Any]) -> None:
    _PROFILE_CACHE[cache_key] = profile


def _clear_profile_cache() -> None:
    _PROFILE_CACHE.clear()


# ---------------------------------------------------------------------------
# Tournament-scoped profile persistence
# ---------------------------------------------------------------------------

def _get_tournament_profile(conn: sqlite3.Connection, team_key: str, tournament_key: str) -> dict[str, Any] | None:
    """Load a cached tournament-scoped profile from ai_team_watcher_profiles."""
    row = conn.execute(
        """
        select profile_json, sample_size, updated_at
        from ai_team_watcher_profiles
        where team_key = ? and tournament_key = ?
        """,
        (team_key, tournament_key),
    ).fetchone()
    if not row:
        return None
    try:
        profile = json.loads(row["profile_json"]) if isinstance(row["profile_json"], str) else row["profile_json"]
    except (ValueError, TypeError):
        profile = {}
    profile["_cached_sample_size"] = int(row["sample_size"] or 0)
    profile["_cached_at"] = row["updated_at"]
    return profile if isinstance(profile, dict) else {}


def _cache_tournament_profile(conn: sqlite3.Connection, team_key: str, tournament_key: str, tournament_name: str | None, profile: dict[str, Any]) -> None:
    """Persist a tournament-scoped profile to ai_team_watcher_profiles."""
    sample_size = int(profile.get("sample_size") or 0)
    if sample_size < 3:
        return  # Don't cache profiles with insufficient data
    conn.execute(
        """
        insert into ai_team_watcher_profiles (team_key, tournament_key, tournament_name, profile_json, sample_size, updated_at)
        values (?, ?, ?, ?, ?, ?)
        on conflict(team_key, tournament_key) do update set
            tournament_name = excluded.tournament_name,
            profile_json = excluded.profile_json,
            sample_size = excluded.sample_size,
            updated_at = excluded.updated_at
        """,
        (
            team_key,
            tournament_key,
            tournament_name,
            json.dumps(profile),
            sample_size,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


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
    # Tournament-scoped profile cache (new table for per-team-per-tournament profiles)
    conn.execute(
        """
        create table if not exists ai_team_watcher_profiles (
            team_key text not null,
            tournament_key text not null,
            tournament_name text,
            profile_json text not null default '{}',
            sample_size integer not null default 0,
            updated_at text not null default current_timestamp,
            primary key (team_key, tournament_key)
        )
        """
    )
    conn.execute(
        """
        create table if not exists ai_team_watcher_observation_guard (
            match_id text not null,
            stage text not null,
            analysis_sig text,
            observed_at text not null default current_timestamp,
            primary key (match_id, stage)
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
    conn.execute("create index if not exists idx_ai_team_watchers_sporty_id on ai_team_watchers(sporty_team_id) where sporty_team_id is not null")
    conn.execute("create index if not exists idx_ai_team_watchers_sofa_id on ai_team_watchers(sofascore_team_id) where sofascore_team_id is not null")
    conn.execute("create index if not exists idx_ai_team_watcher_matches_team on ai_team_watcher_matches(team_key, match_date desc)")
    # Index for tournament-scoped profile lookups
    conn.execute("create index if not exists idx_ai_tw_profiles_lookup on ai_team_watcher_profiles(team_key, tournament_key)")
    conn.execute("create index if not exists idx_ai_tw_observation_guard_observed on ai_team_watcher_observation_guard(observed_at)")
    # Index to speed up name-based fallback in _resolve_watcher_row
    conn.execute("create index if not exists idx_ai_team_watchers_name on ai_team_watchers(team_name)")


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

        # Import engine tables and query prediction accuracy
        weekly_analysis = None
        prediction_accuracy = {
            "accuracy_known": False,
            "samples": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
        }
        try:
            from app.team_watcher.team_watcher_engine import init_tw_tables  # noqa: PLC0415
            init_tw_tables(conn)

            # Parse weekly_analysis_json from the watcher row
            if row is not None:
                raw_wa = None
                try:
                    raw_wa = row["weekly_analysis_json"]
                except (IndexError, KeyError):
                    raw_wa = None
                if raw_wa:
                    try:
                        weekly_analysis = json.loads(raw_wa) if isinstance(raw_wa, str) else raw_wa
                    except (ValueError, TypeError):
                        weekly_analysis = None

            # Query graded prediction stats
            acc_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS samples,
                    SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses
                FROM team_watcher_predictions
                WHERE team_key = ? AND graded_at IS NOT NULL
                """,
                (resolved_key,),
            ).fetchone()

            if acc_row:
                samples = int(acc_row["samples"] or 0)
                wins = int(acc_row["wins"] or 0)
                losses = int(acc_row["losses"] or 0)
                win_rate = round(wins / samples, 3) if samples > 0 else None
                accuracy_known = samples >= 5
                prediction_accuracy = {
                    "accuracy_known": accuracy_known,
                    "samples": samples,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": win_rate,
                }
        except Exception:
            pass  # Engine unavailable — return defaults

    if row is None:
        return {"status": "not_found", "team_key": team_key}

    # Rebuild profile on-the-fly if it was never computed (stored as '{}')
    watcher_data = _watcher_row(row)
    if not _loads(row["profile_json"], {}) and matches:
        try:
            with db_conn() as conn2:
                conn2.row_factory = sqlite3.Row
                rebuilt = _build_profile(conn2, row["team_key"])
            watcher_data["profile"] = rebuilt
            watcher_data["venue_split"] = rebuilt.get("venue_split") or {}
        except Exception:
            pass

    return {
        "status": "success",
        "watcher": watcher_data,
        "matches": [_match_row(match) for match in matches],
        "weekly_analysis": weekly_analysis,
        "prediction_accuracy": prediction_accuracy,
    }


def inspect_sporty_team_ids(limit: int = 20) -> dict[str, Any]:
    _init_db()
    examples: list[dict[str, Any]] = []
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
                if not isinstance(sporty, dict):
                    continue
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
        init_competition_registry_tables(conn)
        stage = _observation_stage(match_doc, analysis)
        analysis_sig = _analysis_signature(analysis)
        guard = conn.execute(
            """
            select analysis_sig
            from ai_team_watcher_observation_guard
            where match_id = ? and stage = ?
            """,
            (match_id, stage),
        ).fetchone()
        if guard and str(guard["analysis_sig"] or "") == analysis_sig:
            return {"status": "skipped", "reason": "already_observed", "match_id": match_id, "stage": stage}

        # ── Auto-verify / auto-create the competition ────────────────────────
        competition_name = _league_name_for_doc(match_doc)
        competition_entry = ensure_competition(
            conn,
            name=competition_name,
            category=str(match_doc.get("category") or ""),
            country=str(match_doc.get("country_name") or ""),
            unique_tournament_id=_unique_tournament_id_for_doc(match_doc),
        )
        competition_key = competition_entry.get("key", "") if competition_entry else ""

        for team in teams:
            resolved_key = _resolve_watcher_key(conn, team)
            # _upsert_watcher may create the row under a different key than
            # `resolved_key` (e.g. team["team_key"] on a brand-new team, when
            # resolved_key is still None) -- always use the key it actually
            # wrote, not the pre-upsert guess, or every first-time team hits
            # a NOT NULL violation on ai_team_watcher_matches.team_key below.
            resolved_key = _upsert_watcher(conn, team, resolved_key=resolved_key)
            observation = _observation_for_team(match_doc, team, analysis)
            conn.execute(
                """
                insert into ai_team_watcher_matches
                    (team_key, match_id, match_date, team_name, team_side, sporty_team_id, sofascore_team_id, opponent, venue, tournament,
                     league_name, team_position, opponent_position, table_gap,
                     goals_for, goals_against, result, status, prediction_json, analysis_json,
                     brief, raw_match_json, web_context_json, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

            # ── Build and cache tournament-scoped profile ────────────────────
            tournament_profile = None
            if competition_key:
                try:
                    tournament_profile = _build_profile(conn, resolved_key, team, tournament_key=competition_key)
                    tournament_name = observation.get("league_name") or observation.get("tournament") or competition_entry.get("name") if competition_entry else None
                    _cache_tournament_profile(conn, resolved_key, competition_key, tournament_name, tournament_profile)
                except Exception as _exc:
                    logger.debug("tournament profile cache failed for %s/%s: %s", resolved_key, competition_key, _exc)

            # ── Update team-competition stats ────────────────────────────────
            if competition_key:
                try:
                    update_team_competition_stats(
                        conn,
                        team_key=resolved_key,
                        competition_key=competition_key,
                        goals_for=observation.get("goals_for"),
                        goals_against=observation.get("goals_against"),
                        result=observation.get("result"),
                        match_date=str(observation.get("match_date") or ""),
                    )
                except Exception as _exc:
                    logger.debug("update_team_competition_stats failed: %s", _exc)

            updated.append({
                "team_key": resolved_key,
                "team_name": team["team_name"],
                "profile": profile,
                "tournament_profile": tournament_profile,
            })
        conn.execute(
            """
            insert into ai_team_watcher_observation_guard (match_id, stage, analysis_sig, observed_at)
            values (?, ?, ?, ?)
            on conflict(match_id, stage) do update set
                analysis_sig = excluded.analysis_sig,
                observed_at = excluded.observed_at
            """,
            (match_id, stage, analysis_sig, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()  # commit match inserts + profile updates before competition registry calls

    # Grade any open TW_Signal predictions for this match and trigger weekly analysis
    # Both calls are wrapped in their own try/except so engine errors never abort observe_match.
    try:
        from app.team_watcher.team_watcher_engine import grade_tw_predictions  # noqa: PLC0415
        # Extract home/away scores from match_doc so grade_tw_predictions can
        # determine the actual outcome.  The score dict uses "home"/"away" keys
        # (same convention used by _observation_for_team above).
        _score_doc = match_doc.get("score") if isinstance(match_doc.get("score"), dict) else {}
        _raw_sporty_doc = match_doc.get("raw_sporty") if isinstance(match_doc.get("raw_sporty"), dict) else {}
        _raw_score = _raw_sporty_doc.get("score") if isinstance(_raw_sporty_doc.get("score"), dict) else {}
        _home_score = _score_doc.get("home") if _score_doc.get("home") is not None else _raw_score.get("home")
        _away_score = _score_doc.get("away") if _score_doc.get("away") is not None else _raw_score.get("away")
        result_for_match = {
            "match_id": match_id,
            "updated": updated,
            "home_score": _home_score,
            "away_score": _away_score,
        }
        grade_tw_predictions(match_id, result_for_match)
    except Exception as _exc:
        logger.warning("grade_tw_predictions failed for match_id=%s: %s", match_id, _exc)

    try:
        from app.team_watcher.team_watcher_engine import _maybe_generate_weekly_analysis  # noqa: PLC0415
        for team_update in updated:
            _maybe_generate_weekly_analysis(team_update["team_key"])
    except Exception as _exc:
        pass

    # ── Post-match AI monitoring: generate context-rich performance notes ──
    try:
        from app.team_watcher.team_watcher_engine import monitor_team_performance  # noqa: PLC0415
        for team_update in updated:
            monitor_team_performance(
                team_key=team_update["team_key"],
                match_id=match_id,
                match_doc=match_doc,
                tw_signal=match_doc.get("tw_signal"),
            )
    except Exception as _exc:
        logger.debug("monitor_team_performance skipped for match_id=%s: %s", match_id, _exc)

    return {"status": "success", "match_id": match_id, "updated": updated}


def _observation_stage(match_doc: dict[str, Any], analysis: dict[str, Any] | None = None) -> str:
    if analysis:
        return "analysis"
    if match_doc.get("ai_analysis") or match_doc.get("ai_analysis_ollama"):
        return "analysis"
    if match_doc.get("sofascore_detail") or match_doc.get("sofascore_event"):
        return "enriched"
    return "ingest"


def _analysis_signature(analysis: dict[str, Any] | None) -> str:
    if not analysis:
        return ""
    try:
        return json.dumps(analysis, sort_keys=True, default=str)[:1000]
    except Exception:
        return str(analysis)[:1000]


def observe_finished_match_by_id(match_id: str, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = _get_finished_or_buffered_match(match_id)
    if not doc:
        return {"status": "not_found", "match_id": match_id}
    return observe_match(doc, analysis=analysis)


def rebuild_all_profiles(limit: int = 5000) -> dict[str, Any]:
    """Rebuild profile_json for all watchers where it is still stored as '{}'."""
    _init_db()
    rebuilt = skipped = errors = 0
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_team_watcher_tables(conn)
        keys = [
            row["team_key"]
            for row in conn.execute(
                "select team_key from ai_team_watchers where profile_json = '{}' or profile_json is null limit ?",
                (limit,),
            ).fetchall()
        ]
    for team_key in keys:
        try:
            with db_conn() as conn:
                conn.row_factory = sqlite3.Row
                profile = _build_profile(conn, team_key)
                conn.execute(
                    """
                    update ai_team_watchers
                    set profile_json = ?, league_name = coalesce(league_name, ?),
                        position = coalesce(position, ?), match_count = ?,
                        overview_json = ?, updated_at = ?
                    where team_key = ?
                    """,
                    (
                        json.dumps(profile),
                        profile.get("league_name"),
                        profile.get("position"),
                        int(profile.get("sample_size") or 0),
                        json.dumps(profile.get("overview") or {}),
                        datetime.now(timezone.utc).isoformat(),
                        team_key,
                    ),
                )
                conn.commit()
            rebuilt += 1
        except Exception as exc:
            logger.debug("rebuild_all_profiles failed for %s: %s", team_key, exc)
            errors += 1
        else:
            if profile.get("sample_size", 0) == 0:
                skipped += 1
    return {"status": "success", "total": len(keys), "rebuilt": rebuilt, "skipped": skipped, "errors": errors}


def rebuild_tournament_profiles(limit: int = 5000) -> dict[str, Any]:
    """Rebuild tournament-scoped profiles for all team-tournament combinations.

    Scans ai_team_watcher_matches for unique (team_key, tournament) pairs and
    rebuilds the corresponding ai_team_watcher_profiles entries.
    """
    _init_db()
    rebuilt = skipped = errors = 0
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_team_watcher_tables(conn)
        # Find unique team-tournament combinations with at least 3 matches
        pairs = conn.execute(
            """
            select team_key, tournament, count(*) as cnt
            from ai_team_watcher_matches
            where tournament is not null and tournament != ''
            group by team_key, tournament
            having cnt >= 3
            limit ?
            """,
            (limit,),
        ).fetchall()

    for pair in pairs:
        team_key = pair["team_key"]
        tournament_key = pair["tournament"]
        try:
            with db_conn() as conn:
                conn.row_factory = sqlite3.Row
                init_team_watcher_tables(conn)
                # Resolve team for league_name
                watcher = _resolve_watcher_row(conn, team_key)
                team = {"team_name": watcher["team_name"]} if watcher else {}
                profile = _build_profile(conn, team_key, team=team, tournament_key=tournament_key)
                tournament_name = pair["tournament"]
                _cache_tournament_profile(conn, team_key, tournament_key, tournament_name, profile)
                conn.commit()
            rebuilt += 1
        except Exception as exc:
            logger.debug("rebuild_tournament_profiles failed for %s/%s: %s", team_key, tournament_key, exc)
            errors += 1
    return {"status": "success", "total": len(pairs), "rebuilt": rebuilt, "skipped": skipped, "errors": errors}


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
        from app.storage.mongo_store import is_configured
        return is_configured()
    except Exception:
        return False


def _list_finished_matches_local_or_mongo(limit: int) -> list[dict[str, Any]]:
    if _mongo_configured():
        try:
            from app.storage.mongo_store import list_finished_matches
            return list_finished_matches(limit=limit)
        except Exception:
            pass
    # Local SQLite fallback (used when PREDICTX_LOCAL_STORAGE_ONLY=true)
    try:
        import json as _json
        from app.storage.db import DB_PATH
        from app.storage.league_memory import _init_db
        from app.storage.db import db_conn
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


def backfill_team_watcher_ids(limit: int = 5000) -> dict[str, Any]:
    """Backfill missing sporty_team_id and sofascore_team_id on existing ai_team_watchers rows.

    Strategy:
    1. Find all team watcher rows with a missing sporty or sofa team ID.
    2. Scan the match buffer (enriched docs) and finished_matches for any match
       involving those teams — enriched docs carry raw_sporty.team_ids and
       sofascore_detail/sofascore_event with the correct IDs.
    3. Re-run observe_match on each found doc so _teams_for_doc extracts the
       IDs and _upsert_watcher persists them via COALESCE (won't overwrite
       existing good data).

    Returns counts of watchers inspected, updated, and still missing.
    """
    _init_db()

    # Step 1 — find watchers missing at least one ID
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_team_watcher_tables(conn)
        missing_rows = conn.execute(
            """
            SELECT team_key, team_name, sporty_team_id, sofascore_team_id
            FROM ai_team_watchers
            WHERE (sporty_team_id IS NULL OR sporty_team_id = '')
               OR (sofascore_team_id IS NULL OR sofascore_team_id = '')
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    if not missing_rows:
        return {"status": "ok", "inspected": 0, "updated": 0, "still_missing": 0}

    missing_keys = {row["team_name"].lower().strip() for row in missing_rows}
    inspected = len(missing_rows)

    # Step 2 — collect enriched docs from buffer + finished_matches
    docs: list[dict[str, Any]] = []
    try:
        from app.storage.buffer import get_buffered_matches
        buf = get_buffered_matches(limit=2000)
        docs.extend(buf)
    except Exception:
        pass
    try:
        finished = _list_finished_matches_local_or_mongo(limit=2000)
        docs.extend(finished)
    except Exception:
        pass

    # Deduplicate by match_id
    seen_ids: set[str] = set()
    unique_docs: list[dict[str, Any]] = []
    for doc in docs:
        mid = str(doc.get("sportybet_id") or doc.get("match_id") or doc.get("_id") or "")
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            unique_docs.append(doc)

    # Step 3 — re-observe docs that involve a team with missing IDs
    processed_matches: set[str] = set()
    for doc in unique_docs:
        raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
        home_name = _team_name(raw_sporty.get("home_team") or doc.get("home_team") or "").lower().strip()
        away_name = _team_name(raw_sporty.get("away_team") or doc.get("away_team") or "").lower().strip()
        if not (home_name in missing_keys or away_name in missing_keys):
            continue
        mid = str(doc.get("sportybet_id") or doc.get("match_id") or doc.get("_id") or "")
        if mid in processed_matches:
            continue
        processed_matches.add(mid)
        try:
            observe_match(doc)
        except Exception:
            pass

    # Step 4 — count how many still have missing IDs
    with db_conn() as conn:
        still_missing = conn.execute(
            """
            SELECT COUNT(*) FROM ai_team_watchers
            WHERE (sporty_team_id IS NULL OR sporty_team_id = '')
               OR (sofascore_team_id IS NULL OR sofascore_team_id = '')
            """,
        ).fetchone()[0]

    updated = max(0, inspected - still_missing)

    logger.info(
        "backfill_team_watcher_ids: inspected=%d docs_scanned=%d matches_processed=%d updated=%d still_missing=%d",
        inspected, len(unique_docs), len(processed_matches), updated, still_missing,
    )
    return {
        "status": "ok",
        "inspected": inspected,
        "docs_scanned": len(unique_docs),
        "matches_processed": len(processed_matches),
        "updated": updated,
        "still_missing": still_missing,
    }


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


def _upsert_watcher(conn: sqlite3.Connection, team: dict[str, Any], resolved_key: str | None = None) -> str:
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
    return db_key


def _teams_for_doc(doc: dict[str, Any]) -> list[dict[str, Any]]:
    raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
    sporty_ids = raw_sporty.get("team_ids") if isinstance(raw_sporty.get("team_ids"), dict) else {}

    # sofascore_detail is the primary source for SofaScore team IDs.
    # sofascore_event is the parsed scheduled-event row — also has home_team/away_team.
    # Fall back through both so IDs are captured as early as enrichment writes
    # sofascore_event (before the heavier sofascore_detail fetch completes).
    sofa_detail = doc.get("sofascore_detail") if isinstance(doc.get("sofascore_detail"), dict) else {}
    sofa_event = doc.get("sofascore_event") if isinstance(doc.get("sofascore_event"), dict) else {}

    def _sofa_team(detail_key: str, event_key: str) -> dict[str, Any]:
        # Try detail first (most complete), then event
        t = sofa_detail.get(detail_key) or sofa_detail.get(event_key) or {}
        if not t:
            t = sofa_event.get(detail_key) or sofa_event.get(event_key) or {}
        return t if isinstance(t, dict) else {}

    sofa_home = _sofa_team("home_team", "homeTeam")
    sofa_away = _sofa_team("away_team", "awayTeam")
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
        "sporty_team_id": team.get("sporty_team_id"),
        "sofascore_team_id": team.get("sofascore_team_id"),
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
            "home_sofascore_team_id": str((doc.get("sofascore_detail") or doc.get("sofascore_event") or {}).get("home_team", {}).get("id") or "") or None,
            "away_sofascore_team_id": str((doc.get("sofascore_detail") or doc.get("sofascore_event") or {}).get("away_team", {}).get("id") or "") or None,
            "team_side": side,
            "data_sources": doc.get("data_sources") or {},
            "time_context": time_context,
            "goal_timing": _extract_goal_timing_from_doc(doc),
        },
    }


def _compute_profile_stats(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """Extract shared profile statistics from match rows.

    This function is used by both _build_profile() and the tournament-scoped
    profile builder to eliminate code duplication.
    """
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
    form = "".join("W" if row["result"] == "win" else "D" if row["result"] == "draw" else "L" for row in finished[:8])
    venue_split = _venue_split(rows)
    goal_timing = _team_goal_timing(rows)
    return {
        "sample": sample,
        "finished": finished,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "gf": gf,
        "ga": ga,
        "over_25": over_25,
        "btts": btts,
        "clean_sheets": clean_sheets,
        "blanks": blanks,
        "ppg": ppg,
        "gf_avg": gf_avg,
        "ga_avg": ga_avg,
        "form": form,
        "venue_split": venue_split,
        "goal_timing": goal_timing,
    }


def _build_profile(
    conn: sqlite3.Connection,
    team_key: str,
    team: dict[str, Any] | None = None,
    tournament_key: str | None = None,
) -> dict[str, Any]:
    """Build a team profile, optionally scoped to a specific tournament.

    When tournament_key is provided, only matches from that tournament are used.
    Falls back to the global profile if fewer than 3 tournament matches exist.
    """
    # Check cache first
    cache_key = _cache_key(team_key, tournament_key)
    cached = _get_cached_profile(cache_key)
    if cached is not None:
        return cached

    watcher = _resolve_watcher_row(conn, team_key)
    watcher_profile = _loads(watcher["profile_json"], {}) if watcher else {}

    # Build tournament-scoped or global query
    if tournament_key:
        rows = conn.execute(
            """
            select *
            from ai_team_watcher_matches
            where team_key = ? and tournament = ?
            order by match_date desc, created_at desc
            limit 30
            """,
            (team_key, tournament_key),
        ).fetchall()
    else:
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

    stats = _compute_profile_stats(rows)
    sample = stats["sample"]
    finished = stats["finished"]

    # For tournament-scoped profiles, fall back to global if too few matches
    if tournament_key and sample < 3:
        return _build_profile(conn, team_key, team=team, tournament_key=None)

    _opp_strength_scores = [
        league_strength_score(row["league_name"])["score"]
        for row in finished
        if row["league_name"]
    ]
    avg_opponent_league_strength = (
        round(sum(_opp_strength_scores) / len(_opp_strength_scores), 1)
        if _opp_strength_scores else None
    )
    analyst_score = max(1, min(99, 42 + stats["ppg"] * 13 + (stats["clean_sheets"] / sample * 7 if sample else 0)))
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
            "record": {"wins": stats["wins"], "draws": stats["draws"], "losses": stats["losses"], "form": stats["form"]},
            "goals": {
                "for_avg": round(stats["gf_avg"], 2),
                "against_avg": round(stats["ga_avg"], 2),
                "over_2_5_rate": round(stats["over_25"] / sample, 3) if sample else 0,
                "btts_rate": round(stats["btts"] / sample, 3) if sample else 0,
                "clean_sheet_rate": round(stats["clean_sheets"] / sample, 3) if sample else 0,
                "blank_rate": round(stats["blanks"] / sample, 3) if sample else 0,
            },
            "preferred_markets": _market_leans(sample, stats["wins"], stats["losses"], stats["over_25"], stats["btts"], stats["clean_sheets"], stats["blanks"]),
            "analyst_score": round(analyst_score, 2),
            "trend": "rising" if stats["form"][:3].count("W") >= 2 else "falling" if stats["form"][:3].count("L") >= 2 else "stable",
            "recent_briefs": [row["brief"] for row in rows[:6] if row["brief"]],
            "venue_split": stats["venue_split"],
        },
        table=table,
        web_context=web_context,
    )
    strengths: list[str] = []
    risks: list[str] = []
    if stats["ppg"] >= 2:
        strengths.append("results_consistency")
    if stats["gf_avg"] >= 1.6:
        strengths.append("attacking_output")
    if sample and stats["clean_sheets"] / sample >= 0.38:
        strengths.append("defensive_clean_sheets")
    if stats["ga_avg"] >= 1.6:
        risks.append("concedes_too_often")
    if sample and stats["blanks"] / sample >= 0.35:
        risks.append("failed_to_score_risk")
    if stats["ppg"] <= 1:
        risks.append("low_points_return")
    markets = _market_leans(sample, stats["wins"], stats["losses"], stats["over_25"], stats["btts"], stats["clean_sheets"], stats["blanks"])
    profile = {
        "sample_size": sample,
        "record": {"wins": stats["wins"], "draws": stats["draws"], "losses": stats["losses"], "form": stats["form"]},
        "goals": {
            "for_avg": round(stats["gf_avg"], 2),
            "against_avg": round(stats["ga_avg"], 2),
            "over_2_5_rate": round(stats["over_25"] / sample, 3) if sample else 0,
            "btts_rate": round(stats["btts"] / sample, 3) if sample else 0,
            "clean_sheet_rate": round(stats["clean_sheets"] / sample, 3) if sample else 0,
            "blank_rate": round(stats["blanks"] / sample, 3) if sample else 0,
        },
        "strengths": strengths or ["insufficient_clear_strength"],
        "risks": risks or ["no_major_risk_detected"],
        "preferred_markets": markets,
        "analyst_score": round(analyst_score, 2),
        "trend": "rising" if stats["form"][:3].count("W") >= 2 else "falling" if stats["form"][:3].count("L") >= 2 else "stable",
        "recent_briefs": [row["brief"] for row in rows[:6] if row["brief"]],
        "league_name": league_name,
        "position": position,
        "table": table,
        "web_context": web_context,
        "overview": overview,
        "last_web_context_at": last_web_context_at,
        "venue_split": stats["venue_split"],
        "goal_timing": stats["goal_timing"],
        "avg_opponent_league_strength": avg_opponent_league_strength,
        "prediction_context": {
            "usable": sample >= 3,
            "confidence": "high" if sample >= 8 else "medium" if sample >= 4 else "low",
            "market_focus": [item["market"] for item in markets[:3]],
            "boost_signals": strengths[:3],
            "risk_signals": risks[:3],
        },
    }
    # Cache the result
    _set_cached_profile(cache_key, profile)
    return profile


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


def _extract_goal_timing_from_doc(doc: dict[str, Any]) -> dict[str, Any]:
    # Imported from app.utils.goal_timing — kept as a local alias for
    # call-site compatibility. All logic lives in the shared module.
    from app.utils.goal_timing import extract_goal_timing_from_doc as _shared
    return _shared(doc)


def _team_goal_timing(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """Aggregate goal timing from raw_match_json.goal_timing across team matches."""
    total_goals = 0
    first_half = 0
    second_half = 0
    bands: dict[str, int] = {
        "1-10min": 0, "11-20min": 0, "21-30min": 0, "31-40min": 0, "41-45min": 0,
        "46-55min": 0, "56-65min": 0, "66-75min": 0, "76-85min": 0, "86-90min": 0,
    }
    first_goal_minutes: list[float] = []
    avg_intervals: list[float] = []
    matches_with_timing = 0

    for row in rows:
        raw = row["raw_match_json"]
        if not raw:
            continue
        try:
            doc = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        gt = doc.get("goal_timing")
        if not isinstance(gt, dict):
            continue
        matches_with_timing += 1
        total_goals += int(gt.get("total_goals") or 0)
        first_half += int(gt.get("first_half_goals") or 0)
        second_half += int(gt.get("second_half_goals") or 0)
        for band in bands:
            key = "band_" + band.replace("-", "_").replace("min", "")
            bands[band] += int(gt.get(key) or 0)
        fgm = gt.get("first_goal_minute")
        if fgm is not None:
            try:
                first_goal_minutes.append(float(fgm))
            except (TypeError, ValueError):
                pass
        agi = gt.get("avg_interval_minutes")
        if agi is not None:
            try:
                avg_intervals.append(float(agi))
            except (TypeError, ValueError):
                pass

    if matches_with_timing == 0:
        return {"available": False}

    n = matches_with_timing
    dominant_half = "first" if first_half > second_half else "second" if second_half > first_half else "even"
    peak_band = max(bands, key=lambda b: bands[b]) if any(bands.values()) else None
    band_pct = {b: round(bands[b] / total_goals * 100, 1) if total_goals else 0 for b in bands}

    return {
        "available": True,
        "sample_matches": n,
        "avg_goals_per_match": round(total_goals / n, 2),
        "first_half_goals": first_half,
        "second_half_goals": second_half,
        "dominant_half": dominant_half,
        "peak_band": peak_band,
        "avg_first_goal_minute": round(sum(first_goal_minutes) / len(first_goal_minutes), 1) if first_goal_minutes else None,
        "avg_interval_between_goals": round(sum(avg_intervals) / len(avg_intervals), 1) if avg_intervals else None,
        "band_distribution": band_pct,
    }


def _compute_pressing_profile(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """Build a pressing / defensive-intensity profile from live_stat_snapshots.

    For each match row in ai_team_watcher_matches we look up the LATEST
    snapshot in live_stat_snapshots (the cumulative full-match totals) and
    read the pressing stats we care about, indexed by the team's side
    (home or away).  We then average across all matches that had stat data.

    Returned block structure
    ------------------------
    {
        "available": bool,          # False when no matches had snapshot data
        "matches_with_stats": int,  # how many of the last N had live stats
        "style":  "high_press" | "mid_block" | "low_block" | "unknown",
        "press_intensity": 0–100,   # composite 0-100 score
        "recoveries_avg":  float,   # ball recoveries per match
        "tackles_won_rate": float,  # tackles won / total tackles (0–1)
        "interceptions_avg": float,
        "final_third_entries_avg": float,
        "sprints_avg": float,
        "accurate_passes_avg": float,   # build-up style proxy
        "long_ball_rate": float,        # long_balls / accurate_passes (0–1)
        "clearances_avg": float,        # defensive block depth proxy
        "press_vs_goals": {
            "matches": int,
            "goals_scored_avg": float,   # goals for in pressing matches
            "goals_conceded_avg": float, # goals against in pressing matches
        },
        "vs_high_press": {
            # how THIS team performs when the OPPONENT was the pressing side
            "matches": int,
            "win_rate": float,
            "goals_scored_avg": float,
        },
    }
    """
    try:
        from app.live.live_stat_history import get_stat_history  # local import avoids circular
    except Exception:
        return {"available": False, "reason": "live_stat_history_unavailable"}

    # Accumulators
    n_with_stats = 0
    recoveries:    list[float] = []
    tackles_total: list[float] = []
    tackles_won:   list[float] = []
    interceptions: list[float] = []
    ft_entries:    list[float] = []
    sprints:       list[float] = []
    acc_passes:    list[float] = []
    long_balls:    list[float] = []
    clearances:    list[float] = []

    # "pressing match" = this team had higher pressure score than opponent
    press_gf: list[float] = []
    press_ga: list[float] = []
    # "under press" = opponent had higher pressure score
    under_results: list[str] = []
    under_gf:      list[float] = []

    for row in rows:
        match_id = str(row["match_id"] or "")
        side     = str(row["team_side"] or "").strip().lower()
        if not match_id or side not in ("home", "away"):
            continue

        opp_side = "away" if side == "home" else "home"

        try:
            snaps = get_stat_history(match_id)
        except Exception:
            continue
        if not snaps:
            continue

        # Use the last (most complete) snapshot — cumulative full-match totals
        snap = snaps[-1]

        def _v(key: str, s: str = side) -> float:
            return float(snap.get(f"{s}_{key}") or 0)

        # Guard: skip if the new columns aren't present yet (pre-migration rows)
        if snap.get(f"{side}_tackles") is None and snap.get(f"{side}_recoveries") is None:
            continue

        n_with_stats += 1

        rec  = _v("recoveries")
        tack = _v("tackles")
        tw   = _v("tackles_won")
        icp  = _v("interceptions")
        fte  = _v("final_third_entries")
        sp   = _v("sprints")
        ap   = _v("accurate_passes")
        lb   = _v("long_balls")
        cl   = _v("clearances")

        recoveries.append(rec)
        if tack > 0:
            tackles_total.append(tack)
            tackles_won.append(tw)
        interceptions.append(icp)
        ft_entries.append(fte)
        sprints.append(sp)
        acc_passes.append(ap)
        long_balls.append(lb)
        clearances.append(cl)

        # Pressure score: same composite as live_pressure.py but using richer stats
        # Weight: recoveries + interceptions capture defensive press,
        #         final_third_entries + dangerous_attacks capture offensive press.
        da      = _v("dangerous_attacks")
        shots   = _v("total_shots")
        opp_da  = _v("dangerous_attacks", opp_side)
        opp_shots = _v("total_shots", opp_side)

        team_press_score = rec * 0.4 + icp * 0.3 + fte * 0.2 + da * 0.07 + shots * 0.03
        opp_press_score  = (
            _v("recoveries", opp_side) * 0.4
            + _v("interceptions", opp_side) * 0.3
            + _v("final_third_entries", opp_side) * 0.2
            + opp_da * 0.07
            + opp_shots * 0.03
        )

        gf = float(row["goals_for"]  or 0) if row["goals_for"]  is not None else None
        ga = float(row["goals_against"] or 0) if row["goals_against"] is not None else None

        if team_press_score > opp_press_score * 1.15:
            # This team was the pressing side
            if gf is not None:
                press_gf.append(gf)
            if ga is not None:
                press_ga.append(ga)
        elif opp_press_score > team_press_score * 1.15:
            # Opponent was pressing — record how this team handled it
            if row["result"]:
                under_results.append(str(row["result"]))
            if gf is not None:
                under_gf.append(gf)

    if n_with_stats == 0:
        return {"available": False, "matches_with_stats": 0}

    def _avg(lst: list[float]) -> float | None:
        return round(sum(lst) / len(lst), 2) if lst else None

    avg_rec  = _avg(recoveries)   or 0.0
    avg_icp  = _avg(interceptions) or 0.0
    avg_fte  = _avg(ft_entries)   or 0.0
    avg_sp   = _avg(sprints)      or 0.0
    avg_ap   = _avg(acc_passes)   or 0.0
    avg_lb   = _avg(long_balls)   or 0.0
    avg_cl   = _avg(clearances)   or 0.0
    tw_rate  = round(sum(tackles_won) / sum(tackles_total), 3) if sum(tackles_total) > 0 else None

    # Composite press intensity: 0–100
    # Anchored so that ~15 recoveries + ~10 interceptions + ~30 FTE = ~60 (high press)
    raw_intensity = avg_rec * 1.8 + avg_icp * 2.5 + avg_fte * 0.8
    press_intensity = round(min(100.0, raw_intensity), 1)

    # Style classification
    if press_intensity >= 60:
        style = "high_press"
    elif press_intensity >= 35:
        style = "mid_block"
    elif n_with_stats >= 2:
        style = "low_block"
    else:
        style = "unknown"

    # Long-ball rate proxy for build-up style
    long_ball_rate = round(avg_lb / avg_ap, 3) if avg_ap > 0 else None

    return {
        "available": True,
        "matches_with_stats": n_with_stats,
        "style": style,
        "press_intensity": press_intensity,
        "recoveries_avg": avg_rec,
        "tackles_won_rate": tw_rate,
        "interceptions_avg": avg_icp,
        "final_third_entries_avg": avg_fte,
        "sprints_avg": avg_sp,
        "accurate_passes_avg": avg_ap,
        "long_ball_rate": long_ball_rate,
        "clearances_avg": avg_cl,
        "press_vs_goals": {
            "matches": len(press_gf),
            "goals_scored_avg": _avg(press_gf),
            "goals_conceded_avg": _avg(press_ga),
        },
        "vs_high_press": {
            "matches": len(under_results),
            "win_rate": round(under_results.count("win") / len(under_results), 3) if under_results else None,
            "goals_scored_avg": _avg(under_gf),
        },
    }


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
    # If profile was never built (stored as {}), synthesise it from top-level columns
    # so the frontend always gets usable data.
    if not profile:
        profile = {
            "analyst_score": 0,
            "trend": "stable",
            "record": {"wins": 0, "draws": 0, "losses": 0, "form": ""},
            "goals": {"for_avg": 0, "against_avg": 0, "over_2_5_rate": 0, "btts_rate": 0, "clean_sheet_rate": 0, "blank_rate": 0},
            "preferred_markets": [],
            "strengths": [],
            "risks": [],
            "venue_split": {"home": {}, "away": {}},
            "prediction_context": {"usable": False, "confidence": "low", "market_focus": []},
            "league_name": row["league_name"],
            "position": row["position"],
            "recent_briefs": [],
        }
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
        from app.storage.mongo_store import get_finished_match
        doc = get_finished_match(match_id)
        if doc:
            return doc
    except Exception:
        pass
    try:
        from app.storage.buffer import get_buffered_match
        return get_buffered_match(match_id)
    except Exception:
        return None


#
# Produces a single prediction signal from three sub-signals derived from
# the team watcher match history:
#
#   A. opponent_tier_edge  — win/draw/loss rates split by opponent strength
#      (stronger / similar / weaker) using stored table_gap values.
#      A team that consistently beats similar-strength opponents is a real edge.
#
#   B. goal_timing_edge    — first goal minute and avg interval between goals
#      extracted from raw_match_json.goal_timing. Teams that score early or
#      have short goal intervals push over/btts picks.
#
#   C. signal_combo_edge   — when the current match has active signals, look up
#      past matches for these teams where the stored prediction_json carried
#      similar signal names. Win rate of those past predictions → if historically
#      these signals + this team = high win rate, boost confidence.
#
# Impact is bounded ±8 and feeds directly into home_power in predict_sofascore_event.


def _slug(name: str) -> str:
    """Convert a team name to a URL-safe slug key."""
    import re as _re
    return _re.sub(r'[^a-z0-9]+', '-', str(name or '').lower().strip()).strip('-')


def _team_name(team: object) -> str:
    """Extract team name from a string or dict."""
    if isinstance(team, dict):
        return str(team.get('name') or team.get('team_name') or '')
    return str(team or '')


def _league_name_for_doc(doc: dict) -> str:
    """Extract league/tournament name from a match document."""
    t = doc.get('tournament') or doc.get('league_name') or ''
    if isinstance(t, dict):
        return str(t.get('name') or '')
    return str(t or '')


def _league_name_from_rows(rows: list) -> str:
    """Infer league name from a list of match rows."""
    for row in rows or []:
        name = row['league_name'] if 'league_name' in row.keys() else None
        if name:
            return str(name)
    return ''


def _unique_tournament_id_for_doc(doc: dict) -> str | None:
    """Extract a unique tournament ID from a match document."""
    detail = doc.get('sofascore_detail') or {}
    t = detail.get('tournament') or doc.get('tournament') or {}
    if isinstance(t, dict):
        return str(t.get('uniqueTournamentId') or t.get('id') or '') or None
    return None


def _resolve_watcher_key(conn, team: dict) -> str | None:
    """Find an existing watcher key for a team by alias matching."""
    team_key = str(team.get('team_key') or '')
    if not team_key:
        return None
    row = conn.execute(
        "SELECT team_key FROM ai_team_watchers WHERE team_key = ? LIMIT 1",
        (team_key,)
    ).fetchone()
    return row['team_key'] if row else None


def _resolve_watcher_row(conn, team_key: str, team: dict | None = None) -> object | None:
    """Fetch the watcher row for a given team key, with provider-ID fallback."""
    if not team_key:
        return None
    row = conn.execute(
        "SELECT * FROM ai_team_watchers WHERE team_key = ? LIMIT 1",
        (str(team_key),)
    ).fetchone()
    if row or not team:
        return row
    sporty_id = team.get("sporty_team_id")
    if sporty_id:
        row = conn.execute(
            "SELECT * FROM ai_team_watchers WHERE sporty_team_id = ? LIMIT 1",
            (str(sporty_id),),
        ).fetchone()
        if row:
            return row
    sofa_id = team.get("sofascore_team_id")
    if sofa_id:
        row = conn.execute(
            "SELECT * FROM ai_team_watchers WHERE sofascore_team_id = ? LIMIT 1",
            (str(sofa_id),),
        ).fetchone()
        if row:
            return row
    return None


def _merge_aliases(existing: object | None, team: dict) -> list:
    """Merge new aliases into the existing alias list."""
    import json as _json
    existing_aliases = []
    if existing:
        try:
            existing_aliases = _json.loads(existing['aliases_json'] or '[]')
        except Exception:
            pass
    new_aliases = team.get('aliases') or []
    seen = {(a.get('provider'), a.get('id')) for a in existing_aliases}
    for alias in new_aliases:
        key = (alias.get('provider'), alias.get('id'))
        if key not in seen:
            existing_aliases.append(alias)
            seen.add(key)
    return existing_aliases


def _table_lookup(doc: dict) -> dict:
    """Build a name->row lookup from standings in the document."""
    detail = doc.get('sofascore_detail') or {}
    standings = detail.get('standings') or doc.get('standings') or []
    table: dict = {}
    for group in standings if isinstance(standings, list) else []:
        rows = group.get('rows') or [] if isinstance(group, dict) else []
        for row in rows:
            name = str((row.get('team') or {}).get('name') or row.get('team_name') or '')
            if name:
                table[name.lower()] = row
    return table


def _team_position(table_map: dict, name: str, sporty_id, sofa_id, *, return_row: bool = False):
    """Look up a team's position from the standings table map."""
    if not table_map or not name:
        return None if return_row else None
    key = str(name or '').lower().strip()
    row = table_map.get(key)
    if row is None:
        # fuzzy: try partial match
        for k, v in table_map.items():
            if key in k or k in key:
                row = v
                break
    if return_row:
        return row
    return int((row or {}).get('position') or 0) or None


def _position_value(row: object | None) -> int | None:
    """Extract position integer from a standings row."""
    if row is None:
        return None
    try:
        return int(row['position'] or 0) or None
    except Exception:
        return None


def _table_gap(team_row: object | None, opponent_row: object | None) -> int | None:
    """Return the position gap between two teams in the standings."""
    t = _position_value(team_row)
    o = _position_value(opponent_row)
    if t is None or o is None:
        return None
    return o - t


def _table_from_rows(rows: list) -> dict | None:
    """Build a minimal table dict from match rows."""
    if not rows:
        return None
    # rows are sqlite3.Row objects -- not JSON-serializable as-is (this dict
    # ends up inside profile_json via json.dumps in observe_match). Convert
    # each to a plain dict first.
    return {'rows': [dict(row) for row in rows]}


def _matchup_context(home: dict, away: dict, doc: dict) -> dict:
    """Build a brief matchup context dict from home/away team data."""
    return {
        'home_position': home.get('team_position'),
        'away_position': away.get('team_position'),
        'table_gap': (
            (away.get('team_position') or 0) - (home.get('team_position') or 0)
            if home.get('team_position') and away.get('team_position') else None
        ),
    }


def _analysis_summary(analysis: dict | None) -> str:
    """Return a short text summary from an analysis dict."""
    if not analysis:
        return ''
    return str(analysis.get('summary') or analysis.get('note') or '')


def _team_web_context(team: dict, doc: dict, team_row, opponent_row) -> dict:
    """Build web context dict for a team signal."""
    return {
        'team_name': team.get('team_name'),
        'team_position': _position_value(team_row),
        'opponent_position': _position_value(opponent_row),
        'table_gap': _table_gap(team_row, opponent_row),
        'league_name': _league_name_for_doc(doc),
    }


def _should_refresh_web_context(watcher: dict, sample: int) -> bool:
    """Return True when the web context is stale or missing."""
    from datetime import datetime, timezone
    web_context = watcher.get('web_context') or {}
    last_at = watcher.get('last_web_context_at')
    if not web_context or not last_at:
        return True
    try:
        dt = datetime.fromisoformat(str(last_at).replace('Z', '+00:00')).astimezone(timezone.utc)
        age_hours = (datetime.now(tz=timezone.utc) - dt).total_seconds() / 3600
        return age_hours > (6 if sample >= 5 else 24)
    except Exception:
        return True


def _build_overview(
    team_name: str,
    league_name: str,
    **kwargs,
) -> dict:
    """Build a team overview dict from available data."""
    return {
        'team_name': team_name,
        'league_name': league_name,
        **{k: v for k, v in kwargs.items() if v is not None},
    }


def team_watch_signal(match_doc: dict[str, Any]) -> dict[str, Any] | None:
    """
    Compute a team-watch-derived prediction signal for a match.

    Returns a signal dict compatible with the signals list in predict_sofascore_event,
    or None if insufficient data exists for both teams.

    Signal structure:
        {
            "name": "team_watch",
            "impact": float,          # net home_power contribution, ±8
            "value": {
                "home": { sub-signal breakdown },
                "away": { sub-signal breakdown },
                "matchup_edge": float,
                "available": bool,
                "samples": { "home": int, "away": int },
            }
        }
    """
    try:
        teams = _teams_for_doc(match_doc)
        if not teams:
            return None

        home_team = next((t for t in teams if t["side"] == "home"), None)
        away_team = next((t for t in teams if t["side"] == "away"), None)
        if not home_team or not away_team:
            return None

        _init_db()
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            init_team_watcher_tables(conn)

            home_matches = _load_watcher_matches(conn, home_team["team_key"])
            away_matches = _load_watcher_matches(conn, away_team["team_key"])

        if not home_matches and not away_matches:
            return None

        active_signal_names = _active_signal_names(match_doc)

        home_sub = _team_sub_signal(home_matches, "home", active_signal_names)
        away_sub = _team_sub_signal(away_matches, "away", active_signal_names)

        # Net impact: home edge minus away edge, bounded ±8
        # Positive = home advantage, negative = away advantage
        matchup_edge = round(
            max(-8.0, min(8.0, home_sub["total_edge"] - away_sub["total_edge"])),
            2,
        )

        if abs(matchup_edge) < 0.5 and not home_sub["available"] and not away_sub["available"]:
            return None

        return {
            "name": "team_watch",
            "impact": matchup_edge,
            "value": {
                "home": home_sub,
                "away": away_sub,
                "matchup_edge": matchup_edge,
                "available": home_sub["available"] or away_sub["available"],
                "samples": {
                    "home": home_sub["samples"],
                    "away": away_sub["samples"],
                },
            },
        }
    except Exception:
        logger.warning(
            "team_watch_signal failed for match id=%s: %s",
            match_doc.get("sportybet_id") or match_doc.get("id") or "unknown",
            __import__("traceback").format_exc().splitlines()[-1],
        )
        return None


def _load_watcher_matches(conn: sqlite3.Connection, team_key: str) -> list[sqlite3.Row]:
    """Load the last 20 finished watcher matches for a team."""
    return conn.execute(
        """
        select goals_for, goals_against, result, team_side,
               table_gap, team_position, opponent_position,
               prediction_json, raw_match_json
        from ai_team_watcher_matches
        where team_key = ?
          and goals_for is not null
          and goals_against is not null
        order by match_date desc, created_at desc
        limit 20
        """,
        (team_key,),
    ).fetchall()


def _team_sub_signal(
    rows: list[sqlite3.Row],
    side: str,
    active_signal_names: set[str],
) -> dict[str, Any]:
    """
    Compute the three sub-signal edges for one team and combine them.

    Returns a dict with individual edges and a total_edge float.
    """
    if not rows:
        return {
            "available": False,
            "samples": 0,
            "opponent_tier_edge": 0.0,
            "goal_timing_edge": 0.0,
            "signal_combo_edge": 0.0,
            "total_edge": 0.0,
        }

    tier_edge = _opponent_tier_edge(rows, side)
    timing_edge = _goal_timing_edge(rows, side)
    combo_edge = _signal_combo_edge(rows, active_signal_names)

    # Weight: tier is most reliable (0.5), timing is factual (0.3), combo is sparse (0.2)
    total = round(tier_edge * 0.5 + timing_edge * 0.3 + combo_edge * 0.2, 2)
    total = max(-4.0, min(4.0, total))  # each side capped at ±4 before matchup diff

    return {
        "available": True,
        "samples": len(rows),
        "opponent_tier_edge": round(tier_edge, 2),
        "goal_timing_edge": round(timing_edge, 2),
        "signal_combo_edge": round(combo_edge, 2),
        "total_edge": total,
    }


def _opponent_tier_edge(rows: list[sqlite3.Row], side: str) -> float:
    """
    Split matches by opponent strength using table_gap:
      stronger  = gap < -3  (opponent ranked higher)
      similar   = gap -3..+3
      weaker    = gap > +3  (opponent ranked lower)

    Compute win rate per tier. Edge = win_rate_vs_similar - 0.45
    (similar-opponent performance is the most predictive tier).
    Falls back to overall win rate when similar-tier sample is thin.
    """
    tiers: dict[str, list[str]] = {"stronger": [], "similar": [], "weaker": []}
    for row in rows:
        gap = row["table_gap"]
        result = str(row["result"] or "")
        if gap is None:
            tiers["similar"].append(result)
            continue
        try:
            gap_int = int(gap)
        except (TypeError, ValueError):
            tiers["similar"].append(result)
            continue
        if gap_int < -3:
            tiers["stronger"].append(result)
        elif gap_int > 3:
            tiers["weaker"].append(result)
        else:
            tiers["similar"].append(result)

    def _win_rate(results: list[str]) -> float | None:
        finished = [r for r in results if r in ("win", "draw", "loss")]
        if len(finished) < 3:
            return None
        wins = sum(1 for r in finished if r == "win")
        draws = sum(1 for r in finished if r == "draw")
        # Points-per-game normalised to [0,1]: max 3pts → 1.0
        ppg = (wins * 3 + draws) / (len(finished) * 3)
        return ppg

    similar_rate = _win_rate(tiers["similar"])
    stronger_rate = _win_rate(tiers["stronger"])

    if similar_rate is not None:
        # Edge relative to a 45% baseline (slightly below 50% to account for draw probability)
        edge = (similar_rate - 0.45) * 10
        # Bonus: if they also perform well vs stronger opponents
        if stronger_rate is not None and stronger_rate > 0.35:
            edge += (stronger_rate - 0.35) * 4
        return max(-4.0, min(4.0, edge))

    # Fallback: overall win rate
    all_results = [r for row in rows for r in [str(row["result"] or "")] if r in ("win", "draw", "loss")]
    if len(all_results) < 3:
        return 0.0
    wins = sum(1 for r in all_results if r == "win")
    draws = sum(1 for r in all_results if r == "draw")
    ppg = (wins * 3 + draws) / (len(all_results) * 3)
    return max(-4.0, min(4.0, (ppg - 0.45) * 8))


def _goal_timing_edge(rows: list[sqlite3.Row], side: str) -> float:
    """
    Extract goal timing from raw_match_json.goal_timing per match.
    Computes:
      - avg first goal minute for this team (lower = scores early)
      - avg goal interval (lower = high-scoring games)

    Edge:
      - First goal avg < 25 min → +1.5 (early pressure)
      - Avg interval < 20 min  → +1.5 (high-scoring tendency)
      - First goal avg > 60 min → -1.0 (late or no goals)
    """
    first_goal_minutes: list[float] = []
    avg_intervals: list[float] = []

    for row in rows:
        raw = row["raw_match_json"]
        if not raw:
            continue
        try:
            doc = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue

        goal_timing = doc.get("goal_timing") or {}
        if not isinstance(goal_timing, dict):
            continue

        goal_minutes = goal_timing.get("goal_minutes") or []
        if isinstance(goal_minutes, list) and goal_minutes:
            try:
                first = float(goal_minutes[0])
                first_goal_minutes.append(first)
            except (TypeError, ValueError):
                pass

        avg_interval = goal_timing.get("average_interval_minutes")
        if avg_interval is not None:
            try:
                avg_intervals.append(float(avg_interval))
            except (TypeError, ValueError):
                pass

    edge = 0.0

    if first_goal_minutes:
        avg_first = sum(first_goal_minutes) / len(first_goal_minutes)
        if avg_first < 25:
            edge += 1.5
        elif avg_first < 40:
            edge += 0.5
        elif avg_first > 60:
            edge -= 1.0

    if avg_intervals:
        avg_interval = sum(avg_intervals) / len(avg_intervals)
        if avg_interval < 18:
            edge += 1.5
        elif avg_interval < 25:
            edge += 0.5
        elif avg_interval > 40:
            edge -= 0.5

    return max(-3.0, min(3.0, edge))


def _signal_combo_edge(rows: list[sqlite3.Row], active_signal_names: set[str]) -> float:
    """
    Look at past matches where the stored prediction_json carried signals
    overlapping with the current active signal names.

    Win rate of those past predictions → edge relative to 50% baseline.
    Requires at least 3 matching past matches to produce a non-zero edge.
    """
    if not active_signal_names:
        return 0.0

    matching_results: list[str] = []
    for row in rows:
        pred_raw = row["prediction_json"]
        if not pred_raw:
            continue
        try:
            pred = json.loads(pred_raw) if isinstance(pred_raw, str) else pred_raw
        except Exception:
            continue
        if not isinstance(pred, dict):
            continue

        past_signals = pred.get("signals") or []
        past_names = {str(s.get("name") or "") for s in past_signals if isinstance(s, dict)}
        overlap = active_signal_names & past_names
        # Require at least 2 overlapping signals to count as a "similar" prediction context
        if len(overlap) < 2:
            continue

        result = str(row["result"] or "")
        if result in ("win", "draw", "loss"):
            matching_results.append(result)

    if len(matching_results) < 3:
        return 0.0

    wins = sum(1 for r in matching_results if r == "win")
    draws = sum(1 for r in matching_results if r == "draw")
    ppg = (wins * 3 + draws) / (len(matching_results) * 3)
    # Edge relative to 50% baseline, capped at ±3
    return max(-3.0, min(3.0, (ppg - 0.50) * 8))


def _active_signal_names(match_doc: dict[str, Any]) -> set[str]:
    """Extract signal names already computed on the match doc (from a prior prediction pass)."""
    prediction = match_doc.get("prediction") or {}
    if not isinstance(prediction, dict):
        return set()
    signals = prediction.get("signals") or []
    return {str(s.get("name") or "") for s in signals if isinstance(s, dict) and s.get("name")}




