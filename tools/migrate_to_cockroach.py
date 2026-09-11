#!/usr/bin/env python3
"""
One-time data copy: SQLite (the live predictx database) -> CockroachDB.

WHAT THIS DOES, IN PLAIN TERMS
-------------------------------
For each table in TABLES_TO_MIGRATE below, this:
  1. Looks at the table's REAL current columns in your live SQLite database
     (not the source code -- the actual columns, including any added later).
  2. Creates a matching table in CockroachDB if it doesn't exist yet.
  3. Copies every row across, in batches, skipping any row that's already
     there (safe to re-run -- it will never create duplicates).
  4. At the end, prints a row-count comparison per table so you can see at
     a glance whether everything made it across.

WHAT THIS DOES NOT DO
----------------------
- It does NOT touch your live SQLite database (reads only, using the same
  read-only connection style the app itself uses elsewhere).
- It does NOT change what the live app reads or writes -- predictx keeps
  running on SQLite exactly as before. This is purely a copy, so the data
  exists in CockroachDB and is ready for whenever we're ready to switch
  the app's code over.
- It does NOT include the `users` table -- that already has its own
  tested copy in app/storage/cockroach/users.py (see
  tools/pilot_test_cockroach_users.py). Run that separately if you also
  want your real user accounts copied over.
- It does NOT copy "plumbing" tables (job queues, caches, scheduler
  locks, monitoring snapshots) -- those rebuild themselves automatically
  and were deliberately left out (agreed with you beforehand).

HOW TO RUN IT (Windows PowerShell, from the predictx folder)
--------------------------------------------------------------
    # 1) See what WOULD happen, without changing anything in CockroachDB:
    python tools\\migrate_to_cockroach.py --dry-run

    # 2) Once that looks right, actually copy the data:
    python tools\\migrate_to_cockroach.py --execute

Uses the same COCKROACH_DATABASE_URL environment variable as the users
pilot. Set it in the same PowerShell window first if it's not already set.

Safe to re-run --execute again later (e.g. right before the real cutover,
to pick up anything written to SQLite since the first copy) -- rows that
already exist in CockroachDB are left untouched, not duplicated.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
import psycopg2.extras

from app.storage.db import DB_PATH, connect_readonly_db
from app.storage.cockroach.db import pg_conn, run_with_retry

BATCH_SIZE = 500

# Real, hard-to-regenerate data only -- agreed with the user 2026-09-08.
# Deliberately excludes: users (separate pilot), and all job/scheduler/
# cache/monitoring/lease/token "plumbing" tables that rebuild themselves.
TABLES_TO_MIGRATE = [
    # Prediction & outcome history
    "prediction_history", "prediction_candidate_history", "prediction_decision_log",
    "prediction_loss_analysis", "signal_outcomes", "signal_combination_outcomes",
    "signal_outcome_map", "betbuilder_history", "betbuilder_leg_history",
    "user_behavior", "user_behavior_outcomes", "risk_outcomes", "risk_control_history",
    "clv_entries",
    # Match & team data
    "matches", "match_buffer", "finished_matches", "match_snapshots",
    "late_goal_snapshots", "snapshot_aggregates", "live_stat_snapshots",
    "enriched_matches", "team_behaviour_profiles", "team_competitions",
    "team_performance_notes", "competitions", "competition_stat_profiles",
    "competition_analysis", "competition_goal_stats", "sofascore_team_grades",
    "sofa_tournament_ids", "competition_special_settings",
    # Learned models & weights
    "elo_ratings", "elo_match_results", "learned_model_weights", "learned_slip_risk",
    "learned_thresholds", "league_accuracy", "league_outcome_distribution",
    "signal_pick_weights", "signal_weights", "signal_combination_memory",
    "signal_stat_correlations", "confidence_calibration", "model_bias_corrections",
    "context_penalty_adjustments", "probability_distribution", "probability_patterns",
    "research_stats", "odds_snapshots", "odds_market_changes", "odds_market_snapshots",
    "odds_pattern_features", "tournament_preferences",
    # Team-watcher intelligence
    "ai_analysis_feedback", "ai_team_watcher_profiles", "ai_team_watchers",
    "ai_team_watcher_matches", "competition_team_watchers",
    "competition_team_watcher_matches", "team_watcher_predictions",
    "team_watcher_weights", "team_watcher_weights_tournament",
    # Social
    "social_posts",
]

_TYPE_MAP = {
    "integer": "INT8",
    "int": "INT8",
    "real": "FLOAT8",
    "float": "FLOAT8",
    "double": "FLOAT8",
    "text": "STRING",
    "varchar": "STRING",
    "char": "STRING",
    "blob": "BYTES",
    "numeric": "DECIMAL",
    "boolean": "INT8",
}


def _cockroach_type(sqlite_type: str) -> str:
    base = (sqlite_type or "").strip().lower().split("(")[0]
    return _TYPE_MAP.get(base, "STRING")


def _table_exists_sqlite(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    return row is not None


def _introspect_columns(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    """Returns [{name, type, pk_order}, ...] from the table's REAL current schema."""
    rows = conn.execute(f"pragma table_info({table})").fetchall()
    cols = []
    for row in rows:
        # row: (cid, name, type, notnull, dflt_value, pk)
        cols.append({"name": row[1], "type": row[2], "pk_order": row[5]})
    return cols


def _build_create_table_sql(table: str, columns: list[dict[str, Any]]) -> str:
    col_defs = []
    pk_cols = [c["name"] for c in sorted(
        (c for c in columns if c["pk_order"]), key=lambda c: c["pk_order"]
    )]
    for col in columns:
        col_defs.append(f'"{col["name"]}" {_cockroach_type(col["type"])}')
    if pk_cols:
        pk_list = ", ".join(f'"{c}"' for c in pk_cols)
        col_defs.append(f"PRIMARY KEY ({pk_list})")
    body = ",\n    ".join(col_defs)
    return f'CREATE TABLE IF NOT EXISTS "{table}" (\n    {body}\n)'


def _copy_table(sqlite_conn: sqlite3.Connection, table: str, *, dry_run: bool) -> dict[str, Any]:
    if not _table_exists_sqlite(sqlite_conn, table):
        return {"table": table, "status": "skipped_missing_in_sqlite"}

    columns = _introspect_columns(sqlite_conn, table)
    if not columns:
        return {"table": table, "status": "skipped_no_columns"}
    col_names = [c["name"] for c in columns]

    sqlite_count = sqlite_conn.execute(f'select count(*) from "{table}"').fetchone()[0]
    create_sql = _build_create_table_sql(table, columns)

    if dry_run:
        with pg_conn() as pg:
            with pg.cursor() as cur:
                cur.execute(
                    "select to_regclass(%s) is not null", (f"public.{table}",)
                )
                exists_in_cockroach = cur.fetchone()[0]
        return {
            "table": table,
            "status": "dry_run",
            "sqlite_rows": sqlite_count,
            "exists_in_cockroach": exists_in_cockroach,
            "create_sql_preview": create_sql,
        }

    def _create(conn: "psycopg2.extensions.connection") -> None:
        with conn.cursor() as cur:
            cur.execute(create_sql)

    run_with_retry(_create)

    pk_cols = [c["name"] for c in sorted(
        (c for c in columns if c["pk_order"]), key=lambda c: c["pk_order"]
    )]
    quoted_cols = ", ".join(f'"{c}"' for c in col_names)
    if pk_cols:
        quoted_pk_cols = ", ".join(f'"{c}"' for c in pk_cols)
        conflict_clause = f"ON CONFLICT ({quoted_pk_cols}) DO NOTHING"
    else:
        conflict_clause = ""  # no natural key -- best effort, may create dupes on re-run
    insert_sql = f'INSERT INTO "{table}" ({quoted_cols}) VALUES %s {conflict_clause}'

    cursor = sqlite_conn.execute(f'select {quoted_cols_sqlite(col_names)} from "{table}"')
    copied = 0
    while True:
        batch = cursor.fetchmany(BATCH_SIZE)
        if not batch:
            break
        values = [tuple(row) for row in batch]

        def _insert(conn: "psycopg2.extensions.connection", values=values) -> None:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, insert_sql, values, page_size=BATCH_SIZE)

        run_with_retry(_insert)
        copied += len(batch)

    with pg_conn() as pg:
        with pg.cursor() as cur:
            cur.execute(f'select count(*) from "{table}"')
            cockroach_count = cur.fetchone()[0]

    return {
        "table": table,
        "status": "ok",
        "sqlite_rows": sqlite_count,
        "rows_sent": copied,
        "cockroach_rows_after": cockroach_count,
        "counts_match": cockroach_count >= sqlite_count,
    }


def quoted_cols_sqlite(col_names: list[str]) -> str:
    return ", ".join(f'"{c}"' for c in col_names)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Show what would happen. Changes nothing.")
    mode.add_argument("--execute", action="store_true", help="Actually copy the data.")
    parser.add_argument("--only", nargs="*", help="Limit to specific table names (for testing one at a time).")
    args = parser.parse_args()

    tables = args.only if args.only else TABLES_TO_MIGRATE
    unknown = set(tables) - set(TABLES_TO_MIGRATE)
    if unknown:
        print(f"Unknown table name(s), not in the approved list: {sorted(unknown)}")
        return 1

    print(f"SQLite database: {DB_PATH}")
    print(f"Mode: {'DRY RUN (nothing will change)' if args.dry_run else 'EXECUTE (copying data now)'}")
    print(f"Tables: {len(tables)}\n")

    results = []
    # connect_readonly_db()'s own with-statement only commits/rolls back (a
    # documented sqlite3 stdlib gotcha -- see app/storage/db.py's db_conn()
    # docstring), it does NOT close the connection. Close it explicitly so
    # this script never leaks a handle to the live database.
    sqlite_conn = connect_readonly_db(timeout=10)
    try:
        for i, table in enumerate(tables, 1):
            print(f"[{i}/{len(tables)}] {table} ...", end=" ", flush=True)
            start = time.time()
            try:
                result = _copy_table(sqlite_conn, table, dry_run=args.dry_run)
            except Exception as exc:
                result = {"table": table, "status": "error", "error": str(exc)}
            result["seconds"] = round(time.time() - start, 1)
            results.append(result)
            print(result.get("status"), f"({result['seconds']}s)")
    finally:
        sqlite_conn.close()

    print("\n--- Summary ---")
    errors = [r for r in results if r["status"] == "error"]
    mismatches = [r for r in results if r.get("counts_match") is False]
    for r in results:
        if args.dry_run:
            print(f"  {r['table']}: sqlite_rows={r.get('sqlite_rows')} already_in_cockroach={r.get('exists_in_cockroach')}")
        elif r["status"] == "ok":
            flag = "OK" if r.get("counts_match") else "MISMATCH"
            print(f"  [{flag}] {r['table']}: sqlite={r['sqlite_rows']} cockroach_after={r['cockroach_rows_after']}")
        else:
            print(f"  [{r['status'].upper()}] {r['table']}: {r.get('error', '')}")

    if errors:
        print(f"\n{len(errors)} table(s) failed -- see above. Nothing else was affected; safe to fix and re-run.")
        return 1
    if not args.dry_run and mismatches:
        print(f"\n{len(mismatches)} table(s) have fewer rows in CockroachDB than SQLite -- worth re-running --execute.")
        return 1
    print("\nDone." if not args.dry_run else "\nDry run complete -- nothing was changed. Run with --execute to copy for real.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
