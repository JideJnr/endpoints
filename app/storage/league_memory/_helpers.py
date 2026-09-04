"""
league_memory._helpers
~~~~~~~~~~~~~~~~~~~~~~
Pure utility functions used across crud.py and queries.py.
No DB connections opened here; all functions operate on data already in memory
or on a caller-supplied ``conn`` argument.
"""
from __future__ import annotations

import logging
import re
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

from app.utils.match_helpers import _team_name

logger = logging.getLogger(__name__)

_TEAM_HISTORY_CACHE_DAYS = 7

# Audit counters for the last-resort text-matching grading fallback
# (`_side_from_selection_and_match`). Grading trains every downstream
# learning table (self-learner weights, calibration curves, bias
# corrections), so a silent mis-grade here teaches the wrong lesson
# everywhere. These counters let an operator check, without grepping logs,
# how often grading actually had to fall back to fuzzy name matching and
# how often that match was unambiguous vs. had to be refused.
_TEXT_MATCH_FALLBACK_STATS = {"resolved": 0, "ambiguous": 0, "unresolved": 0}


def get_text_match_fallback_stats() -> dict[str, int]:
    """Return in-process counts of how the last-resort side-matching
    fallback (`_side_from_selection_and_match`) has resolved so far.

    ``resolved``: a side was inferred unambiguously from team-name text.
    ``ambiguous``: both sides plausibly matched the selection text, so no
        side was guessed (grading falls through to void instead).
    ``unresolved``: no side could be inferred at all (e.g. no usable
        match_name, or no name/token overlap with the selection).
    Reset only on process restart; intended for periodic audit sampling,
    not as a persisted metric.
    """
    return dict(_TEXT_MATCH_FALLBACK_STATS)


def _contains_word(text: str, word: str) -> bool:
    """Whole-word containment check (word-boundary aware).

    Plain ``word in text`` substring checks are what let a team name like
    "Home Farm" trip a literal "home" check, or a short team name like
    "Sporting" silently match inside "Sporting Gijon". This requires the
    word to appear as its own token, not merely as a substring of a longer
    one.
    """
    if not word:
        return False
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None
def normalize_league(value: str | None) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split()).strip()


def _league_from_match(match: dict[str, Any]) -> str:
    tournament = match.get("tournament")
    if isinstance(tournament, dict):
        tournament_name = tournament.get("name") or tournament.get("uniqueTournament", {}).get("name") or ""
        category = tournament.get("category") or {}
        category_text = category.get("name") if isinstance(category, dict) else ""
        if category_text and tournament_name and not str(tournament_name).lower().startswith(str(category_text).lower() + " "):
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
    text = str(league_name or "Unknown").strip()
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
    normalized = str(value or "").strip().lower()
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
        name = str(market.get("name") or "").lower()
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
        "signal_combination_key": row["signal_combination_key"] if "signal_combination_key" in row.keys() else None,
        "signal_combination": _safe_json(row["signal_combination_json"] if "signal_combination_json" in row.keys() else "{}", {}),
        "live_context": _safe_json(row["live_context_json"] if "live_context_json" in row.keys() else "{}", {}),
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
    sel = str(selection or "").lower()
    pt = str(pick_type or "").lower()

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
            # These are home-or-draw / away-or-draw style picks. Only trust the
            # literal words "home"/"away" as whole words here — a plain
            # substring check would also fire on a team literally named e.g.
            # "Home Farm", mislabeling the pick.
            if _contains_word(sel_lower, "home"):
                return "win" if home >= away else "loss"
            if _contains_word(sel_lower, "away"):
                return "win" if away >= home else "loss"
            # Generic "or draw protection" — treat as favourite side wins or draws
            return "win" if home == away or home > away else "loss"
        if "home or away" in sel_lower or "away or home" in sel_lower or sel_lower.strip() == "12":
            return "win" if home != away else "loss"
        if _contains_word(sel_lower, "home"):
            return "win" if home > away else "loss"
        if _contains_word(sel_lower, "away"):
            return "win" if away > home else "loss"
        if _contains_word(sel_lower, "draw"):
            return "win" if home == away else "loss"
        # No literal "home"/"away"/"draw" wording — the selection is most
        # likely a team display name (e.g. "Arsenal Win", or for
        # double_chance specifically "Arsenal or Chelsea" -- the
        # home-or-away/12 double-chance pick phrased with both team names
        # instead of the literal words). Try the structured-ish name match
        # as a last resort before giving up.
        side = _side_from_selection_and_match(sel_lower, match_name)
        if side == "home":
            return "win" if home > away else "loss"
        if side == "away":
            return "win" if away > home else "loss"
        if side == "ambiguous" and pt == "double_chance":
            # Both team full names appear in the selection text with no
            # draw wording anywhere -- the only realistic way that happens
            # for a double_chance pick is "{TeamA} or {TeamB}" (12/home-or-
            # away), not a genuine ambiguity between two candidate teams.
            return "win" if home != away else "loss"
        return "void"

    return "void"


def _side_from_selection_and_match(selection: str, match_name: str | None) -> str | None:
    """Last-resort inference of which side (home/away) a free-text selection
    picks, by fuzzy-matching it against ``match_name`` ("Home vs Away").

    This is the fallback of last resort for grading — every prediction's
    win/loss label, and everything that learns from it, can depend on this
    guess being right. Two failure modes matter more than raw accuracy:

    1. A short/generic name being a substring of the other team's longer
       name (e.g. home="Sporting", away="Sporting Gijon" — a naive
       ``"sporting" in sel`` would match "home" even when the selection is
       plainly "Sporting Gijon"). We require a whole-word match, not a
       substring, and we check BOTH sides before deciding, so a name that
       is contained within the other's is never silently preferred.
    2. Silently guessing when the text is genuinely ambiguous (both sides
       plausibly match, or a tied token overlap). Callers must treat the
       string "ambiguous" the same as "unresolved" — never as a real side.

    Returns "home", "away", "ambiguous" (do not guess — both sides match),
    or None (nothing usable could be inferred).
    """
    if not match_name or " vs " not in match_name:
        _TEXT_MATCH_FALLBACK_STATS["unresolved"] += 1
        return None
    home_name, away_name = [part.strip().lower() for part in match_name.split(" vs ", 1)]
    sel = str(selection or "").lower()
    if not home_name or not away_name:
        _TEXT_MATCH_FALLBACK_STATS["unresolved"] += 1
        return None

    home_full = _contains_word(sel, home_name)
    away_full = _contains_word(sel, away_name)
    if home_full and not away_full:
        _TEXT_MATCH_FALLBACK_STATS["resolved"] += 1
        return "home"
    if away_full and not home_full:
        _TEXT_MATCH_FALLBACK_STATS["resolved"] += 1
        return "away"
    if home_full and away_full:
        logger.info(
            "grading fallback: selection %r matched both team names in %r — refusing to guess",
            selection, match_name,
        )
        _TEXT_MATCH_FALLBACK_STATS["ambiguous"] += 1
        return "ambiguous"

    # Neither full team name appears as a whole word (nicknames, abbreviations,
    # partial names). Fall back to token overlap, but only trust it when one
    # side has strictly more overlapping tokens than the other.
    sel_tokens = set(re.split(r"\W+", sel))
    home_tokens = {part for part in re.split(r"\W+", home_name) if len(part) >= 4}
    away_tokens = {part for part in re.split(r"\W+", away_name) if len(part) >= 4}
    home_hits = len(home_tokens & sel_tokens)
    away_hits = len(away_tokens & sel_tokens)
    if home_hits and away_hits and home_hits == away_hits:
        logger.info(
            "grading fallback: selection %r tied on token overlap with both sides of %r — refusing to guess",
            selection, match_name,
        )
        _TEXT_MATCH_FALLBACK_STATS["ambiguous"] += 1
        return "ambiguous"
    if home_hits > away_hits:
        logger.debug("grading fallback: resolved %r -> home via token overlap against %r", selection, match_name)
        _TEXT_MATCH_FALLBACK_STATS["resolved"] += 1
        return "home"
    if away_hits > home_hits:
        logger.debug("grading fallback: resolved %r -> away via token overlap against %r", selection, match_name)
        _TEXT_MATCH_FALLBACK_STATS["resolved"] += 1
        return "away"
    _TEXT_MATCH_FALLBACK_STATS["unresolved"] += 1
    return None


def build_pick(
    kind: str,
    selection: str,
    confidence: float,
    reason: str,
    *,
    source: str | None = None,
    include_market_intent: bool = False,
) -> dict[str, Any]:
    """Unified pick-dict factory used across prediction_agent, enriched_prediction, etc."""
    pick: dict[str, Any] = {
        "type": kind,
        "selection": selection,
        "confidence": max(1, min(95, round(confidence))),
        "reason": reason,
    }
    if include_market_intent:
        try:
            from app.market.market_intent import classify_market_intent
            pick["market_intent"] = classify_market_intent(kind, selection)
        except Exception:
            pass
        pick["family"] = "live" if kind.startswith("live_") else kind
    if source is not None:
        pick["source"] = source
    return pick


def build_sporty_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Build the minimal sporty_doc dict used by predict_sporty_match."""
    return {
        **doc,
        "id": doc.get("id") or doc.get("sportybet_id"),
        "name": doc.get("name") or doc.get("sportybet_name"),
        "markets": doc.get("markets") or doc.get("sportybet_markets") or [],
    }


def _grade_live_next_or_no_goal(row: Any, final_home: int, final_away: int) -> str | None:
    """Grade live_next_goal/live_no_goal (and their "_grid"-suffixed variants)
    for prediction_history rows, using the score AT PICK TIME.

    Unlike every other market grading_reason/grade_market_intent handles,
    "does a/no team score NEXT" cannot be resolved from the final score
    alone -- it needs the score at the moment the pick was made. That score
    is not recoverable from grade_market_intent's inputs (see the comment
    on the live_next_goal/live_no_goal fallthrough in
    app/market/market_intent.py), so those two markets have always graded
    "void" for prediction_history rows even though the analogous
    prediction_candidate_history rows grade correctly via
    _grade_candidate_row (app/storage/league_memory/queries.py), which has
    pick-time score through context_json.

    This mirrors that _grade_candidate_row logic exactly, reading pick-time
    score from live_context_json's score_home/score_away fields (added
    alongside this fix -- see signal_combinations._normalise_live_context).
    Kept as its own copy rather than a shared import: queries.py imports
    from this module at load time, so importing queries.py back from here
    would be circular.

    Returns None -- not "void" -- when there is no usable pick-time score
    (rows recorded before this fix, or a live_context that never carried a
    score). The caller falls through to grading_reason/grade_market_intent
    in that case, which is where "void" is actually decided, same as
    before this fix existed.
    """
    pick_type = str(row["pick_type"] or "").lower()
    pt_base = pick_type[:-5] if pick_type.endswith("_grid") else pick_type
    if pt_base not in {"live_next_goal", "live_no_goal"}:
        return None
    try:
        live_context_json = row["live_context_json"]
    except (IndexError, KeyError):
        return None
    try:
        context = json.loads(live_context_json or "{}")
    except Exception:
        context = {}
    if not isinstance(context, dict) or "score_home" not in context or "score_away" not in context:
        return None
    selection = row["selection"]
    sel = str(selection or "").lower()
    start_home = _to_int(context.get("score_home"), 0)
    start_away = _to_int(context.get("score_away"), 0)
    start_total = start_home + start_away
    final_total = final_home + final_away
    if pt_base == "live_no_goal" or "no more goal" in sel or "no goal" in sel or sel.strip() in {"none", "no more goals"}:
        return "win" if final_total == start_total else "loss"
    side = _side_from_selection_and_match(sel, row["match_name"])
    if side in ("home", "away"):
        home_delta = final_home - start_home
        away_delta = final_away - start_away
        picked_delta = home_delta if side == "home" else away_delta
        other_delta = away_delta if side == "home" else home_delta
        if picked_delta > 0 and other_delta == 0:
            return "win"
        if picked_delta == 0 and other_delta > 0:
            return "loss"
        return "void"
    if "next goal" in sel:
        # Legacy generic phrasing with no identifiable team in the selection
        # text -- keep the old "did any goal happen" behaviour as a last
        # resort rather than voiding rows we used to grade.
        return "win" if final_total > start_total else "loss"
    return "void"


def grade_prediction_row(
    row: Any,
    final_home: int,
    final_away: int,
) -> tuple[str, dict[str, Any]]:
    """Apply grading_reason; returns (result, grade_info).

    ``grading_reason`` calls ``grade_market_intent`` first, then
    ``_fallback_grade`` (which handles team-name display-text cases via
    ``_side_from_selection_and_match``). There is no longer a separate
    ``_grade_pick_for_match`` call here — that was a duplicate path that
    could produce different win/loss labels for the same pick depending on
    which code path reached it.

    live_next_goal/live_no_goal (and "_grid" variants) are special-cased
    ahead of grading_reason via _grade_live_next_or_no_goal, above -- see
    its docstring. Every other pick_type is unaffected.
    """
    live_result = _grade_live_next_or_no_goal(row, final_home, final_away)
    if live_result is not None:
        from app.market.market_intent import classify_market_intent as _classify_market_intent
        grade_info = {
            "version": "grading_reason_v1",
            "result": live_result,
            "final_score": {"home": final_home, "away": final_away, "total": final_home + final_away},
            "market_intent": _classify_market_intent(row["pick_type"], row["selection"]),
            "reason": "graded from live_context_json score-at-pick-time",
            "graded_via": "live_context_score_delta",
        }
        return live_result, grade_info
    from app.monitoring.prediction_audit import grading_reason as _grading_reason
    grade_info = _grading_reason(row["pick_type"], row["selection"], final_home, final_away, row["match_name"])
    result = grade_info["result"]
    # Keep "void" out of prediction_history — treat as "void" rather than
    # forcing a spurious win/loss by falling back further.
    grade_info["result"] = result
    return result, grade_info


def update_prediction_result(
    conn: Any,
    prediction_id: Any,
    result: str,
    final_home: int,
    final_away: int,
    grade_info: dict[str, Any],
) -> None:
    """Execute the standard UPDATE on prediction_history for a graded row."""
    import json as _json
    conn.execute(
        """
        update prediction_history
        set result = ?, final_home = ?, final_away = ?, grading_reason_json = ?, graded_at = current_timestamp
        where id = ?
        """,
        (result, final_home, final_away, _json.dumps(grade_info), prediction_id),
    )


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


# ── Slip-level risk bands (learned_slip_risk) ───────────────────────────────
#
# Two different odds banding schemes exist on purpose: _odds_band above is
# for a SINGLE leg's own price (rarely above ~5.0 for a leg worth including
# at all). _combined_odds_band below is for a whole slip's multiplied-out
# price, which realistically ranges from ~1.5 (a two-leg slip of favourites)
# into the thousands (many legs stacked, or a couple of long-shot legs
# multiplied together) -- a single leg's banding scheme would put almost
# every real slip in the same "3.00+" bucket and learn nothing useful.

_LEG_COUNT_BANDS: tuple[tuple[int, str], ...] = (
    (1, "1"), (3, "2-3"), (6, "4-6"), (9, "7-9"),
)


def _leg_count_band(leg_count: int | None) -> str:
    n = int(leg_count or 0)
    for ceiling, band in _LEG_COUNT_BANDS:
        if n <= ceiling:
            return band
    return "10+"


_COMBINED_ODDS_BANDS: tuple[tuple[float, str], ...] = (
    (3.0, "<3"), (10.0, "3-10"), (50.0, "10-50"), (200.0, "50-200"), (1000.0, "200-1000"),
)


def _combined_odds_band(combined_odds: float | None) -> str:
    if combined_odds is None or combined_odds <= 0:
        return "unknown"
    for ceiling, band in _COMBINED_ODDS_BANDS:
        if combined_odds < ceiling:
            return band
    return "1000+"


# Natural risk ordering for each dimension's bands, lowest-risk first. Used
# to walk the learned win rates in order and find where they fall off a
# cliff, rather than comparing band labels as strings.
LEG_COUNT_BAND_ORDER: tuple[str, ...] = ("1", "2-3", "4-6", "7-9", "10+")
COMBINED_ODDS_BAND_ORDER: tuple[str, ...] = ("<3", "3-10", "10-50", "50-200", "200-1000", "1000+")


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

