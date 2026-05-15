from __future__ import annotations

import math
from typing import Any

from app.sofascore_client import fetch_team_history


MAX_GOALS = 7
HOME_ADVANTAGE = 1.10


def run_poisson(home_team_id: int, away_team_id: int, last_n: int = 10) -> dict[str, Any]:
    home_stats = _team_stats(home_team_id, last_n)
    away_stats = _team_stats(away_team_id, last_n)

    home_lambda = home_stats["scored"] * HOME_ADVANTAGE * (away_stats["conceded"] / 1.3)
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

    probabilities = {
        "home_win": round(home_win * 100, 1),
        "draw": round(draw * 100, 1),
        "away_win": round(away_win * 100, 1),
        "over_2_5": round(over_2_5 * 100, 1),
        "btts": round(btts * 100, 1),
    }
    prediction = max(
        {"Home Win": home_win, "Draw": draw, "Away Win": away_win},
        key={"Home Win": home_win, "Draw": draw, "Away Win": away_win}.get,
    )

    return {
        "home_lambda": round(home_lambda, 3),
        "away_lambda": round(away_lambda, 3),
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
    try:
        events = fetch_team_history(team_id).get("events", [])
    except Exception:
        events = []
    finished = [event for event in events if event.get("status", {}).get("type") == "finished"][:last_n]
    if not finished:
        return {"scored": 1.4, "conceded": 1.2, "matches": 0}

    scored = conceded = 0
    for event in finished:
        score = event.get("score") or {}
        is_home = event.get("home_team", {}).get("id") == team_id
        scored += _to_int(score.get("home" if is_home else "away"), 0)
        conceded += _to_int(score.get("away" if is_home else "home"), 0)

    count = len(finished)
    return {"scored": round(scored / count, 3), "conceded": round(conceded / count, 3), "matches": count}


def _poisson_prob(lam: float, goals: int) -> float:
    return (math.exp(-lam) * (lam ** goals)) / math.factorial(goals)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
