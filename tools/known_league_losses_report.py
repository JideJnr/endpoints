#!/usr/bin/env python3
"""
Pull every LOSS in a known (TOP_30 catalogue) league from prediction_history,
with full context, for manual/human analysis of why they lost.

READ-ONLY. Does not modify the database.

USAGE
-----
    cd path\\to\\predictx
    python3 tools\\known_league_losses_report.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3

from app.competition.league_strength import _catalogue_score

DB_PATH = "data/predictx_memory.sqlite3"

SIDE_SIGNALS = [
    "h2h_edge", "odds_edge", "common_opponent_edge", "recent_history_edge",
    "league_position_edge", "venue_form_edge", "avg_rating_edge", "market_steam",
]


def pct(n, d):
    return f"{n/d:.1%}" if d else "n/a"


def main() -> int:
    db_path = Path(DB_PATH)
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=10)
    con.execute("PRAGMA mmap_size=0")
    con.row_factory = sqlite3.Row

    rows = con.execute(
        "select id, match_name, league_name, country_name, pick_type, selection, "
        "confidence, reason, result, final_home, final_away, created_at, graded_at, "
        "signals_json, picks_json, signal_combination_key, engine, prediction_mode "
        "from prediction_history where result in ('win','loss')"
    ).fetchall()

    known_losses = []
    known_wins_n = 0
    for r in rows:
        if _catalogue_score(r["league_name"] or "", country=r["country_name"]) is None:
            continue
        if r["result"] == "win":
            known_wins_n += 1
            continue
        known_losses.append(r)

    print("=" * 100)
    print(f"KNOWN-LEAGUE LOSSES: {len(known_losses)}  (known wins: {known_wins_n}, "
          f"known total: {len(known_losses) + known_wins_n})")
    print("=" * 100)

    by_league = defaultdict(int)
    by_pick_type = defaultdict(int)
    by_conf_bucket = defaultdict(int)
    by_signal_combo = defaultdict(int)
    top_contributor_counter = defaultdict(int)
    conflict_count = 0

    full_dump = []

    for r in known_losses:
        by_league[r["league_name"] or "(none)"] += 1
        by_pick_type[r["pick_type"] or "unknown"] += 1
        c = r["confidence"]
        b = f"{(c//5)*5}-{(c//5)*5+4}" if c is not None else "none"
        by_conf_bucket[b] += 1
        by_signal_combo[r["signal_combination_key"] or "(none)"] += 1

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
            if s.get("name") in SIDE_SIGNALS and abs(impact) >= 1.5:
                directions[s.get("name")] = "home" if impact > 0 else "away"
        scored.sort(reverse=True)
        top_signal = scored[0][1] if scored else None
        if top_signal:
            top_contributor_counter[top_signal] += 1
        has_conflict = len(set(directions.values())) > 1
        if has_conflict:
            conflict_count += 1

        full_dump.append({
            "id": r["id"],
            "match": r["match_name"],
            "league": r["league_name"],
            "country": r["country_name"],
            "pick_type": r["pick_type"],
            "selection": r["selection"],
            "confidence": r["confidence"],
            "reason": r["reason"],
            "final_score": f"{r['final_home']}-{r['final_away']}" if r["final_home"] is not None else None,
            "created_at": r["created_at"],
            "graded_at": r["graded_at"],
            "engine": r["engine"],
            "prediction_mode": r["prediction_mode"],
            "signal_combination_key": r["signal_combination_key"],
            "top_signal": top_signal,
            "side_signal_conflict": has_conflict,
            "top_5_signals": scored[:5],
        })

        print(f"[{r['id']:>6}] {(r['match_name'] or '')[:38]:38} "
              f"{(r['league_name'] or '')[:22]:22} {r['pick_type'] or '':14} "
              f"{r['selection'] or '':16} conf={r['confidence']:>3} "
              f"score={full_dump[-1]['final_score'] or 'n/a':6} "
              f"top_sig={top_signal or 'n/a':20} conflict={has_conflict}")

    print()
    print("=" * 100)
    print("BY LEAGUE (losses)")
    print("=" * 100)
    for ln, n in sorted(by_league.items(), key=lambda x: -x[1]):
        print(f"  {ln[:40]:40} {n}")

    print()
    print("BY PICK TYPE (losses)")
    for pt, n in sorted(by_pick_type.items(), key=lambda x: -x[1]):
        print(f"  {pt:20} {n}")

    print()
    print("BY CONFIDENCE BUCKET (losses)")
    def sortkey(k):
        try:
            return int(k.split("-")[0])
        except Exception:
            return -1
    for b, n in sorted(by_conf_bucket.items(), key=lambda x: sortkey(x[0])):
        print(f"  conf {b:8} {n}")

    print()
    print("BY SIGNAL_COMBINATION_KEY (losses, top 20)")
    for combo, n in sorted(by_signal_combo.items(), key=lambda x: -x[1])[:20]:
        print(f"  {n:>3}  {combo}")

    print()
    print("TOP CONTRIBUTING SIGNAL IN LOSSES")
    for name, n in sorted(top_contributor_counter.items(), key=lambda x: -x[1]):
        print(f"  {n:>3}  {name}")
    print(f"\nSide-signal direction conflicts in known-league losses: {conflict_count} / {len(known_losses)}")

    out_path = db_path.parent / "known_league_losses.json"
    out_path.write_text(json.dumps(full_dump, indent=2, ensure_ascii=False))
    print(f"\nFull per-loss dump saved to {out_path}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
