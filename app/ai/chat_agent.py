from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import normalize_league, observe_matches, record_prediction
from app.ai.prediction_agent import predict_sporty_match
from app.data_clients.sportybet_client import fetch_live_matches_post


@dataclass
class ChatIntent:
    market: str
    limit: int = 10
    min_odds: float | None = None
    min_minute: int = 1
    save: bool = True


def run_chat_prediction(message: str) -> dict[str, Any]:
    intent = parse_chat_intent(message)
    if intent.market == "no_next_goal":
        result = predict_no_next_goal(intent)
    elif intent.market == "next_team_to_score":
        result = predict_next_team_to_score(intent)
    else:
        result = predict_next_goal(intent)
    return {
        "status": "success",
        "message": message,
        "intent": intent.__dict__,
        **result,
        "answer": format_chat_answer(result["picks"], intent),
    }


def parse_chat_intent(message: str) -> ChatIntent:
    text = message.lower()
    limit = _first_int(text, default=10)
    min_odds = _min_odds(text)
    min_minute = _minute_floor(text)

    if any(token in text for token in ("no next goal", "no more goal", "no goal", "end at", "ends at")):
        return ChatIntent("no_next_goal", limit=limit, min_odds=min_odds or 3.0, min_minute=min_minute)
    if "next team" in text or "team to score" in text:
        return ChatIntent("next_team_to_score", limit=limit, min_odds=min_odds, min_minute=min_minute)
    return ChatIntent("next_goal", limit=limit, min_odds=min_odds or 1.7, min_minute=min_minute)


def predict_no_next_goal(intent: ChatIntent) -> dict[str, Any]:
    matches = fetch_live_matches_post()
    observe_summary = observe_matches("sportybet", matches)
    rows: list[dict[str, Any]] = []
    skipped = 0

    for match in matches:
        minute = _minute(match)
        if minute < intent.min_minute:
            continue
        candidates = _no_next_goal_candidates(match, intent.min_odds or 3.0)
        if not candidates:
            continue
        if _strong_next_goal_conflict(match):
            skipped += 1
            continue

        home_goals, away_goals = _score(match)
        league = _league(match)
        favorite = _favorite_side(match)
        state = _score_state(home_goals, away_goals, favorite)
        memory = _memory_for(league, _minute_bucket(minute), state)
        exact_rate = memory["exact"]["next_goal_rate"]
        if memory["exact"]["samples"] >= 2 and exact_rate is not None and exact_rate > 0.55:
            skipped += 1
            continue

        market, selection, odds = candidates[0]
        confidence, memory_note = _no_goal_confidence(minute, home_goals + away_goals, odds, memory)
        pick = {
            "match_id": match.get("id"),
            "minute": minute,
            "score": f"{home_goals}-{away_goals}",
            "match": match.get("name"),
            "league": league,
            "market": market,
            "selection": selection,
            "odds": odds,
            "confidence": confidence,
            "memory": memory_note,
            "score_state": state,
        }
        rows.append(pick)

    picks = sorted(rows, key=lambda item: (-item["confidence"], item["odds"]))[: intent.limit]
    _save_chat_picks(picks, "no_next_goal", "Chat no-next-goal prediction") if intent.save else None
    return {
        "tool": "sportybet_live_no_next_goal",
        "count": len(picks),
        "observed": observe_summary,
        "skipped_conflicts": skipped,
        "picks": picks,
    }


def predict_next_goal(intent: ChatIntent) -> dict[str, Any]:
    matches = fetch_live_matches_post()
    observe_summary = observe_matches("sportybet", matches)
    rows: list[dict[str, Any]] = []
    min_odds = intent.min_odds or 1.7

    for match in matches:
        minute = _minute(match)
        if minute < intent.min_minute:
            continue
        home_goals, away_goals = _score(match)
        if home_goals + away_goals > 5:
            continue
        candidate = _live_goal_candidate(match, min_odds)
        if not candidate:
            continue
        market, selection, odds = candidate
        prediction = predict_sporty_match(match)
        best = (prediction.get("picks") or [{}])[0]
        confidence = int(best.get("confidence") or 50)
        rows.append({
            "match_id": match.get("id"),
            "minute": minute,
            "score": f"{home_goals}-{away_goals}",
            "match": match.get("name"),
            "league": _league(match),
            "market": market,
            "selection": selection,
            "odds": odds,
            "confidence": confidence,
            "reason": best.get("reason") or "live state supports another goal",
        })

    picks = sorted(rows, key=lambda item: (-item["confidence"], item["odds"]))[: intent.limit]
    _save_chat_picks(picks, "live_goals", "Chat next-goal prediction") if intent.save else None
    return {"tool": "sportybet_live_next_goal", "count": len(picks), "observed": observe_summary, "picks": picks}


def predict_next_team_to_score(intent: ChatIntent) -> dict[str, Any]:
    matches = fetch_live_matches_post()
    observe_summary = observe_matches("sportybet", matches)
    rows: list[dict[str, Any]] = []

    for match in matches:
        minute = _minute(match)
        if minute < intent.min_minute:
            continue
        home_goals, away_goals = _score(match)
        team, reason = _next_team(match, home_goals, away_goals)
        if not team:
            continue
        confidence = 52
        if abs(home_goals - away_goals) == 1:
            confidence += 8
        if minute >= 70:
            confidence += 5
        rows.append({
            "match_id": match.get("id"),
            "minute": minute,
            "score": f"{home_goals}-{away_goals}",
            "match": match.get("name"),
            "league": _league(match),
            "selection": team,
            "odds": None,
            "confidence": min(80, confidence),
            "reason": reason,
        })

    picks = sorted(rows, key=lambda item: (-item["confidence"], item["minute"]))[: intent.limit]
    _save_chat_picks(picks, "next_team_to_score", "Chat next-team-to-score prediction") if intent.save else None
    return {"tool": "sportybet_live_next_team_to_score", "count": len(picks), "observed": observe_summary, "picks": picks}


def format_chat_answer(picks: list[dict[str, Any]], intent: ChatIntent) -> str:
    if not picks:
        return f"No qualifying {intent.market.replace('_', ' ')} picks found."
    lines = [f"Found {len(picks)} {intent.market.replace('_', ' ')} picks:"]
    for index, pick in enumerate(picks, start=1):
        odds = "n/a" if pick.get("odds") is None else f"{pick['odds']:.2f}"
        lines.append(
            f"{index}. {pick.get('minute')}' | {pick.get('score')} | {pick.get('match')} | "
            f"{pick.get('selection')} | odds {odds}"
        )
    return "\n".join(lines)


def _first_int(text: str, default: int) -> int:
    match = re.search(r"\b(\d{1,2})\b", text)
    if not match:
        return default
    value = int(match.group(1))
    return max(1, min(50, value))


def _min_odds(text: str) -> float | None:
    patterns = (
        r"(?:minimum|min|above|over|odd|odds)\s+(?:of\s+)?(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*\+?\s*odds?",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1))
    return None


def _minute_floor(text: str) -> int:
    match = re.search(r"(\d{1,2})\s*\+|after\s+(\d{1,2})|from\s+(\d{1,2})", text)
    if not match:
        return 1
    values = [group for group in match.groups() if group]
    return int(values[0]) if values else 1


def _minute(match: dict[str, Any]) -> int:
    played_seconds = match.get("played_seconds")
    if isinstance(played_seconds, str) and ":" in played_seconds:
        return _to_int(played_seconds.split(":", 1)[0])
    return int(_to_int(played_seconds) / 60) if played_seconds else 0


def _score(match: dict[str, Any]) -> tuple[int, int]:
    score = match.get("score") or {}
    return _to_int(score.get("home")), _to_int(score.get("away"))


def _league(match: dict[str, Any]) -> str:
    return " ".join(part for part in [str(match.get("category") or ""), str(match.get("tournament") or "")] if part).strip()


def _no_next_goal_candidates(match: dict[str, Any], min_odds: float) -> list[tuple[str, str, float]]:
    home_goals, away_goals = _score(match)
    total = home_goals + away_goals
    targets = {f"Under {total + 0.5:g}", f"Under {total}.5"}
    candidates: list[tuple[str, str, float]] = []
    for market in match.get("markets") or []:
        name = market.get("name") or ""
        lowered = name.lower()
        if lowered == "over/under":
            for selection in market.get("selections") or []:
                selection_name = selection.get("name") or ""
                odds = _to_float(selection.get("odds"))
                if odds and odds >= min_odds and selection_name in targets:
                    candidates.append((name, selection_name, odds))
        if "next goal" in lowered or "next team" in lowered or "team to score" in lowered:
            for selection in market.get("selections") or []:
                selection_name = selection.get("name") or ""
                odds = _to_float(selection.get("odds"))
                lowered_selection = selection_name.lower()
                if odds and odds >= min_odds and any(token in lowered_selection for token in ("no goal", "none", "no next", "no more")):
                    candidates.append((name, selection_name, odds))
    return sorted(candidates, key=lambda item: item[2])


def _live_goal_candidate(match: dict[str, Any], min_odds: float) -> tuple[str, str, float] | None:
    candidates: list[tuple[str, str, float]] = []
    for market in match.get("markets") or []:
        name = market.get("name") or ""
        lowered = name.lower()
        if not any(token in lowered for token in ("next goal", "to score", "goal", "over/under")):
            continue
        for selection in market.get("selections") or []:
            selection_name = selection.get("name") or ""
            odds = _to_float(selection.get("odds"))
            if odds and odds >= min_odds and (selection_name in {"Over 0.5", "Yes"} or "next goal" in lowered):
                candidates.append((name, selection_name, odds))
    return sorted(candidates, key=lambda item: item[2])[0] if candidates else None


def _strong_next_goal_conflict(match: dict[str, Any]) -> bool:
    prediction = predict_sporty_match(match)
    for pick in prediction.get("picks") or []:
        selection = (pick.get("selection") or "").lower()
        confidence = _to_int(pick.get("confidence"))
        if confidence >= 68 and (pick.get("type") == "live_goals" or "over 0.5" in selection or "next goal" in selection):
            return True
    return False


def _favorite_side(match: dict[str, Any]) -> str | None:
    odds = []
    for market in match.get("markets") or []:
        if (market.get("name") or "").lower() != "1x2":
            continue
        selections = market.get("selections") or []
        for index, selection in enumerate(selections):
            decimal = _to_float(selection.get("odds"))
            if not decimal or decimal <= 1:
                continue
            side = "home" if index == 0 else "away" if index == len(selections) - 1 else "draw"
            if side in {"home", "away"}:
                odds.append({"side": side, "probability": 1 / decimal})
    return max(odds, key=lambda item: item["probability"])["side"] if len(odds) >= 2 else None


def _next_team(match: dict[str, Any], home_goals: int, away_goals: int) -> tuple[str | None, str]:
    home = match.get("home_team")
    away = match.get("away_team")
    if home_goals < away_goals and away_goals - home_goals <= 1:
        return home, "home team is chasing in a close live score"
    if away_goals < home_goals and home_goals - away_goals <= 1:
        return away, "away team is chasing in a close live score"
    favorite = _favorite_side(match)
    if favorite == "home":
        return home, "home side is live market favorite"
    if favorite == "away":
        return away, "away side is live market favorite"
    return home if home_goals <= away_goals else away, "live score state gives this side the better next-goal angle"


def _score_state(home_goals: int, away_goals: int, favorite: str | None) -> str:
    if home_goals == away_goals:
        return "favorite_drawing" if favorite else "draw"
    leading = "home" if home_goals > away_goals else "away"
    if not favorite:
        return "home_leading" if leading == "home" else "away_leading"
    return "favorite_leading" if leading == favorite else "favorite_losing"


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


def _memory_for(league: str, bucket: str, state: str) -> dict[str, dict[str, Any]]:
    key = normalize_league(league)
    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        exact = conn.execute(
            """
            select sum(samples) samples, sum(next_goal_hits) hits
            from snapshot_aggregates
            where league_key = ? and minute_bucket = ? and score_state = ?
            """,
            (key, bucket, state),
        ).fetchone()
        global_state = conn.execute(
            """
            select sum(samples) samples, sum(next_goal_hits) hits
            from snapshot_aggregates
            where minute_bucket = ? and score_state = ?
            """,
            (bucket, state),
        ).fetchone()
    return {"exact": _pack_memory(exact), "global_state": _pack_memory(global_state)}


def _pack_memory(row: sqlite3.Row | None) -> dict[str, Any]:
    samples = (row["samples"] or 0) if row else 0
    hits = (row["hits"] or 0) if row else 0
    return {"samples": samples, "next_goal_rate": round(hits / samples, 3) if samples else None}


def _no_goal_confidence(minute: int, total: int, odds: float, memory: dict[str, dict[str, Any]]) -> tuple[int, str]:
    confidence = 34 + min(minute, 90) * 0.35
    if total == 0:
        confidence += 4
    elif total == 1:
        confidence += 6
    elif total == 2:
        confidence += 3
    elif total >= 4:
        confidence -= 5
    if minute >= 70:
        confidence += 4
    if minute >= 80:
        confidence += 5
    if odds >= 7:
        confidence -= 8

    exact = memory["exact"]
    if exact["samples"] >= 2 and exact["next_goal_rate"] is not None:
        no_goal_rate = 1 - exact["next_goal_rate"]
        confidence += (no_goal_rate - 0.5) * 22
        note = f"exact memory {exact['samples']} samples, next-goal {exact['next_goal_rate']:.0%}"
    elif memory["global_state"]["samples"] >= 4 and memory["global_state"]["next_goal_rate"] is not None:
        rate = memory["global_state"]["next_goal_rate"]
        confidence += ((1 - rate) - 0.5) * 8
        note = f"global state memory {memory['global_state']['samples']} samples, next-goal {rate:.0%}"
    else:
        note = "no exact memory"
    return max(25, min(74, round(confidence))), note


def _save_chat_picks(picks: list[dict[str, Any]], pick_type: str, reason: str) -> None:
    for pick in picks:
        record_prediction({
            "match_id": pick.get("match_id"),
            "source": "sportybet",
            "name": pick.get("match"),
            "league_name": pick.get("league"),
            "signals": [
                {"name": "chat_tool", "value": pick_type, "impact": 0},
                {"name": "memory", "value": pick.get("memory"), "impact": 0},
            ],
            "picks": [{
                "type": pick_type,
                "selection": pick.get("selection"),
                "confidence": pick.get("confidence"),
                "reason": reason,
                "odds": pick.get("odds"),
            }],
        })


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
