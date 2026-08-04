"""
Competition Registry
====================
Auto-verifies and auto-creates competition entries when matches are ingested.
Tracks competition metadata and links teams to competitions for granular
performance analytics.

This module is the central authority for competition lifecycle management:
- When a match is ingested, the associated competition is automatically
  verified/created and indexed.
- Team-competition relationships are maintained with per-competition stats.
- Granular performance notes are recorded after each match to support
  context-aware prediction improvement.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.db import _ensure_column, _init_db, db_conn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_competition_registry_tables(conn: sqlite3.Connection) -> None:
    """Create competition registry tables if they do not already exist."""
    conn.execute("pragma busy_timeout = 30000")

    # ── competitions ────────────────────────────────────────────────────────
    conn.execute(
        """
        create table if not exists competitions (
            id                    integer primary key autoincrement,
            key                   text    not null unique,
            name                  text    not null,
            category              text,
            country               text,
            tier                  integer not null default 4,
            unique_tournament_id  integer,
            sofascore_id          text,
            sportybet_id          text,
            enabled               integer not null default 1,
            metadata_json         text    not null default '{}',
            created_at            text    not null default current_timestamp,
            updated_at            text    not null default current_timestamp
        )
        """
    )

    # ── team_competitions ───────────────────────────────────────────────────
    conn.execute(
        """
        create table if not exists team_competitions (
            id                    integer primary key autoincrement,
            team_key              text    not null,
            competition_key       text    not null,
            team_name             text    not null,
            competition_name      text    not null,
            matches_played        integer not null default 0,
            wins                  integer not null default 0,
            draws                 integer not null default 0,
            losses                integer not null default 0,
            goals_for             integer not null default 0,
            goals_against         integer not null default 0,
            clean_sheets          integer not null default 0,
            failed_to_score       integer not null default 0,
            btts_count            integer not null default 0,
            over_25_count         integer not null default 0,
            prediction_correct    integer not null default 0,
            prediction_total      integer not null default 0,
            last_match_date       text,
            form_json             text    not null default '[]',
            performance_notes_json text   not null default '[]',
            created_at            text    not null default current_timestamp,
            updated_at            text    not null default current_timestamp,
            unique (team_key, competition_key)
        )
        """
    )

    # ── team_performance_notes ──────────────────────────────────────────────
    conn.execute(
        """
        create table if not exists team_performance_notes (
            id              integer primary key autoincrement,
            team_key        text    not null,
            competition_key text    not null,
            match_id        text    not null,
            note_type       text    not null,
            title           text    not null,
            description     text    not null,
            context_json    text    not null default '{}',
            severity        text    not null default 'info',
            created_at      text    not null default current_timestamp
        )
        """
    )

    # ── Indexes ─────────────────────────────────────────────────────────────
    conn.execute("create index if not exists idx_competitions_key   on competitions(key)")
    conn.execute("create index if not exists idx_competitions_tier  on competitions(tier)")
    conn.execute(
        "create index if not exists idx_team_competitions_team  on team_competitions(team_key)"
    )
    conn.execute(
        "create index if not exists idx_team_competitions_comp  on team_competitions(competition_key)"
    )
    conn.execute(
        "create index if not exists idx_team_comp_notes_team "
        "on team_performance_notes(team_key, competition_key)"
    )
    conn.execute(
        "create index if not exists idx_team_comp_notes_match "
        "on team_performance_notes(match_id)"
    )


# ---------------------------------------------------------------------------
# Competition CRUD
# ---------------------------------------------------------------------------

def ensure_competition(
    conn: sqlite3.Connection,
    name: str,
    category: str = "",
    country: str = "",
    unique_tournament_id: int | None = None,
    sofascore_id: str = "",
    sportybet_id: str = "",
) -> dict[str, Any]:
    """Ensure a competition exists in the registry. Create it if missing.

    Returns the competition row as a dict (empty dict if name is blank).
    """
    from app.league_memory import normalize_league  # local import breaks circular dependency
    key = normalize_league(name)
    if not key:
        return {}

    row = conn.execute(
        "select * from competitions where key = ?",
        (key,),
    ).fetchone()

    if row:
        # Backfill any newly discovered identifiers
        updates: dict[str, Any] = {}
        if unique_tournament_id and not row["unique_tournament_id"]:
            updates["unique_tournament_id"] = unique_tournament_id
        if sofascore_id and not row["sofascore_id"]:
            updates["sofascore_id"] = sofascore_id
        if sportybet_id and not row["sportybet_id"]:
            updates["sportybet_id"] = sportybet_id
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [key]
            conn.execute(
                f"update competitions set {set_clause}, updated_at = ? where key = ?",
                (*values, datetime.now(timezone.utc).isoformat()),
            )
        return dict(row)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        insert into competitions
            (key, name, category, country, tier, unique_tournament_id,
             sofascore_id, sportybet_id, created_at, updated_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (key, name, category, country, 4, unique_tournament_id,
         sofascore_id, sportybet_id, now, now),
    )
    logger.info("competition_registry: created competition '%s' (key=%s)", name, key)
    return {
        "key": key,
        "name": name,
        "category": category,
        "country": country,
        "tier": 4,
        "unique_tournament_id": unique_tournament_id,
        "sofascore_id": sofascore_id,
        "sportybet_id": sportybet_id,
    }


def get_competition(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """Return a competition row by its normalized key, or None."""
    row = conn.execute(
        "select * from competitions where key = ?",
        (key,),
    ).fetchone()
    return dict(row) if row else None


def list_competitions(
    conn: sqlite3.Connection,
    tier: int | None = None,
    enabled: bool | None = None,
) -> list[dict[str, Any]]:
    """List competitions, optionally filtered by tier and/or enabled flag."""
    query = "select * from competitions where 1=1"
    params: list[Any] = []
    if tier is not None:
        query += " and tier = ?"
        params.append(tier)
    if enabled is not None:
        query += " and enabled = ?"
        params.append(1 if enabled else 0)
    query += " order by name asc"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


# ---------------------------------------------------------------------------
# Team-Competition relationship
# ---------------------------------------------------------------------------

def ensure_team_competition(
    conn: sqlite3.Connection,
    team_key: str,
    competition_key: str,
    team_name: str = "",
    competition_name: str = "",
) -> dict[str, Any]:
    """Ensure a team-competition relationship row exists."""
    if not team_key or not competition_key:
        return {}

    row = conn.execute(
        "select * from team_competitions where team_key = ? and competition_key = ?",
        (team_key, competition_key),
    ).fetchone()
    if row:
        return dict(row)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        insert into team_competitions
            (team_key, competition_key, team_name, competition_name, created_at, updated_at)
        values (?, ?, ?, ?, ?, ?)
        """,
        (team_key, competition_key, team_name, competition_name, now, now),
    )
    return {
        "team_key": team_key,
        "competition_key": competition_key,
        "team_name": team_name,
        "competition_name": competition_name,
        "matches_played": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "goals_for": 0,
        "goals_against": 0,
    }


def update_team_competition_stats(
    conn: sqlite3.Connection,
    team_key: str,
    competition_key: str,
    goals_for: int | None,
    goals_against: int | None,
    result: str | None,
    match_date: str = "",
) -> None:
    """Update team-competition aggregate stats after a completed match."""
    if not team_key or not competition_key:
        return

    ensure_team_competition(conn, team_key, competition_key)

    set_parts = ["updated_at = ?"]
    values: list[Any] = [datetime.now(timezone.utc).isoformat()]

    if goals_for is not None:
        set_parts.append("goals_for = goals_for + ?")
        values.append(goals_for)
    if goals_against is not None:
        set_parts.append("goals_against = goals_against + ?")
        values.append(goals_against)
    if result in ("win", "draw", "loss"):
        col = {"win": "wins", "draw": "draws", "loss": "losses"}[result]
        set_parts.append(f"{col} = {col} + 1")
        set_parts.append("matches_played = matches_played + 1")
    if goals_for is not None and goals_against is not None:
        if goals_against == 0:
            set_parts.append("clean_sheets = clean_sheets + 1")
        if goals_for == 0:
            set_parts.append("failed_to_score = failed_to_score + 1")
        if goals_for > 0 and goals_against > 0:
            set_parts.append("btts_count = btts_count + 1")
        if goals_for + goals_against >= 3:
            set_parts.append("over_25_count = over_25_count + 1")
    if match_date:
        set_parts.append("last_match_date = ?")
        values.append(match_date)

    values.extend([team_key, competition_key])
    conn.execute(
        f"update team_competitions set {', '.join(set_parts)} "
        "where team_key = ? and competition_key = ?",
        values,
    )


def record_team_prediction_outcome(
    conn: sqlite3.Connection,
    team_key: str,
    competition_key: str,
    correct: bool,
) -> None:
    """Record whether a team-level prediction was correct in this competition."""
    if not team_key or not competition_key:
        return
    ensure_team_competition(conn, team_key, competition_key)
    conn.execute(
        """
        update team_competitions
        set prediction_total = prediction_total + 1,
            prediction_correct = prediction_correct + ?,
            updated_at = ?
        where team_key = ? and competition_key = ?
        """,
        (1 if correct else 0, datetime.now(timezone.utc).isoformat(),
         team_key, competition_key),
    )


# ---------------------------------------------------------------------------
# Performance notes
# ---------------------------------------------------------------------------

def add_performance_note(
    conn: sqlite3.Connection,
    team_key: str,
    competition_key: str,
    match_id: str,
    note_type: str,
    title: str,
    description: str,
    context: dict[str, Any] | None = None,
    severity: str = "info",
) -> None:
    """Persist a granular performance note for a team in a competition."""
    if not team_key or not competition_key or not match_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        insert into team_performance_notes
            (team_key, competition_key, match_id, note_type, title,
             description, context_json, severity, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            team_key, competition_key, match_id, note_type, title,
            description, json.dumps(context or {}), severity, now,
        ),
    )


def get_team_performance_notes(
    conn: sqlite3.Connection,
    team_key: str,
    competition_key: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent performance notes for a team in a competition."""
    rows = conn.execute(
        """
        select * from team_performance_notes
        where team_key = ? and competition_key = ?
        order by created_at desc
        limit ?
        """,
        (team_key, competition_key, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_team_competition_stats(
    conn: sqlite3.Connection,
    team_key: str,
    competition_key: str,
) -> dict[str, Any] | None:
    """Return team-competition stats row, or None."""
    row = conn.execute(
        "select * from team_competitions where team_key = ? and competition_key = ?",
        (team_key, competition_key),
    ).fetchone()
    return dict(row) if row else None


def get_team_competition_history(
    conn: sqlite3.Connection,
    team_key: str,
    competition_key: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Return recent match-level rows from ai_team_watcher_matches for a
    team in a specific competition."""
    rows = conn.execute(
        """
        select * from ai_team_watcher_matches
        where team_key = ? and league_name = ?
        order by match_date desc, created_at desc
        limit ?
        """,
        (team_key, competition_key, limit),
    ).fetchall()
    return [dict(r) for r in rows]
