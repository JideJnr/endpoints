from __future__ import annotations

from statistics import mean
from typing import Any

from app.league_memory import late_goal_memory_signal
from app.league_strength import league_strength_edge


HIGH_LATE_GOAL_LEAGUES = (
    "spain",
    "laliga",
    "la liga",
    "primera",
    "netherlands",
    "eredivisie",
    "germany",
    "bundesliga",
    "norway",
    "sweden",
    "belgium",
)


def predict_sofascore_event(
    event: dict[str, Any],
    home_history: list[dict[str, Any]] | None = None,
    away_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    home_history = home_history or []
    away_history = away_history or []
    home = event.get("home_team", {})
    away = event.get("away_team", {})
    status = event.get("status", {})
    score = event.get("score", {})
    tournament = event.get("tournament", {})

    signals: list[dict[str, Any]] = []
    picks: list[dict[str, Any]] = []

    home_form = _team_history_features(home.get("id"), home_history)
    away_form = _team_history_features(away.get("id"), away_history)
    form_edge = _form_edge(event, home_form, away_form, signals)
    league_edge = _league_strength_edge(event, home_history, away_history, signals)
    h2h_edge = _h2h_edge(event, signals)
    table_edge = _table_edge(event, signals)
    odds_edge = _odds_edge(event, signals)

    total_goals = _to_int(score.get("home"), 0) + _to_int(score.get("away"), 0)
    minute = _event_minute(event)
    is_live = status.get("type") == "inprogress"
    late_goal_league = _is_high_late_goal_league(tournament.get("name"))
    memory_signal = late_goal_memory_signal(event)
    memory_boost = _late_goal_memory_boost(memory_signal, signals)

    home_power = form_edge + league_edge + h2h_edge + table_edge + odds_edge
    if abs(home_power) >= 12:
        side = home.get("name") if home_power > 0 else away.get("name")
        picks.append(_pick("match_result", f"{side} or draw protection", 58 + min(abs(home_power), 22), "stronger side with safety"))
    elif abs(home_power) >= 6:
        side = home.get("name") if home_power > 0 else away.get("name")
        picks.append(_pick("double_chance", f"{side} double chance", 55 + min(abs(home_power), 18), "small edge, safer market"))

    goal_pressure = _goal_pressure(home_form, away_form, event, signals)
    live_chase_pressure = _live_chase_pressure(event, home_power, goal_pressure, signals)
    if goal_pressure >= 14:
        picks.append(_pick("goals", "Over 1.5 goals", 64 + min(goal_pressure, 16), "both teams show goal trend"))
    if goal_pressure >= 22:
        picks.append(_pick("goals", "Over 2.5 goals", 55 + min(goal_pressure, 18), "high combined scoring/conceding trend"))
    if _btts_pressure(home_form, away_form) >= 16:
        picks.append(_pick("goals", "Both teams to score", 54 + min(_btts_pressure(home_form, away_form), 16), "both sides regularly score and concede"))

    if is_live and minute >= 70 and late_goal_league and total_goals <= 2:
        picks.append(_pick("live_goals", "Late goal watch", 61 + memory_boost, "league profile plus learned late-goal memory"))
        signals.append({"name": "late_goal_league", "value": tournament.get("name"), "impact": 7})

    if live_chase_pressure >= 12:
        picks.append(_pick("live_goals", "Next goal / Over 0.5 live", 62 + min(live_chase_pressure, 12) + memory_boost, "close scoreline plus chasing pressure"))

    if is_live and total_goals == 0 and minute >= 55:
        picks.append(_pick("live_goals", "Over 0.5 live", 57, "0-0 after halftime creates one-goal value window"))

    red_card_signal = _red_card_signal(event, home_power)
    if red_card_signal:
        picks.append(red_card_signal)
        signals.append({"name": "red_card_state", "value": red_card_signal["selection"], "impact": 10})

    if not picks:
        picks.append(_pick("no_bet", "No strong bet", 50, "not enough edge from available data"))

    return {
        "match_id": event.get("id"),
        "name": event.get("name"),
        "source": "sofascore",
        "status": status,
        "minute": minute,
        "score": score,
        "tournament": tournament,
        "teams": {"home": home, "away": away},
        "features": {"home": home_form, "away": away_form},
        "signals": sorted(signals, key=lambda s: abs(s.get("impact", 0)), reverse=True),
        "picks": sorted(picks, key=lambda p: p["confidence"], reverse=True),
    }


def predict_sporty_match(match: dict[str, Any]) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    picks: list[dict[str, Any]] = []
    minute = _sporty_minute(match.get("played_seconds"))
    score = match.get("score", {})
    home_goals = _to_int(score.get("home"), 0)
    away_goals = _to_int(score.get("away"), 0)
    total_goals = home_goals + away_goals
    tournament = match.get("tournament") or ""
    category = match.get("category") or ""
    odds = _sporty_main_odds(match)
    memory_signal = late_goal_memory_signal(match)
    memory_boost = _late_goal_memory_boost(memory_signal, signals)

    if odds:
        fav = max(odds, key=lambda item: item["implied_probability"])
        signals.append({"name": "market_favorite", "value": fav["name"], "impact": round(fav["implied_probability"] * 20, 2)})
        if fav["implied_probability"] >= 0.58:
            picks.append(_pick("market_value", f"{fav['name']} side protection", 58 + int((fav["implied_probability"] - 0.58) * 60), "market shows clear favorite"))

    if minute >= 70 and total_goals <= 2 and _is_high_late_goal_league(f"{category} {tournament}"):
        picks.append(_pick("live_goals", "Late goal watch", 60 + memory_boost, "league profile, late match state, and learned memory"))
        signals.append({"name": "late_goal_window", "value": f"{minute}' with {total_goals} goals", "impact": 8})

    if minute >= 55 and total_goals == 0:
        picks.append(_pick("live_goals", "Over 0.5 live", 56, "goalless second half window"))

    red_card_pick = _red_card_signal(match, _sporty_market_edge(odds))
    if red_card_pick:
        picks.append(red_card_pick)
        signals.append({"name": "red_card_state", "value": red_card_pick["selection"], "impact": 10})

    if not picks:
        picks.append(_pick("no_bet", "No strong bet", 50, "not enough edge from available live data"))

    return {
        "match_id": match.get("id"),
        "name": match.get("name"),
        "source": "sportybet",
        "period": match.get("period"),
        "minute": minute,
        "score": score,
        "tournament": tournament,
        "category": category,
        "signals": sorted(signals, key=lambda s: abs(s.get("impact", 0)), reverse=True),
        "picks": sorted(picks, key=lambda p: p["confidence"], reverse=True),
    }


def _team_history_features(team_id: int | None, history: list[dict[str, Any]]) -> dict[str, Any]:
    finished = [m for m in history if m.get("status", {}).get("type") == "finished"][:10]
    if not team_id or not finished:
        return {"sample_size": 0}

    goals_for = []
    goals_against = []
    results = []
    btts = []
    over_25 = []
    for match in finished:
        is_home = match.get("home_team", {}).get("id") == team_id
        home_goals = _to_int(match.get("score", {}).get("home"), 0)
        away_goals = _to_int(match.get("score", {}).get("away"), 0)
        gf = home_goals if is_home else away_goals
        ga = away_goals if is_home else home_goals
        goals_for.append(gf)
        goals_against.append(ga)
        btts.append(gf > 0 and ga > 0)
        over_25.append(gf + ga > 2)
        results.append("W" if gf > ga else "D" if gf == ga else "L")

    return {
        "sample_size": len(finished),
        "avg_goals_for": round(mean(goals_for), 2),
        "avg_goals_against": round(mean(goals_against), 2),
        "over_2_5_rate": round(sum(over_25) / len(over_25), 2),
        "btts_rate": round(sum(btts) / len(btts), 2),
        "form": results,
        "form_points": sum(3 if r == "W" else 1 if r == "D" else 0 for r in results),
    }


def _form_edge(event: dict[str, Any], home_form: dict[str, Any], away_form: dict[str, Any], signals: list[dict[str, Any]]) -> float:
    pregame = event.get("pregame_form") or {}
    home_pre = pregame.get("home_team") or {}
    away_pre = pregame.get("away_team") or {}
    edge = 0.0

    if home_form.get("sample_size") and away_form.get("sample_size"):
        edge += (home_form.get("form_points", 0) - away_form.get("form_points", 0)) * 0.8
        edge += (home_form.get("avg_goals_for", 0) - away_form.get("avg_goals_for", 0)) * 5
        edge -= (home_form.get("avg_goals_against", 0) - away_form.get("avg_goals_against", 0)) * 3
        signals.append({"name": "recent_history_edge", "value": round(edge, 2), "impact": round(edge, 2)})

    home_rating = _to_float(home_pre.get("avg_rating"))
    away_rating = _to_float(away_pre.get("avg_rating"))
    if home_rating is not None and away_rating is not None:
        rating_edge = (home_rating - away_rating) * 10
        edge += rating_edge
        signals.append({"name": "avg_rating_edge", "value": round(rating_edge, 2), "impact": round(rating_edge, 2)})

    return edge


def _league_strength_edge(
    event: dict[str, Any],
    home_history: list[dict[str, Any]],
    away_history: list[dict[str, Any]],
    signals: list[dict[str, Any]],
) -> float:
    strength = league_strength_edge(event, home_history, away_history)
    edge = strength.get("edge", 0.0)
    signals.append({
        "name": "league_strength_edge",
        "value": {
            "home_recent_avg": strength.get("home_recent_league_strength", {}).get("avg_score"),
            "away_recent_avg": strength.get("away_recent_league_strength", {}).get("avg_score"),
            "match_league": strength.get("match_league"),
            "note": "higher recent league strength means the team has been tested in a stronger competition",
        },
        "impact": edge,
    })
    return edge


def _h2h_edge(event: dict[str, Any], signals: list[dict[str, Any]]) -> float:
    h2h = event.get("h2h") or {}
    team_duel = h2h.get("team_duel") or h2h.get("teamDuel") or {}
    if not isinstance(team_duel, dict):
        return 0.0
    home_wins = _to_int(team_duel.get("homeWins") or team_duel.get("home_wins"), 0)
    away_wins = _to_int(team_duel.get("awayWins") or team_duel.get("away_wins"), 0)
    draws = _to_int(team_duel.get("draws"), 0)
    sample_size = home_wins + away_wins + draws
    if sample_size < 2:
        return 0.0
    raw_edge = (home_wins - away_wins) / sample_size
    edge = round(max(-8, min(8, raw_edge * 10)), 2)
    if abs(edge) < 1:
        return 0.0
    signals.append({
        "name": "h2h_edge",
        "value": {
            "home_wins": home_wins,
            "away_wins": away_wins,
            "draws": draws,
            "sample_size": sample_size,
        },
        "impact": edge,
    })
    return edge


def _table_edge(event: dict[str, Any], signals: list[dict[str, Any]]) -> float:
    pregame = event.get("pregame_form") or {}
    home_position = _to_int((pregame.get("home_team") or {}).get("position"), 0)
    away_position = _to_int((pregame.get("away_team") or {}).get("position"), 0)
    if not home_position or not away_position:
        return 0.0
    edge = max(min((away_position - home_position) * 1.5, 15), -15)
    signals.append({"name": "league_position_edge", "value": edge, "impact": edge})
    return edge


def _odds_edge(event: dict[str, Any], signals: list[dict[str, Any]]) -> float:
    market = ((event.get("odds_featured") or {}).get("default") or {}).get("choices") or []
    prices = {choice.get("name"): _fraction_to_probability(choice.get("fractional_value")) for choice in market}
    home_prob = prices.get("1") or prices.get("Home")
    away_prob = prices.get("2") or prices.get("Away")
    if home_prob is None or away_prob is None:
        return 0.0
    edge = (home_prob - away_prob) * 30
    signals.append({"name": "odds_edge", "value": round(edge, 2), "impact": round(edge, 2)})
    edge += _odds_momentum_edge(market, signals)
    return edge


def _odds_momentum_edge(market: list[dict[str, Any]], signals: list[dict[str, Any]]) -> float:
    """Reward a side when current odds shortened meaningfully from opening odds."""
    edge = 0.0
    for choice in market:
        name = choice.get("name")
        current = _fraction_to_probability(choice.get("fractional_value"))
        opening = _fraction_to_probability(choice.get("initial_fractional_value"))
        if current is None or opening is None:
            continue
        move = current - opening
        if abs(move) < 0.035:
            continue
        side_edge = min(abs(move) * 120, 10)
        if name in ("1", "Home"):
            edge += side_edge if move > 0 else -side_edge
        elif name in ("2", "Away"):
            edge -= side_edge if move > 0 else -side_edge
        else:
            continue
        signals.append({
            "name": "market_steam",
            "value": {"side": name, "probability_move": round(move, 3)},
            "impact": round(side_edge if move > 0 else -side_edge, 2),
        })
    return edge


def _goal_pressure(home_form: dict[str, Any], away_form: dict[str, Any], event: dict[str, Any], signals: list[dict[str, Any]]) -> float:
    if not home_form.get("sample_size") or not away_form.get("sample_size"):
        return 0.0
    pressure = (
        home_form.get("avg_goals_for", 0)
        + away_form.get("avg_goals_for", 0)
        + home_form.get("avg_goals_against", 0)
        + away_form.get("avg_goals_against", 0)
    ) * 4
    pressure += (home_form.get("over_2_5_rate", 0) + away_form.get("over_2_5_rate", 0)) * 8
    if _is_high_late_goal_league((event.get("tournament") or {}).get("name")):
        pressure += 3
    signals.append({"name": "goal_pressure", "value": round(pressure, 2), "impact": round(pressure, 2)})
    return pressure


def _btts_pressure(home_form: dict[str, Any], away_form: dict[str, Any]) -> float:
    if not home_form.get("sample_size") or not away_form.get("sample_size"):
        return 0.0
    return (home_form.get("btts_rate", 0) + away_form.get("btts_rate", 0)) * 12


def _live_chase_pressure(event: dict[str, Any], home_power: float, goal_pressure: float, signals: list[dict[str, Any]]) -> float:
    status = event.get("status") or {}
    if status.get("type") != "inprogress":
        return 0.0
    minute = _event_minute(event)
    if minute < 58:
        return 0.0

    score = event.get("score") or {}
    home_goals = _to_int(score.get("home"), 0)
    away_goals = _to_int(score.get("away"), 0)
    score_diff = home_goals - away_goals
    if abs(score_diff) > 1:
        return 0.0

    pressure = max(goal_pressure - 12, 0)
    if score_diff == 0:
        pressure += 4
    elif score_diff < 0 and home_power > 8:
        pressure += 8
    elif score_diff > 0 and home_power < -8:
        pressure += 8
    elif score_diff < 0 and home_power > 0:
        pressure += 4
    elif score_diff > 0 and home_power < 0:
        pressure += 4

    if minute >= 75:
        pressure += 3

    if pressure:
        signals.append({
            "name": "live_chase_pressure",
            "value": {"minute": minute, "score_diff": score_diff},
            "impact": round(pressure, 2),
        })
    return pressure


def _red_card_signal(match: dict[str, Any], edge: float) -> dict[str, Any] | None:
    home_red = _to_int(match.get("home_red_cards") or match.get("homeRedCards"), 0)
    away_red = _to_int(match.get("away_red_cards") or match.get("awayRedCards"), 0)
    if home_red == away_red:
        return None
    if home_red > away_red:
        selection = "Away team pressure after home red card" if edge <= 8 else "Favorite weakened by red card, avoid home win"
    else:
        selection = "Home team pressure after away red card" if edge >= -8 else "Underdog weakened by red card, favorite protection"
    return _pick("red_card", selection, 63, "red card changes win probability and goal pressure")


def _late_goal_memory_boost(memory_signal: dict[str, Any] | None, signals: list[dict[str, Any]]) -> int:
    if not memory_signal:
        return 0
    signals.append(memory_signal)
    return max(-8, min(8, round(memory_signal.get("impact", 0))))


def _sporty_main_odds(match: dict[str, Any]) -> list[dict[str, Any]]:
    for market in match.get("markets", []):
        name = (market.get("name") or "").lower()
        if "1x2" in name or "winner" in name or name in {"3 way", "match result"}:
            odds = []
            for selection in market.get("selections", []):
                decimal = _to_float(selection.get("odds"))
                if decimal and decimal > 1:
                    odds.append({
                        "name": selection.get("name"),
                        "decimal": decimal,
                        "implied_probability": 1 / decimal,
                    })
            return odds
    return []


def _sporty_market_edge(odds: list[dict[str, Any]]) -> float:
    if len(odds) < 2:
        return 0.0
    home = odds[0]["implied_probability"]
    away = odds[-1]["implied_probability"]
    return (home - away) * 30


def _pick(kind: str, selection: str, confidence: float, reason: str) -> dict[str, Any]:
    return {
        "type": kind,
        "selection": selection,
        "confidence": max(1, min(95, round(confidence))),
        "reason": reason,
    }


def _event_minute(event: dict[str, Any]) -> int:
    status = event.get("status") or {}
    description = str(status.get("description") or "")
    digits = "".join(ch for ch in description if ch.isdigit())
    return _to_int(digits, 0)


def _sporty_minute(played_seconds: Any) -> int:
    if played_seconds is None:
        return 0
    if isinstance(played_seconds, str) and ":" in played_seconds:
        mins, _ = played_seconds.split(":", 1)
        return _to_int(mins, 0)
    return int(_to_int(played_seconds, 0) / 60)


def _is_high_late_goal_league(name: str | None) -> bool:
    text = (name or "").lower()
    return any(token in text for token in HIGH_LATE_GOAL_LEAGUES)


def _fraction_to_probability(value: Any) -> float | None:
    if not value or "/" not in str(value):
        return None
    top, bottom = str(value).split("/", 1)
    numerator = _to_float(top)
    denominator = _to_float(bottom)
    if numerator is None or denominator in (None, 0):
        return None
    decimal = numerator / denominator + 1
    return 1 / decimal


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
