from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config.config import get_settings


DB_PATH: Path = get_settings().database_path
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_BUSY_TIMEOUT_MS = 30000
_local = threading.local()


def configure_connection(conn: sqlite3.Connection, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Apply the app-wide SQLite connection policy."""
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma synchronous = normal")
    conn.execute(f"pragma busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("pragma cache_size = -8000")
    return conn


def connect_db(*, timeout: int = DEFAULT_TIMEOUT_SECONDS, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a SQLite connection using the shared app database settings."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=timeout, check_same_thread=check_same_thread)
    return configure_connection(conn)


def connect_readonly_db(*, timeout: int = 2) -> sqlite3.Connection:
    """Open the shared database in read-only mode with the same row handling."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout = 30000")
    return conn


def get_db() -> sqlite3.Connection:
    """Return a thread-local persistent connection, creating it on first use."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect_db(timeout=DEFAULT_TIMEOUT_SECONDS, check_same_thread=False)
        _local.conn = conn
    return conn


def close_db() -> None:
    """Close and discard the current thread's persistent connection."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


@contextmanager
def db_conn(timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Iterator[sqlite3.Connection]:
    """Yield a short-lived connection with consistent WAL and busy-timeout pragmas.

    IMPORTANT: sqlite3.Connection's own context-manager protocol
    (`with conn:`) only commits or rolls back the open transaction -- it
    does NOT close the connection (a well-known stdlib gotcha). Every
    caller across this app uses `with db_conn(...) as conn:` /
    `with _conn(...) as conn:` expecting a short-lived connection, so this
    wrapper explicitly closes it afterward. Without this, every one of the
    ~300 call sites throughout the app leaked a raw OS-level file handle to
    the database on every single call, for the entire lifetime of the
    process -- the connection was never returned, just abandoned for the
    garbage collector to maybe clean up eventually. That's the most likely
    real cause of persistent "database is locked" / "disk I/O error"
    symptoms that get worse the longer the app has been running: leaked
    handles pile up, and in WAL mode any connection still holding a read
    lock (even an abandoned one waiting on GC) blocks the WAL file from
    being checkpointed back into the main database file.
    """
    conn = connect_db(timeout=timeout)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _is_sqlite_lock(exc: Exception) -> bool:
    """Return True when *exc* is a SQLite database-locked / busy error."""
    msg = str(exc).lower()
    return 'database is locked' in msg or 'unable to open' in msg or 'disk i/o error' in msg


@contextmanager
def _conn(timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Iterator[sqlite3.Connection]:
    """Backward-compatible alias for modules that already use app-level DB contexts."""
    with db_conn(timeout=timeout) as conn:
        yield conn


def is_sqlite_lock(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message

import threading

_DB_SCHEMA_READY = False
_DB_SCHEMA_LOCK = threading.RLock()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def _ensure_specialist_performance_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        create table if not exists specialist_performance (
            specialist_name text not null,
            league_key text not null,
            pick_type text not null,
            samples integer not null default 0,
            wins integer not null default 0,
            losses integer not null default 0,
            win_rate real,
            weight real not null default 1.0,
            last_updated text,
            unique (specialist_name, league_key, pick_type)
        )
    """)
    conn.execute("create index if not exists idx_specialist_performance_scope on specialist_performance(league_key, pick_type, samples)")




# Backward-compatible underscore alias
_is_sqlite_lock = is_sqlite_lock


def _run_schema_migrations() -> bool:
    return os.getenv("PREDICTX_RUN_SCHEMA_MIGRATIONS", "").strip().lower() in {"1", "true", "yes", "on"}


def _run_legacy_backfills() -> bool:
    """Large backfills are opt-in so routine health checks stay bounded."""
    return os.getenv("PREDICTX_RUN_LEGACY_BACKFILLS", "").strip().lower() in {"1", "true", "yes", "on"}


def _existing_schema_can_be_trusted() -> bool:
    """Fast path for the long-running production database."""
    if _run_schema_migrations() or not DB_PATH.exists():
        return False
    try:
        with connect_readonly_db(timeout=2) as conn:
            row = conn.execute(
                """
                select count(*)
                from sqlite_master
                where type = 'table'
                  and name in ('prediction_history', 'match_buffer', 'job_runs', 'team_behaviour_profiles', 'user_behavior', 'matches')
                """
            ).fetchone()
        # Require ALL core tables to exist, not just 3
        return int(row[0] if row else 0) >= 6
    except sqlite3.OperationalError as exc:
        if _is_sqlite_lock(exc):
            return True
        return False


def _ensure_prediction_history_columns(conn: sqlite3.Connection) -> None:
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
    _ensure_column(conn, "prediction_history", "models_json", "text not null default '{}'")
    _ensure_column(conn, "prediction_history", "signal_combination_key", "text")
    _ensure_column(conn, "prediction_history", "signal_combination_json", "text not null default '{}'")
    _ensure_column(conn, "prediction_history", "live_context_json", "text not null default '{}'")
    # Dual-engine consolidation: which engine produced this row ('deterministic'
    # or 'ai_llm'), and whether it's the one actually shown/counted as THE pick
    # for its match after arbitration between the two engines. is_final defaults
    # to 1 so every pre-existing, single-engine row keeps behaving exactly as
    # before -- only a row that lost an arbitration comparison ever gets set to 0.
    _ensure_column(conn, "prediction_history", "engine", "text not null default 'deterministic'")
    _ensure_column(conn, "prediction_history", "is_final", "integer not null default 1")


def _ensure_match_fact_columns(conn: sqlite3.Connection) -> None:
    table = conn.execute("select 1 from sqlite_master where type = 'table' and name = 'matches'").fetchone()
    if not table:
        return
    _ensure_column(conn, "matches", "half_time_home_goals", "integer")
    _ensure_column(conn, "matches", "half_time_away_goals", "integer")
    _ensure_column(conn, "matches", "goal_times_json", "text not null default '[]'")
    _ensure_column(conn, "matches", "average_goal_interval_minutes", "real")
    _ensure_column(conn, "matches", "live_statistics_json", "text not null default '{}'")
    _ensure_column(conn, "matches", "provider_capabilities_json", "text not null default '{}'")


def _ensure_signal_combination_outcomes_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists signal_combination_outcomes (
            id integer primary key autoincrement,
            combination_key text not null,
            combination_json text not null default '{}',
            signal_names_json text not null default '[]',
            match_id text not null,
            match_name text,
            tournament text,
            country text,
            match_date text,
            result text not null,
            pick_type text,
            selection text,
            confidence integer,
            prediction_mode text,
            live_context_json text not null default '{}',
            recorded_at text not null default current_timestamp,
            unique (match_id, pick_type, selection, combination_key)
        )
        """
    )
    conn.execute("create index if not exists idx_signal_combos_key on signal_combination_outcomes(combination_key, result)")
    conn.execute("create index if not exists idx_signal_combos_scope on signal_combination_outcomes(country, tournament, pick_type, result)")


def _migrate_future_buffer(conn: sqlite3.Connection) -> None:
    """One-time migration: move future_match_buffer rows into match_buffer then drop the table."""
    has_future = conn.execute(
        "select 1 from sqlite_master where type='table' and name='future_match_buffer'"
    ).fetchone()
    if not has_future:
        return
    conn.execute(
        """
        insert or ignore into match_buffer
            (match_id, match_date, tournament, category, name, start_time, period,
             score_home, score_away, is_live, is_finished, ingested_at, enriched_at,
             data_source, sportybet_id, sofascore_id, raw_sporty, raw_enriched)
        select match_id, match_date, tournament, category, name, start_time, period,
               score_home, score_away, is_live, is_finished, ingested_at, enriched_at,
               data_source, sportybet_id, sofascore_id, raw_sporty, raw_enriched
        from future_match_buffer
        """
    )
    conn.execute("drop table if exists future_match_buffer")


def _init_db_unlocked() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Apply WAL + busy_timeout on a short-lived connection before the long schema write.
    # This ensures WAL is active so readers never block writers during init.
    try:
        _pragma_conn = sqlite3.connect(str(DB_PATH), timeout=10)
        _pragma_conn.execute("pragma journal_mode = wal")
        _pragma_conn.execute("pragma synchronous = normal")
        _pragma_conn.execute("pragma busy_timeout = 60000")
        _pragma_conn.close()
    except sqlite3.OperationalError:
        pass
    with db_conn(timeout=60) as conn:
        conn.execute("pragma busy_timeout = 60000")
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
                half_time_home_goals integer,
                half_time_away_goals integer,
                final_home_goals integer,
                final_away_goals integer,
                goal_times_json text not null default '[]',
                average_goal_interval_minutes real,
                live_statistics_json text not null default '{}',
                provider_capabilities_json text not null default '{}',
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
                signal_combination_key text,
                signal_combination_json text not null default '{}',
                live_context_json text not null default '{}',
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
        _ensure_signal_combination_outcomes_table(conn)
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
        conn.execute(
            """
            create table if not exists user_behavior (
                id integer primary key autoincrement,
                match_id text not null,
                user_action text not null,
                pick_type text,
                selection text,
                confidence real,
                metadata_json text not null default '{}',
                created_at text not null default current_timestamp
            )
            """
        )
        conn.execute(
            """
            create index if not exists idx_user_behavior_match
            on user_behavior(match_id, user_action)
            """
        )
        conn.execute(
            """
            create index if not exists idx_user_behavior_action
            on user_behavior(user_action, created_at)
            """
        )
        _ensure_column(conn, "matches", "match_fingerprint", "text")
        _ensure_column(conn, "matches", "start_time", "text")
        _ensure_match_fact_columns(conn)
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
        _ensure_prediction_history_columns(conn)
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
        conn.execute("""
            create table if not exists team_history_cache (
                team_id    text primary key,
                events_json text not null,
                cached_at  text not null
            )
        """)
        # ── Competition registry tables ──────────────────────────────────────
        conn.execute("""
            create table if not exists competitions (
                id                   integer primary key autoincrement,
                key                  text    not null unique,
                name                 text    not null,
                category             text,
                country              text,
                tier                 integer not null default 4,
                unique_tournament_id integer,
                sofascore_id         text,
                sportybet_id         text,
                enabled              integer not null default 1,
                metadata_json        text    not null default '{}',
                created_at           text    not null default current_timestamp,
                updated_at           text    not null default current_timestamp
            )
        """)
        conn.execute("""
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
        """)
        conn.execute("""
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
        """)
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
        # ── Buffer tables (match_buffer, finished_matches) ───────────────────
        conn.execute("""
            create table if not exists match_buffer (
                match_id        text primary key,
                match_date      text,
                tournament      text,
                category        text,
                name            text,
                start_time      integer,
                period          text,
                score_home      text,
                score_away      text,
                is_live         integer not null default 0,
                is_finished     integer not null default 0,
                ingested_at     text not null default current_timestamp,
                enriched_at     text,
                data_source     text not null default 'sportybet',
                sportybet_id    text,
                sofascore_id    text,
                sofascore_only  integer not null default 0,
                raw_sporty      text,
                raw_enriched    text
            )
        """)
        conn.execute("create index if not exists idx_buffer_date        on match_buffer(match_date)")
        conn.execute("create index if not exists idx_buffer_live        on match_buffer(is_live)")
        conn.execute("create index if not exists idx_buffer_enrich      on match_buffer(enriched_at)")
        conn.execute("create index if not exists idx_buffer_sofa_only   on match_buffer(sofascore_only, match_date)")
        conn.execute("create index if not exists idx_buffer_sofascore   on match_buffer(sofascore_id)")
        _ensure_column(conn, "match_buffer", "data_source",    "text not null default 'sportybet'")
        _ensure_column(conn, "match_buffer", "sportybet_id",   "text")
        _ensure_column(conn, "match_buffer", "sofascore_only", "integer not null default 0")
        # Migrate any remaining future_match_buffer rows into match_buffer then drop the table
        _migrate_future_buffer(conn)
        conn.execute("""
            create table if not exists sofa_event_list_cache (
                date        text primary key,
                fetched_at  text not null,
                events_json text not null
            )
        """)
        conn.execute("""
            create table if not exists finished_matches (
                match_id    text primary key,
                match_date  text,
                home_team   text,
                away_team   text,
                tournament  text,
                score_home  text,
                score_away  text,
                finished_at text not null default current_timestamp,
                raw_json    text not null,
                raw_doc     text
            )
        """)
        _ensure_specialist_performance_table(conn)
        conn.commit()




def _init_db() -> None:
    global _DB_SCHEMA_READY
    if _DB_SCHEMA_READY:
        return
    with _DB_SCHEMA_LOCK:
        if _DB_SCHEMA_READY:
            return
        if _existing_schema_can_be_trusted():
            with db_conn(timeout=30) as conn:
                conn.execute("pragma busy_timeout = 30000")
                _ensure_prediction_history_columns(conn)
                _ensure_match_fact_columns(conn)
                _ensure_signal_combination_outcomes_table(conn)
                _ensure_specialist_performance_table(conn)
            _DB_SCHEMA_READY = True
            return
        try:
            _init_db_unlocked()
            _DB_SCHEMA_READY = True
        except sqlite3.OperationalError as exc:
            if _is_sqlite_lock(exc) and DB_PATH.exists():
                # Runtime callers should not fail just because another worker is
                # writing. The schema is created at process startup; retry on the
                # next call if this process has not completed initialization yet.
                return
            raise

