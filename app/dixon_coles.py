from __future__ import annotations

from typing import Any

from app.poisson import _poisson_prob, _team_stats


MAX_GOALS = 7
HOME_ADVANTAGE = 1.10
RHO = -0.13


def _tau(home_goals: int, away_goals: int, mu: float, lam: float, rho: float) -> float:
    """Dixon-Coles low-score correction factor."""
    if home_goals == 0 and away_goals == 0:
        return 1 - mu * lam * rho
    if home_goals == 0 and away_goals == 1:
        return 1 + mu * rho
    if home_goals == 1 and away_goals == 0:
        return 1 + lam * rho
    if home_goals == 1 and away_goals == 1:
        return 1 - rho
    return 1.0


def run_dixon_coles(home_team_id: int, away_team_id: int, last_n: int = 10) -> dict[str, Any]:
    home_stats = _team_stats(home_team_id, last_n)
    away_stats = _team_stats(away_team_id, last_n)

    mu = home_stats["scored"] * HOME_ADVANTAGE * (away_stats["conceded"] / 1.3)
    lam = away_stats["scored"] * (home_stats["conceded"] / 1.3)
    mu = max(mu, 0.3)
    lam = max(lam, 0.3)

    home_win = draw = away_win = over_2_5 = btts = 0.0
    scorelines: list[tuple[int, int, float]] = []

    for home_goals in range(MAX_GOALS):
        for away_goals in range(MAX_GOALS):
            probability = (
                _poisson_prob(mu, home_goals)
                * _poisson_prob(lam, away_goals)
                * _tau(home_goals, away_goals, mu, lam, RHO)
            )
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

    total_1x2 = home_win + draw + away_win
    if total_1x2 <= 0:
        total_1x2 = 1.0

    return {
        "model": "dixon_coles",
        "home_lambda": round(mu, 3),
        "away_lambda": round(lam, 3),
        "home_stats": home_stats,
        "away_stats": away_stats,
        "probabilities": {
            "home_win": round(home_win / total_1x2 * 100, 1),
            "draw": round(draw / total_1x2 * 100, 1),
            "away_win": round(away_win / total_1x2 * 100, 1),
            "over_2_5": round(over_2_5 * 100, 1),
            "btts": round(btts * 100, 1),
        },
        "top_scorelines": [
            {"score": f"{home}-{away}", "probability": f"{probability}%"}
            for home, away, probability in sorted(scorelines, key=lambda item: item[2], reverse=True)[:5]
        ],
    }
