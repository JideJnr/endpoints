#!/usr/bin/env python3
"""
One-time cleanup of the odds-market tracking tables, which have no
retention policy and were growing ~600MB/day (1.74M+ rows in the 12 days
from Aug 16-28 2026, dominating the database's 7.2GB size).

This is a ONE-TIME cleanup, not a recurring job (that was a deliberate
choice -- these tables will start growing unbounded again unless you add a
scheduled retention job later; nothing here prevents that).

TABLES CLEARED
--------------
  odds_market_changes       -- per-tick odds-change log (the big one)
  odds_market_change_state  -- last-seen-odds state per market/selection
  odds_snapshots            -- periodic odds snapshots per match
  odds_snapshot_state       -- snapshot bookkeeping
  odds_pattern_features     -- derived odds-movement pattern features

These are read by app/market/market.py, app/market/odds_pattern.py, and
app/risk/clv.py for live signal generation (steam-move detection, market
regime, closing-line-value). Clearing them means those signals will have
no data until fresh odds activity accumulates after your next matches are
tracked -- a real, temporary quality dip in that specific slice of signals,
traded off against not letting the database keep ballooning. You already
decided this trade-off is worth it for a one-time cleanup.

NOT touched here: match details (matches, finished_matches, etc.),
predictions/results, or reference data models depend on (Elo ratings, team
history, competition data) -- see tools/reset_prediction_data.py for the
separate prediction/learning reset, which also leaves those alone.

IMPORTANT -- deleting ~1.9 million rows does NOT shrink the file by itself.
SQLite marks the freed pages for reuse but doesn't return them to the OS
until you VACUUM. After this script finishes, run the existing compaction
tool to actually reclaim the disk space:

    python tools\\compact_db.py --src data\\predictx_memory.sqlite3 --dst data\\predictx_memory_compacted.sqlite3

Then, with the server still stopped, back up the old file somewhere safe,
rename data\\predictx_memory_compacted.sqlite3 to data\\predictx_memory.sqlite3
(replacing the old one), delete the old -wal/-shm files if present, and
restart the server.

USAGE
-----
Stop the server first (same reason as reset_prediction_data.py -- SQLite
needs an exclusive lock; a live server holding the file open will conflict
with these deletes).

    cd path\\to\\predictx
    "C:\\Program Files\\Python39\\python.exe" tools\\prune_odds_market_data.py

No full row-level backup is taken by default -- these are high-volume,
low-value transient odds ticks, and backing up ~1.9M rows would mostly just
recreate the disk usage you're trying to remove. Pass --backup-summary to
save a small JSON with per-table row counts and date ranges (not the row
data itself) before deleting, if you want a record of what was cleared.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PRUNE_TABLES = [
    "odds_market_changes",
    "odds_market_change_state",
    "odds_snapshots",
    "odds_snapshot_state",
    "odds_pattern_features",
]

# Best-effort timestamp column per table, for the optional summary only.
TIMESTAMP_COLUMNS = {
    "odds_market_changes": "snapshot_time",
    "odds_market_change_state": "updated_at",
    "odds_snapshots": "snapshot_time",
    "odds_snapshot_state": "updated_at",
    "odds_pattern_features": "computed_at",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/predictx_memory.sqlite3", help="Path to the sqlite database")
    parser.add_argument("--dry-run", action="store_true", help="Only show counts; don't delete")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt")
    parser.add_argument("--backup-summary", action="store_true", help="Save a small JSON summary (counts + date ranges) before deleting, not a full row backup")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path.resolve()}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")

    existing_tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    tables = [t for t in PRUNE_TABLES if t in existing_tables]
    missing = [t for t in PRUNE_TABLES if t not in existing_tables]
    if missing:
        print(f"NOTE: these tables don't exist in this database (skipping): {missing}")

    print("\nCurrent row counts:")
    summary: dict[str, dict] = {}
    total_before = 0
    for t in tables:
        n = conn.execute(f'select count(*) from "{t}"').fetchone()[0]
        total_before += n
        entry = {"rows": n}
        tcol = TIMESTAMP_COLUMNS.get(t)
        if tcol:
            try:
                lo, hi = conn.execute(f'select min("{tcol}"), max("{tcol}") from "{t}"').fetchone()
                entry["date_range"] = [lo, hi]
            except Exception:
                pass
        summary[t] = entry
        extra = f"  ({entry.get('date_range')})" if entry.get("date_range") else ""
        print(f"  {n:>10}  {t}{extra}")
    print(f"  {total_before:>10}  TOTAL rows across {len(tables)} tables")

    if args.backup_summary:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        summary_path = db_path.parent / f"odds_prune_summary_{timestamp}.json"
        summary_path.write_text(json.dumps(summary, indent=2))
        print(f"\nSummary saved to {summary_path} (counts + date ranges only, not row data).")

    if args.dry_run:
        print("\n--dry-run set: stopping here, nothing deleted.")
        conn.close()
        return 0

    if not args.yes:
        answer = input(
            f"\nType 'yes' to permanently delete {total_before} rows from the {len(tables)} "
            "odds-tracking tables listed above (no full backup -- see script docstring for why): "
        )
        if answer.strip().lower() != "yes":
            print("Aborted -- nothing deleted.")
            conn.close()
            return 1

    print("\nDeleting...")
    for t in tables:
        conn.execute(f'DELETE FROM "{t}"')
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
        print(f"\nDone. All {len(tables)} odds tables are empty.")
        print("The file itself won't shrink until you VACUUM it. Next step:")
        print("  python tools\\compact_db.py --src data\\predictx_memory.sqlite3 --dst data\\predictx_memory_compacted.sqlite3")
        print("Then (server still stopped): back up the old file, swap the compacted one into")
        print("its place, remove any leftover -wal/-shm files, and restart the server.")
    else:
        print("\nWARNING: some tables still have rows after deletion -- check output above.")
    return 0 if all_zero else 2


if __name__ == "__main__":
    raise SystemExit(main())
