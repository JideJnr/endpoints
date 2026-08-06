"""
league_memory._helpers
~~~~~~~~~~~~~~~~~~~~~~
Pure utility functions used across crud.py and queries.py.
No DB connections opened here; all functions operate on data already in memory
or on a caller-supplied ``conn`` argument.
"""
from __future__ import annotations

import re
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

_TEAM_HISTORY_CACHE_DAYS = 7
def normalize_league(value: str | None) -> str:
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split()).strip()


def _league_from_match(match: dict[str, Any]) -> str:
    tournament = match.get("tournament")
    if isinstance(tournament, dict):
        tournament_name = tournament.get("name") or tournament.get("uniqueTournament", {}).get("name") or ""
        category = tournament.get("category") or {}
        category_text = category.get("name") if isinstance(category, dict) else ""
        if category_text and tournament_name and not tournament_name.lower().startswith(str(category_text).lower() + " "):
            return f"{category_text} {tournament_name}".strip()
        return tournament_name
    if match.get("league_name"):
        return str(match.get("league_name"))
    category = match.get("category") or match.get("country")
    if not category:
        raw_sporty = match.get("raw_sporty") or {}
        raw_event = raw_sporty.get("raw_event") or match.get("raw_event") or {}
        category = ((raw_event.get("sport") or {}).get("category") or {}).get("name")
    category_text = str(category or "").strip()
    tournament_text = str(tournament or "").strip()
    if category_text and tournament_text.lower().startswith(category_text.lower() + " "):
        return tournament_text
    return " ".join(part for part in [category_text, tournament_text] if part).strip()


def _country_from_match(match: dict[str, Any], league_name: str | None = None) -> str:
    for value in (match.get("country"), match.get("country_name"), match.get("category")):
        if value:
            return str(value)
    tournament = match.get("tournament")
    if isinstance(tournament, dict):
        category = tournament.get("category") or {}
        if isinstance(category, dict) and category.get("name"):
            return str(category.get("name"))
    raw_sporty = match.get("raw_sporty") or {}
    raw_event = raw_sporty.get("raw_event") or match.get("raw_event") or {}
    category = ((raw_event.get("sport") or {}).get("category") or {}).get("name")
    if category:
        return str(category)
    return _country_from_league(league_name)


def _country_from_league(league_name: str | None) -> str:
    text = (league_name or "Unknown").strip()
    countries = {
        "argentina", "australia", "austria", "belgium", "brazil", "bulgaria",
        "canada", "chile", "china", "colombia", "croatia", "czech republic",
        "denmark", "ecuador", "egypt", "england", "finland", "france", "germany",
        "ghana", "greece", "india", "indonesia", "ireland", "israel",
        "international", "italy", "japan", "kenya", "kuwait", "liberia", "mexico", "morocco", "netherlands",
        "nigeria", "norway", "oman", "paraguay", "peru", "poland", "portugal",
        "romania", "russia", "saudi arabia", "scotland", "serbia",
        "senegal", "south africa", "south korea", "spain", "sweden", "switzerland",
        "togo", "turkey", "ukraine", "uruguay", "usa", "united states", "wales",
    }
    lower = text.lower()
    for country in sorted(countries, key=len, reverse=True):
        if lower == country or lower.startswith(country + " ") or f" {country} " in f" {lower} ":
            return "USA" if country in {"usa", "united states"} else country.title()
    return "Global"


def _is_country_like(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    if normalized in {"global", "international"}:
        return True
    countries = {
        "argentina", "australia", "austria", "belgium", "brazil", "bulgaria",
        "canada", "chile", "china", "colombia", "croatia", "czech republic",
        "denmark", "ecuador", "egypt", "england", "finland", "france", "germany",
        "ghana", "greece", "india", "indonesia", "ireland", "israel",
        "italy", "japan", "kenya", "kuwait", "liberia", "mexico", "morocco",
        "netherlands", "nigeria", "norway", "oman", "paraguay", "peru",
        "poland", "portugal", "romania", "russia", "saudi arabia", "scotland",
        "senegal", "serbia", "south africa", "south korea", "spain", "sweden",
        "switzerland", "togo", "turkey", "ukraine", "uruguay", "usa",
        "united states", "wales",
    }
    return normalized in countries


def _match_fingerprint(league: str, match: dict[str, Any]) -> str:
    home = _team_name(match, "home") or ""
    away = _team_name(match, "away") or ""
    start = str(match.get("start_time") or match.get("start_timestamp") or "")[:10]
    parts = [normalize_league(league), normalize_league(home), normalize_league(away), start]
    return "|".join(parts)


def _match_minute(match: dict[str, Any]) -> int:
    if match.get("minute"):
        return _to_int(match.get("minute"), 0)
    played_seconds = match.get("played_seconds")
    if isinstance(played_seconds, str) and ":" in played_seconds:
        return _to_int(played_seconds.split(":", 1)[0], 0)
    if played_seconds:
        return int(_to_int(played_seconds, 0) / 60)
    status = match.get("status") or {}
    description = str(status.get("description") or "")
    digits = "".join(ch for ch in description if ch.isdigit())
    return _to_int(digits, 0)


def _minute_bucket(minute: int) -> str:
    if minute <= 15:
        return "00-15"
    if minute <= 30:
        return "16-30"
    if minute <= 45:
        return "31-45"
    if minute <= 60:
        return "46-60"
    if minute <= 70:
        return "61-70"
    if minute <= 80:
        return "71-80"
    return "81-90+"


def _bucket_bounds(bucket: str) -> tuple[int, int]:
    if bucket == "00-15":
        return 0, 15
    if bucket == "16-30":
        return 16, 30
    if bucket == "31-45":
        return 31, 45
    if bucket == "46-60":
        return 46, 60
    if bucket == "61-70":
        return 61, 70
    if bucket == "71-80":
        return 71, 80
    return 81, 130


def _score_state(home_goals: int, away_goals: int, favorite_side: str | None) -> str:
    score_diff = home_goals - away_goals
    if score_diff == 0:
        return "favorite_drawing" if favorite_side else "draw"
    leading_side = "home" if score_diff > 0 else "away"
    if not favorite_side:
        return "home_leading" if leading_side == "home" else "away_leading"
    if leading_side == favorite_side:
        return "favorite_leading"
    return "favorite_losing"


def _red_card_state(home_red_cards: int, away_red_cards: int) -> str:
    if home_red_cards == away_red_cards:
        return "even"
    return "home_red" if home_red_cards > away_red_cards else "away_red"


def _favorite_from_match(match: dict[str, Any]) -> dict[str, Any]:
    odds = _main_decimal_odds(match)
    if len(odds) < 2:
        return {}
    home = odds[0]
    away = odds[-1]
    if home["probability"] == away["probability"]:
        return {}
    favorite = home if home["probability"] > away["probability"] else away
    return {"side": favorite["side"], "probability": favorite["probability"]}


def _main_decimal_odds(match: dict[str, Any]) -> list[dict[str, Any]]:
    sporty_markets = match.get("markets") or []
    for market in sporty_markets:
        name = (market.get("name") or "").lower()
        if "1x2" in name or "winner" in name or name in {"3 way", "match result"}:
            odds = []
            for index, selection in enumerate(market.get("selections", [])):
                decimal = _to_float(selection.get("odds"))
                if decimal and decimal > 1:
                    odds.append({
                        "side": "home" if index == 0 else "away" if index == len(market.get("selections", [])) - 1 else "draw",
                        "probability": 1 / decimal,
                    })
            return odds

    choices = (((match.get("odds_featured") or {}).get("default") or {}).get("choices") or [])
    odds = []
    for choice in choices:
        probability = _fraction_to_probability(choice.get("fractional_value"))
        name = choice.get("name")
        if probability is None:
            continue
        side = "home" if name in ("1", "Home") else "away" if name in ("2", "Away") else "draw"
        odds.append({"side": side, "probability": probability})
    return odds


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


def _rate(hits: Any, samples: int) -> float | None:
    if hits is None or not samples:
        return None
    return round((hits or 0) / samples, 3)


def _team_name(match: dict[str, Any], side: str) -> str | None:
    team = match.get(f"{side}_team")
    if isinstance(team, dict):
        return team.get("name")
    return team


def _safe_json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except Exception:
        return fallback


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


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


def _memory_row(row: sqlite3.Row | None, fallback_key: str | None = None, fallback_name: str | None = None) -> dict[str, Any]:
    if not row:
        return {
            "league_key": fallback_key,
            "league_name": fallback_name,
            "samples": 0,
            "late_goals": 0,
            "late_goal_rate": 0,
        }
    samples = row["samples"] or 0
    late_goals = row["late_goals"] or 0
    return {
        "league_key": row["league_key"],
        "league_name": row["league_name"],
        "samples": samples,
        "late_goals": late_goals,
        "late_goal_rate": round(late_goals / samples, 3) if samples else 0,
    }


def _snapshot_memory_row(row: sqlite3.Row) -> dict[str, Any]:
    samples = row["samples"] or 0
    return {
        "league_key": row["league_key"],
        "league_name": row["league_name"],
        "minute_bucket": row["minute_bucket"],
        "score_state": row["score_state"],
        "samples": samples,
        "next_goal_rate": _rate(row["next_goal_hits"], samples),
        "over_1_5_rate": _rate(row["over_1_5_hits"], samples),
        "over_2_5_rate": _rate(row["over_2_5_hits"], samples),
        "favorite_recovered_rate": _rate(row["favorite_recovered_hits"], samples),
        "red_card_team_conceded_rate": _rate(row["red_card_team_conceded_hits"], samples),
    }


def _match_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source": row["source"],
        "id": row["match_id"],
        "league": {"id": row["league_key"], "name": row["league_name"]},
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "score": {"home": row["final_home_goals"], "away": row["final_away_goals"]},
        "is_finished": bool(row["is_finished"]),
        "last_seen_at": row["last_seen_at"],
    }


def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "minute": row["minute"],
        "minute_bucket": row["minute_bucket"],
        "score": {"home": row["home_goals"], "away": row["away_goals"]},
        "total_goals": row["total_goals"],
        "score_state": row["score_state"],
        "favorite_side": row["favorite_side"],
        "favorite_probability": row["favorite_probability"],
        "red_card_state": row["red_card_state"],
        "outcomes": {
            "next_goal_happened": row["next_goal_happened"],
            "over_1_5_hit": row["over_1_5_hit"],
            "over_2_5_hit": row["over_2_5_hit"],
            "favorite_recovered": row["favorite_recovered"],
            "red_card_team_conceded": row["red_card_team_conceded"],
        },
        "observed_at": row["observed_at"],
        "resolved_at": row["resolved_at"],
    }


def _prediction_row(row: sqlite3.Row) -> dict[str, Any]:
    picks = _safe_json(row["picks_json"] if "picks_json" in row.keys() else "[]", [])
    stored_best = picks[0] if picks else {}
    models = _safe_json(row["models_json"] if "models_json" in row.keys() else "{}", {})
    result = row["result"] if "result" in row.keys() else None
    passed_models = _get_passed_models(models, result) if result in ("win", "loss") else []
    return {
        "id": row["id"],
        "source": row["source"],
        "match_id": row["match_id"],
        "match_name": row["match_name"],
        "league_name": row["league_name"],
        "best_pick": {
            **stored_best,
            "type": stored_best.get("type") or row["pick_type"],
            "selection": stored_best.get("selection") or row["selection"],
            "confidence": stored_best.get("confidence") or row["confidence"],
            "reason": stored_best.get("reason") or row["reason"],
        },
        "signals": _safe_json(row["signals_json"] if "signals_json" in row.keys() else "[]", []),
        "picks": picks,
        "models": models,
        "passed_models": passed_models,
        "audit": _safe_json(row["audit_json"] if "audit_json" in row.keys() else "{}", {}),
        "prediction_mode": row["prediction_mode"] if "prediction_mode" in row.keys() else "prematch",
        "data_source": row["data_source"] if "data_source" in row.keys() else None,
        "live_data_sources": _safe_json(row["live_data_sources_json"] if "live_data_sources_json" in row.keys() else "[]", []),
        "grading_reason": _safe_json(row["grading_reason_json"] if "grading_reason_json" in row.keys() else "{}", {}),
        "created_at": row["created_at"],
    }


def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
    pick = {
        "type": row["pick_type"],
        "selection": row["selection"],
        "confidence": row["confidence"],
        "reason": row["reason"],
    }
    return {
        "id": row["id"],
        "source": row["source"],
        "match_id": row["match_id"],
        "match_url": f"/match/{row['match_id']}",
        "match_name": row["match_name"],
        "league_name": row["league_name"],
        "country_name": row["country_name"],
        "decision_type": row["decision_type"],
        "best_pick": pick,
        "readiness": _safe_json(row["readiness_json"], {}),
        "signals": _safe_json(row["signals_json"], []),
        "picks": _safe_json(row["picks_json"], []),
        "audit": _safe_json(row["audit_json"], {}),
        "contextual_intelligence": _safe_json(row["contextual_json"], {}),
        "result": row["result"],
        "final_home": row["final_home"],
        "final_away": row["final_away"],
        "grading_reason": _safe_json(row["grading_reason_json"], {}),
        "graded_at": row["graded_at"],
        "created_at": row["created_at"],
    }


def _get_passed_models(models: dict[str, Any], result: str) -> list[str]:
    """Return the list of model names whose prediction matched the graded result."""
    if not models or result not in ("win", "loss"):
        return []
    passed = []
    for name, model in models.items():
        if not isinstance(model, dict):
            continue
        probs = model.get("probabilities") if model else None
        if probs and isinstance(probs, dict):
            predicted = max(probs, key=probs.get)
            matched = False
            if result == "win" and predicted == "home_win":
                matched = True
            elif result == "loss" and predicted == "away_win":
                matched = True
            elif result == "draw" and predicted == "draw":
                matched = True
            if matched and name not in passed:
                passed.append(name)
            continue
        prediction = model.get("prediction")
        if prediction and isinstance(prediction, str):
            pred_lower = prediction.lower()
            matched = False
            if result == "win" and ("home" in pred_lower or "home win" in pred_lower):
                matched = True
            elif result == "loss" and ("away" in pred_lower or "away win" in pred_lower):
                matched = True
            elif result == "draw" and "draw" in pred_lower:
                matched = True
            if matched and name not in passed:
                passed.append(name)
    return passed


def _standings_from_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}
    for match in matches:
        if not match.get("is_finished"):
            continue
        home = match.get("home_team")
        away = match.get("away_team")
        home_goals = match.get("score", {}).get("home")
        away_goals = match.get("score", {}).get("away")
        if home is None or away is None or home_goals is None or away_goals is None:
            continue
        for team in (home, away):
            table.setdefault(team, {"team": team, "played": 0, "wins": 0, "draws": 0, "losses": 0, "goals_for": 0, "goals_against": 0, "points": 0})
        table[home]["played"] += 1
        table[away]["played"] += 1
        table[home]["goals_for"] += home_goals
        table[home]["goals_against"] += away_goals
        table[away]["goals_for"] += away_goals
        table[away]["goals_against"] += home_goals
        if home_goals > away_goals:
            table[home]["wins"] += 1
            table[away]["losses"] += 1
            table[home]["points"] += 3
        elif away_goals > home_goals:
            table[away]["wins"] += 1
            table[home]["losses"] += 1
            table[away]["points"] += 3
        else:
            table[home]["draws"] += 1
            table[away]["draws"] += 1
            table[home]["points"] += 1
            table[away]["points"] += 1
    rows = sorted(table.values(), key=lambda item: (item["points"], item["goals_for"] - item["goals_against"], item["goals_for"]), reverse=True)
    for index, row in enumerate(rows, start=1):
        row["position"] = index
        row["goal_diff"] = row["goals_for"] - row["goals_against"]
    return rows


def _match_1x2_odds_profile(match: dict[str, Any]) -> dict[str, float | str] | None:
    markets = match.get("sportybet_markets") or match.get("markets") or []
    odds: dict[str, float] = {}
    for market in markets or []:
        name = str(market.get("name") or "").lower()
        if not (market.get("id") == "1" or "1x2" in name or name == "match result"):
            continue
        for selection in market.get("selections") or market.get("choices") or []:
            sel = str(selection.get("name") or selection.get("label") or "").lower()
            odd = _safe_float(selection.get("odds") or selection.get("decimalOdds") or selection.get("decimal_odds"))
            if not odd or odd <= 1:
                continue
            if sel in {"home", "1"}:
                odds["home_odds"] = odd
            elif sel in {"draw", "x"}:
                odds["draw_odds"] = odd
            elif sel in {"away", "2"}:
                odds["away_odds"] = odd
    if not {"home_odds", "draw_odds", "away_odds"} <= set(odds):
        return None
    favorite_side, favorite_odds = min(
        (("home", odds["home_odds"]), ("draw", odds["draw_odds"]), ("away", odds["away_odds"])),
        key=lambda item: item[1],
    )
    return {**odds, "favorite_side": favorite_side, "favorite_odds": favorite_odds}


def _normalise_start_seconds(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        ts = float(value)
    except Exception:
        return None
    if ts > 10_000_000_000:
        ts = ts / 1000
    return ts


def _datetime_to_seconds(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        try:
            dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _date_from_start(value: Any) -> str | None:
    ts = _normalise_start_seconds(value)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def _sofa_ids_from_raw(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        doc = json.loads(raw)
    except Exception:
        return []
    candidates = [
        doc.get("sofascore_id"),
        (doc.get("sofascore_event") or {}).get("id") if isinstance(doc.get("sofascore_event"), dict) else None,
        (doc.get("sofascore_detail") or {}).get("id") if isinstance(doc.get("sofascore_detail"), dict) else None,
        ((doc.get("sofascore_detail") or {}).get("raw_event") or {}).get("id") if isinstance(doc.get("sofascore_detail"), dict) else None,
    ]
    out: list[str] = []
    for value in candidates:
        if value is not None and str(value) not in out:
            out.append(str(value))
    return out


def _same_team(left: Any, right: Any) -> bool:
    return normalize_league(str(left or "")) == normalize_league(str(right or ""))


def _close_match_row(row: sqlite3.Row, home: str, away: str) -> dict[str, Any]:
    home_goals = int(row["final_home_goals"] or 0)
    away_goals = int(row["final_away_goals"] or 0)
    return {
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "score": {"home": home_goals, "away": away_goals},
        "total_goals": home_goals + away_goals,
        "league": row["league_name"],
        "country": row["country_name"],
        "start_time": row["start_time"],
        "involves_home": _same_team(row["home_team"], home) or _same_team(row["away_team"], home),
        "involves_away": _same_team(row["home_team"], away) or _same_team(row["away_team"], away),
    }


def _team_form_from_rows(rows: list[sqlite3.Row], team: str) -> dict[str, Any]:
    if not team:
        return {"samples": 0, "points_per_game": 0.0, "avg_goals_for": 0.0, "avg_goals_against": 0.0}
    samples = points = goals_for = goals_against = 0
    for row in rows:
        is_home = _same_team(row["home_team"], team)
        is_away = _same_team(row["away_team"], team)
        if not is_home and not is_away:
            continue
        hg = int(row["final_home_goals"] or 0)
        ag = int(row["final_away_goals"] or 0)
        gf, ga = (hg, ag) if is_home else (ag, hg)
        samples += 1
        goals_for += gf
        goals_against += ga
        points += 3 if gf > ga else 1 if gf == ga else 0
    return {
        "samples": samples,
        "points_per_game": round(points / samples, 3) if samples else 0.0,
        "avg_goals_for": round(goals_for / samples, 3) if samples else 0.0,
        "avg_goals_against": round(goals_against / samples, 3) if samples else 0.0,
    }


def _grade_pick(pick_type: str | None, selection: str | None, home: int, away: int) -> str:
    return _grade_pick_for_match(pick_type, selection, home, away, None)


def _grade_pick_for_match(pick_type: str | None, selection: str | None, home: int, away: int, match_name: str | None = None) -> str:
    total = home + away
    sel = (selection or "").lower()
    pt = (pick_type or "").lower()

    if pt == "no_bet":
        return "void"

    if pt == "goals":
        if "under 3.5" in sel:
            return "win" if total < 4 else "loss"
        if "under 2.5" in sel:
            return "win" if total < 3 else "loss"
        if "under 1.5" in sel:
            return "win" if total < 2 else "loss"
        if "over 2.5" in sel:
            return "win" if total > 2 else "loss"
        if "over 1.5" in sel:
            return "win" if total > 1 else "loss"
        if "over 0.5" in sel:
            return "win" if total > 0 else "loss"
        if ("both teams to score" in sel or "btts" in sel) and (" no" in sel or "- no" in sel):
            return "win" if not (home > 0 and away > 0) else "loss"
        if "both teams to score" in sel or "btts" in sel:
            return "win" if home > 0 and away > 0 else "loss"
        return "void"

    if pt == "live_goals":
        if "over 0.5" in sel or "next goal" in sel or "late goal" in sel:
            return "win" if total > 0 else "loss"
        return "void"

    if pt == "live_team_to_score":
        return "void"

    if pt in ("match_result", "double_chance", "market_value", "ensemble_1x2", "value_bet"):
        sel_lower = sel
        if "home or draw" in sel_lower or "draw or home" in sel_lower or sel_lower.strip() == "1x":
            return "win" if home >= away else "loss"
        if "away or draw" in sel_lower or "draw or away" in sel_lower or sel_lower.strip() == "x2":
            return "win" if away >= home else "loss"
        if "home or away" in sel_lower or "away or home" in sel_lower or sel_lower.strip() == "12":
            return "win" if home != away else "loss"
        # Handle "{Team} or draw protection" and "{Team} double chance"
        if "or draw" in sel_lower or "double chance" in sel_lower:
            picked_side = _side_from_selection_and_match(sel_lower, match_name)
            if picked_side == "home":
                return "win" if home >= away else "loss"
            if picked_side == "away":
                return "win" if away >= home else "loss"
            # These are home-or-draw / away-or-draw style picks
            # Try to detect which side from context
            if "home" in sel_lower:
                return "win" if home >= away else "loss"
            if "away" in sel_lower:
                return "win" if away >= home else "loss"
            # Generic "or draw protection" — treat as favourite side wins or draws
            return "win" if home == away or home > away else "loss"
        if "home" in sel_lower:
            return "win" if home > away else "loss"
        if "away" in sel_lower:
            return "win" if away > home else "loss"
        if "draw" in sel_lower:
            return "win" if home == away else "loss"
        return "void"

    return "void"


def _side_from_selection_and_match(selection: str, match_name: str | None) -> str | None:
    if not match_name or " vs " not in match_name:
        return None
    home_name, away_name = [part.strip().lower() for part in match_name.split(" vs ", 1)]
    sel = selection.lower()
    if home_name and home_name in sel:
        return "home"
    if away_name and away_name in sel:
        return "away"
    home_tokens = [part for part in re.split(r"\W+", home_name) if len(part) >= 4]
    away_tokens = [part for part in re.split(r"\W+", away_name) if len(part) >= 4]
    home_hits = sum(1 for token in home_tokens if token in sel)
    away_hits = sum(1 for token in away_tokens if token in sel)
    if home_hits > away_hits:
        return "home"
    if away_hits > home_hits:
        return "away"
    return None


def _betbuilder_leg_key(match_id: Any, pick_type: Any, selection: Any) -> str:
    return f"{match_id}|{pick_type}|{selection}"


def _odds_band(odds: float | None) -> str:
    if odds is None:
        return "unknown"
    if odds < 1.30:
        return "1.01-1.29"
    if odds < 1.60:
        return "1.30-1.59"
    if odds < 2.00:
        return "1.60-1.99"
    if odds < 3.00:
        return "2.00-2.99"
    return "3.00+"


def _infer_betbuilder_pick_type(selection: str) -> str:
    """Infer a pick_type for betbuilder selections that omitted it."""
    try:
        from app.market.market_intent import classify_market_intent
    except Exception:
        classify_market_intent = None
    text = str(selection or "")
    if classify_market_intent:
        intent = classify_market_intent("", text, {})
        market = str(intent.get("market") or "")
        if market in {"total_goals", "btts"}:
            return "goals"
        if market == "double_chance":
            return "double_chance"
        if market == "1x2":
            return "match_result"
    lower = text.lower()
    if "over" in lower or "under" in lower or "both teams" in lower or "btts" in lower:
        return "goals"
    if " or draw" in lower or "home or away" in lower or lower.strip() in {"1x", "x2", "12"}:
        return "double_chance"
    if lower.strip() in {"home win", "away win", "draw", "home", "away"}:
        return "match_result"
    return ""


def _decorate_betbuilder_selections(selections: list[dict[str, Any]], leg_results: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    leg_results = leg_results or {}
    decorated = []
    for selection in selections:
        match_id = str(selection.get("match_id") or "")
        pick_type = str(selection.get("type") or selection.get("pick_type") or "")
        pick_selection = str(selection.get("selection") or "")
        result = leg_results.get(_betbuilder_leg_key(match_id, pick_type, pick_selection), {})
        decorated.append({
            **selection,
            "match_url": f"/match/{match_id}" if match_id else None,
            "analysis_url": f"/match/{match_id}" if match_id else None,
            "leg_result": result.get("result"),
            "graded_at": result.get("graded_at"),
            "grading_reason": result.get("grading_reason"),
            "odds_band": result.get("odds_band") or _odds_band(_to_float(selection.get("odds"))),
        })
    return decorated


def _betbuilder_learning_summary(legs: list[dict[str, Any]], slip_result: str) -> dict[str, Any]:
    wins = [leg for leg in legs if leg.get("result") == "win"]
    losses = [leg for leg in legs if leg.get("result") == "loss"]
    by_market: dict[str, dict[str, int]] = {}
    by_league: dict[str, dict[str, int]] = {}
    for leg in legs:
        result = str(leg.get("result") or "void")
        for bucket, key in ((by_market, str(leg.get("type") or leg.get("pick_type") or "unknown")), (by_league, str(leg.get("league") or leg.get("league_name") or "Global"))):
            bucket.setdefault(key, {"wins": 0, "losses": 0, "voids": 0})
            if result == "win":
                bucket[key]["wins"] += 1
            elif result == "loss":
                bucket[key]["losses"] += 1
            else:
                bucket[key]["voids"] += 1
    return {
        "slip_result": slip_result,
        "legs": len(legs),
        "wins": len(wins),
        "losses": len(losses),
        "voids": len([leg for leg in legs if leg.get("result") == "void"]),
        "by_market": by_market,
        "by_league": by_league,
        "failure_points": [
            {
                "match_id": leg.get("match_id"),
                "selection": leg.get("selection"),
                "type": leg.get("type") or leg.get("pick_type"),
                "reason": (leg.get("grading_reason") or {}).get("reason"),
            }
            for leg in losses
        ],
    }

