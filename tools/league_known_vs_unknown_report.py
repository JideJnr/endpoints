#!/usr/bin/env python3
"""
Known-league vs. unknown-league win-rate report.

"Known" = the curated TOP_30_COMPETITIONS catalogue in
app/competition/competition_special.py (Premier League, Champions League,
etc). "Unknown" = everything else. Uses the app's own
app.competition.league_strength._catalogue_score() -- the same function
the live prediction engine uses to decide whether it knows a league --
so this report always reflects what the engine itself would classify,
not a separate ad hoc definition.

Built 2026-09-09 after auditing why several genuinely-curated leagues
(Belgian Pro League, Brasileirão Série A, Primeira Liga) were being
counted as "unknown" purely due to string-formatting differences between
how prediction_history.league_name is stored ("<country> <league>", e.g.
"Belgium Pro League") and how TOP_30_COMPETITIONS names them. That gap
is now fixed directly in _catalogue_score() (comma/stage-suffix
stripping, country-prefix stripping when a country is supplied, and an
alias table for sponsor-branded names) -- this script just calls it with
prediction_history's own country_name column.

READ-ONLY. Does not modify the database.

USAGE
-----
    cd path\\to\\predictx
    python3 tools\\league_known_vs_unknown_report.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3

from app.competition.league_strength import _catalogue_score

DB_PATH = "data/predictx_memory.sqlite3"


def pct(n: int, d: int) -> str:
    return f"{n / d:.1%}" if d else "n/a"


def main() -> int:
    db_path = Path(DB_PATH)
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path.resolve()}")
        return 1

    # NOTE: this DB is large and can live on a network/FUSE mount -- open
    # read-only + immutable and disable mmap, or sqlite can throw a spurious
    # "disk I/O error" on some mounts even though the file is perfectly fine.
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=10)
    con.execute("PRAGMA mmap_size=0")
    con.row_factory = sqlite3.Row

    rows = con.execute(
        "select league_name, country_name, result from prediction_history "
        "where result in ('win','loss')"
    ).fetchall()

    known = [0, 0]
    unknown = [0, 0]
    known_leagues: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    unknown_leagues: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    for r in rows:
        ln = r["league_name"] or "(none)"
        is_known = _catalogue_score(r["league_name"] or "", country=r["country_name"]) is not None
        idx = 0 if r["result"] == "win" else 1
        if is_known:
            known[idx] += 1
            known_leagues[ln][idx] += 1
        else:
            unknown[idx] += 1
            unknown_leagues[ln][idx] += 1

    def summary(label: str, bucket: list[int]) -> None:
        n = sum(bucket)
        print(f"{label:12} {bucket[0]}W-{bucket[1]}L   n={n:<5} win_rate={pct(bucket[0], n)}")

    print("=" * 80)
    print("KNOWN (TOP_30 catalogue) vs UNKNOWN league win rate")
    print("=" * 80)
    summary("KNOWN", known)
    summary("UNKNOWN", unknown)

    print()
    print("Known leagues (by volume):")
    for ln, (w, l) in sorted(known_leagues.items(), key=lambda x: -(x[1][0] + x[1][1])):
        n = w + l
        print(f"  {ln[:42]:42} {w}W-{l}L  n={n:>3}  {pct(w, n)}")

    print()
    print("Top 20 unknown leagues (by volume):")
    for ln, (w, l) in sorted(unknown_leagues.items(), key=lambda x: -(x[1][0] + x[1][1]))[:20]:
        n = w + l
        print(f"  {ln[:42]:42} {w}W-{l}L  n={n:>3}  {pct(w, n)}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
