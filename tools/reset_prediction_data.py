#!/usr/bin/env python3
"""
Reset PredictX's prediction/learning history for a fresh start after the
prediction-pipeline overhaul, while keeping reference data (finished match
results, Elo ratings, competition/team data, odds tracking) intact.

WHY THIS EXISTS
---------------
The old pick-selection/grading/self-learning logic trained a bunch of tables
(signal weights, calibration bands, learned thresholds, etc.) on picks that
were often wrong for reasons unrelated to the actual matches -- a real
favorite was frequently published as a hedge instead. Those learned tables
are now contaminated with lessons drawn from the OLD bug's behavior, not the
matches themselves. This script clears them out (with a backup first) so the
fixed pipeline (see app/enrichment/enriched_prediction.py etc., merged into
Latest as of commit 4f1e89b) starts learning clean.

It deliberately does NOT touch:
  - finished_matches, matches, match_buffer, elo_ratings, elo_match_results,
    competition/team reference tables -- the statistical models need this
    historical data to function; wiping it would hurt accuracy, not help it.
  - odds_market_changes / odds_market_change_state / odds_snapshots /
    odds_pattern_features -- these are actively read by live signal
    generation (market.py, odds_pattern.py, clv.py) and are NOT part of the
    "learned from bad grading" problem. They're a separate, unbounded-growth
    issue (1.74M rows in 12 days as of Aug 28 2026) that needs a retention
    job, not a wipe, as a follow-up. (tools/compact_db.py already exists for
    reclaiming disk space after such a cleanup, via VACUUM INTO.)

USAGE
-----
Run this on the machine where the app actually runs, with the uvicorn server
STOPPED first -- SQLite needs an exclusive lock to run these deletes safely,
and a live server holding the file open can cause "database is locked"
errors or, worse, an inconsistent read while you're mid-delete.

    cd path\\to\\predictx
    "C:\\Program Files\\Python39\\python.exe" tools\\reset_prediction_data.py

It will:
  1. Print current row counts for every table it's about to touch.
  2. Write a full backup of just those tables to
     data/pre_reset_backup_<timestamp>.sqlite3 (small -- a few MB, not a
     copy of the whole 7GB file).
  3. Ask for a typed "yes" confirmation before deleting anything.
  4. Delete all rows from the tables listed below and reset their
     autoincrement counters.
  5. Print row counts again to confirm (should all be 0).

Add --dry-run to do steps 1-2 only (no deletion) if you want to see the
backup and counts first without committing to anything.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Tables that hold predictions themselves, or anything learned/calibrated
# from grading outcomes under the old (buggy) pick-selection and grading
# logic. Reset all of these for a clean slate.
RESET_TABLES = [
    "prediction_history",
    "prediction_candidate_history",
    "prediction_decision_log",
    "prediction_monitor_snapshots",
    "betbuilder_history",
    "betbuilder_leg_history",
    "clv_entries",
    "team_watcher_predictions",
    "confidence_calibration",
    "signal_weights",
    "signal_pick_weights",
    "signal_outcomes",
    "signal_combination_outcomes",
    "signal_combination_memory",
    "signal_outcome_map",
    "learned_thresholds",
    "learned_model_weights",
    "model_bias_corrections",
    "risk_outcomes",
    "risk_control_history",
    "league_accuracy",
    "league_outcome_distribution",
    "context_penalty_adjustments",
    "specialist_performance",
    "ai_analysis_feedback",
    "team_watcher_weights",
    "team_watcher_weights_tournament",
    "tournament_preferences",
    "research_stats",
    "probability_distribution",
    "probability_patterns",
    "user_behavior",
    "user_behavior_outcomes",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/predictx_memory.sqlite3", help="Path to the sqlite database")
    parser.add_argument("--dry-run", action="store_true", help="Only show counts and write the backup; don't delete")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path.resolve()}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")

    existing_tables = {
        row[0] for row in conn.execute("select name from sqlite_master where type='table'")
    }
    tables = [t for t in RESET_TABLES if t in existing_tables]
    missing = [t for t in RESET_TABLES if t not in existing_tables]
    if missing:
        print(f"NOTE: these tables don't exist in this database (skipping): {missing}")

    print("\nCurrent row counts:")
    counts_before: dict[str, int] = {}
    for t in tables:
        n = conn.execute(f'select count(*) from "{t}"').fetchone()[0]
        counts_before[t] = n
        print(f"  {n:>10}  {t}")
    total_before = sum(counts_before.values())
    print(f"  {total_before:>10}  TOTAL rows across {len(tables)} tables")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.parent / f"pre_reset_backup_{timestamp}.sqlite3"
    print(f"\nWriting backup of these tables to {backup_path} ...")
    conn.execute("ATTACH DATABASE ? AS backup", (str(backup_path),))
    for t in tables:
        conn.execute(f'CREATE TABLE backup."{t}" AS SELECT * FROM main."{t}"')
    conn.commit()
    conn.execute("DETACH DATABASE backup")
    print(f"Backup written ({backup_path.stat().st_size / 1e6:.1f} MB).")

    if args.dry_run:
        print("\n--dry-run set: stopping here, nothing deleted.")
        conn.close()
        return 0

    if not args.yes:
        answer = input(
            f"\nType 'yes' to permanently delete {total_before} rows from the {len(tables)} "
            "tables listed above (backup already saved): "
        )
        if answer.strip().lower() != "yes":
            print("Aborted -- nothing deleted. Backup file above is still there if you want it.")
            conn.close()
            return 1

    print("\nDeleting...")
    for t in tables:
        conn.execute(f'DELETE FROM "{t}"')
    # Reset autoincrement counters for these tables so new rows start at 1
    # again (harmless no-op for tables that don't use AUTOINCREMENT).
    placeholders = ",".join("?" for _ in tables)
    conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", tables)
    conn.commit()

    print("\nRow counts after deletion:")
    all_zero = True
    for t in tables:
        n = conn.execute(f'select count(*) from "{t}"').fetchone()[0]
        print(f"  {n:>10}  {t}")
        if n != 0:
            all_zero = False

    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    if all_zero:
        print(f"\nDone. All {len(tables)} tables are empty. Backup saved at {backup_path}.")
        print("You can now restart the server (launch_predictx.bat).")
    else:
        print("\nWARNING: some tables still have rows after deletion -- check output above.")
    return 0 if all_zero else 2


if __name__ == "__main__":
    raise SystemExit(main())
