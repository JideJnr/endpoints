from __future__ import annotations

import math
from typing import Any

from app.storage.db import db_conn
from app.data_clients.sofascore_client import fetch_team_history


from app.utils.primitives import _to_int

MAX_GOALS = 7
HOME_ADVANTAGE = 1.0


def run_poisson(home_team_id: int, away_team_id: int, last_n: int = 10) -> dict[str, Any]:
    home_stats = _team_stats(home_team_id, last_n)
    away_stats = _team_stats(away_team_id, last_n)

    home_advantage = _learned_home_advantage_multiplier()
    home_lambda = home_stats["scored"] * home_advantage * (away_stats["conceded"] / 1.3)
    away_lambda = away_stats["scored"] * (home_stats["conceded"] / 1.3)
    home_lambda = max(home_lambda, 0.3)
    away_lambda = max(away_lambda, 0.3)

    home_win = draw = away_win = over_2_5 = btts = 0.0
    scorelines = []
    for home_goals in range(MAX_GOALS):
        for away_goals in range(MAX_GOALS):
            probability = _poisson_prob(home_lambda, home_goals) * _poisson_prob(away_lambda, away_goals)
            if home_goals > away_goals:
                home_win += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away_win += probability
            if home_goals + away_goals > 2:
                over_2_5 += probability
            if home_goals > 0 and away_goals > 0:
                btts += probability
            scorelines.append((home_goals, away_goals, round(probability * 100, 2)))

    calibrated_1x2 = _apply_bias_corrections({
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win,
    })
    probabilities = {
        "home_win": round(calibrated_1x2["home_win"] * 100, 1),
        "draw": round(calibrated_1x2["draw"] * 100, 1),
        "away_win": round(calibrated_1x2["away_win"] * 100, 1),
        "over_2_5": round(over_2_5 * 100, 1),
        "btts": round(btts * 100, 1),
    }
    prediction = max(
        {"Home Win": calibrated_1x2["home_win"], "Draw": calibrated_1x2["draw"], "Away Win": calibrated_1x2["away_win"]},
        key={"Home Win": calibrated_1x2["home_win"], "Draw": calibrated_1x2["draw"], "Away Win": calibrated_1x2["away_win"]}.get,
    )

    return {
        "home_lambda": round(home_lambda, 3),
        "away_lambda": round(away_lambda, 3),
        "home_advantage_multiplier": home_advantage,
        "home_stats": home_stats,
        "away_stats": away_stats,
        "probabilities": probabilities,
        "prediction": prediction,
        "top_scorelines": [
            {"score": f"{h}-{a}", "probability": f"{p}%"}
            for h, a, p in sorted(scorelines, key=lambda item: item[2], reverse=True)[:5]
        ],
    }


def _team_stats(team_id: int, last_n: int) -> dict[str, Any]:
    """Build scoring/conceding averages from SofaScore history + MongoDB finished matches."""
    # 1. SofaScore API history (live/recent)
    try:
        events = fetch_team_history(team_id).get("events", [])
    except Exception:
        events = []
    sofa_finished = [e for e in events if e.get("status", {}).get("type") == "finished"][:last_n]

    # 2. MongoDB finished matches (our own recorded results — grows with training)
    mongo_finished: list[dict[str, Any]] = []
    try:
        from app.storage.mongo_store import get_team_finished_matches, is_configured
        if is_configured():
            mongo_finished = get_team_finished_matches(team_id, limit=last_n)
    except Exception:
        pass

    # 3. SQLite local fallback (when MongoDB not configured)
    local_finished: list[dict[str, Any]] = []
    if not mongo_finished:
        local_finished = _local_team_matches(str(team_id), last_n)

    # Merge: SofaScore first (richest), then MongoDB/local to fill gaps
    historical = mongo_finished or local_finished
    all_finished = sofa_finished
    if len(all_finished) < last_n:
        all_finished = all_finished + historical
    all_finished = all_finished[:last_n]

    if not all_finished:
        return {"scored": 1.4, "conceded": 1.2, "matches": 0}

    scored = conceded = 0
    for event in all_finished:
        score = event.get("score") or {}
        home_id = event.get("home_team", {}).get("id") if isinstance(event.get("home_team"), dict) else None
        is_home = str(home_id) == str(team_id) if home_id else False
        scored   += _to_int(score.get("home" if is_home else "away"), 0)
        conceded += _to_int(score.get("away" if is_home else "home"), 0)

    count = len(all_finished)
    return {"scored": round(scored / count, 3), "conceded": round(conceded / count, 3), "matches": count}


def _local_team_matches(team_id: str, limit: int) -> list[dict[str, Any]]:
    """Pull finished matches for a team from our local SQLite finished_matches archive."""
    try:
        from app.storage.db import DB_PATH
        from app.storage.league_memory import _init_db
        import sqlite3 as _sqlite3
        import json as _json
        _init_db()
        with db_conn(timeout=30) as conn:
            conn.row_factory = _sqlite3.Row
            # Check if finished_matches table exists (MongoDB stub may not have it)
            tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'").fetchall()}
            if "finished_matches" not in tables:
                return []
            rows = conn.execute(
                """
                select raw_json from finished_matches
                where json_extract(raw_json, '$.home_team_id') = ?
                   or json_extract(raw_json, '$.away_team_id') = ?
                   or json_extract(raw_json, '$.home_team.id') = ?
                   or json_extract(raw_json, '$.away_team.id') = ?
                order by rowid desc limit ?
                """,
                (team_id, team_id, team_id, team_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            try:
                doc = _json.loads(row["raw_json"])
                score = doc.get("score") or {}
                if score.get("home") is None or score.get("away") is None:
                    continue
                # Normalise to the shape _team_stats expects
                result.append({
                    "score": score,
                    "home_team": {"id": doc.get("home_team")},
                    "away_team": {"id": doc.get("away_team")},
                    "status": {"type": "finished"},
                })
            except Exception:
                continue
        return result
    except Exception:
        return []


def _poisson_prob(lam: float, goals: int) -> float:
    return (math.exp(-lam) * (lam ** goals)) / math.factorial(goals)


def _learned_home_advantage_multiplier() -> float:
    try:
        from app.monitoring.self_learner import get_bias_corrections
        bias = get_bias_corrections()
        return max(0.80, min(1.05, float(bias.get("home_advantage_multiplier") or 1.0)))
    except Exception:
        return HOME_ADVANTAGE


def _apply_bias_corrections(probs: dict[str, float]) -> dict[str, float]:
    try:
        from app.monitoring.self_learner import get_bias_corrections
        bias = get_bias_corrections()
        weighted = {
            key: float(value) * float(bias.get(f"{key}_multiplier") or 1.0)
            for key, value in probs.items()
        }
    except Exception:
        weighted = dict(probs)
    total = sum(weighted.values())
    if total <= 0:
        return dict(probs)
    return {key: value / total for key, value in weighted.items()}


