#!/usr/bin/env python3
"""
Full "why did it pick that" breakdown for specific match_ids, PLUS a check of
whether /analytics/model-explorer would actually show these matches at all
given its 2000-row cap.

WHY THIS EXISTS
---------------
The frontend match pages give you a match_id directly (e.g. sr:match:...,
sofascore:...) instead of team names, so this looks matches up exactly by
that id across BOTH tables that hold predictions:
  - prediction_history       (the actual PUBLISHED pick for the match)
  - prediction_candidate_history (every candidate the pipeline considered,
    including ones that were NOT published -- this is where you can see
    "the system actually scored Home higher, but published Away anyway,
    and here's why" if that's what happened)

It also replicates the exact query /analytics/model-explorer uses (see
app/routers/frontend.py::get_model_explorer) to check whether a given
match_id would even be visible on that page -- that endpoint unions
prediction_history + prediction_candidate_history, orders by created_at
DESC, and only keeps the newest 2000 rows BEFORE any filtering. Since
prediction_candidate_history logs far more rows per match than
prediction_history (multiple candidates per match vs one published pick),
a busy day can push older matches out of that window entirely, so a match
can be graded and correct in the database while still not appearing on
that particular page.

This is a READ-ONLY diagnostic. It does not modify the database.

USAGE
-----
    cd path\\to\\predictx
    "C:\\Program Files\\Python39\\python.exe" tools\\explain_by_match_id.py
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_PATH = "data/predictx_memory.sqlite3"

MATCH_IDS = [
    "sr:match:68161514",
    "sr:match:68161504",
    "sofascore:16361896",
]

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

MODEL_EXPLORER_LIMIT = 2000


def _fmt_value(name: str, value) -> str:
    if isinstance(value, dict):
        if name == "h2h_edge":
            return (f"home_wins={value.get('home_wins', value.get('home_weighted_wins'))} "
                    f"away_wins={value.get('away_wins', value.get('away_weighted_wins'))} "
                    f"draws={value.get('draws', value.get('weighted_draws'))} "
                    f"sample={value.get('sample_size', value.get('weighted_sample'))}")
        return json.dumps(value)[:150]
    return str(value)


def explain_signals(signals_json: str, label: str) -> None:
    try:
        signals = json.loads(signals_json or "[]")
    except Exception:
        signals = []
    scored = []
    for s in signals:
        impact = s.get("impact")
        try:
            impact = float(impact)
        except (TypeError, ValueError):
            continue
        scored.append((impact, s.get("name"), s.get("value")))
    scored.sort(key=lambda x: -abs(x[0]))

    print(f"\n  [{label}] Top 6 signals by |impact|:")
    for impact, name, value in scored[:6]:
        print(f"      {impact:+7.2f}  {name:28} {_fmt_value(name, value)}")

    directions = {}
    for s in signals:
        name = s.get("name")
        if name not in SIDE_SIGNALS:
            continue
        impact = s.get("impact")
        try:
            impact = float(impact)
        except (TypeError, ValueError):
            continue
        if abs(impact) < DIRECTION_THRESHOLD:
            continue
        directions[name] = "HOME" if impact > 0 else "AWAY"
    home_votes = [n for n, d in directions.items() if d == "HOME"]
    away_votes = [n for n, d in directions.items() if d == "AWAY"]
    print(f"  [{label}] Side-evidence vote: HOME={home_votes}  AWAY={away_votes}")

    ensemble_sig = next((s for s in signals if s.get("name") == "ensemble_model"), None)
    if ensemble_sig:
        probs = (ensemble_sig.get("value") or {}).get("probabilities") or {}
        print(f"  [{label}] Ensemble probabilities: home={probs.get('home_win')}  "
              f"draw={probs.get('draw')}  away={probs.get('away_win')}")

    elo_sig = next((s for s in signals if s.get("name") == "elo_model"), None)
    if elo_sig:
        v = elo_sig.get("value") or {}
        print(f"  [{label}] Elo: home_win_prob={v.get('home_win_probability')}  "
              f"away_win_prob={v.get('away_win_probability')}  elo_diff={v.get('elo_diff')}")


def main() -> int:
    db_path = Path(DB_PATH)
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path.resolve()}")
        return 1
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row

    for match_id in MATCH_IDS:
        print("=" * 100)
        print(f"MATCH_ID: {match_id}")
        print("=" * 100)

        ph_rows = conn.execute(
            "select * from prediction_history where match_id = ? order by created_at", (match_id,)
        ).fetchall()
        pch_rows = conn.execute(
            "select * from prediction_candidate_history where match_id = ? order by created_at, "
            "case role when 'primary' then 0 else 1 end",
            (match_id,),
        ).fetchall()

        if not ph_rows and not pch_rows:
            print("  NOT FOUND in prediction_history or prediction_candidate_history for this exact match_id.")
            print("  (Double check the id format matches exactly what's stored -- try without the")
            print("   'sr:match:' / 'sofascore:' prefix if this comes back empty, in case the DB")
            print("   stores it differently.)")
            print()
            continue

        for row in ph_rows:
            print(f"\n--- PUBLISHED (prediction_history id={row['id']}) ---")
            print(f"  {row['match_name']}  pick_type={row['pick_type']}  selection={row['selection']}  "
                  f"confidence={row['confidence']}  result={row['result']}  created_at={row['created_at']}")
            explain_signals(row["signals_json"], "published")
            try:
                picks = json.loads(row["picks_json"] or "[]")
            except Exception:
                picks = []
            for p in picks:
                pf = p.get("publication_filter")
                if pf:
                    print(f"  publication_filter on '{p.get('selection')}': {json.dumps(pf)[:400]}")
                if p.get("clear_winner"):
                    print(f"  clear_winner=True on '{p.get('selection')}'")
            # alternatives that were computed but not published, if recorded on the primary pick
            for p in picks:
                if p.get("role") == "alternative" or p != (picks[0] if picks else None):
                    print(f"  alternative candidate in picks_json: {p.get('selection')} "
                          f"conf={p.get('confidence')} role={p.get('role')}")

        if pch_rows:
            print(f"\n--- ALL CANDIDATES CONSIDERED (prediction_candidate_history, {len(pch_rows)} rows) ---")
            for row in pch_rows:
                print(f"\n  candidate id={row['id']} role={row['role']} selection={row['selection']} "
                      f"pick_type={row['pick_type']} confidence={row['confidence']} result={row['result']}")
                explain_signals(row["signals_json"], f"candidate:{row['selection']}")
        print()

    # --- model-explorer visibility check ---
    print("=" * 100)
    print(f"MODEL-EXPLORER VISIBILITY CHECK (replicates the union+limit={MODEL_EXPLORER_LIMIT} query)")
    print("=" * 100)
    union_rows = conn.execute(
        f"""
        select id, match_id, created_at, 'prediction_history' as source_table
        from prediction_history where pick_type != 'no_bet'
        union all
        select id, match_id, created_at, 'candidate_history' as source_table
        from prediction_candidate_history where pick_type != 'no_bet'
        order by created_at desc
        """
    ).fetchall()
    total_union = len(union_rows)
    print(f"Total rows across both tables (excluding no_bet): {total_union}")
    print(f"model-explorer only keeps the newest {MODEL_EXPLORER_LIMIT} of these before filtering.\n")
    for match_id in MATCH_IDS:
        positions = [i for i, r in enumerate(union_rows) if r["match_id"] == match_id]
        if not positions:
            print(f"  {match_id}: not present in the union at all (not in either table, or pick_type='no_bet').")
            continue
        best_rank = min(positions)
        visible = best_rank < MODEL_EXPLORER_LIMIT
        print(f"  {match_id}: {len(positions)} row(s), best rank #{best_rank+1} of {total_union} "
              f"-> {'VISIBLE' if visible else 'TRUNCATED OUT of the default limit=2000 view'}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
