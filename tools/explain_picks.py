#!/usr/bin/env python3
"""
Explain exactly why the pipeline picked what it picked for a specific set of
graded matches: which signal had the biggest impact, what the 8 side-evidence
signals (h2h_edge, odds_edge, common_opponent_edge, recent_history_edge,
league_position_edge, venue_form_edge, avg_rating_edge, market_steam) each
argued, whether they conflicted, and whether any publication filter
capped/blocked the pick.

This is a READ-ONLY diagnostic. It does not modify the database.

USAGE
-----
    cd path\\to\\predictx
    "C:\\Program Files\\Python39\\python.exe" tools\\explain_picks.py

The list of matches to explain is hardcoded below (GAMES) -- these are the
11 post-reset predictions the user asked about on 2026-08-29. Edit GAMES to
investigate a different batch later; matching is by home/away team name
(case-insensitive substring) plus the given date, so it's robust to exact
game-id/provider differences.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_PATH = "data/predictx_memory.sqlite3"

# (home_team, away_team, date_prefix) -- date_prefix matches created_at's
# leading "YYYY-MM-DD"; these are all 2026-08-29 per the user's report.
GAMES = [
    ("Melville United AFC", "Auckland City FC", "2026-08-29"),
    ("Tauro FC", "CA Independiente de La Chorrera", "2026-08-29"),
    ("La Familia FC", "CA Independiente B", "2026-08-29"),
    ("Caroline Springs George Cross FC", "South Melbourne FC", "2026-08-29"),
    ("Dimas Escazu", "LD Alajuelense", "2026-08-29"),
    ("Petone FC", "Island Bay United", "2026-08-29"),
    ("East Coast Bays", "Auckland United FC", "2026-08-29"),
    ("Sporting FC", "Inter San Carlos", "2026-08-29"),
    ("CD Suchitepequez", "CSD Coban Imperial", "2026-08-29"),
    ("Monaro Panthers FC", "Canberra Juventus FC", "2026-08-29"),
    ("Juticalpa FC", "Lobos UPNFM", "2026-08-29"),
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

# Signals whose "value" is just a number/small scalar worth printing inline;
# everything else gets a short type summary instead of a huge nested dump.
SIMPLE_VALUE_SIGNALS = {"recent_history_edge", "avg_rating_edge", "league_strength_edge"}


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _fmt_value(name: str, value) -> str:
    if isinstance(value, dict):
        if name == "h2h_edge":
            return (f"home_wins={value.get('home_wins', value.get('home_weighted_wins'))} "
                    f"away_wins={value.get('away_wins', value.get('away_weighted_wins'))} "
                    f"draws={value.get('draws', value.get('weighted_draws'))} "
                    f"sample={value.get('sample_size', value.get('weighted_sample'))}")
        if name in ("venue_form_edge", "league_position_edge", "common_opponent_edge"):
            keys = [k for k in ("edge", "interpretation", "home_position", "away_position") if k in value]
            return ", ".join(f"{k}={value[k]}" for k in keys) or json.dumps(value)[:120]
        return json.dumps(value)[:150]
    return str(value)


def explain_row(row: dict) -> None:
    match_name = row["match_name"]
    selection = row["selection"]
    result = row["result"]
    confidence = row["confidence"]
    try:
        signals = json.loads(row["signals_json"] or "[]")
    except Exception:
        signals = []
    try:
        picks = json.loads(row["picks_json"] or "[]")
    except Exception:
        picks = []

    print("=" * 100)
    print(f"{match_name}  |  pick_type={row['pick_type']}  selection={selection}  "
          f"confidence={confidence}  result={result}")

    # Top contributing signals by absolute impact
    scored = []
    for s in signals:
        impact = s.get("impact")
        try:
            impact = float(impact)
        except (TypeError, ValueError):
            continue
        scored.append((impact, s.get("name"), s.get("value")))
    scored.sort(key=lambda x: -abs(x[0]))

    print("\n  Top 5 signals by |impact| (what actually drove the confidence number):")
    for impact, name, value in scored[:5]:
        print(f"    {impact:+7.2f}  {name:28} {_fmt_value(name, value)}")

    # The 8 side-evidence signals specifically, with direction
    print("\n  Side-evidence signals (home/away direction, threshold |impact|>=1.5):")
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
            print(f"    {name:22} impact={impact:+6.2f}  (below threshold, no vote)")
            continue
        direction = "HOME" if impact > 0 else "AWAY"
        directions[name] = direction
        print(f"    {name:22} impact={impact:+6.2f}  -> favors {direction}")
    missing = [s for s in SIDE_SIGNALS if not any(sig.get('name') == s for sig in signals)]
    if missing:
        print(f"    (not present for this match: {', '.join(missing)})")

    home_votes = [n for n, d in directions.items() if d == "HOME"]
    away_votes = [n for n, d in directions.items() if d == "AWAY"]
    print(f"\n  Vote count: HOME={len(home_votes)} {home_votes}  AWAY={len(away_votes)} {away_votes}")
    if home_votes and away_votes:
        print("  >>> CONFLICT: side-evidence signals disagree on direction.")
    elif home_votes or away_votes:
        print("  No conflict among side-evidence signals with a vote.")

    # Publication filter markers baked into the published pick, if any
    published = None
    for p in picks:
        if str(p.get("selection") or "") == str(selection or ""):
            published = p
            break
    if published:
        pf = published.get("publication_filter")
        if pf:
            print(f"\n  Publication filter applied: {json.dumps(pf)[:300]}")
        if published.get("clear_winner"):
            print("  This pick was flagged clear_winner=True.")
    conflict_sig = next((s for s in signals if s.get("name") == "directional_signal_conflict"), None)
    if conflict_sig:
        print(f"\n  directional_signal_conflict signal present: {json.dumps(conflict_sig.get('value'))[:300]}")

    ensemble_sig = next((s for s in signals if s.get("name") == "ensemble_model"), None)
    if ensemble_sig:
        probs = (ensemble_sig.get("value") or {}).get("probabilities") or {}
        print(f"\n  Ensemble probabilities: home={probs.get('home_win')}  draw={probs.get('draw')}  away={probs.get('away_win')}")
    print()


def main() -> int:
    db_path = Path(DB_PATH)
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path.resolve()}")
        return 1
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row

    found_any = False
    for home, away, date_prefix in GAMES:
        rows = conn.execute(
            "select * from prediction_history where created_at like ? order by created_at desc",
            (f"{date_prefix}%",),
        ).fetchall()
        match = None
        for r in rows:
            mn = _norm(r["match_name"] or "")
            if _norm(home) in mn and _norm(away) in mn:
                match = r
                break
        if not match:
            print("=" * 100)
            print(f"NOT FOUND in prediction_history: {home} vs {away} ({date_prefix}) -- "
                  "check prediction_candidate_history or confirm the reset/rerun actually wrote this match.")
            print()
            continue
        found_any = True
        explain_row(dict(match))

    conn.close()
    if not found_any:
        print("\nNone of the 11 games were found in prediction_history. If the reset+rerun just "
              "happened, double check the server is pointed at data/predictx_memory.sqlite3 and that "
              "these matches were graded (result is not null) rather than still pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
