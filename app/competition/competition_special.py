from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.storage.db import DB_PATH
from app.storage.db import _ensure_column
from app.storage.league_memory import _init_db
from app.storage.db import db_conn
from app.utils.match_state import classify_match_state
from app.market.season_stage import (
    classify_table_size,
    detect_season_stage,
    season_aware_table_weight,
)
from app.utils.doc_helpers import _band
from app.utils.primitives import _optional_int, _parse_datetime, _safe_num
from app.utils.goal_timing import extract_goal_timing_from_detail as _extract_goal_timing

logger = logging.getLogger(__name__)


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
    # league_strength: curated footballing quality score on a 20–98 scale.
    # Used as the hardcoded prior in league_strength.py's hybrid scoring.
    # Tier guide: 1=elite (88–98), 2=top (75–87), 3=strong (62–74),
    #             4=mid (50–61), 5=lower (38–49), 6=weak (20–37)
    # ── Tier 1: elite continental / top-5 leagues ────────────────────────
    {"key": "premier-league",      "name": "Premier League",            "unique_tournament_id": 17,    "league_strength": 95},
    {"key": "la-liga",             "name": "LaLiga",                    "unique_tournament_id": 8,     "league_strength": 93},
    {"key": "champions-league",    "name": "UEFA Champions League",     "unique_tournament_id": 7,     "league_strength": 98},
    {"key": "serie-a",             "name": "Serie A",                   "unique_tournament_id": 23,    "league_strength": 91},
    {"key": "bundesliga",          "name": "Bundesliga",                "unique_tournament_id": 35,    "league_strength": 90},
    {"key": "ligue-1",             "name": "Ligue 1",                   "unique_tournament_id": 34,    "league_strength": 86},
    # ── Tier 2: top domestic + major continental ──────────────────────────
    {"key": "europa-league",       "name": "UEFA Europa League",        "unique_tournament_id": 679,   "league_strength": 84},
    {"key": "eredivisie",          "name": "Eredivisie",                "unique_tournament_id": 37,    "league_strength": 78},
    {"key": "primeira-liga",       "name": "Primeira Liga",             "unique_tournament_id": 238,   "league_strength": 77},
    {"key": "super-lig",           "name": "Süper Lig",                 "unique_tournament_id": 52,    "league_strength": 76},
    {"key": "brasileirao",         "name": "Brasileirão Série A",       "unique_tournament_id": 325,   "league_strength": 76},
    {"key": "copa-libertadores",   "name": "Copa Libertadores",         "unique_tournament_id": 384,   "league_strength": 80},
    {"key": "conference-league",   "name": "UEFA Conference League",    "unique_tournament_id": 329,   "league_strength": 75},
    # ── Tier 3: strong second-tier / competitive domestic ────────────────
    {"key": "championship",        "name": "EFL Championship",          "unique_tournament_id": 18,    "league_strength": 72},
    {"key": "belgian-pro-league",  "name": "Belgian Pro League",        "unique_tournament_id": 38,    "league_strength": 70},
    {"key": "scottish-premiership","name": "Scottish Premiership",      "unique_tournament_id": 36,    "league_strength": 67},
    {"key": "mls",                 "name": "Major League Soccer",       "unique_tournament_id": 242,   "league_strength": 66},
    {"key": "liga-mx",             "name": "Liga MX",                   "unique_tournament_id": 406,   "league_strength": 66},
    {"key": "argentine-primera",   "name": "Argentine Primera División","unique_tournament_id": 390,   "league_strength": 68},
    {"key": "liga-profesional",    "name": "Liga Profesional Argentina","unique_tournament_id": 155,   "league_strength": 65},
    {"key": "copa-sudamericana",   "name": "Copa Sudamericana",         "unique_tournament_id": 480,   "league_strength": 68},
    {"key": "swiss-super-league",  "name": "Swiss Super League",        "unique_tournament_id": 215,   "league_strength": 64},
    {"key": "austrian-bundesliga", "name": "Austrian Bundesliga",       "unique_tournament_id": 45,    "league_strength": 63},
    {"key": "danish-superliga",    "name": "Danish Superliga",          "unique_tournament_id": 39,    "league_strength": 62},
    # ── Tier 4: mid-level domestic ───────────────────────────────────────
    {"key": "saudi-pro-league",    "name": "Saudi Pro League",          "unique_tournament_id": 203,   "league_strength": 60},
    {"key": "j1-league",           "name": "J1 League",                 "unique_tournament_id": 98,    "league_strength": 58},
    {"key": "k-league-1",          "name": "K League 1",                "unique_tournament_id": 116,   "league_strength": 55},
    {"key": "eliteserien",         "name": "Eliteserien",               "unique_tournament_id": 20,    "league_strength": 57},
    {"key": "allsvenskan",         "name": "Allsvenskan",               "unique_tournament_id": 67,    "league_strength": 56},
    {"key": "colombia-primera-a",  "name": "Categoría Primera A",       "unique_tournament_id": 11539, "league_strength": 52},
)
_CATALOGUE_BY_KEY = {entry["key"]: entry for entry in TOP_30_COMPETITIONS}



def _opponent_quality_score(team_id: str, team_name: str) -> float:
    """Real per-opponent quality signal, derived from the opponent's ELO
    rating (app.models.elo.get_elo, team_id-keyed, K=32, default 1500 for
    an unrated team) instead of the previous `_name_quality_score()`
    placeholder, which scored a team's quality by the character length of
    its name string.

    Mapped onto the 0-100 scale `_recent_play_strength()` already expects
    via `(quality - 50) * 0.25`: an opponent a full division stronger
    (+400 ELO, roughly top-to-bottom of a league) reads as 100; a full
    division weaker reads as 0. The previous name-length version was not
    just noisy, it was a near-constant bias: every value it could return
    (0.4-1.0) read as ~49 points BELOW the 50 baseline on this same scale,
    so `_recent_play_strength()` applied roughly the same ~-12-point
    penalty to every team's strength_score regardless of which opponents
    were actually faced.
    """
    if not team_id:
        return 50.0
    try:
        from app.models.elo import get_elo
        rating = get_elo(team_id)
    except Exception as exc:
        logger.warning("_opponent_quality_score: ELO lookup failed for team_id=%s (%s) — defaulting to 50", team_id, exc)
        return 50.0
    return max(0.0, min(100.0, 50.0 + (rating - 1500.0) / 8.0))


def _classify_competition(key: str, league_name: str = "") -> str:
    """
    "known" vs "learned" classification the user asked for -- purely
    descriptive metadata threaded through to downstream consumers
    (the competition_intelligence signal, the ensemble, the AI prompt);
    does NOT change TOP_30_COMPETITIONS, its scoring/importance logic, or
    anything in _competition_intelligence_context -- those stay exactly
    as they are.

    "known": key is in the curated TOP_30_COMPETITIONS / World Cup
    catalogue below (unchanged).

    "learned": not in the curated catalogue, but has crossed
    self_learner.MIN_SAMPLES graded predictions in league_accuracy -- the
    same "trust it fully" sample bar used everywhere else in this
    codebase, not a new one invented for this.

    "unclassified": neither -- e.g. a dynamically-discovered competition
    (see _ensure_dynamic_competition) with too little graded history yet
    to say anything about it. apply_known_competition_context()'s own
    "known" boolean does not distinguish any of this today: it's True for
    the curated catalogue AND for any dynamically-tracked competition
    that merely has a settings row, however thin its history -- exactly
    the ambiguity being split out here.
    """
    if key == DEFAULT_WORLD_CUP.get("key") or key in _CATALOGUE_BY_KEY:
        return "known"
    try:
        from app.monitoring.self_learner import get_league_accuracy, MIN_SAMPLES
        lacc = get_league_accuracy(league_name or key)
        if lacc.get("known"):
            total_samples = sum(int(pt.get("samples") or 0) for pt in lacc.get("by_pick_type") or [])
            if total_samples >= MIN_SAMPLES:
                return "learned"
    except Exception as exc:
        logger.warning("_classify_competition: self_learner lookup failed for key=%s — defaulting to unclassified (%s)", key, exc)
    return "unclassified"


def apply_known_competition_context(doc: dict[str, Any]) -> dict[str, Any]:
    """Attach competition-special intelligence to any ordinary match document.

    Checks the TOP_30_COMPETITIONS catalogue first (fast, no DB), then falls
    back to a DB lookup for dynamically tracked competitions discovered via ingest.
    """
    event = doc.get("sofascore_event") if isinstance(doc.get("sofascore_event"), dict) else {}
    detail = doc.get("sofascore_detail") if isinstance(doc.get("sofascore_detail"), dict) else {}
    tournament = event.get("tournament") if isinstance(event.get("tournament"), dict) else {}
    tournament_id = tournament.get("id") or tournament.get("tournament_id") or doc.get("unique_tournament_id")
    name = str(tournament.get("name") or doc.get("tournament") or "")

    key: str | None = None
    comp_name: str = name

    if tournament_id is not None:
        entry = next((item for item in TOP_30_COMPETITIONS if str(item["unique_tournament_id"]) == str(tournament_id)), None)
        if entry:
            key, comp_name = entry["key"], entry["name"]

    if not key and tournament_id is not None:
        try:
            _init_db()
            with db_conn(timeout=5) as conn:
                init_competition_tables(conn)
                row = conn.execute(
                    "select key, name from competition_special_settings where unique_tournament_id = ? limit 1",
                    (int(tournament_id),),
                ).fetchone()
            if row:
                key, comp_name = row[0], row[1]
        except Exception:
            pass

    if not key:
        doc["known_competition"] = {"known": False, "provider": "sofascore", "tournament": name or None}
        return doc

    intelligence = _competition_intelligence_context(key, event, detail, doc)
    context = {
        "known": True,
        "provider": "sofascore",
        "key": key,
        "name": comp_name,
        "unique_tournament_id": tournament_id,
        "importance": _match_importance_context(key, event),
        "intelligence": intelligence,
        # See _classify_competition's own docstring: "known" above already
        # covers curated + any dynamically-tracked competition; this splits
        # that into "known" (curated) vs "learned" (earned trust from
        # graded history) vs "unclassified" (neither yet).
        "classification": _classify_competition(key, comp_name),
    }
    doc["known_competition"] = context
    doc["competition_special"] = {"key": key, "name": comp_name, "source": "known_competition_match"}
    doc["competition_intelligence"] = intelligence
    doc["competition_team_watchers"] = intelligence.get("team_watchers")
    doc["ai_team_watchers"] = intelligence.get("ai_team_watchers")
    doc["team_strength_context"] = intelligence.get("team_strength")
    doc["table_context"] = intelligence.get("table")

    # Attach recent competition round analysis if available (R14.1–R14.3).
    # Uses a lazy import to avoid circular import issues at module init time.
    try:
        from app.competition.competition_analyser import (
            get_latest_analysis,
            init_competition_analysis_table,
        )
        with db_conn(timeout=5) as conn:
            init_competition_analysis_table(conn)
            analysis = get_latest_analysis(key, conn)
        if analysis:
            generated_at = _parse_datetime(analysis.get("generated_at"))
            if generated_at is not None:
                age_days = (datetime.now(timezone.utc) - generated_at).days
                if age_days <= 7:
                    doc["competition_round_analysis"] = analysis
    except Exception:
        pass

    return doc


def _normalise_competition_name(value: str) -> str:
    return "".join(char for char in str(value or "").lower() if char.isalnum())


def list_top_competitions() -> list[dict[str, Any]]:
    """Return all tracked competitions with persisted enablement (catalogue + dynamic)."""
    _init_db()
    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        _ensure_catalogue_settings(conn)
        rows = conn.execute(
            "select * from competition_special_settings order by enabled desc, name asc"
        ).fetchall()
    return [_settings_row(row) for row in rows]


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
        create table if not exists competition_goal_stats (
            competition_key text not null,
            match_id        text not null,
            match_date      text,
            home_team       text,
            away_team       text,
            total_goals     integer not null default 0,
            first_half_goals  integer not null default 0,
            second_half_goals integer not null default 0,
            band_1_10   integer not null default 0,
            band_11_20  integer not null default 0,
            band_21_30  integer not null default 0,
            band_31_40  integer not null default 0,
            band_41_45  integer not null default 0,
            band_46_55  integer not null default 0,
            band_56_65  integer not null default 0,
            band_66_75  integer not null default 0,
            band_76_85  integer not null default 0,
            band_86_90  integer not null default 0,
            first_goal_minute integer,
            avg_interval_minutes real,
            goal_minutes_json text not null default '[]',
            updated_at text not null default current_timestamp,
            primary key (competition_key, match_id)
        )
        """
    )
    conn.execute("create index if not exists idx_comp_goal_stats_key on competition_goal_stats(competition_key, match_date desc)")
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
        create table if not exists competition_special_migrations (
            name text primary key,
            applied_at text not null default current_timestamp
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
            sportybet_match_id text,
            primary key (competition_key, match_id)
        )
        """
    )
    _ensure_column(conn, "competition_special_buffer", "importance_context_json", "text not null default '{}'")
    _ensure_column(conn, "competition_special_buffer", "sportybet_match_id", "text")
    conn.execute("create index if not exists idx_comp_special_date on competition_special_buffer(competition_key, match_date)")
    conn.execute("create index if not exists idx_comp_special_start on competition_special_buffer(competition_key, start_time)")
    conn.execute("create index if not exists idx_comp_special_sporty on competition_special_buffer(sportybet_match_id)")
    conn.execute(
        """
        create table if not exists competition_team_watchers (
            competition_key text not null,
            team_id text not null,
            team_name text not null,
            analyst_name text not null,
            profile_json text not null default '{}',
            match_count integer not null default 0,
            last_match_id text,
            last_brief text,
            updated_at text not null default current_timestamp,
            primary key (competition_key, team_id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists competition_team_watcher_matches (
            competition_key text not null,
            team_id text not null,
            match_id text not null,
            match_date text,
            opponent text,
            venue text,
            goals_for integer,
            goals_against integer,
            result text,
            status text,
            prediction_json text,
            brief text,
            raw_match_json text not null default '{}',
            created_at text not null default current_timestamp,
            primary key (competition_key, team_id, match_id)
        )
        """
    )
    conn.execute("create index if not exists idx_team_watchers_key on competition_team_watchers(competition_key, updated_at desc)")
    conn.execute("create index if not exists idx_team_watcher_matches_team on competition_team_watcher_matches(competition_key, team_id, match_date desc)")


def get_competition_settings(key: str = "world-cup-2026") -> dict[str, Any]:
    _init_db()
    with db_conn() as conn:
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
    with db_conn() as conn:
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
    from app.data_clients.sofascore_client import fetch_scheduled_events

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
    with db_conn() as conn:
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


def list_competition_buffer(key: str = "world-cup-2026", limit: int = 200, skip_mirror: bool = False, lite: bool = False) -> dict[str, Any]:
    """skip_mirror=True skips the ensure_competition_main_buffer() write-mirror
    pass below -- that mirror already runs every ~5 min via the background
    competition_special job, so a single-competition detail view calling this
    normally (skip_mirror=False, the default) gets a cheap freshness nudge.
    But competition_dashboard_summary() calls this once per tracked
    competition on every dashboard load; without skip_mirror that meant every
    page view re-wrote every buffered match for every competition back into
    match_buffer -- ~1600 redundant writes per load, the actual reason that
    endpoint was slow (see composite.py's /competition-special/dashboard).

    lite=True builds each row via _buffer_row(lite=True) -- the minimal
    fields _competition_summary() actually reads, skipping the expensive
    full match_facts enrichment for unmerged rows -- and omits `matches`
    from the response entirely, since competition_dashboard_summary() only
    ever uses `count`/`summary` and discards the full match list. This is
    the other half of the dashboard-slowness fix: skip_mirror alone stopped
    the redundant writes, but building a fully enriched doc for every one of
    up to ~1500 matches just to produce a count was still slow enough to
    stall the whole app for the duration of the request."""
    _init_db()
    if not skip_mirror:
        ensure_competition_main_buffer(key)
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        _ensure_catalogue_settings(conn)
        rows = conn.execute(
            """
            select csb.*,
                   mb.match_id as main_match_id,
                   mb.raw_enriched as main_raw_enriched,
                   mb.enriched_at as main_enriched_at
            from competition_special_buffer csb
            left join match_buffer mb
              on mb.match_id = ('sofascore:' || csb.match_id)
            where csb.competition_key = ?
            order by csb.start_time asc
            limit ?
            """,
            (key, limit),
        ).fetchall()
    matches = [_buffer_row(row, lite=lite) for row in rows]
    result = {
        "status": "success",
        "competition": get_competition_settings(key),
        "count": len(matches),
        "summary": _competition_summary(matches),
    }
    if not lite:
        result["matches"] = matches
    return result


def competition_status(key: str = "world-cup-2026", skip_mirror: bool = False) -> dict[str, Any]:
    # skip_mirror: the dashboard summary (competition_dashboard_summary) calls
    # this once per enabled competition -- ensure_competition_main_buffer() is
    # a WRITE (re-mirrors every buffered match for the competition into
    # match_buffer), not a read, and it already runs on its own ~5 min
    # background cycle. Doing it again here, per competition, per dashboard
    # load, was the second (and bigger) copy of the same redundant-write
    # problem list_competition_buffer's skip_mirror already fixed -- with the
    # dashboard now fanning out over competitions in parallel threads, 31
    # of these firing at once is what was causing "database is locked"
    # errors and 150+ second load times. Single-competition detail views
    # (frontend.py) don't pass this, so they keep the original eager refresh.
    _init_db()
    if not skip_mirror:
        ensure_competition_main_buffer(key)
    with db_conn() as conn:
        init_competition_tables(conn)
        _ensure_catalogue_settings(conn)
        row = conn.execute(
            """
            select
              count(*) as total,
              sum(case when coalesce(mb.raw_enriched, csb.raw_detail) is not null then 1 else 0 end) as enriched,
              sum(case when coalesce(json_extract(mb.raw_enriched, '$.prediction'), csb.prediction_json) is not null then 1 else 0 end) as predicted,
              min(csb.match_date) as first_match_date,
              max(csb.match_date) as last_match_date,
              max(coalesce(mb.enriched_at, csb.enriched_at)) as last_enriched_at,
              max(csb.predicted_at) as last_predicted_at
            from competition_special_buffer csb
            left join match_buffer mb
              on mb.match_id = ('sofascore:' || csb.match_id)
            where csb.competition_key = ?
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


def list_team_watchers(key: str = "world-cup-2026", limit: int = 80) -> dict[str, Any]:
    _init_db()
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        rows = conn.execute(
            """
            select *
            from competition_team_watchers
            where competition_key = ?
            order by match_count desc, updated_at desc, team_name asc
            limit ?
            """,
            (key, limit),
        ).fetchall()
    watchers = [_watcher_row(row) for row in rows]
    return {"status": "success", "competition_key": key, "count": len(watchers), "watchers": watchers}


def get_team_watcher(key: str, team_id: str) -> dict[str, Any]:
    _init_db()
    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        row = conn.execute(
            """
            select *
            from competition_team_watchers
            where competition_key = ? and team_id = ?
            """,
            (key, str(team_id)),
        ).fetchone()
        matches = conn.execute(
            """
            select *
            from competition_team_watcher_matches
            where competition_key = ? and team_id = ?
            order by match_date desc, created_at desc
            limit 20
            """,
            (key, str(team_id)),
        ).fetchall()
    if row is None:
        return {"status": "not_found", "competition_key": key, "team_id": str(team_id)}
    return {
        "status": "success",
        "competition_key": key,
        "watcher": _watcher_row(row),
        "matches": [_watcher_match_row(match) for match in matches],
    }


def enrich_predict_competition(key: str = "world-cup-2026", limit: int = 12, allow_repeat: bool = False) -> dict[str, Any]:
    from app.enrichment.enriched_prediction import prediction_readiness
    from app.utils.prediction_flow import apply_prediction_state
    from app.data_clients.sofascore_client import fetch_event_detail, fetch_event

    _init_db()
    mirrored = ensure_competition_main_buffer(key)
    processed = enriched = predicted = deferred = errors = 0
    items: list[dict[str, Any]] = []
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        rows = conn.execute(
            """
            select * from competition_special_buffer
            where competition_key = ?
              and sportybet_match_id is null
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
        # sportybet_match_id is null: once a tracked-competition match is
        # linked to its real SportyBet match (sort_enriched_doc_into_competition,
        # called from the normal ingest path in buffer.py, sets this the
        # moment SofaScore data resolves for that SportyBet match), the
        # SportyBet-side match_buffer row becomes the canonical, fully merged
        # record for that fixture -- and it gets its own prediction from the
        # normal deterministic+LLM ensemble, which sees strictly more data
        # (real odds, SportyBet history) than this SofaScore-only path ever
        # does. Before this filter, this function kept generating and
        # refreshing its own independent prediction for every tracked match
        # forever, oblivious to that link -- so a match like Premier
        # League's Newcastle vs Bournemouth ended up with two live,
        # differently-confidenced predictions under two different match ids
        # (competition_special:premier-league's "sofascore:X" and the real
        # "sr:match:Y"), and both could get pulled into the same bet-builder
        # slip as if they were different games. Once linked, this loop now
        # leaves the row alone entirely -- the merged match_buffer row is
        # already being kept fresh by the normal ingest/enrichment pipeline,
        # so there is nothing left for this SofaScore-only path to usefully
        # do for it.

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
                match_id=_main_buffer_match_id(fresh_event.get("id")),
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
    with db_conn() as conn:
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
    """Sync fixtures, mirror to main buffer, enrich and predict — always runs.
    The competition_analysis pipeline toggle controls the separate weekly AI analysis job.
    """
    settings = get_competition_settings(key)
    if not settings.get("enabled"):
        return {"status": "idle", "competition": key, "enabled": False}
    configured_start = _parse_date(settings.get("start_date") or date.today().isoformat())
    configured_end_raw = settings.get("end_date") or ""
    configured_end = _parse_date(configured_end_raw) if configured_end_raw else date.today() + timedelta(days=14)
    loaded_until = _sync_cursor(key) or _loaded_until(key)
    if loaded_until and loaded_until < configured_end:
        window_start = loaded_until + timedelta(days=1)
    else:
        window_start = max(date.today(), configured_start)
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
    """Run the full cycle for all enabled competitions — always sorts, enriches, and predicts.
    The competition_analysis pipeline toggle controls the separate weekly AI analysis job.
    """
    enabled = [item for item in list_top_competitions() if item.get("enabled")][:max(1, limit)]
    due = [item for item in enabled if _competition_cycle_due(item)]
    results = []
    for item in due:
        result = run_competition_special_cycle(item["key"])
        if result.get("status") == "success":
            _mark_competition_cycle(item["key"])
        results.append(result)
    return {
        "status": "success",
        "processed": len(results),
        "enabled": len(enabled),
        "skipped_not_due": len(enabled) - len(due),
        "competitions": results,
    }


def refresh_competition_context(key: str = "world-cup-2026", limit: int = 12) -> dict[str, Any]:
    """Continuously refresh table/team-strength/odds context for competition rows."""
    from app.data_clients.sofascore_client import fetch_event, fetch_event_detail

    _init_db()
    refreshed = errors = 0
    items: list[dict[str, Any]] = []
    with db_conn(timeout=30) as conn:
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


def sort_enriched_doc_into_competition(doc: dict[str, Any]) -> None:
    """Sort any ingest-enriched doc into competition tracking using the existing main buffer match_id.

    Writes to competition_special_buffer for competition-scoped analysis queries and
    registers both teams in competition_team_watchers — but does NOT create a separate
    prefixed entry in the main buffer. The SportyBet match already in the main buffer
    is the canonical record; competition context is attached to it via raw_enriched.
    """
    try:
        event = doc.get("sofascore_event") if isinstance(doc.get("sofascore_event"), dict) else {}
        if not event:
            return
        tournament = event.get("tournament") if isinstance(event.get("tournament"), dict) else {}
        tournament_id = tournament.get("id")
        if tournament_id is None:
            return
        try:
            tournament_id = int(tournament_id)
        except (TypeError, ValueError):
            return

        # Resolve the SAME key a curated tracked competition already uses
        # (e.g. "premier-league"), not a freshly-generated dynamic slug like
        # "premier-league-17". _tournament_key() used to be called
        # unconditionally here, so every SportyBet-side match got its
        # sportybet_match_id link written onto a phantom dynamic-key row
        # that enrich_predict_competition("premier-league", ...) never
        # reads from -- the real curated row never learned the match was
        # already covered by SportyBet, so it kept generating its own
        # independent prediction for the same real match forever (this is
        # exactly why Newcastle vs Bournemouth had two live predictions
        # under two different match ids). Mirrors the catalogue-first,
        # DB-fallback-second key resolution apply_known_competition_context()
        # already uses elsewhere in this file for the same tournament_id.
        key = None
        catalogue_entry = next(
            (item for item in TOP_30_COMPETITIONS if str(item["unique_tournament_id"]) == str(tournament_id)),
            None,
        )
        if catalogue_entry:
            key = catalogue_entry["key"]
        if not key:
            try:
                with db_conn(timeout=5) as lookup_conn:
                    init_competition_tables(lookup_conn)
                    existing_row = lookup_conn.execute(
                        "select key from competition_special_settings where unique_tournament_id = ? limit 1",
                        (tournament_id,),
                    ).fetchone()
                if existing_row:
                    key = existing_row[0]
            except Exception:
                pass
        if not key:
            key = _tournament_key(tournament_id, tournament)

        name = str(tournament.get("name") or key)
        match_date = doc.get("match_date") or _event_match_date(event, date.today().isoformat())
        sportybet_match_id = _real_sportybet_match_id(doc)
        _init_db()
        with db_conn() as conn:
            init_competition_tables(conn)
            _ensure_dynamic_competition(conn, key, name, tournament_id)
            _upsert_competition_index(conn, key, event, match_date, sportybet_match_id)
            conn.commit()
    except Exception:
        pass


def _tournament_key(tournament_id: int, tournament: dict[str, Any]) -> str:
    """Derive a stable slug key from tournament name + id."""
    name = str(tournament.get("name") or "").strip()
    slug = "-".join(_normalise_competition_name(name).split()) if name else ""
    return f"{slug}-{tournament_id}" if slug else f"tournament-{tournament_id}"


def _ensure_dynamic_competition(conn: sqlite3.Connection, key: str, name: str, tournament_id: int) -> None:
    """Upsert a competition_special_settings row for any dynamically discovered competition.

    IMPORTANT: enabled defaults to 0 here, not 1. A previous version of this
    function auto-enabled every competition it had ever seen a single match
    from, which meant the always-on job_competition_special scheduler job
    (see app/scheduling/scheduler.py) ended up running the expensive
    day-by-day SofaScore fixture-sync (sync_competition_fixtures) for ~297
    leagues instead of the ~30 the TOP_30_COMPETITIONS catalogue actually
    curates for — including things like a Swedish district-level 5th tier
    league pulling more ingestion volume than Bundesliga. Being "known" (row
    exists here, so apply_known_competition_context() and the
    competition_intelligence signal in enriched_prediction.py can tag/score
    the match) is deliberately kept separate from being "enabled" (runs the
    always-on fixture-sync job) -- the DB lookup in
    apply_known_competition_context() does not filter by enabled, so
    creating this row still gets the match tagged even while disabled. An
    operator can still deliberately flip a specific dynamic competition on
    via update_competition_settings() / the settings UI.

    Also guards against creating a second row for a tournament_id that
    already has a settings row under a different key (this happened for
    real: 'championship' the curated key and 'championship-18' a dynamic
    duplicate both pointed at unique_tournament_id 18) -- if any row for
    this tournament_id already exists, skip inserting a duplicate entirely.
    """
    existing = conn.execute(
        "select key from competition_special_settings where unique_tournament_id = ? limit 1",
        (tournament_id,),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """
        insert into competition_special_settings
            (key, name, enabled, unique_tournament_id, season_id, start_date, end_date, metadata_json)
        values (?, ?, 0, ?, null, '', '', ?)
        on conflict(key) do nothing
        """,
        (key, name, tournament_id, json.dumps({"source": "ingest", "mode": "dynamic"})),
    )


def disable_dynamic_competitions() -> dict[str, Any]:
    """
    One-time (idempotent, safe to re-run) cleanup for the auto-enable bug
    fixed in _ensure_dynamic_competition above: disables every currently
    ENABLED competition_special_settings row that was dynamically
    auto-registered (metadata_json.mode == "dynamic"), stopping their
    day-by-day SofaScore fixture-sync immediately. Does not delete any rows
    or history, and does not touch the curated TOP_30_COMPETITIONS /
    World Cup rows (metadata_json.mode == "competition_special") or any row
    an operator has manually configured with different metadata. Reversible
    per-key via update_competition_settings().
    """
    _init_db()
    with db_conn() as conn:
        init_competition_tables(conn)
        before = conn.execute(
            "select count(*) from competition_special_settings where enabled = 1"
        ).fetchone()[0]
        cur = conn.execute(
            """
            update competition_special_settings
            set enabled = 0, updated_at = current_timestamp
            where enabled = 1
              and json_extract(metadata_json, '$.mode') = 'dynamic'
            """
        )
        disabled = cur.rowcount
        after = conn.execute(
            "select count(*) from competition_special_settings where enabled = 1"
        ).fetchone()[0]
        conn.commit()
    return {
        "status": "success",
        "enabled_before": before,
        "disabled": disabled,
        "enabled_after": after,
    }


def _ensure_catalogue_settings(conn: sqlite3.Connection) -> None:
    """Seed catalogue rows once, preserving all operator configuration."""
    catalogue = (DEFAULT_WORLD_CUP, *TOP_30_COMPETITIONS)
    # This used to run the full 31-row INSERT loop unconditionally on every
    # single call -- get_competition_settings/competition_status/
    # competition_dashboard_summary all call this on every request, and the
    # dashboard fans out across ~31 competitions on parallel threads, so one
    # page load meant dozens of write transactions all fighting for SQLite's
    # single writer lock at once (on top of the background scheduler's own
    # writes running concurrently). That write pressure -- not the two
    # redundant buffer-mirror calls already fixed above -- turned out to be
    # the real remaining cause of "database is locked" errors on the
    # dashboard. A cheap read-only count lets the overwhelmingly common case
    # (catalogue already seeded) skip every INSERT statement.
    (existing_count,) = conn.execute(
        "select count(*) from competition_special_settings where key in (%s)"
        % ",".join("?" for _ in catalogue),
        [entry["key"] for entry in catalogue],
    ).fetchone()
    if existing_count < len(catalogue):
        for default in catalogue:
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
    # One-time historic catalogue-ID correction. This used to run on every
    # call (every GET to the competitions list/settings/status endpoints),
    # and _purge_wrong_competition_key_rows() scans and json.loads()s every
    # historical competition_special_buffer row for two leagues to do it —
    # cost that grows with match history and was the root cause of the
    # competitions list "loading eventually but taking a long time." A
    # migration marker now makes this run once, ever, per database.
    _MIGRATION_NAME = "purge_wrong_competition_key_rows_v1"
    already_applied = conn.execute(
        "select 1 from competition_special_migrations where name = ?",
        (_MIGRATION_NAME,),
    ).fetchone()
    if not already_applied:
        # Correct two historic catalogue IDs without overwriting any other
        # operator configuration. MLS was previously pointed at 955, while
        # 242 is its SofaScore unique-tournament ID; Brasileirão Série A is
        # 325.
        conn.execute("""update competition_special_settings
                        set unique_tournament_id = 242, updated_at = current_timestamp
                        where key = 'mls' and unique_tournament_id = 955""")
        conn.execute("""update competition_special_settings
                        set unique_tournament_id = 325, updated_at = current_timestamp
                        where key = 'brasileirao' and unique_tournament_id = 242""")
        conn.execute("""update competition_special_settings
                        set unique_tournament_id = 52, updated_at = current_timestamp
                        where key = 'super-lig' and unique_tournament_id = 325""")
        conn.execute("""update competition_special_settings
                        set unique_tournament_id = 18,
                            metadata_json = json_remove(coalesce(metadata_json, '{}'), '$.last_sync_end_date'),
                            updated_at = current_timestamp
                        where key = 'championship' and unique_tournament_id = 37""")
        conn.execute("""update competition_special_settings
                        set unique_tournament_id = 37,
                            metadata_json = json_remove(coalesce(metadata_json, '{}'), '$.last_sync_end_date'),
                            updated_at = current_timestamp
                        where key = 'eredivisie' and unique_tournament_id = 44""")
        _purge_wrong_competition_key_rows(conn, "championship", 18)
        _purge_wrong_competition_key_rows(conn, "eredivisie", 37)
        conn.execute(
            "insert into competition_special_migrations (name) values (?) on conflict(name) do nothing",
            (_MIGRATION_NAME,),
        )


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


def _purge_wrong_competition_key_rows(conn: sqlite3.Connection, key: str, expected_tournament_id: int) -> int:
    """Remove regenerable competition rows stored under a key with the wrong tournament ID."""
    rows = conn.execute(
        "select match_id, raw_event from competition_special_buffer where competition_key = ?",
        (key,),
    ).fetchall()
    wrong_ids: list[str] = []
    for row in rows:
        try:
            raw_event = row["raw_event"] if hasattr(row, "keys") else row[1]
            event = json.loads(raw_event or "{}")
        except Exception:
            continue
        actual_id = _event_unique_tournament_id(event)
        if actual_id is not None and actual_id != expected_tournament_id:
            wrong_ids.append(str(row["match_id"]))
    if not wrong_ids:
        return 0
    placeholders = ",".join("?" for _ in wrong_ids)
    removed = conn.execute(
        f"delete from competition_special_buffer where competition_key = ? and match_id in ({placeholders})",
        (key, *wrong_ids),
    ).rowcount
    prefixed = [f"competition:{key}:{item}" for item in wrong_ids]
    main_placeholders = ",".join("?" for _ in prefixed)
    conn.execute(f"delete from match_buffer where match_id in ({main_placeholders})", prefixed)
    return int(removed or 0)


def _loaded_until(key: str) -> date | None:
    try:
        _init_db()
        with db_conn() as conn:
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
    with db_conn(timeout=30) as conn:
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


def _upsert_competition_index(
    conn: sqlite3.Connection,
    key: str,
    event: dict[str, Any],
    match_date: str,
    sportybet_match_id: str,
) -> None:
    """Write a competition_special_buffer row referencing the existing main buffer match_id.

    Unlike _upsert_competition_event, this does NOT mirror to the main buffer —
    the SportyBet match is already there. The buffer row stores the sportybet_match_id
    so competition analysis queries can join back to the canonical record.
    """
    status = event.get("status") or {}
    score = event.get("score") or {}
    tournament = event.get("tournament") or {}
    event_match_date = _event_match_date(event, match_date)
    importance = _match_importance_context(key, event)
    conn.execute(
        """
        insert into competition_special_buffer (
            competition_key, match_id, match_date, group_name, round_name, name, start_time,
            status, score_home, score_away, raw_event, importance_context_json, sportybet_match_id
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            importance_context_json = excluded.importance_context_json,
            sportybet_match_id = coalesce(excluded.sportybet_match_id, competition_special_buffer.sportybet_match_id)
        """,
        (
            key,
            str(event.get("id")),
            event_match_date,
            tournament.get("name"),
            str(event.get("round") or ""),
            event.get("name"),
            int((event.get("start_timestamp") or 0) * 1000),
            str(status.get("type") or status.get("description") or "").lower(),
            _score_value(score.get("home")),
            _score_value(score.get("away")),
            json.dumps(event),
            json.dumps(importance),
            sportybet_match_id or None,
        ),
    )
    _register_competition_teams(conn, key, event)


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
            str(status.get("type") or status.get("description") or "").lower(),
            _score_value(score.get("home")),
            _score_value(score.get("away")),
            json.dumps(event),
            json.dumps(importance),
        ),
    )
    _register_competition_teams(conn, key, event)
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
    _update_team_watchers_for_match(key, event, detail, prediction)
    _update_competition_goal_stats(key, event, detail)
    intelligence = _competition_intelligence_context(key, event, detail, doc)
    doc["competition_intelligence"] = intelligence
    doc["team_strength_context"] = intelligence.get("team_strength")
    doc["table_context"] = intelligence.get("table")
    try:
        from app.market.market import snapshot_odds, get_movement
        snapshot_odds(doc)
        doc["odds_movement"] = get_movement(doc.get("sportybet_id"))
        intelligence["odds_movement"] = doc["odds_movement"]
    except Exception as exc:
        intelligence["odds_movement"] = {"error": str(exc)}
    with db_conn(timeout=30) as conn:
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


def backfill_category_labels() -> dict[str, Any]:
    """One-time cleanup for the "World" category bug fixed alongside this
    function: every match_buffer row mirrored in by competition_special
    (sofascore_only=1) before the fix was permanently stamped with the
    literal string "World" instead of its real country, because
    _competition_doc()/_competition_raw_sporty() hardcoded it. New/refreshed
    matches now get the real country via _event_category_name(); this
    backfill relabels the ones already written to disk using the same
    event data already stored in their own raw_enriched/raw_sporty JSON --
    no re-fetch from SofaScore needed. Only ever touches sofascore_only=1
    rows, so a correctly-labeled row from the normal SportyBet ingest path
    can never be affected.

    Run via the app's own db_conn() (in-process, same machine as the DB
    file) rather than from an external script -- earlier attempts to do
    this write from outside the running app hit repeated "disk I/O error"
    failures against the live WAL file and never completed.
    """
    _init_db()
    updated = 0
    skipped = 0
    candidates = 0
    with db_conn(timeout=60) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select match_id, raw_enriched, raw_sporty
            from match_buffer
            where sofascore_only = 1
              and (
                json_extract(raw_enriched, '$.category') = 'World'
                or json_extract(raw_sporty, '$.category') = 'World'
              )
            """
        ).fetchall()
        candidates = len(rows)

        for row in rows:
            match_id = row["match_id"]
            try:
                enriched = json.loads(row["raw_enriched"]) if row["raw_enriched"] else None
            except Exception:
                enriched = None
            try:
                sporty = json.loads(row["raw_sporty"]) if row["raw_sporty"] else None
            except Exception:
                sporty = None

            event_for_lookup = None
            if isinstance(enriched, dict):
                event_for_lookup = enriched.get("sofascore_event") or enriched.get("raw_sofascore_event")
            new_category = _event_category_name(event_for_lookup) if isinstance(event_for_lookup, dict) else None

            if not new_category or new_category == "World":
                skipped += 1
                continue

            changed = False
            if isinstance(enriched, dict) and enriched.get("category") != new_category:
                enriched["category"] = new_category
                if isinstance(enriched.get("raw_sporty"), dict):
                    enriched["raw_sporty"]["category"] = new_category
                changed = True
            if isinstance(sporty, dict) and sporty.get("category") != new_category:
                sporty["category"] = new_category
                changed = True

            if not changed:
                skipped += 1
                continue

            conn.execute(
                "update match_buffer set raw_enriched = ?, raw_sporty = ? where match_id = ?",
                (
                    json.dumps(enriched) if enriched is not None else row["raw_enriched"],
                    json.dumps(sporty) if sporty is not None else row["raw_sporty"],
                    match_id,
                ),
            )
            updated += 1

    return {"status": "success", "candidates": candidates, "updated": updated, "skipped": skipped}


def cleanup_duplicate_competition_predictions(dry_run: bool = True) -> dict[str, Any]:
    """One-time cleanup for the duplicate-prediction bug fixed alongside
    sort_enriched_doc_into_competition()'s key resolution: before that fix,
    a tracked-competition match (e.g. Premier League) that also has a real
    SportyBet match ended up with two independent, live predictions under
    two different match ids -- one from this competition_special pipeline
    (source LIKE 'competition_special:%'), one from the normal
    deterministic+LLM ensemble -- and both could be pulled into the same
    bet-builder slip as if they were different games.

    This removes the stale competition_special-side prediction_history rows
    for matches that are CONFIRMED already covered by a properly merged
    SportyBet match (match_buffer.data_source = 'both' for the same
    sofascore_id) -- the merged match's own prediction is strictly
    better-informed (real odds, more history) and is what should be used
    going forward. Only ever touches UNGRADED rows (graded_at is null) --
    anything already graded is real historical/learning data and is never
    touched, matching how every other cleanup this session has treated
    graded history. dry_run=true (default) makes no changes, just reports
    what would be removed.
    """
    _init_db()
    with db_conn(timeout=60) as conn:
        conn.row_factory = sqlite3.Row
        merged_sofascore_ids = {
            str(row[0])
            for row in conn.execute(
                "select distinct sofascore_id from match_buffer "
                "where data_source = 'both' and sofascore_id is not null and sofascore_id != ''"
            ).fetchall()
        }
        if not merged_sofascore_ids:
            return {"status": "success", "candidates": 0, "removed": 0, "dry_run": dry_run, "examples": []}

        stale_rows = conn.execute(
            """
            select id, match_id, match_name, source, sofascore_id, created_at
            from prediction_history
            where source like 'competition_special:%'
              and graded_at is null
              and sofascore_id is not null and sofascore_id != ''
            """
        ).fetchall()

        candidates = [
            dict(row) for row in stale_rows
            if str(row["sofascore_id"]) in merged_sofascore_ids
        ]

        if not dry_run and candidates:
            ids = [c["id"] for c in candidates]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(f"delete from prediction_history where id in ({placeholders})", ids)

    return {
        "status": "success",
        "candidates": len(candidates),
        "removed": len(candidates) if not dry_run else 0,
        "dry_run": dry_run,
        "examples": [
            {"match_id": c["match_id"], "match_name": c["match_name"], "source": c["source"], "created_at": c["created_at"]}
            for c in candidates[:15]
        ],
    }


def _event_category_name(event: dict[str, Any]) -> str:
    """Real country/category for a SofaScore event (e.g. "England" for the
    Premier League) -- read the same way app/data_clients/sofa_pipeline.py
    already does it for the normal ingest path. Falls back to "World" only
    when SofaScore genuinely has no category (true international
    competitions) or the field is missing.

    This used to be hardcoded to the literal string "World" everywhere a
    tracked-competition match doc got built, regardless of the real country
    -- so a competition_special-tracked Premier League match and a normal
    SportyBet-ingested Premier League match carried different `category`
    values for the exact same competition. The frontend's
    parseCountryLeague() (football_frontend/src/pages/main/country/page.tsx)
    groups matches by `category` first, so this showed up as two separate
    sections -- "World / Premier League" and "England / Premier League" --
    for one real league.
    """
    try:
        raw_event = event.get("raw_event") or {}
        category = (raw_event.get("tournament") or {}).get("category") or {}
        name = category.get("name")
        return str(name) if name else "World"
    except Exception:
        return "World"


def _competition_doc(key: str, event: dict[str, Any], detail: dict[str, Any], *, skip_facts_enrichment: bool = False) -> dict[str, Any]:
    start = event.get("start_timestamp") or detail.get("start_timestamp")
    match_date = datetime.fromtimestamp(float(start), tz=timezone.utc).date().isoformat() if start else date.today().isoformat()
    markets = _special_markets(detail)
    importance = _match_importance_context(key, event)
    intelligence = _competition_intelligence_context(key, event, detail, {})
    main_id = _main_buffer_match_id(event.get("id"))
    legacy_id = _prefixed_match_id(key, event.get("id"))
    category_name = _event_category_name(event)
    doc = {
        "id": main_id,
        "match_id": main_id,
        "competition_match_id": str(event.get("id") or ""),
        "legacy_competition_id": legacy_id,
        "sofascore_id": event.get("id"),
        # Explicitly None until job_reconcile_competition_sporty() merges a
        # real SportyBet event. Leaving this key absent caused downstream
        # fallback chains (doc.get("sportybet_id") or doc.get("id")) to
        # resolve to the "sofascore:{id}" match_id and store it as the
        # sportybet_id in prediction_history and booking requests.
        "sportybet_id": None,
        "sofascore_match_status": "matched",
        "data_source": "sofascore",
        "sofascore_only": True,
        "name": event.get("name"),
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "tournament": (event.get("tournament") or {}).get("name"),
        "category": category_name,
        "match_date": match_date,
        "start_time": int(float(start or 0) * 1000) if start else None,
        "period": (event.get("status") or {}).get("description") or "Not started",
        "score": event.get("score") or {},
        "sofascore_event": event,
        "raw_sofascore_event": event.get("raw_event") or event,
        "sofascore_detail": detail,
        "sportybet_detail": None,
        "sportybet_data_status": "not_applicable",
        "raw_sporty": {
            "id": main_id,
            "legacy_id": legacy_id,
            "name": event.get("name"),
            "home_team": event.get("home_team"),
            "away_team": event.get("away_team"),
            "tournament": (event.get("tournament") or {}).get("name"),
            "category": category_name,
            "period": (event.get("status") or {}).get("description") or "Not started",
            "start_time": int(float(start or 0) * 1000) if start else None,
            "markets": markets,
            "competition_special_proxy": True,
            "source": "sofascore",
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
                "markets": False,
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
    # competition_special never used to go through match_facts' enrichment at
    # all (it computes goal timing itself via the richer band-based
    # app.utils.goal_timing module -- that part is fine and untouched by
    # this), so tracked-competition matches were silently missing
    # half_time_score, normalized live_statistics, and
    # provider_live_capabilities that every SportyBet-ingested match gets.
    # This brings competition-tracked matches (predictions AND the mirrored
    # match_buffer row's raw_enriched) up to the same baseline.
    #
    # skip_facts_enrichment: the competition dashboard summary rebuilds this
    # doc once per unmerged match just to classify its live/finished state
    # and count it -- it never reads half_time_score, live_statistics, or
    # goal_timing, so paying for this normalization there (x up to ~1500
    # matches per dashboard load) was pure waste and a big share of why that
    # endpoint could take minutes and stall the whole app. See
    # competition_dashboard_summary()/list_competition_buffer(lite=True).
    if not skip_facts_enrichment:
        try:
            from app.match_facts import enrich_match_facts
            doc = enrich_match_facts(doc)
        except Exception as exc:
            from app.utils.health_counters import record_health_event
            record_health_event("competition_special", "match_facts_enrichment_error", exc)
    return doc


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


def _buffer_row(row: sqlite3.Row, *, lite: bool = False) -> dict[str, Any]:
    main_doc = _json_row_value(row, "main_raw_enriched")
    prediction = (main_doc or {}).get("prediction") or (json.loads(row["prediction_json"] or "null") if row["prediction_json"] else None)
    importance = json.loads(row["importance_context_json"] or "{}")

    if lite:
        # Dashboard-summary path (competition_dashboard_summary via
        # list_competition_buffer(..., lite=True)): _competition_summary()
        # only ever reads match_state/enriched/predicted/importance_context/
        # group off of this dict. Building the full event/prediction/
        # competition_intelligence payload and -- worse -- running every
        # unmerged match through _competition_doc()'s full enrichment
        # (goal timing, live-stat normalization, none of which the summary
        # reads) for up to ~1500 matches per dashboard load is what made
        # this endpoint take minutes and freeze the whole app meanwhile.
        # When the row is already merged into match_buffer (main_doc
        # present, the common case) we can classify state straight off that
        # without touching raw_event/raw_detail at all.
        if main_doc:
            doc = main_doc
        else:
            event = json.loads(row["raw_event"] or "{}")
            detail = json.loads(row["raw_detail"] or "null") if row["raw_detail"] else None
            doc = (
                _competition_doc(row["competition_key"], event, detail or {}, skip_facts_enrichment=True)
                if detail else {"sofascore_event": event, "period": row["status"], "start_time": row["start_time"]}
            )
        state = classify_match_state(doc)
        return {
            "competition_key": row["competition_key"],
            "match_id": str(_row_get(row, "main_match_id") or _main_buffer_match_id(row["match_id"])),
            "group": row["group_name"],
            "match_state": state,
            "enriched": bool(main_doc or row["raw_detail"]),
            "predicted": bool(prediction),
            "importance_context": importance,
        }

    event = json.loads(row["raw_event"] or "{}")
    detail = json.loads(row["raw_detail"] or "null") if row["raw_detail"] else None
    doc = main_doc or (_competition_doc(row["competition_key"], event, detail or {}) if detail else {"sofascore_event": event, "period": row["status"], "start_time": row["start_time"]})
    intelligence = (detail or {}).get("competition_intelligence") if isinstance(detail, dict) else None
    if not intelligence:
        intelligence = doc.get("competition_intelligence") if isinstance(doc, dict) else {}
    state = classify_match_state(doc)
    return {
        "competition_key": row["competition_key"],
        "match_id": str(_row_get(row, "main_match_id") or _main_buffer_match_id(row["match_id"])),
        "legacy_match_id": _prefixed_match_id(row["competition_key"], row["match_id"]),
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
        "enriched": bool(main_doc or row["raw_detail"]),
        "predicted": bool(prediction),
        "enriched_at": _row_get(row, "main_enriched_at") or row["enriched_at"],
        "predicted_at": row["predicted_at"],
        "prediction": prediction,
        "readiness": (detail or {}).get("prediction_readiness") if isinstance(detail, dict) else None,
        "importance_context": importance,
        "competition_intelligence": intelligence,
        "event": event,
    }


def _row_get(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


def _json_row_value(row: sqlite3.Row, key: str) -> dict[str, Any] | None:
    value = _row_get(row, key)
    if not value:
        return None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _watcher_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        profile = json.loads(row["profile_json"] or "{}")
    except Exception:
        profile = {}
    return {
        "competition_key": row["competition_key"],
        "team_id": row["team_id"],
        "team_name": row["team_name"],
        "detail_path": f"/competition/{row['competition_key']}/team/{row['team_id']}",
        "api_path": f"/competition-special/{row['competition_key']}/team-watchers/{row['team_id']}",
        "analyst_name": row["analyst_name"],
        "profile": profile,
        "match_count": row["match_count"],
        "last_match_id": row["last_match_id"],
        "last_brief": row["last_brief"],
        "updated_at": row["updated_at"],
    }


def _watcher_match_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        prediction = json.loads(row["prediction_json"] or "null") if row["prediction_json"] else None
    except Exception:
        prediction = None
    try:
        raw_match = json.loads(row["raw_match_json"] or "{}")
    except Exception:
        raw_match = {}
    return {
        "match_id": row["match_id"],
        "match_date": row["match_date"],
        "opponent": row["opponent"],
        "venue": row["venue"],
        "goals_for": row["goals_for"],
        "goals_against": row["goals_against"],
        "result": row["result"],
        "status": row["status"],
        "prediction": prediction,
        "brief": row["brief"],
        "raw_match": raw_match,
        "created_at": row["created_at"],
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


def _competition_cycle_due(settings: dict[str, Any]) -> bool:
    metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
    last_run = _parse_datetime(metadata.get("last_cycle_at"))
    if last_run is None:
        return True
    key = str(settings.get("key") or "")
    cadence = 5 * 60 if key == DEFAULT_WORLD_CUP["key"] else 15 * 60
    return (datetime.now(timezone.utc) - last_run).total_seconds() >= cadence


def _mark_competition_cycle(key: str) -> None:
    settings = get_competition_settings(key)
    metadata = settings.get("metadata") if isinstance(settings.get("metadata"), dict) else {}
    metadata["last_cycle_at"] = datetime.now(timezone.utc).isoformat()
    _init_db()
    with db_conn(timeout=30) as conn:
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




def _prefixed_match_id(key: str, match_id: Any) -> str:
    prefix = f"competition:{key}:"
    value = str(match_id or "").strip()
    if value.startswith(prefix) or value.startswith("competition:"):
        return value
    return f"{prefix}{value}"


def _main_buffer_match_id(match_id: Any) -> str:
    value = str(match_id or "").strip()
    if value.startswith("sofascore:") or value.startswith("sr:match:"):
        return value
    if value.startswith("competition:"):
        value = value.rsplit(":", 1)[-1]
    return f"sofascore:{value}" if value else ""


def _match_importance_context(key: str, event: dict[str, Any]) -> dict[str, Any]:
    """Classify competition fixture importance from event metadata."""
    tournament = event.get("tournament") if isinstance(event.get("tournament"), dict) else {}
    status = event.get("status") if isinstance(event.get("status"), dict) else {}
    round_value = event.get("round") or event.get("round_info") or event.get("roundInfo") or {}
    round_text = " ".join(
        str(value or "")
        for value in (
            round_value.get("name") if isinstance(round_value, dict) else round_value,
            event.get("round_name"),
            event.get("stage"),
            event.get("slug"),
            event.get("name"),
        )
    ).lower()
    score = 50
    reasons: list[str] = []

    knockout_terms = {
        "final": 96,
        "semi": 88,
        "quarter": 82,
        "round of 16": 76,
        "last 16": 76,
        "knockout": 74,
        "playoff": 72,
        "play-off": 72,
        "decider": 72,
    }
    for term, value in knockout_terms.items():
        if term in round_text:
            score = max(score, value)
            reasons.append(term)

    if "group" in round_text:
        score = max(score, 62)
        reasons.append("group_stage")
    if key == DEFAULT_WORLD_CUP["key"]:
        score += 6
        reasons.append("world_cup")
    if tournament.get("unique_tournament_id") or tournament.get("id"):
        reasons.append("verified_competition")

    status_text = str(status.get("type") or status.get("description") or "").lower()
    state = classify_match_state(
        {
            "period": status.get("description") or status.get("type"),
            "status": status,
            "start_time": int((event.get("start_timestamp") or 0) * 1000) if event.get("start_timestamp") else None,
            "score": event.get("score") or {},
        }
    )
    if state.get("is_live") or status_text in {"inprogress", "live"}:
        score += 4
        reasons.append("live")
    if state.get("is_terminal"):
        score -= 8
        reasons.append("terminal")

    score = max(0, min(100, int(score)))
    tier = "critical" if score >= 90 else "high" if score >= 78 else "medium" if score >= 60 else "normal"
    return {
        "competition_key": key,
        "importance_score": score,
        "tier": tier,
        "is_high_importance": score >= 78,
        "is_critical": score >= 90,
        "round": round_value,
        "round_text": " ".join(round_text.split()),
        "status": status,
        "reasons": sorted(set(reasons)),
    }


def _score_value(value: Any) -> str:
    return "" if value is None else str(value)

def _mirror_competition_event_to_main_buffer(
    conn: sqlite3.Connection,
    key: str,
    event: dict[str, Any],
    match_date: str,
    importance: dict[str, Any],
    enriched_doc: dict[str, Any] | None = None,
) -> None:
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
    match_id = _main_buffer_match_id(event.get("id"))
    raw_sporty = _competition_raw_sporty(key, event, match_date, importance)
    # Reconcile in both directions.  The usual path is Competition Special
    # first and SportyBet later (handled by buffer.ingest_matches), but a
    # normal SportyBet ingest can already exist when this mirror runs.  In
    # that order, retain its real event id and raw market shape instead of
    # replacing it with the SofaScore proxy.
    linked = conn.execute(
        """select sportybet_id, raw_sporty, raw_enriched
           from match_buffer
           where sofascore_id = ? and match_id != ?
             and sportybet_id is not null and sportybet_id != ''
           order by ingested_at desc limit 1""",
        (str(event.get("id") or ""), match_id),
    ).fetchone()
    linked_sporty_id = str(linked[0] or "") if linked else ""
    linked_raw_sporty = linked[1] if linked else None
    linked_enriched = linked[2] if linked else None
    if linked_raw_sporty:
        try:
            raw_sporty = json.loads(linked_raw_sporty)
        except (TypeError, ValueError):
            # A malformed legacy payload must not stop the competition
            # cycle; keep the proxy and let the next Sporty ingest repair it.
            linked_sporty_id = ""
            linked_enriched = None
    # event's team dicts are already normalized to snake_case with an "id"
    # field by sofascore_client.py (home_team/away_team, not the raw API's
    # homeTeam/awayTeam) -- these two columns exist so
    # buffer.py::_resolve_sofascore_only_match can look up a sofascore-only
    # row by team ID directly instead of json.loads()-ing raw_sporty for
    # every candidate row on every SportyBet ingest.
    home_team_id = str((event.get("home_team") or {}).get("id") or "") or None
    away_team_id = str((event.get("away_team") or {}).get("id") or "") or None
    conn.execute(
        """
        insert into match_buffer (
            match_id, match_date, tournament, category, name, start_time, period,
            score_home, score_away, is_live, is_finished, ingested_at,
            data_source, sportybet_id, sofascore_id, sofascore_only, raw_sporty, raw_enriched, enriched_at,
            sofascore_home_team_id, sofascore_away_team_id
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            data_source = case
                when match_buffer.sportybet_id is not null then 'both'
                else excluded.data_source
            end,
            sportybet_id = coalesce(match_buffer.sportybet_id, excluded.sportybet_id),
            sofascore_id = excluded.sofascore_id,
            sofascore_only = case
                when match_buffer.sportybet_id is not null then 0
                else excluded.sofascore_only
            end,
            -- Do NOT null this out. A row that was already merged with a
            -- SportyBet match (sportybet_id set by buffer.py's ingest-time
            -- or backfill merge) must keep that link -- this mirror runs
            -- every ~5 min for every tracked competition, and used to
            -- unconditionally wipe sportybet_id back to null on every pass,
            -- silently undoing the merge and causing SportyBet's next
            -- ingest to recreate a duplicate row (see buffer.py re-split fix).
            raw_sporty = case
                when match_buffer.sportybet_id is not null then match_buffer.raw_sporty
                else excluded.raw_sporty
            end,
            raw_enriched = case
                when match_buffer.sportybet_id is not null then match_buffer.raw_enriched
                else coalesce(excluded.raw_enriched, match_buffer.raw_enriched)
            end,
            enriched_at = coalesce(excluded.enriched_at, match_buffer.enriched_at),
            sofascore_home_team_id = excluded.sofascore_home_team_id,
            sofascore_away_team_id = excluded.sofascore_away_team_id
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
            "both" if linked_sporty_id else "sofascore",
            linked_sporty_id or None,
            str(event.get("id") or ""),
            1,
            json.dumps(raw_sporty),
            linked_enriched or (json.dumps(enriched_doc) if enriched_doc else None),
            datetime.now(timezone.utc).isoformat() if enriched_doc else None,
            home_team_id,
            away_team_id,
        ),
    )


def _competition_raw_sporty(key: str, event: dict[str, Any], match_date: str, importance: dict[str, Any]) -> dict[str, Any]:
    status = event.get("status") or {}
    score = event.get("score") or {}
    return {
        "id": _main_buffer_match_id(event.get("id")),
        "legacy_id": _prefixed_match_id(key, event.get("id")),
        "competition_source_id": str(event.get("id") or ""),
        "name": event.get("name"),
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "tournament": (event.get("tournament") or {}).get("name"),
        "category": _event_category_name(event),
        "match_date": match_date,
        "start_time": int((event.get("start_timestamp") or 0) * 1000),
        "period": status.get("description") or status.get("type") or "Not start",
        "score": {"home": _score_value(score.get("home")), "away": _score_value(score.get("away"))},
        "markets": _special_markets(),
        "competition_special": {"key": key, "source": "sofascore"},
        "competition_special_proxy": True,
        "source": "sofascore",
        "match_importance_context": importance,
        "importance_context": importance,
        "sofascore_event": event,
    }


# _extract_goal_timing is imported from app.utils.goal_timing


def _update_competition_goal_stats(
    key: str,
    event: dict[str, Any],
    detail: dict[str, Any],
) -> None:
    """Persist per-match goal timing into competition_goal_stats.

    Timing data is extracted from the sofascore detail via the shared
    extract_goal_timing_from_detail utility.  If the caller already ran
    enrich_match_facts() and has doc["goal_timing"] available, that richer
    result (which includes the same bands) is used directly instead of
    re-parsing incidents from scratch.
    """
    status = str(((event.get("status") or {}).get("type") or "")).lower()
    if status not in ("finished", "ended", "after extra time", "after penalties"):
        return

    # Prefer pre-computed goal_timing from enrich_match_facts if present on detail
    # (competition special ingest sometimes passes an enriched detail dict).
    # Fall back to extracting from incidents if not.
    precomputed = detail.get("goal_timing") if isinstance(detail.get("goal_timing"), dict) else None
    if precomputed and precomputed.get("goal_count"):
        # match_facts.goal_timing_summary uses different key names — normalise
        timing = {
            "total_goals": precomputed.get("goal_count", 0),
            "first_half_goals": sum(1 for m in (precomputed.get("goal_minutes") or []) if m <= 45),
            "second_half_goals": sum(1 for m in (precomputed.get("goal_minutes") or []) if m > 45),
            "first_goal_minute": (precomputed.get("goal_minutes") or [None])[0],
            "avg_interval_minutes": precomputed.get("average_interval_minutes"),
            "goal_minutes": precomputed.get("goal_minutes") or [],
        }
        from app.utils.doc_helpers import _band as _gt_band
        gm = timing["goal_minutes"]
        for b_key, lo, hi in (
            ("band_1_10", 1, 10), ("band_11_20", 11, 20), ("band_21_30", 21, 30),
            ("band_31_40", 31, 40), ("band_41_45", 41, 45), ("band_46_55", 46, 55),
            ("band_56_65", 56, 65), ("band_66_75", 66, 75), ("band_76_85", 76, 85),
            ("band_86_90", 86, 90),
        ):
            timing[b_key] = _gt_band(gm, lo, hi)
    else:
        timing = _extract_goal_timing(detail)

    if timing["total_goals"] == 0 and not timing["goal_minutes"]:
        return
    match_id = str(event.get("id") or "")
    if not match_id:
        return
    match_date = _event_match_date(event, date.today().isoformat())
    home = str((event.get("home_team") or {}).get("name") or "")
    away = str((event.get("away_team") or {}).get("name") or "")
    now = datetime.now(timezone.utc).isoformat()
    _init_db()
    with db_conn(timeout=30) as conn:
        init_competition_tables(conn)
        conn.execute(
            """
            insert into competition_goal_stats (
                competition_key, match_id, match_date, home_team, away_team,
                total_goals, first_half_goals, second_half_goals,
                band_1_10, band_11_20, band_21_30, band_31_40, band_41_45,
                band_46_55, band_56_65, band_66_75, band_76_85, band_86_90,
                first_goal_minute, avg_interval_minutes, goal_minutes_json, updated_at
            ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(competition_key, match_id) do update set
                match_date=excluded.match_date, home_team=excluded.home_team,
                away_team=excluded.away_team, total_goals=excluded.total_goals,
                first_half_goals=excluded.first_half_goals,
                second_half_goals=excluded.second_half_goals,
                band_1_10=excluded.band_1_10, band_11_20=excluded.band_11_20,
                band_21_30=excluded.band_21_30, band_31_40=excluded.band_31_40,
                band_41_45=excluded.band_41_45, band_46_55=excluded.band_46_55,
                band_56_65=excluded.band_56_65, band_66_75=excluded.band_66_75,
                band_76_85=excluded.band_76_85, band_86_90=excluded.band_86_90,
                first_goal_minute=excluded.first_goal_minute,
                avg_interval_minutes=excluded.avg_interval_minutes,
                goal_minutes_json=excluded.goal_minutes_json,
                updated_at=excluded.updated_at
            """,
            (
                key, match_id, match_date, home, away,
                timing["total_goals"], timing["first_half_goals"], timing["second_half_goals"],
                timing["band_1_10"], timing["band_11_20"], timing["band_21_30"],
                timing["band_31_40"], timing["band_41_45"], timing["band_46_55"],
                timing["band_56_65"], timing["band_66_75"], timing["band_76_85"],
                timing["band_86_90"], timing["first_goal_minute"],
                timing["avg_interval_minutes"],
                json.dumps(timing["goal_minutes"]), now,
            ),
        )
        conn.commit()


def _competition_goal_profile(key: str, limit: int = 50) -> dict[str, Any]:
    """Aggregate goal timing stats across recent matches for a competition."""
    _init_db()
    with db_conn(timeout=30) as conn:
        init_competition_tables(conn)
        rows = conn.execute(
            """
            select total_goals, first_half_goals, second_half_goals,
                   band_1_10, band_11_20, band_21_30, band_31_40, band_41_45,
                   band_46_55, band_56_65, band_66_75, band_76_85, band_86_90,
                   first_goal_minute, avg_interval_minutes
            from competition_goal_stats
            where competition_key = ?
            order by match_date desc
            limit ?
            """,
            (key, limit),
        ).fetchall()

    if not rows:
        return {"available": False, "sample_matches": 0}

    n = len(rows)
    total_goals = sum(r["total_goals"] for r in rows)
    fh = sum(r["first_half_goals"] for r in rows)
    sh = sum(r["second_half_goals"] for r in rows)

    bands = {}
    for band in ("band_1_10", "band_11_20", "band_21_30", "band_31_40", "band_41_45",
                 "band_46_55", "band_56_65", "band_66_75", "band_76_85", "band_86_90"):
        bands[band] = sum(r[band] for r in rows)

    first_goal_minutes = [r["first_goal_minute"] for r in rows if r["first_goal_minute"] is not None]
    avg_intervals = [r["avg_interval_minutes"] for r in rows if r["avg_interval_minutes"] is not None]

    avg_goals = round(total_goals / n, 2)
    fh_pct = round(fh / total_goals * 100, 1) if total_goals else 0
    sh_pct = round(sh / total_goals * 100, 1) if total_goals else 0
    dominant_half = "first" if fh > sh else "second" if sh > fh else "even"

    # Peak band — which 10-min window has the most goals
    peak_band = max(bands, key=lambda b: bands[b]) if any(bands.values()) else None
    peak_band_label = peak_band.replace("band_", "").replace("_", "-") + "min" if peak_band else None

    avg_first_goal = round(sum(first_goal_minutes) / len(first_goal_minutes), 1) if first_goal_minutes else None
    avg_interval = round(sum(avg_intervals) / len(avg_intervals), 1) if avg_intervals else None

    # Normalise band counts to percentages of total goals
    band_pct = {}
    for band, count in bands.items():
        label = band.replace("band_", "").replace("_", "-") + "min"
        band_pct[label] = round(count / total_goals * 100, 1) if total_goals else 0

    return {
        "available": True,
        "sample_matches": n,
        "avg_goals_per_match": avg_goals,
        "first_half_goals": fh,
        "second_half_goals": sh,
        "first_half_pct": fh_pct,
        "second_half_pct": sh_pct,
        "dominant_half": dominant_half,
        "peak_band": peak_band_label,
        "avg_first_goal_minute": avg_first_goal,
        "avg_interval_between_goals": avg_interval,
        "band_distribution": band_pct,
        "raw_band_counts": {b.replace("band_", "").replace("_", "-") + "min": v for b, v in bands.items()},
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

    # Detect season stage and table size so we don't treat 0-point / bottom-of-table
    # standings as meaningful when the season hasn't started or is just beginning.
    season_stage = detect_season_stage(standings)
    table_size_info = classify_table_size(standings)

    home_table = _team_standing_context(standings, home, season_stage)
    away_table = _team_standing_context(standings, away, season_stage)
    home_strength = _recent_play_strength(detail.get("home_last_matches") or [], home)
    away_strength = _recent_play_strength(detail.get("away_last_matches") or [], away)
    home_watcher = _team_watcher_context(key, home)
    away_watcher = _team_watcher_context(key, away)
    try:
        from app.team_watcher.team_watcher import team_watchers_for_match

        ai_team_watchers = team_watchers_for_match(doc)
    except Exception as exc:
        logger.warning(
            "_competition_intelligence_context: team_watchers_for_match failed for key=%s — %s",
            key, exc,
        )
        ai_team_watchers = {"available": False, "error": str(exc)}

    # When standings are unreliable (season not started / beginning),
    # reduce the table edge so it doesn't dominate the prediction.
    table_edge = _safe_num((home_table or {}).get("points_per_game")) - _safe_num((away_table or {}).get("points_per_game"))
    if not season_stage.get("standings_meaningful"):
        table_edge *= 0.25  # heavily discount table PPG edge when standings are unreliable

    strength_edge = _safe_num(home_strength.get("strength_score")) - _safe_num(away_strength.get("strength_score"))
    # Same small-sample discount table_edge gets above, applied here for the
    # same reason: strength_score is built from at most `limit` (8) recent
    # matches per side (_recent_play_strength), and with too few of them
    # the edge is mostly noise. Threshold of 4 matches per side matches the
    # guard app/enrichment/enriched_prediction.py::_competition_intelligence_signal
    # already applies when consuming this same edge, so both places agree
    # on what "thin" means for this signal.
    if int(home_strength.get("sample_size") or 0) < 4 or int(away_strength.get("sample_size") or 0) < 4:
        strength_edge *= 0.25
    watcher_edge = _safe_num((home_watcher.get("profile") or {}).get("team_score")) - _safe_num((away_watcher.get("profile") or {}).get("team_score"))
    return {
        "competition_key": key,
        "table": {
            "available": bool(home_table or away_table),
            "home": home_table,
            "away": away_table,
            "edge_ppg": round(table_edge, 3),
            "leader": "home" if table_edge > 0.15 else "away" if table_edge < -0.15 else "even",
            "season_stage": season_stage.get("stage"),
            "season_not_started": season_stage.get("season_not_started"),
            "season_beginning": season_stage.get("season_beginning"),
            "standings_meaningful": season_stage.get("standings_meaningful"),
            "table_size": table_size_info.get("table_size"),
            "table_category": table_size_info.get("category"),
        },
        "team_strength": {
            "home": home_strength,
            "away": away_strength,
            "edge": round(strength_edge, 2),
            "leader": "home" if strength_edge > 5 else "away" if strength_edge < -5 else "even",
            "basis": "recent_play_results_goal_difference_and_opponent_quality",
        },
        "team_watchers": {
            "available": bool(home_watcher.get("available") or away_watcher.get("available")),
            "home": home_watcher,
            "away": away_watcher,
            "edge": round(watcher_edge, 2),
            "leader": "home" if watcher_edge > 5 else "away" if watcher_edge < -5 else "even",
            "basis": "long_term_competition_team_ai_profiles",
        },
        "ai_team_watchers": ai_team_watchers,
        "goal_profile": _competition_goal_profile(key),
        "readiness_notes": _competition_readiness_notes(home_table, away_table, home_strength, away_strength, season_stage),
    }


def _team_standing_context(
    standings: list[dict[str, Any]],
    team: dict[str, Any],
    season_stage: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    team_id = str(team.get("id") or "")
    team_name = str(team.get("name") or "").lower()
    for row in standings or []:
        row_team = row.get("team") or {}
        if team_id and str(row_team.get("id") or "") == team_id:
            return _standing_summary(row, season_stage)
        if team_name and str(row_team.get("name") or "").lower() == team_name:
            return _standing_summary(row, season_stage)
    return None


def _standing_summary(
    row: dict[str, Any],
    season_stage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    played = int(_safe_num(row.get("played")) or 0)
    points = int(_safe_num(row.get("points")) or 0)
    gf = int(_safe_num(row.get("goals_for")) or 0)
    ga = int(_safe_num(row.get("goals_against")) or 0)
    stage = (season_stage or {}).get("stage", "in_progress")
    standings_meaningful = (season_stage or {}).get("standings_meaningful", True)
    return {
        "position": row.get("position"),
        "team": row.get("team"),
        "played": played,
        "points": points,
        "points_per_game": round(points / played, 3) if played else 0,
        "goal_difference": _goal_diff(row.get("goal_diff"), gf - ga),
        "promotion": row.get("promotion"),
        "season_stage": stage,
        "standings_meaningful": standings_meaningful,
        # When standings are unreliable, PPG is not a reliable signal.
        # We still report it but flag it as unreliable.
        "ppg_reliable": standings_meaningful and played >= 3,
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
        # Win/draw/loss only — not goal difference — so a 1-0 win and a
        # 5-0 win against the same opponent carry the same weight.
        points += 3 if own > opp else 1 if own == opp else 0
        opponent = away if is_home else home
        opponent_quality += _opponent_quality_score(
            str(opponent.get("id") or ""), str(opponent.get("name") or "")
        )
    ppg = points / sample if sample else 0.0
    quality = opponent_quality / sample if sample else 50.0
    # Strength score is now purely PPG-based + opponent quality.
    # Goal difference is intentionally excluded: a narrow win against a
    # strong opponent is worth the same as a comfortable one.
    strength_score = max(1, min(99, 45 + ppg * 12 + (quality - 50) * 0.25))
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
    season_stage: dict[str, Any] | None = None,
) -> list[str]:
    notes: list[str] = []
    if not home_table or not away_table:
        notes.append("table_context_missing_or_incomplete")
    if int(home_strength.get("sample_size") or 0) < 3 or int(away_strength.get("sample_size") or 0) < 3:
        notes.append("recent_play_sample_low")
    if abs(float(home_strength.get("strength_score") or 0) - float(away_strength.get("strength_score") or 0)) >= 12:
        notes.append("team_strength_gap_detected")
    # Season stage awareness: when the season hasn't started or is just
    # beginning, standings are unreliable and should be flagged.
    if season_stage:
        stage = season_stage.get("stage")
        if stage == "not_started":
            notes.append("season_not_started_standings_unreliable")
        elif stage == "beginning":
            notes.append("season_beginning_standings_unreliable")
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


def _team_identity(team: dict[str, Any] | None) -> tuple[str, str]:
    team = team or {}
    team_id = str(team.get("id") or team.get("team_id") or team.get("name") or "").strip()
    team_name = str(team.get("name") or team.get("short_name") or team_id or "Unknown Team").strip()
    return team_id or team_name, team_name


def _register_competition_teams(conn: sqlite3.Connection, key: str, event: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for side in ("home_team", "away_team"):
        team = event.get(side) if isinstance(event.get(side), dict) else {}
        team_id, team_name = _team_identity(team)
        if not team_id:
            continue
        conn.execute(
            """
            insert into competition_team_watchers
                (competition_key, team_id, team_name, analyst_name, updated_at)
            values (?, ?, ?, ?, ?)
            on conflict(competition_key, team_id) do update set
                team_name = excluded.team_name,
                analyst_name = excluded.analyst_name,
                updated_at = excluded.updated_at
            """,
            (key, team_id, team_name, f"{team_name} Watcher", now),
        )


def _update_team_watchers_for_match(
    key: str,
    event: dict[str, Any],
    detail: dict[str, Any],
    prediction: dict[str, Any] | None,
) -> None:
    status = str(((event.get("status") or {}).get("type") or (event.get("status") or {}).get("description") or "")).lower()
    score = event.get("score") or {}
    home_team = event.get("home_team") if isinstance(event.get("home_team"), dict) else {}
    away_team = event.get("away_team") if isinstance(event.get("away_team"), dict) else {}
    home_score = _optional_score(score.get("home"))
    away_score = _optional_score(score.get("away"))
    match_date = _event_match_date(event, date.today().isoformat())
    rows = [
        _team_match_observation(key, event, detail, prediction, home_team, away_team, "home", home_score, away_score, status, match_date),
        _team_match_observation(key, event, detail, prediction, away_team, home_team, "away", away_score, home_score, status, match_date),
    ]
    rows = [row for row in rows if row]
    if not rows:
        return

    _init_db()
    # Fetch the competition goal profile once — shared across all teams in this match
    # so we don't query the DB once per team.
    comp_goal_profile = _competition_goal_profile(key)

    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        init_competition_tables(conn)
        _register_competition_teams(conn, key, event)
        for row in rows:
            # ------------------------------------------------------------------
            # Check whether the existing DB row already has the same result and
            # status.  If nothing meaningful changed, skip the profile rebuild to
            # avoid rebuilding 40 profiles per competition cycle unnecessarily.
            # ------------------------------------------------------------------
            existing = conn.execute(
                """
                select result, status from competition_team_watcher_matches
                where competition_key = ? and team_id = ? and match_id = ?
                """,
                (key, row["team_id"], row["match_id"]),
            ).fetchone()
            row_changed = (
                existing is None
                or existing["result"] != row["result"]
                or existing["status"] != row["status"]
            )

            conn.execute(
                """
                insert into competition_team_watcher_matches
                    (competition_key, team_id, match_id, match_date, opponent, venue,
                     goals_for, goals_against, result, status, prediction_json, brief, raw_match_json, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(competition_key, team_id, match_id) do update set
                    match_date = excluded.match_date,
                    opponent = excluded.opponent,
                    venue = excluded.venue,
                    goals_for = excluded.goals_for,
                    goals_against = excluded.goals_against,
                    result = excluded.result,
                    status = excluded.status,
                    prediction_json = excluded.prediction_json,
                    brief = excluded.brief,
                    raw_match_json = excluded.raw_match_json
                """,
                (
                    key,
                    row["team_id"],
                    row["match_id"],
                    row["match_date"],
                    row["opponent"],
                    row["venue"],
                    row["goals_for"],
                    row["goals_against"],
                    row["result"],
                    row["status"],
                    json.dumps(prediction) if prediction else None,
                    row["brief"],
                    json.dumps(row["raw_match"]),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            # Only rebuild the profile when something actually changed.
            if row_changed:
                profile = _build_team_watcher_profile(conn, key, row["team_id"], goal_profile=comp_goal_profile)
                conn.execute(
                    """
                    update competition_team_watchers
                    set profile_json = ?,
                        match_count = ?,
                        last_match_id = ?,
                        last_brief = ?,
                        updated_at = ?
                    where competition_key = ? and team_id = ?
                    """,
                    (
                        json.dumps(profile),
                        int(profile.get("sample_size") or 0),
                        row["match_id"],
                        row["brief"],
                        datetime.now(timezone.utc).isoformat(),
                        key,
                        row["team_id"],
                    ),
                )
        conn.commit()


def _team_match_observation(
    key: str,
    event: dict[str, Any],
    detail: dict[str, Any],
    prediction: dict[str, Any] | None,
    team: dict[str, Any],
    opponent: dict[str, Any],
    venue: str,
    goals_for: int | None,
    goals_against: int | None,
    status: str,
    match_date: str,
) -> dict[str, Any] | None:
    team_id, team_name = _team_identity(team)
    if not team_id:
        return None
    _, opponent_name = _team_identity(opponent)
    result = None
    if goals_for is not None and goals_against is not None:
        result = "win" if goals_for > goals_against else "loss" if goals_for < goals_against else "draw"
    brief = _team_match_brief(team_name, opponent_name, venue, goals_for, goals_against, result, prediction)
    return {
        "team_id": team_id,
        "team_name": team_name,
        "match_id": _main_buffer_match_id(event.get("id")),
        "competition_match_id": str(event.get("id") or ""),
        "match_date": match_date,
        "opponent": opponent_name,
        "venue": venue,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "result": result,
        "status": status,
        "brief": brief,
        "raw_match": {
            "name": event.get("name"),
            "round": event.get("round"),
            "status": status,
            "detail_sources": {
                "standings": bool(detail.get("standings")),
                "history": bool(detail.get("home_last_matches") or detail.get("away_last_matches")),
                "statistics": bool(detail.get("statistics") or detail.get("match_statistics")),
            },
        },
    }


def _team_match_brief(
    team_name: str,
    opponent_name: str,
    venue: str,
    goals_for: int | None,
    goals_against: int | None,
    result: str | None,
    prediction: dict[str, Any] | None,
) -> str:
    score_text = f"{goals_for}-{goals_against}" if goals_for is not None and goals_against is not None else "score unavailable"
    result_text = result or "pending"
    pick = ((prediction or {}).get("picks") or [{}])[0] if isinstance(prediction, dict) else {}
    pick_text = f" Prediction leaned {pick.get('selection')}." if pick.get("selection") else ""
    return f"{team_name} {result_text} vs {opponent_name} ({venue}, {score_text}).{pick_text}".strip()


def _build_team_watcher_profile(
    conn: sqlite3.Connection,
    key: str,
    team_id: str,
    goal_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        select *
        from competition_team_watcher_matches
        where competition_key = ? and team_id = ?
        order by match_date desc, created_at desc
        limit 20
        """,
        (key, team_id),
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
    failed_to_score = sum(1 for row in finished if int(row["goals_for"] or 0) == 0)
    ppg = (wins * 3 + draws) / sample if sample else 0.0
    gf_avg = gf / sample if sample else 0.0
    ga_avg = ga / sample if sample else 0.0
    team_score = max(1, min(99, 40 + ppg * 14 + (gf_avg - ga_avg) * 8 + (clean_sheets / sample * 8 if sample else 0)))
    strengths: list[str] = []
    weaknesses: list[str] = []
    if ppg >= 2:
        strengths.append("results_consistency")
    if gf_avg >= 1.7:
        strengths.append("scoring_output")
    if ga_avg <= 0.9 and sample:
        strengths.append("defensive_control")
    if sample and clean_sheets / sample >= 0.4:
        strengths.append("clean_sheet_profile")
    if ppg <= 1:
        weaknesses.append("low_points_return")
    if gf_avg <= 0.9 and sample:
        weaknesses.append("chance_conversion")
    if ga_avg >= 1.6:
        weaknesses.append("defensive_leakage")
    if sample and failed_to_score / sample >= 0.35:
        weaknesses.append("blank_risk")
    preferred_markets = _preferred_team_markets(sample, wins, losses, over_25, btts, clean_sheets, failed_to_score)

    # Enrich preferred markets with competition-level goal-timing intelligence.
    # If the competition's own goal profile shows a dominant half or a very early/late
    # peak band, surface that as an additional market suggestion when the team's own
    # record supports it (sample >= 3).
    if goal_profile and goal_profile.get("available") and sample >= 3:
        dominant_half = goal_profile.get("dominant_half")
        peak_band = goal_profile.get("peak_band")          # e.g. "46-55min"
        avg_first_goal = goal_profile.get("avg_first_goal_minute")
        if dominant_half == "second" and gf_avg >= 1.2:
            if not any(m.get("market") == "second_half_goals" for m in preferred_markets):
                preferred_markets.append({
                    "market": "second_half_goals",
                    "confidence": "low",
                    "reason": f"competition_dominant_half_{dominant_half}",
                })
        elif dominant_half == "first" and gf_avg >= 1.2:
            if not any(m.get("market") == "first_half_goals" for m in preferred_markets):
                preferred_markets.append({
                    "market": "first_half_goals",
                    "confidence": "low",
                    "reason": f"competition_dominant_half_{dominant_half}",
                })
        if avg_first_goal is not None and avg_first_goal < 25 and gf_avg >= 1.0:
            if not any(m.get("market") == "early_goal" for m in preferred_markets):
                preferred_markets.append({
                    "market": "early_goal",
                    "confidence": "low",
                    "reason": f"competition_avg_first_goal_{avg_first_goal}min",
                })
    preferred_markets = preferred_markets[:5]
    form = "".join(("W" if row["result"] == "win" else "D" if row["result"] == "draw" else "L") for row in finished[:6])
    recent_briefs = [row["brief"] for row in rows[:5] if row["brief"]]
    return {
        "sample_size": sample,
        "record": {"wins": wins, "draws": draws, "losses": losses, "form": form},
        "goals": {
            "for": gf,
            "against": ga,
            "for_avg": round(gf_avg, 2),
            "against_avg": round(ga_avg, 2),
            "over_2_5_rate": round(over_25 / sample, 3) if sample else 0,
            "btts_rate": round(btts / sample, 3) if sample else 0,
            "clean_sheet_rate": round(clean_sheets / sample, 3) if sample else 0,
            "failed_to_score_rate": round(failed_to_score / sample, 3) if sample else 0,
        },
        "strengths": strengths or ["insufficient_clear_strength"],
        "weaknesses": weaknesses or ["no_major_weakness_detected"],
        "preferred_markets": preferred_markets,
        "team_score": round(team_score, 2),
        "trend": "rising" if form[:3].count("W") >= 2 else "falling" if form[:3].count("L") >= 2 else "stable",
        "recent_briefs": recent_briefs,
        "prediction_context": _watcher_prediction_context(sample, team_score, preferred_markets, strengths, weaknesses),
    }


def _preferred_team_markets(
    sample: int,
    wins: int,
    losses: int,
    over_25: int,
    btts: int,
    clean_sheets: int,
    failed_to_score: int,
) -> list[dict[str, Any]]:
    if not sample:
        return [{"market": "no_pick", "confidence": "low", "reason": "not_enough_team_history"}]
    markets: list[dict[str, Any]] = []
    if wins / sample >= 0.55:
        markets.append({"market": "team_positive_result", "confidence": "medium", "reason": "win_rate_profile"})
    if losses / sample <= 0.25:
        markets.append({"market": "draw_no_bet_or_double_chance", "confidence": "medium", "reason": "low_loss_rate"})
    if over_25 / sample >= 0.55:
        markets.append({"market": "over_2_5", "confidence": "medium", "reason": "high_total_goals_rate"})
    if btts / sample >= 0.55:
        markets.append({"market": "btts_yes", "confidence": "medium", "reason": "both_teams_scoring_pattern"})
    if clean_sheets / sample >= 0.4:
        markets.append({"market": "opponent_under_or_btts_no", "confidence": "medium", "reason": "clean_sheet_rate"})
    if failed_to_score / sample >= 0.35:
        markets.append({"market": "team_under_goals", "confidence": "medium", "reason": "blank_risk"})
    return markets[:4] or [{"market": "context_only", "confidence": "low", "reason": "mixed_team_profile"}]


def _watcher_prediction_context(
    sample: int,
    team_score: float,
    preferred_markets: list[dict[str, Any]],
    strengths: list[str],
    weaknesses: list[str],
) -> dict[str, Any]:
    return {
        "usable": sample >= 3,
        "confidence": "high" if sample >= 8 else "medium" if sample >= 4 else "low",
        "team_score": round(team_score, 2),
        "market_focus": [item.get("market") for item in preferred_markets[:3]],
        "boost_signals": strengths[:3],
        "risk_signals": weaknesses[:3],
    }


def _team_watcher_context(key: str, team: dict[str, Any]) -> dict[str, Any]:
    team_id, team_name = _team_identity(team)
    if not team_id:
        return {"available": False, "team_name": team_name, "reason": "missing_team_id"}
    try:
        _init_db()
        with db_conn(timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            init_competition_tables(conn)
            row = conn.execute(
                """
                select team_id, team_name, analyst_name, profile_json, match_count, last_brief, updated_at
                from competition_team_watchers
                where competition_key = ? and team_id = ?
                """,
                (key, team_id),
            ).fetchone()
        if not row:
            return {"available": False, "team_id": team_id, "team_name": team_name, "reason": "watcher_not_ready"}
        profile = json.loads(row["profile_json"] or "{}")

        # When the local competition profile is thin (< 3 matches), try to enrich
        # it with the main team watcher's tournament-scoped profile so we don't
        # fall back to empty data just because the competition started recently.
        if int(row["match_count"] or 0) < 3:
            try:
                from app.team_watcher.team_watcher import get_watcher as _get_main_watcher
                from app.team_watcher.team_watcher import _slug as _tw_slug
                team_key = _tw_slug(team_name)
                main = _get_main_watcher(team_key, limit=5)
                tournament_profile = (main.get("tournament_profiles") or {}).get(key)
                if tournament_profile:
                    # Merge: local data takes precedence, main tournament profile fills gaps
                    profile = {**tournament_profile, **profile, "enriched_from_main_watcher": True}
            except Exception as tw_exc:
                logger.debug("_team_watcher_context: main watcher enrichment skipped team_id=%s: %s", team_id, tw_exc)

        return {
            "available": bool(profile),
            "team_id": row["team_id"],
            "team_name": row["team_name"],
            "analyst_name": row["analyst_name"],
            "match_count": row["match_count"],
            "profile": profile,
            "last_brief": row["last_brief"],
            "updated_at": row["updated_at"],
        }
    except Exception as exc:
        return {"available": False, "team_id": team_id, "team_name": team_name, "error": str(exc)}

def competition_dashboard_summary(buffer_limit: int = 200) -> dict[str, Any]:
    """Return a lightweight summary for every ENABLED tracked competition.

    Was iterating every row in competition_special_settings (both the curated
    catalogue and every dynamically-discovered-but-disabled competition ever
    seen via ingest -- 281 rows in production vs. 31 actually enabled),
    sequentially, with each iteration calling list_competition_buffer() which
    itself unconditionally re-wrote every buffered match for that competition
    back into match_buffer (a WRITE, not a read) via ensure_competition_main_buffer().
    One dashboard page load meant ~281 sequential DB round-trips plus ~1600
    redundant writes -- this is why the endpoint was slow. Fixed three ways:
    only the enabled competitions get a full summary built (disabled ones
    have nothing meaningful to show anyway); list_competition_buffer is
    called with skip_mirror=True since the mirror already runs on its own
    ~5 min cycle via the background competition_special job; and the
    per-competition work now runs concurrently instead of one at a time.
    `total_tracked` still reports the true settings-table count so nothing
    about competition management/admin visibility is lost.
    """
    _init_db()
    with db_conn(timeout=30) as conn:
        init_competition_tables(conn)
        _ensure_catalogue_settings(conn)
        total_tracked = conn.execute("select count(*) from competition_special_settings").fetchone()[0]
        rows = conn.execute(
            "select key from competition_special_settings where enabled = 1 order by name asc"
        ).fetchall()
    enabled_keys = [row[0] for row in rows]
    summaries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    def _build_one(key: str) -> dict[str, Any]:
        settings = get_competition_settings(key)
        buffer = list_competition_buffer(key, limit=buffer_limit, skip_mirror=True, lite=True)
        status = competition_status(key, skip_mirror=True)

        with db_conn(timeout=30) as conn:
            init_competition_tables(conn)
            from app.competition.competition_analyser import get_latest_analysis, init_competition_analysis_table
            init_competition_analysis_table(conn)
            latest_analysis = get_latest_analysis(key, conn)

        return {
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

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(enabled_keys)))) as pool:
        futures = {pool.submit(_build_one, key): key for key in enabled_keys}
        for future in as_completed(futures):
            key = futures[future]
            try:
                summaries.append(future.result())
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
        "total_tracked": total_tracked,
        "enabled_count": sum(1 for s in summaries if s["enabled"]),
        "competitions": summaries,
        "errors": errors,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _real_sportybet_match_id(doc: dict[str, Any]) -> str:
    value = str(doc.get("sportybet_id") or "").strip()
    if not value or value.startswith("sofascore:") or value.startswith("competition:"):
        return ""
    return value
