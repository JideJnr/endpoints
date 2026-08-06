from __future__ import annotations

from typing import Any

from app.data_clients.sofascore_client import fetch_standings, fetch_team_history


LEAGUE_TIERS = {
    "premier league": 1,
    "la liga": 1,
    "bundesliga": 1,
    "serie a": 1,
    "ligue 1": 1,
    "championship": 2,
    "la liga 2": 2,
    "2. bundesliga": 2,
    "serie b": 2,
    "ligue 2": 2,
    "eredivisie": 2,
    "primeira liga": 2,
    "league one": 3,
    "league two": 4,
    "champions league": 1,
    "europa league": 2,
    "conference league": 3,
}

OPPONENT_WEIGHT = {"top": 3.0, "upper": 2.0, "lower": 1.0, "bottom": 0.5, "unknown": 1.0}


def analyse_schedule(team_id: int, last_n: int = 10) -> dict[str, Any]:
    try:
        events = fetch_team_history(team_id).get("events", [])
    except Exception:
        events = []
    finished = [event for event in events if event.get("status", {}).get("type") == "finished"][:last_n]
    if not finished:
        return {"error": "no history available", "team_id": team_id}

    raw_form = []
    weighted_score = 0.0
    max_possible = 0.0
    tiers = []
    quality_wins = 0
    soft_losses = 0
    details = []

    for event in finished:
        home_id = event.get("home_team", {}).get("id")
        away_id = event.get("away_team", {}).get("id")
        is_home = home_id == team_id
        opponent_id = away_id if is_home else home_id
        opponent = event.get("away_team" if is_home else "home_team", {})
        score = event.get("score") or {}
        home_goals = _to_int(score.get("home"), 0)
        away_goals = _to_int(score.get("away"), 0)
        goals_for = home_goals if is_home else away_goals
        goals_against = away_goals if is_home else home_goals
        result = "W" if goals_for > goals_against else "D" if goals_for == goals_against else "L"
        raw_form.append(result)

        raw_t = event.get("tournament") or {}
        tournament = raw_t if isinstance(raw_t, dict) else {}
        t_name = tournament.get("name") or (raw_t if isinstance(raw_t, str) else "")
        tier = _league_tier(t_name)
        tiers.append(tier)
        standing = _opponent_standing(opponent_id, tournament.get("tournament_id"), event.get("season_id"))
        bucket = _opponent_bucket(standing.get("position"), standing.get("total_teams"))
        weight = OPPONENT_WEIGHT[bucket]

        if result == "W":
            weighted_score += 3 * weight
            if bucket in ("top", "upper"):
                quality_wins += 1
        elif result == "D":
            weighted_score += weight
        elif bucket in ("bottom", "lower"):
            soft_losses += 1
        max_possible += 3 * weight

        details.append({
            "match": event.get("name"),
            "result": result,
            "opponent": opponent.get("name"),
            "opponent_position": standing.get("position"),
            "opponent_bucket": bucket,
            "league_tier": tier,
            "weight": weight,
        })

    wins = raw_form.count("W")
    draws = raw_form.count("D")
    losses = raw_form.count("L")
    top_count = sum(1 for detail in details if detail["opponent_bucket"] in ("top", "upper"))
    difficulty = "Very Hard" if top_count >= 7 else "Hard" if top_count >= 5 else "Moderate" if top_count >= 3 else "Easy"
    form_score = round((weighted_score / max_possible) * 100, 1) if max_possible else 50.0

    if soft_losses > 2:
        context = f"Lost {soft_losses} games against weak opposition - genuine form concern."
    elif quality_wins >= 3:
        context = f"Won {quality_wins} games against strong opponents - form is reliable."
    elif losses >= 4 and difficulty in ("Hard", "Very Hard"):
        context = f"Lost {losses} of last {last_n} but faced a {difficulty.lower()} schedule."
    else:
        context = f"{wins}W {draws}D {losses}L from last {last_n} against a {difficulty.lower()} schedule."

    return {
        "team_id": team_id,
        "matches_analysed": len(finished),
        "raw_form": raw_form,
        "raw_record": {"W": wins, "D": draws, "L": losses},
        "weighted_form_score": form_score,
        "schedule_difficulty": difficulty,
        "avg_opponent_tier": round(sum(tiers) / len(tiers), 2) if tiers else 3,
        "quality_wins": quality_wins,
        "soft_losses": soft_losses,
        "context": context,
        "schedule_details": details,
    }


def compare_schedules(home_team_id: int, away_team_id: int) -> dict[str, Any]:
    home = analyse_schedule(home_team_id)
    away = analyse_schedule(away_team_id)
    if "error" in home or "error" in away:
        return {"home": home, "away": away, "verdict": "insufficient data"}

    diff = round(home["weighted_form_score"] - away["weighted_form_score"], 1)
    if diff > 20:
        verdict = f"Home team form is significantly stronger on quality-adjusted basis (+{diff})."
    elif diff > 10:
        verdict = f"Home team has edge in quality-adjusted form (+{diff})."
    elif diff < -20:
        verdict = f"Away team form is significantly stronger on quality-adjusted basis ({diff})."
    elif diff < -10:
        verdict = f"Away team has edge in quality-adjusted form ({diff})."
    else:
        verdict = f"Both teams have similar quality-adjusted form (diff: {diff})."

    tier_diff = round(away["avg_opponent_tier"] - home["avg_opponent_tier"], 1)
    if tier_diff >= 1.5:
        verdict += " Home team appears to play stronger opposition."
    elif tier_diff <= -1.5:
        verdict += " Away team appears to play stronger opposition."

    return {
        "home": _schedule_summary(home),
        "away": _schedule_summary(away),
        "verdict": verdict,
    }


def _opponent_standing(opponent_id: int, tournament_id: int | None, season_id: int | None) -> dict[str, Any]:
    if not opponent_id or not tournament_id or not season_id:
        return {}
    try:
        rows = fetch_standings(tournament_id, season_id)
    except Exception:
        return {}
    for row in rows:
        if row.get("team", {}).get("id") == opponent_id:
            return {"position": row.get("position"), "points": row.get("points"), "total_teams": len(rows)}
    return {}


def _opponent_bucket(position: int | None, total_teams: int | None) -> str:
    if not position or not total_teams:
        return "unknown"
    percentile = position / total_teams
    if percentile <= 0.25:
        return "top"
    if percentile <= 0.50:
        return "upper"
    if percentile <= 0.75:
        return "lower"
    return "bottom"


def _league_tier(name: str) -> int:
    text = (name or "").lower()
    for key, tier in LEAGUE_TIERS.items():
        if key in text:
            return tier
    return 3


def _schedule_summary(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "form_score": data["weighted_form_score"],
        "schedule_difficulty": data["schedule_difficulty"],
        "quality_wins": data["quality_wins"],
        "soft_losses": data["soft_losses"],
        "context": data["context"],
        "raw_form": data["raw_form"],
    }


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
