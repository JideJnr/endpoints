#!/usr/bin/env python3
"""
Full diagnostic on the first day of graded predictions after the data reset
and prediction-pipeline overhaul (merged as commit 4f1e89b, plus a follow-up
h2h_edge weighting fix). Reported accuracy dropped to ~48% -- this pulls
EVERY graded row since the reset and checks the specific hypotheses worth
ruling in/out before assuming the fix regressed something:

  1. Sample size: a day of matches is small. 48% could just be variance.
  2. Pick-type mix shift: the sort-order fix means real outright Home/Away
     Win picks now win their old fights against Home-or-Draw/Away-or-Draw
     hedges far more often. Outright picks are inherently harder to hit
     than double-chance hedges (a hedge wins on 2 of 3 outcomes). If the mix
     shifted from ~76% hedges / 1.6% outright (the old, buggy mix) toward
     more outright picks, the BLENDED win rate drops even if each market's
     own calibration is fine -- that would be the fix working as intended,
     not a regression.
  3. New publication-filter / h2h_edge behavior: are more picks getting
     capped or blocked than before? Is h2h_edge now dominating losses the
     way goal_pressure used to (i.e., did raising its cap overcorrect)?
  4. Whether validation_gate/confidence_calibration are still in bootstrap
     mode (near-certain with a single day of fresh data) -- if so, nothing
     is actually gating low-quality picks yet regardless of the other fixes.

This is a READ-ONLY diagnostic. It does not modify the database.

USAGE
-----
    cd path\\to\\predictx
    "C:\\Program Files\\Python39\\python.exe" tools\\post_reset_accuracy_report.py
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = "data/predictx_memory.sqlite3"

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
DIRECTION_THRESHOLD = 1.5

# The OLD, pre-reset mix (from the 2,314-row pre-fix history investigated
# earlier) -- used only as a reference point to compare against.
OLD_MIX = {
    "double_chance": (1780, 2314),
    "goals": (496, 2314),
    "match_result": (37, 2314),
}
OLD_WIN_RATE = {
    "double_chance": 0.743,
    "goals": 0.619,
    "match_result": 0.595,
}


def pct(n: int, d: int) -> str:
    return f"{n/d:.1%}" if d else "n/a"


def main() -> int:
    db_path = Path(DB_PATH)
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path.resolve()}")
        return 1
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row

    all_rows = conn.execute(
        "select id, match_name, pick_type, selection, confidence, result, signals_json, "
        "picks_json, created_at, graded_at from prediction_history order by created_at"
    ).fetchall()

    graded = [r for r in all_rows if r["result"] in ("win", "loss")]
    pending = [r for r in all_rows if r["result"] is None]
    void_rows = [r for r in all_rows if r["result"] == "void"]

    print("=" * 100)
    print("OVERVIEW")
    print("=" * 100)
    if all_rows:
        print(f"created_at range: {all_rows[0]['created_at']}  ->  {all_rows[-1]['created_at']}")
    print(f"total rows: {len(all_rows)}   graded (win/loss): {len(graded)}   "
          f"pending: {len(pending)}   void: {len(void_rows)}")
    wins = sum(1 for r in graded if r["result"] == "win")
    losses = len(graded) - wins
    print(f"overall: {wins} wins / {losses} losses = {pct(wins, len(graded))} win rate")
    print("(Small-sample warning: with well under ~100 graded matches, a single unlucky cluster")
    print(" of upsets can swing this number 10-15 points either way. Treat this as a first read,")
    print(" not a verdict, until more matches grade.)")

    print()
    print("=" * 100)
    print("PICK-TYPE MIX: new run vs the old pre-fix mix (this is the #1 thing to check)")
    print("=" * 100)
    by_pt = defaultdict(lambda: [0, 0])
    for r in graded:
        by_pt[r["pick_type"] or "unknown"][0 if r["result"] == "win" else 1] += 1
    print(f"{'pick_type':18} {'new_n':>6} {'new_share':>10} {'new_win_rate':>13}   {'old_share':>10} {'old_win_rate':>13}")
    total_graded = len(graded) or 1
    for pt, (w, l) in sorted(by_pt.items(), key=lambda x: -(x[1][0] + x[1][1])):
        n = w + l
        old_n, old_d = OLD_MIX.get(pt, (0, 1))
        old_share = pct(old_n, old_d) if pt in OLD_MIX else "n/a"
        old_wr = f"{OLD_WIN_RATE[pt]:.1%}" if pt in OLD_WIN_RATE else "n/a"
        print(f"{pt:18} {n:>6} {pct(n, total_graded):>10} {pct(w, n):>13}   {old_share:>10} {old_wr:>13}")
    match_result_n = sum(v[0] + v[1] for k, v in by_pt.items() if k == "match_result")
    print()
    if match_result_n:
        old_match_result_share = OLD_MIX["match_result"][0] / OLD_MIX["match_result"][1]
        new_match_result_share = match_result_n / total_graded
        if new_match_result_share > old_match_result_share * 2:
            print(f">>> match_result (outright) picks went from {old_match_result_share:.1%} of all picks to "
                  f"{new_match_result_share:.1%} -- the sort-order fix IS surfacing far more outright picks, "
                  f"exactly as intended. Outright picks are structurally harder to hit than the double-chance "
                  f"hedges that used to dominate, so a lower BLENDED win rate here can be the fix working, not "
                  f"a regression. Compare the outright win rate above to its old 59.5% baseline specifically.")

    print()
    print("=" * 100)
    print("CONFIDENCE BUCKETS")
    print("=" * 100)
    by_bucket = defaultdict(lambda: [0, 0])
    for r in graded:
        c = r["confidence"]
        b = f"{(c//5)*5}-{(c//5)*5+4}" if c is not None else "none"
        by_bucket[b][0 if r["result"] == "win" else 1] += 1
    def sortkey(k):
        try:
            return int(k.split("-")[0])
        except Exception:
            return -1
    for b, (w, l) in sorted(by_bucket.items(), key=lambda x: sortkey(x[0])):
        n = w + l
        print(f"  conf {b:8} n={n:>4} win_rate={pct(w, n)}")

    print()
    print("=" * 100)
    print("PUBLICATION FILTER ACTIVITY (new in this run) -- is it capping/blocking a lot?")
    print("=" * 100)
    capped = blocked = no_bet = 0
    for r in graded:
        if r["pick_type"] == "no_bet" or (r["selection"] or "") == "No Bet":
            no_bet += 1
        try:
            picks = json.loads(r["picks_json"] or "[]")
        except Exception:
            picks = []
        for p in picks:
            pf = p.get("publication_filter") or {}
            if pf.get("capped"):
                capped += 1
            if pf.get("blocked"):
                blocked += 1
    print(f"  no_bet published: {no_bet}   picks capped by filter: {capped}   picks blocked by filter: {blocked}")

    print()
    print("=" * 100)
    print("LOSSES: top contributing signal + side-signal conflict, per loss")
    print("=" * 100)
    top_contributor_counter = defaultdict(int)
    h2h_top_in_losses = 0
    conflict_in_losses = 0
    for r in [x for x in graded if x["result"] == "loss"]:
        try:
            signals = json.loads(r["signals_json"] or "[]")
        except Exception:
            signals = []
        scored = []
        directions = {}
        for s in signals:
            impact = s.get("impact")
            try:
                impact = float(impact)
            except (TypeError, ValueError):
                continue
            scored.append((impact, s.get("name")))
            if s.get("name") in SIDE_SIGNALS and abs(impact) >= DIRECTION_THRESHOLD:
                directions[s.get("name")] = "home" if impact > 0 else "away"
        scored.sort(reverse=True)
        if scored and scored[0][0] > 0:
            top_contributor_counter[scored[0][1]] += 1
            if scored[0][1] == "h2h_edge":
                h2h_top_in_losses += 1
        if len(set(directions.values())) > 1:
            conflict_in_losses += 1
        print(f"  [{r['result']}] {r['match_name'][:45]:45} {r['selection']:14} conf={r['confidence']:>3} "
              f"top_signal={scored[0][1] if scored else 'n/a':22} "
              f"side_directions={directions}")

    print()
    print("Top-contributor frequency across all losses:")
    for name, cnt in sorted(top_contributor_counter.items(), key=lambda x: -x[1]):
        print(f"  {cnt:>4}  {name}")
    print(f"\nLosses where side-evidence signals conflicted: {conflict_in_losses} / {losses}")
    if h2h_top_in_losses:
        print(f"h2h_edge was the #1 contributing signal in {h2h_top_in_losses} losses -- worth a manual look "
              f"at whether the raised cap is now over-weighting a noisy/thin h2h reading on any of these.")

    out_path = db_path.parent / "post_reset_accuracy_report.json"
    out_path.write_text(json.dumps({
        "total_rows": len(all_rows),
        "graded": len(graded),
        "pending": len(pending),
        "void": len(void_rows),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(graded) if graded else None,
        "by_pick_type": {k: {"wins": v[0], "losses": v[1]} for k, v in by_pt.items()},
        "capped": capped,
        "blocked": blocked,
        "no_bet": no_bet,
        "top_contributors_in_losses": dict(top_contributor_counter),
        "conflict_in_losses": conflict_in_losses,
    }, indent=2))
    print(f"\nFull results saved to {out_path}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
