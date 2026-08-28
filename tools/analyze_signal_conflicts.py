#!/usr/bin/env python3
"""
Find signals that are individually reliable but routinely get overridden by
another signal when the two disagree on direction (home vs away) -- the same
pattern found manually for h2h_edge vs recent_history_edge, generalized to
every pair of "side evidence" signals.

WHY THIS EXISTS
---------------
The pick-direction logic (app/enrichment/enriched_prediction.py,
app/ai/prediction_agent.py) combines several signals that each independently
argue home or away: h2h_edge, odds_edge, common_opponent_edge,
recent_history_edge, league_position_edge, venue_form_edge, avg_rating_edge,
market_steam. When two of them disagree, whichever one actually matches the
published pick's direction effectively "won" that disagreement (by raw scale,
by being consulted in a support-gate check, or by chance). This script looks
at every graded prediction where two of these signals disagreed, records
which one's direction matched the published pick, and checks whether that
pick actually won. If a signal is frequently the "loser" of these
disagreements AND picks that follow the signal that beat it do WORSE than
picks would have if that signal had been followed instead, that signal is
underweighted the same way h2h_edge was.

This is a READ-ONLY analysis script. It does not modify the database.

USAGE
-----
    cd path\\to\\predictx
    "C:\\Program Files\\Python39\\python.exe" tools\\analyze_signal_conflicts.py

Add --min-n to change the minimum sample size a pair/signal needs before
being reported (default 15).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from itertools import combinations
from pathlib import Path

# Same 8 signals the pick-direction logic itself treats as side evidence
# (see _rules_side_signal_total / _directional_signal_map in
# app/enrichment/enriched_prediction.py and app/ai/prediction_agent.py).
SIDE_SIGNALS = [
    "h2h_edge",
    "odds_edge",
    "common_opponent_edge",
    "recent_history_edge",
    "league_position_edge",
    "venue_form_edge",
    "avg_rating_edge",
    "market_steam",
]

# Same threshold _directional_signal_map uses before treating a signal's
# impact as a real directional opinion rather than noise.
DIRECTION_THRESHOLD = 1.5

# Only these selections have an unambiguous home/away direction. "Home or
# Away" excludes the draw but not a side, and goals-market selections
# (BTTS/Over/Under) aren't about home/away at all, so both are skipped.
SELECTION_DIRECTION = {
    "home win": "home",
    "away win": "away",
    "home or draw": "home",
    "away or draw": "away",
}


def _signal_directions(signals_json: str) -> dict[str, str]:
    try:
        signals = json.loads(signals_json)
    except Exception:
        return {}
    directions: dict[str, str] = {}
    if not isinstance(signals, list):
        return directions
    for signal in signals:
        name = signal.get("name")
        if name not in SIDE_SIGNALS:
            continue
        impact = signal.get("impact")
        try:
            impact = float(impact)
        except (TypeError, ValueError):
            continue
        if abs(impact) < DIRECTION_THRESHOLD:
            continue
        directions[name] = "home" if impact > 0 else "away"
    return directions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/predictx_memory.sqlite3")
    parser.add_argument("--min-n", type=int, default=15, help="minimum sample size to report a pair/signal")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path.resolve()}")
        return 1

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    rows = conn.execute(
        "select id, match_name, pick_type, selection, confidence, result, signals_json "
        "from prediction_history where result in ('win','loss')"
    ).fetchall()
    conn.close()

    # pair_stats[(winner, loser)] -> [wins, losses]  (winner = signal whose
    # direction matched the published pick; loser = the signal it overrode)
    pair_stats: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    # signal_followed[S] -> [wins, losses] when S's direction was followed
    # over some disagreeing signal
    signal_followed: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    # signal_overridden[S] -> [wins, losses] of the pick that resulted when S
    # was overridden by some other disagreeing signal
    signal_overridden: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    neither_count = 0
    considered = 0

    for _id, _match, _pick_type, selection, _confidence, result, signals_json in rows:
        direction_pick = SELECTION_DIRECTION.get(str(selection or "").strip().lower())
        if direction_pick is None:
            continue
        directions = _signal_directions(signals_json)
        if len(directions) < 2:
            continue
        outcome_idx = 0 if result == "win" else 1

        for a, b in combinations(sorted(directions), 2):
            da, db = directions[a], directions[b]
            if da == db:
                continue  # not a conflict
            considered += 1
            a_matches = da == direction_pick
            b_matches = db == direction_pick
            if a_matches and not b_matches:
                pair_stats[(a, b)][outcome_idx] += 1
                signal_followed[a][outcome_idx] += 1
                signal_overridden[b][outcome_idx] += 1
            elif b_matches and not a_matches:
                pair_stats[(b, a)][outcome_idx] += 1
                signal_followed[b][outcome_idx] += 1
                signal_overridden[a][outcome_idx] += 1
            else:
                neither_count += 1

    def win_rate(wl: list[int]) -> tuple[int, float]:
        n = wl[0] + wl[1]
        return n, (wl[0] / n if n else 0.0)

    print(f"Graded rows considered: {len(rows)}")
    print(f"Directional signal-pair disagreements found: {considered} (neither side matched the pick in {neither_count} of them)")
    print()

    print("=== Per-signal: win rate when FOLLOWED (beat a disagreeing signal) vs when OVERRIDDEN (lost to one) ===")
    print(f"{'signal':22} {'followed_n':>11} {'followed_wr':>12} {'overridden_n':>13} {'overridden_wr':>14} {'gap':>7}  underweighted?")
    summary_rows = []
    for s in SIDE_SIGNALS:
        fn, fwr = win_rate(signal_followed[s])
        on, owr = win_rate(signal_overridden[s])
        if fn < args.min_n or on < args.min_n:
            continue
        gap = fwr - owr
        flag = "<-- YES, being overridden costs accuracy" if gap > 0.05 else ""
        print(f"{s:22} {fn:>11} {fwr:>11.1%} {on:>13} {owr:>13.1%} {gap:>+6.1%}  {flag}")
        summary_rows.append({"signal": s, "followed_n": fn, "followed_win_rate": fwr, "overridden_n": on, "overridden_win_rate": owr, "gap": gap})
    print()
    print("Reading this: 'followed' = picks where this signal's direction was published despite another")
    print("signal disagreeing. 'overridden' = picks where this signal disagreed but a DIFFERENT signal's")
    print("direction was published instead. A positive gap means picks that ignored this signal did worse")
    print("than picks that followed it -- i.e. this signal deserved more weight than it's getting.")
    print()

    print(f"=== Specific pairs (winner beat loser), min n={args.min_n}, sorted by win-rate gap vs overall ===")
    pair_rows = []
    for (winner, loser), wl in pair_stats.items():
        n, wr = win_rate(wl)
        if n < args.min_n:
            continue
        pair_rows.append((winner, loser, n, wr, wl))
    pair_rows.sort(key=lambda r: r[3])
    print(f"{'winner (followed)':22} {'loser (overridden)':22} {'n':>5} {'win_rate':>9}")
    for winner, loser, n, wr, wl in pair_rows:
        print(f"{winner:22} {loser:22} {n:>5} {wr:>8.1%}")
    print()
    print("Low win_rate here means: when <winner> beat <loser> in a disagreement and the pick followed")
    print("<winner>, that pick often lost -- i.e. <loser> should probably have won that argument instead.")

    out_path = db_path.parent / "signal_conflict_analysis.json"
    out_path.write_text(json.dumps({
        "per_signal": summary_rows,
        "pairs": [
            {"winner": w, "loser": l, "n": n, "win_rate": wr}
            for w, l, n, wr, _ in pair_rows
        ],
    }, indent=2))
    print(f"\nFull results saved to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
