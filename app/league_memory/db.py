from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.config import get_settings

DB_PATH = get_settings().database_path
_DB_SCHEMA_READY = False
_DB_SCHEMA_LOCK = threading.RLock()
_local = threading.local()


def get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma synchronous = normal")
        conn.execute("pragma busy_timeout = 30000")
        conn.execute("pragma cache_size = -8000")
        _local.conn = conn
    return conn


def close_db() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None


@contextmanager
def _conn(timeout: int = 30):
    try:
        yield get_db()
    except sqlite3.OperationalError:
        with sqlite3.connect(DB_PATH, timeout=timeout) as fresh:
            fresh.row_factory = sqlite3.Row
            fresh.execute("pragma journal_mode = wal")
            fresh.execute("pragma synchronous = normal")
            fresh.execute("pragma busy_timeout = 30000")
            yield fresh


def _run_legacy_backfills() -> bool:
    return os.getenv("PREDICTX_RUN_LEGACY_BACKFILLS", "").strip().lower() in {"1", "true", "yes", "on"}


def _run_schema_migrations() -> bool:
    return os.getenv("PREDICTX_RUN_SCHEMA_MIGRATIONS", "").strip().lower() in {"1", "true", "yes", "on"}


def _existing_schema_can_be_trusted() -> bool:
    if _run_schema_migrations() or not DB_PATH.exists():
        return False
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2) as conn:
            row = conn.execute(
                """
                select count(*)
                from sqlite_master
                where type = 'table'
                  and name in ('prediction_history', 'match_buffer', 'job_runs', 'team_behaviour_profiles')
                """
            ).fetchone()
        # Require ALL core tables to exist
        return int(row[0] if row else 0) >= 4
    except sqlite3.OperationalError as exc:
        if _is_sqlite_lock(exc):
            return True
        return False


def _init_db() -> None:
    global _DB_SCHEMA_READY
    if _DB_SCHEMA_READY:
        return
    with _DB_SCHEMA_LOCK:
        if _DB_SCHEMA_READY:
            return
        if _existing_schema_can_be_trusted():
            _DB_SCHEMA_READY = True
            return
        try:
            _init_db_unlocked()
            _DB_SCHEMA_READY = True
        except sqlite3.OperationalError as exc:
            if _is_sqlite_lock(exc) and DB_PATH.exists():
                return
            raise


def _init_db_unlocked() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=60) as conn:
        conn.execute("pragma busy_timeout = 60000")
        try:
            conn.execute("pragma journal_mode = wal")
            conn.execute("pragma synchronous = normal")
        except sqlite3.OperationalError:
            pass
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
                prediction_mode text not null default 'prematch',
                data_source text,
                live_data_sources_json text not null default '[]',
                created_at text not null default current_timestamp
            )
            """
        )
        conn.execute(
            """
            create table if not exists prediction_candidate_history (
                id integer primary key autoincrement,
                source text not null,
                match_id text not null,
                match_name text,
                league_name text,
                country_name text,
                pick_type text,
                selection text,
                confidence integer,
                reason text,
                role text,
                context_json text not null default '{}',
                signals_json text not null default '[]',
                result text,
                final_home integer,
                final_away integer,
                graded_at text,
                created_at text not null default current_timestamp
            )
            """
        )
        conn.execute(
            """
            create table if not exists prediction_decision_log (
                id integer primary key autoincrement,
                source text not null,
                match_id text not null,
                sofascore_id text,
                sportybet_id text,
                match_name text,
                league_name text,
                country_name text,
                decision_type text not null,
                pick_type text,
                selection text,
                confidence integer,
                reason text,
                readiness_json text not null default '{}',
                signals_json text not null default '[]',
                picks_json text not null default '[]',
                audit_json text not null default '{}',
                contextual_json text not null default '{}',
                result text,
                final_home integer,
                final_away integer,
                grading_reason_json text not null default '{}',
                graded_at text,
                created_at text not null default current_timestamp
            )
            """
        )
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
        conn.execute(
            """
            create table if not exists betbuilder_history (
                id integer primary key autoincrement,
                selections_json text not null,
                combined_odds real,
                confidence integer,
                request_json text not null default '{}',
                result text,
                graded_at text,
                created_at text not null default current_timestamp
            )
            """
        )
        conn.execute(
            """
            create table if not exists betbuilder_leg_history (
                id integer primary key autoincrement,
                bet_id integer not null,
                leg_index integer not null default 0,
                match_id text not null,
                match_name text,
                league_name text,
                country_name text,
                pick_type text,
                selection text,
                odds real,
                odds_band text,
                confidence integer,
                role text,
                signals_json text not null default '[]',
                context_json text not null default '{}',
                market_intent_json text not null default '{}',
                result text,
                grading_reason_json text not null default '{}',
                graded_at text,
                created_at text not null default current_timestamp,
                unique (bet_id, match_id, pick_type, selection)
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
            create table if not exists team_behaviour_profiles (
                team_name text primary key,
                btts_rate real,
                over_2_5_rate real,
                clean_sheet_rate real,
                comeback_rate real,
                high_scorer_flag integer,
                loss_to_nil_rate real,
                sample_size integer,
                computed_at text
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
        conn.execute("""
            create table if not exists team_history_cache (
                team_id    text primary key,
                events_json text not null,
                cached_at  text not null
            )
        """)
        conn.execute("""
            create table if not exists specialist_performance (
                specialist_name  text not null,
                league_key       text not null default '__global__',
                pick_type        text not null default '__all__',
                samples          integer not null default 0,
                wins             integer not null default 0,
                losses           integer not null default 0,
                win_rate         real,
                weight           real not null default 1.0,
                last_updated     text not null default current_timestamp,
                primary key (specialist_name, league_key, pick_type)
            )
        """)
        _ensure_column(conn, "matches", "match_fingerprint", "text")
        _ensure_column(conn, "matches", "start_time", "text")
        conn.execute("create index if not exists idx_matches_league on matches(league_key)")
        conn.execute("create index if not exists idx_matches_last_seen on matches(last_seen_at)")
        conn.execute("create index if not exists idx_matches_fingerprint on matches(match_fingerprint)")
        conn.execute("create index if not exists idx_snapshots_group on match_snapshots(league_key, minute_bucket, score_state)")
        conn.execute("create index if not exists idx_snapshots_resolved on match_snapshots(resolved_at)")
        conn.execute("create index if not exists idx_predictions_match on prediction_history(match_id)")
        conn.execute("create index if not exists idx_predictions_created on prediction_history(created_at)")
        conn.execute("create index if not exists idx_signal_outcomes_signal on signal_outcomes(signal_name)")
        conn.execute("create index if not exists idx_signal_outcomes_scope on signal_outcomes(country, tournament, result)")
        conn.execute("create index if not exists idx_odds_match on odds_snapshots(match_id)")
        conn.execute("create index if not exists idx_odds_date on odds_snapshots(match_date)")
        conn.execute("create index if not exists idx_odds_match_time on odds_snapshots(match_id, snapshot_time)")
        _ensure_column(conn, "prediction_history", "result", "text")
        _ensure_column(conn, "prediction_history", "final_home", "integer")
        _ensure_column(conn, "prediction_history", "final_away", "integer")
        _ensure_column(conn, "prediction_history", "graded_at", "text")
        _ensure_column(conn, "prediction_history", "country_name", "text")
        _ensure_column(conn, "prediction_history", "sofascore_id", "text")
        _ensure_column(conn, "prediction_history", "sportybet_id", "text")
        _ensure_column(conn, "prediction_history", "prediction_mode", "text not null default 'prematch'")
        _ensure_column(conn, "prediction_history", "data_source", "text")
        _ensure_column(conn, "prediction_history", "live_data_sources_json", "text not null default '[]'")
        _ensure_column(conn, "prediction_history", "audit_json", "text not null default '{}'")
        _ensure_column(conn, "prediction_history", "grading_reason_json", "text not null default '{}'")
        if _run_legacy_backfills():
            conn.execute(
                """
                update prediction_history
                set country_name = case
                    when instr(league_name, ' ') > 0 then substr(league_name, 1, instr(league_name, ' ') - 1)
                    else 'Global'
                end
                where country_name is null or country_name = ''
                """
            )
        conn.execute("create index if not exists idx_predictions_graded on prediction_history(graded_at)")
        conn.execute("create index if not exists idx_predictions_match_mode on prediction_history(match_id, prediction_mode, created_at)")
        conn.execute("create index if not exists idx_predictions_scope on prediction_history(league_name, country_name, pick_type, selection)")
        conn.execute("create index if not exists idx_candidate_match on prediction_candidate_history(match_id)")
        _ensure_column(conn, "prediction_candidate_history", "sofascore_id", "text")
        _ensure_column(conn, "prediction_candidate_history", "sportybet_id", "text")
        _ensure_column(conn, "prediction_candidate_history", "audit_json", "text not null default '{}'")
        _ensure_column(conn, "prediction_candidate_history", "grading_reason_json", "text not null default '{}'")
        conn.execute("create index if not exists idx_candidate_scope on prediction_candidate_history(league_name, country_name, pick_type, selection)")
        conn.execute("create index if not exists idx_candidate_graded on prediction_candidate_history(graded_at)")
        conn.execute("create index if not exists idx_decision_match on prediction_decision_log(match_id)")
        conn.execute("create index if not exists idx_decision_graded on prediction_decision_log(graded_at)")
        conn.execute("create index if not exists idx_decision_scope on prediction_decision_log(league_name, country_name, decision_type, pick_type)")
        _ensure_column(conn, "betbuilder_history", "request_json", "text not null default '{}'")
        _ensure_column(conn, "betbuilder_history", "result", "text")
        _ensure_column(conn, "betbuilder_history", "leg_results_json", "text not null default '[]'")
        _ensure_column(conn, "betbuilder_history", "learning_json", "text not null default '{}'")
        _ensure_column(conn, "betbuilder_history", "graded_at", "text")
        conn.execute("create index if not exists idx_betbuilder_legs_bet on betbuilder_leg_history(bet_id)")
        conn.execute("create index if not exists idx_betbuilder_legs_scope on betbuilder_leg_history(league_name, country_name, pick_type, selection, odds_band)")
        conn.execute("create index if not exists idx_betbuilder_legs_graded on betbuilder_leg_history(graded_at)")
        _ensure_column(conn, "matches", "country_name", "text")
        if _run_legacy_backfills():
            conn.execute(
                """
                update matches
                set country_name = case
                    when instr(league_name, ' ') > 0 then substr(league_name, 1, instr(league_name, ' ') - 1)
                    else 'Global'
                end
                where country_name is null or country_name = ''
                """
            )
        conn.execute("create index if not exists idx_matches_scope on matches(league_name, country_name, is_finished)")
        conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def _is_sqlite_lock(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message
