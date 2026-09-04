"""Backtest: does the live scoreline-grid projection (`live_projection.py`)
actually beat doing nothing live-aware at all?

Reuses real historical data already sitting in your local SQLite DB:
- `prediction_history.models_json` for the pre-match Dixon-Coles lambda
  (home_lambda/away_lambda) that was already computed for that match.
- `live_stat_snapshots` for one real in-play snapshot per match (minute,
  live xG, shots on target/total) — this table only has real data where the
  enrich worker actually captured a live stats snapshot before the match
  finished.
- `finished_matches.raw_json.goal_events` to reconstruct the score AT that
  snapshot's minute (needed as an input to the live projection — a match
  can only be reconstructed if goal timings were captured for it).

Compares two approaches against the ACTUAL final result:
1. "naive" — the pre-match Dixon-Coles probabilities, completely ignoring
   the live snapshot (what you get today if you just never re-ran the model).
2. "live-adjusted" — `project_live_match()` from this same package, fed the
   pre-match lambda + the live snapshot.

Scored on: Brier score for the match-winner (1X2) distribution (lower is
better) and exact top-1 correct-score hit rate.

Run with: python -m app.live.backtest_live_projection
(from the predictx/ directory, inside your normal venv — this needs the
same sqlite file your app already uses, so run it locally, not through any
remote/mounted view of the repo.)

CAVEATS (read before trusting the numbers)
--------------------------------------------
- Sample size is whatever `live_stat_snapshots` currently holds intersected
  with reconstructable goal timings — likely small (a few dozen matches at
  most right now, since that table is written once per match rather than
  continuously). Treat any single run's numbers as a smoke test, not a
  statistically solid backtest, until that table has accumulated a lot more
  history — this script is exactly the tool to re-run periodically as it
  grows.
- Red cards are known-unmodelled (see live_projection.py docstring) — if a
  match in the sample had a card at/near the snapshot minute, expect that
  row specifically to be a miss; that is a real gap, not a bug in this
  script.
- "naive" here means "pre-match Dixon-Coles, frozen at kickoff" — it is a
  deliberately weak baseline (today's actual live picks in
  `enriched_prediction.py` are somewhat live-aware already, just not via a
  unified grid), so treat this as "does re-running the model live help at
  all", not "does this beat production".
"""

from __future__ import annotations

import json
import math
from typing import Any, Optional

from app.storage.db import DB_PATH
from app.models.dixon_coles import _tau, RHO
from app.models.poisson import _poisson_prob
from app.live.live_projection import LiveInputs, project


def _prematch_grid(mu: float, lam: float, max_goals: int = 7) -> dict[tuple[int, int], float]:
    grid: dict[tuple[int, int], float] = {}
    total = 0.0
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = _poisson_prob(mu, h) * _poisson_prob(lam, a) * _tau(h, a, mu, lam, RHO)
            grid[(h, a)] = p
            total += p
    return {k: v / total for k, v in grid.items()} if total > 0 else grid


def _outcome_vec(home: int, away: int) -> tuple[int, int, int]:
    if home > away:
        return (1, 0, 0)
    if home == away:
        return (0, 1, 0)
    return (0, 0, 1)


def _brier(pred: tuple[float, float, float], actual: tuple[int, int, int]) -> float:
    return sum((p - a) ** 2 for p, a in zip(pred, actual))


def _score_at_minute(goal_events: list[dict[str, Any]], minute: int) -> Optional[tuple[int, int]]:
    if not goal_events:
        return None
    home = away = 0
    for g in goal_events:
        gm = g.get("minute") or 0
        if gm <= minute:
            if g.get("side") == "home":
                home += 1
            elif g.get("side") == "away":
                away += 1
    return home, away


def load_backtest_rows() -> list[dict[str, Any]]:
    import sqlite3

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        select distinct p.match_id, p.models_json, p.final_home, p.final_away,
               s.minute, s.home_xg, s.away_xg, s.home_shots_on_target, s.away_shots_on_target,
               s.home_total_shots, s.away_total_shots
        from prediction_history p
        join live_stat_snapshots s on s.match_id = p.match_id
        where p.models_json is not null and p.final_home is not null
        group by p.match_id
        """
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        try:
            models = json.loads(d["models_json"])
            dc = models.get("dixon_coles") or {}
            home_lambda, away_lambda = dc.get("home_lambda"), dc.get("away_lambda")
        except Exception:
            home_lambda = away_lambda = None
        if home_lambda is None or away_lambda is None:
            continue

        fm = conn.execute(
            "select raw_json from finished_matches where match_id=? limit 1", (d["match_id"],)
        ).fetchone()
        score_at_minute = None
        if fm and fm["raw_json"]:
            try:
                fmd = json.loads(fm["raw_json"])
                score_at_minute = _score_at_minute(fmd.get("goal_events") or [], d["minute"])
            except Exception:
                pass
        if score_at_minute is None:
            continue  # can't reconstruct the live state this match was in — skip, don't guess

        out.append({
            "match_id": d["match_id"], "home_lambda": home_lambda, "away_lambda": away_lambda,
            "minute": d["minute"], "home_xg": d["home_xg"], "away_xg": d["away_xg"],
            "home_sot": d["home_shots_on_target"] or 0, "away_sot": d["away_shots_on_target"] or 0,
            "home_shots": d["home_total_shots"] or 0, "away_shots": d["away_total_shots"] or 0,
            "home_score": score_at_minute[0], "away_score": score_at_minute[1],
            "final_home": d["final_home"], "final_away": d["final_away"],
        })
    conn.close()
    return out


def run_backtest() -> None:
    rows = load_backtest_rows()
    n = len(rows)
    print(f"Reconstructable matches: {n}\n")
    if n == 0:
        print("Nothing to compare yet — no matches have both a live_stat_snapshots row "
              "AND reconstructable goal timing. Re-run this after more live matches have "
              "been processed.")
        return

    naive_briers, live_briers = [], []
    naive_hits = live_hits = 0

    for d in rows:
        mu, lam = d["home_lambda"], d["away_lambda"]
        actual = _outcome_vec(d["final_home"], d["final_away"])

        pre_grid = _prematch_grid(mu, lam)
        naive_pred = (
            sum(p for (h, a), p in pre_grid.items() if h > a),
            sum(p for (h, a), p in pre_grid.items() if h == a),
            sum(p for (h, a), p in pre_grid.items() if h < a),
        )
        naive_top1 = max(pre_grid.items(), key=lambda kv: kv[1])[0]

        inputs = LiveInputs(
            home_lambda_full=mu, away_lambda_full=lam, elapsed_minutes=d["minute"],
            home_score=d["home_score"], away_score=d["away_score"],
            home_xg_so_far=d["home_xg"], away_xg_so_far=d["away_xg"],
            home_shots_on_target=int(d["home_sot"]), away_shots_on_target=int(d["away_sot"]),
            home_shots_total=int(d["home_shots"]), away_shots_total=int(d["away_shots"]),
        )
        proj = project(inputs)
        mw = proj.match_winner()
        live_pred = (mw["home_win"], mw["draw"], mw["away_win"])
        live_top1 = tuple(int(x) for x in proj.correct_score_top(1)[0]["score"].split("-"))

        naive_briers.append(_brier(naive_pred, actual))
        live_briers.append(_brier(live_pred, actual))
        naive_hits += int(naive_top1 == (d["final_home"], d["final_away"]))
        live_hits += int(live_top1 == (d["final_home"], d["final_away"]))

        print(f"{d['match_id']:>28}  min={d['minute']:>3}  "
              f"score@snap={d['home_score']}-{d['away_score']:<3}  final={d['final_home']}-{d['final_away']:<3}  "
              f"naive_top1={naive_top1[0]}-{naive_top1[1]:<3}  live_top1={live_top1[0]}-{live_top1[1]}")

    print(f"\n--- Brier score, match-winner (1X2), lower is better, n={n} ---")
    print(f"naive (pre-match only):   {sum(naive_briers)/n:.4f}")
    print(f"live-adjusted:            {sum(live_briers)/n:.4f}")
    print(f"\n--- Exact correct-score top-1 hit rate, n={n} ---")
    print(f"naive:          {naive_hits}/{n} ({100*naive_hits/n:.0f}%)")
    print(f"live-adjusted:  {live_hits}/{n} ({100*live_hits/n:.0f}%)")


if __name__ == "__main__":
    run_backtest()
