from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Stats:
    inserted: int = 0
    scanned: int = 0
    groups: int = 0


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.execute("pragma busy_timeout = 60000")
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists odds_market_changes (
            id integer primary key autoincrement,
            match_id text not null,
            match_name text,
            match_date text,
            market_id text,
            market_name text,
            specifier text,
            selection_id text,
            selection_name text,
            odds real,
            probability real,
            source text,
            snapshot_time text not null default current_timestamp
        )
        """
    )
    conn.execute(
        """
        create table if not exists odds_market_change_state (
            match_id text not null,
            source text not null,
            market_id text not null,
            specifier text not null,
            selection_id text not null,
            last_odds real,
            updated_at text not null default current_timestamp,
            primary key (match_id, source, market_id, specifier, selection_id)
        )
        """
    )
    conn.execute("create index if not exists idx_odds_market_changes_match on odds_market_changes(match_id)")
    conn.execute("create index if not exists idx_odds_market_changes_date on odds_market_changes(match_date)")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?",
        (name,),
    ).fetchone()
    return bool(row)


def _float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _implied(decimal_odds: float | None) -> float | None:
    if not decimal_odds or decimal_odds <= 0:
        return None
    return round(100 / decimal_odds, 2)


def lean_market_table(
    conn: sqlite3.Connection,
    *,
    min_change: float,
    commit_every: int,
    limit_matches: int | None,
) -> Stats:
    if not _table_exists(conn, "odds_market_snapshots"):
        print("odds_market_snapshots_missing")
        return Stats()

    _ensure_tables(conn)
    conn.row_factory = sqlite3.Row

    match_clause = ""
    params: list[object] = []
    if limit_matches:
        # Deterministic subset: first N match_ids in lexical order.
        match_ids = [
            row["match_id"]
            for row in conn.execute(
                "select distinct match_id from odds_market_snapshots order by match_id asc limit ?",
                (int(limit_matches),),
            ).fetchall()
        ]
        if not match_ids:
            return Stats()
        placeholders = ",".join("?" for _ in match_ids)
        match_clause = f"where match_id in ({placeholders})"
        params.extend(match_ids)

    cursor = conn.execute(
        f"""
        select match_id, match_name, match_date, market_id, market_name, specifier,
               selection_id, selection_name, odds, source, snapshot_time
        from odds_market_snapshots
        {match_clause}
        where odds is not null
        order by match_id asc, source asc, market_id asc, specifier asc, selection_id asc, snapshot_time asc
        """,
        tuple(params),
    )

    stats = Stats()

    last_key: tuple[str, str, str, str, str] | None = None
    last_odds: float | None = None
    last_row: sqlite3.Row | None = None

    def flush_last(force: bool) -> None:
        nonlocal last_row, last_odds, last_key
        if last_row is None or last_key is None:
            return
        if force:
            _insert_change_row(conn, last_row)
            stats.inserted += 1
        # reset group
        last_row = None
        last_odds = None
        last_key = None

    t0 = time.time()
    for row in cursor:
        stats.scanned += 1
        key = (
            str(row["match_id"] or ""),
            str(row["source"] or ""),
            str(row["market_id"] or row["market_name"] or ""),
            str(row["specifier"] or ""),
            str(row["selection_id"] or row["selection_name"] or ""),
        )
        odds = _float(row["odds"])
        if odds is None:
            continue

        if last_key is None:
            # start new group: always keep the first observation
            last_key = key
            last_odds = odds
            last_row = row
            _insert_change_row(conn, row)
            stats.inserted += 1
            stats.groups += 1
            continue

        if key != last_key:
            # close previous group: keep the last observation too
            flush_last(force=True)
            last_key = key
            last_odds = odds
            last_row = row
            _insert_change_row(conn, row)
            stats.inserted += 1
            stats.groups += 1
            continue

        # within group: write only if meaningfully changed
        if last_odds is None or abs(odds - last_odds) >= float(min_change):
            _insert_change_row(conn, row)
            stats.inserted += 1
            last_odds = odds
        last_row = row

        if commit_every and stats.inserted and stats.inserted % int(commit_every) == 0:
            conn.commit()
            elapsed = round(time.time() - t0, 1)
            print(f"progress inserted={stats.inserted} scanned={stats.scanned} groups={stats.groups} seconds={elapsed}")

    # force the final row of the final group
    flush_last(force=True)
    conn.commit()
    elapsed = round(time.time() - t0, 1)
    print(f"done inserted={stats.inserted} scanned={stats.scanned} groups={stats.groups} seconds={elapsed}")
    return stats


def _insert_change_row(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    match_id = str(row["match_id"] or "")
    source = str(row["source"] or "unknown")
    market_id = str(row["market_id"] or row["market_name"] or "Market")
    specifier = str(row["specifier"] or "")
    selection_id = str(row["selection_id"] or row["selection_name"] or "Selection")
    odds = _float(row["odds"])
    conn.execute(
        """
        insert into odds_market_changes (
            match_id, match_name, match_date, market_id, market_name, specifier,
            selection_id, selection_name, odds, probability, source, snapshot_time
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            match_id,
            row["match_name"],
            row["match_date"],
            market_id,
            row["market_name"],
            specifier,
            selection_id,
            row["selection_name"],
            odds,
            _implied(odds),
            source,
            row["snapshot_time"],
        ),
    )
    conn.execute(
        """
        insert into odds_market_change_state (
            match_id, source, market_id, specifier, selection_id, last_odds, updated_at
        ) values (?, ?, ?, ?, ?, ?, current_timestamp)
        on conflict(match_id, source, market_id, specifier, selection_id) do update set
            last_odds = excluded.last_odds,
            updated_at = current_timestamp
        """,
        (match_id, source, market_id, specifier, selection_id, odds),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Convert odds_market_snapshots into lean per-selection change points (odds_market_changes)."
    )
    parser.add_argument("--db", required=True, help="Path to predictx_memory.sqlite3")
    parser.add_argument("--min-change", type=float, default=0.01, help="Minimum absolute odds delta to record a new point.")
    parser.add_argument("--commit-every", type=int, default=50000, help="Commit interval for long runs.")
    parser.add_argument("--limit-matches", type=int, default=0, help="Optional: only process first N match_ids for a test run.")
    parser.add_argument("--drop-old", action="store_true", help="Drop odds_market_snapshots after conversion (requires VACUUM to reclaim space).")
    args = parser.parse_args(argv)

    db = Path(args.db).expanduser().resolve()
    if not db.exists():
        print(f"error: db not found: {db}", file=sys.stderr)
        return 2

    conn = _connect(db)
    try:
        stats = lean_market_table(
            conn,
            min_change=float(args.min_change),
            commit_every=int(args.commit_every),
            limit_matches=(int(args.limit_matches) or None),
        )
        if args.drop_old:
            if _table_exists(conn, "odds_market_snapshots"):
                print("dropping odds_market_snapshots ...")
                conn.execute("drop table odds_market_snapshots")
                conn.commit()
                print("vacuuming ...")
                conn.execute("vacuum")
                conn.commit()
        print(f"result inserted={stats.inserted} scanned={stats.scanned} groups={stats.groups}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

