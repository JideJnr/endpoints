from __future__ import annotations

import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.buffer import get_buffered_match
from app.league_memory import list_prediction_history


_ANALYSIS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 10 * 60


def enriched_match_analysis(sportybet_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    cache_key = str(sportybet_id)
    now = time.time()
    if not force_refresh:
        cached = _ANALYSIS_CACHE.get(cache_key)
        if cached and now - cached[0] <= _CACHE_TTL_SECONDS:
            return {**cached[1], "cached": True}

    doc = get_buffered_match(cache_key)
    if not doc:
        raise ValueError("Match not found in the active buffer")

    engine_pick = _best_engine_pick(cache_key)
    if not engine_pick:
        engine_pick = _best_pick_from_doc(doc)
    if not engine_pick:
        raise ValueError("No prediction-engine pick found for this match")

    gated = similarity_gate(doc, _similar_matches(doc, limit=10))[:5]
    groq_input = _analysis_doc(doc, engine_pick, gated)
    analysis = _run_groq_enriched(groq_input)
    if analysis.get("status") in {"groq_unavailable", "agent_build_failed", "error"}:
        return analysis

    groq_selection = analysis.get("recommendation") or analysis.get("groq_recommendation")
    confirmed = _same_outcome(engine_pick.get("selection"), groq_selection)
    result = {
        "status": "success",
        "sportybet_id": cache_key,
        "match_id": cache_key,
        "match_name": doc.get("sportybet_name") or doc.get("match_name") or doc.get("name") or cache_key,
        "league_name": doc.get("tournament") or doc.get("league_name"),
        "country_name": doc.get("category") or doc.get("country_name"),
        "groq_recommendation": groq_selection,
        "groq_confidence": _to_int(analysis.get("confidence"), 0),
        "value_bet": bool(analysis.get("value_bet")),
        "market_signal": analysis.get("market_signal"),
        "btts": analysis.get("btts"),
        "over_2_5": analysis.get("over_2_5"),
        "reasoning": analysis.get("reasoning"),
        "key_factors": analysis.get("key_factors") or [],
        "prediction_engine_pick": engine_pick,
        "similar_matches": gated,
        "similar_matches_used": len(gated),
        "estimated_odds": _pick_decimal_odds(engine_pick),
        "confirmed": confirmed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }
    _ANALYSIS_CACHE[cache_key] = (now, result)
    return result


def build_ai_betbuilder(payload: dict[str, Any]) -> dict[str, Any]:
    target_odds = max(1.01, _to_float(payload.get("target_odds")) or 5.0)
    max_total_odds = max(target_odds, _to_float(payload.get("max_total_odds")) or target_odds * 1.35)
    candidates = upcoming_prediction_candidates(limit=50)
    analyses: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for candidate in candidates:
        match_id = str(candidate.get("match_id") or "")
        if not match_id:
            continue
        analysis = enriched_match_analysis(match_id)
        if analysis.get("status") != "success":
            failures.append({"match_id": match_id, "match": candidate.get("match_name"), "status": analysis.get("status"), "message": analysis.get("message")})
            continue
        analyses.append(analysis)

    if len(analyses) < 2:
        return {
            "status": "error",
            "message": "Fewer than two AI analyses succeeded",
            "groq_powered": True,
            "analyses_run": len(candidates),
            "analyses_succeeded": len(analyses),
            "failures": failures,
        }

    synthesis = synthesize_sure_picks(analyses, target_odds=target_odds, max_total_odds=max_total_odds)
    return {
        "status": "success",
        "groq_powered": True,
        "request": {"target_odds": target_odds, "max_total_odds": max_total_odds},
        "candidate_count": len(candidates),
        "analyses_run": len(candidates),
        "analyses_succeeded": len(analyses),
        "failures": failures,
        "picks": synthesis.get("ranked_picks") or [],
        "selections": synthesis.get("ranked_picks") or [],
        "combined_odds": synthesis.get("combined_odds") or 0,
        "avg_confidence": synthesis.get("avg_confidence") or 0,
        "confidence": synthesis.get("avg_confidence") or 0,
        "target_met": not synthesis.get("target_not_met"),
        "target_not_met": bool(synthesis.get("target_not_met")),
        "confirmed_count": synthesis.get("confirmed_count") or 0,
        "no_consensus": bool(synthesis.get("no_consensus")),
        "synthesis_reasoning": synthesis.get("synthesis_reasoning"),
        "analyses": analyses,
    }


def synthesize_sure_picks(
    analyses: list[dict[str, Any]],
    *,
    target_odds: float,
    max_total_odds: float,
) -> dict[str, Any]:
    clean = [item for item in analyses if item.get("status") == "success" or item.get("groq_recommendation")]
    if len(clean) < 2:
        raise ValueError("At least two completed enriched analyses are required")

    ranked = []
    for item in clean[:20]:
        groq_conf = _to_int(item.get("groq_confidence") or item.get("confidence"), 0)
        similar_used = _to_int(item.get("similar_matches_used"), 0)
        conviction = round(groq_conf * (1 + 0.10 * similar_used), 2)
        engine_pick = item.get("prediction_engine_pick") or {}
        confirmed = bool(item.get("confirmed")) or _same_outcome(engine_pick.get("selection"), item.get("groq_recommendation"))
        ranked.append({
            "match_id": item.get("match_id") or item.get("sportybet_id"),
            "match": item.get("match_name") or item.get("match"),
            "league": item.get("league_name"),
            "country": item.get("country_name"),
            "type": engine_pick.get("type") or item.get("pick_type") or "ai_pick",
            "pick_type": engine_pick.get("type") or item.get("pick_type") or "ai_pick",
            "selection": item.get("groq_recommendation") or engine_pick.get("selection"),
            "groq_confidence": groq_conf,
            "confidence": groq_conf,
            "odds": round(_to_float(item.get("estimated_odds")) or _pick_decimal_odds(engine_pick), 3),
            "estimated_odds": round(_to_float(item.get("estimated_odds")) or _pick_decimal_odds(engine_pick), 3),
            "conviction_score": conviction,
            "confirmed": confirmed,
            "similar_matches_used": similar_used,
            "source": "groq",
            "synthesis_reasoning": "",
        })
    ranked.sort(key=lambda item: (item["confirmed"], item["conviction_score"], item["groq_confidence"]), reverse=True)

    try:
        model_plan = _run_groq_synthesis(ranked, target_odds, max_total_odds)
        selected_ids = [str(item.get("match_id") or "") for item in model_plan.get("selected_picks") or []]
        selected = [item for item in ranked if str(item.get("match_id") or "") in selected_ids] if selected_ids else []
        reasoning = model_plan.get("synthesis_reasoning") or "Groq ranked the completed analyses by consensus and conviction."
    except Exception as exc:
        selected = []
        reasoning = f"Deterministic synthesis used after Groq synthesis failed: {exc}"

    if not selected:
        selected = _select_by_odds(ranked, target_odds, max_total_odds)
    selected = _trim_to_ceiling(selected, max_total_odds)
    if not selected:
        selected = ranked[:1]
    combined = _combined_odds(selected)
    target_not_met = combined < target_odds
    avg_confidence = round(sum(float(item.get("groq_confidence") or 0) for item in selected) / len(selected)) if selected else 0
    confirmed_count = len([item for item in selected if item.get("confirmed")])
    for item in selected:
        item["synthesis_reasoning"] = item.get("synthesis_reasoning") or (
            "Prediction engine and Groq agree." if item.get("confirmed") else "Included as a high-conviction AI pick."
        )
    return {
        "status": "success",
        "ranked_picks": selected,
        "combined_odds": round(combined, 3),
        "avg_confidence": avg_confidence,
        "confirmed_count": confirmed_count,
        "no_consensus": confirmed_count == 0,
        "target_not_met": target_not_met,
        "synthesis_reasoning": reasoning,
    }


def upcoming_prediction_candidates(limit: int = 50) -> list[dict[str, Any]]:
    try:
        from app.current_predictions import list_recent_dashboard_predictions

        rows = list_recent_dashboard_predictions(hours=72, limit=max(limit, 200))
    except Exception:
        rows = list_prediction_history(limit=max(limit, 200)).get("predictions") or []
    today = date.today()
    tomorrow = today + timedelta(days=1)
    allowed_dates = {today.isoformat(), tomorrow.isoformat()}
    candidates = []
    for row in rows:
        if row.get("is_finished") or str(row.get("result") or "").lower() in {"cancelled", "finished"}:
            continue
        match_date = str(row.get("match_date") or "")[:10]
        if match_date and match_date not in allowed_dates:
            continue
        pick = row.get("best_pick") or _best_pick(row.get("picks") or [])
        if not pick:
            continue
        candidates.append({**row, "best_pick": pick})
    candidates.sort(key=lambda item: int((item.get("best_pick") or {}).get("confidence") or 0), reverse=True)
    return candidates[:limit]


def similarity_gate(doc: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.similar_matches import _extract_target_odds_implied

    target_implied = _extract_target_odds_implied(doc)
    team_terms = [term for term in (_team_name(doc, "home"), _team_name(doc, "away")) if len(term) >= 4]
    scored_candidates = [item for item in candidates if float(item.get("similarity_score") or 0) >= 0.40]
    totals = [_total_goals(item.get("final_score")) for item in scored_candidates]
    totals = [value for value in totals if value is not None]
    avg_total = sum(totals) / len(totals) if totals else None
    passing = []
    for item in scored_candidates:
        dimensions: list[str] = []
        if target_implied and _odds_dimension(target_implied, item.get("odds")):
            dimensions.append("odds")
        total = _total_goals(item.get("final_score"))
        if avg_total is not None and total is not None and abs(total - avg_total) <= 1:
            dimensions.append("score_pattern")
        name = str(item.get("match_name") or "").lower()
        if any(term.lower() in name for term in team_terms):
            dimensions.append("team")
        if dimensions:
            prediction = item.get("prediction") or {}
            passing.append({
                **item,
                "similarity_dimension": "+".join(dimensions),
                "prediction_made": {
                    "type": prediction.get("pick_type") or prediction.get("type"),
                    "selection": prediction.get("selection"),
                    "confidence": prediction.get("confidence"),
                    "result": prediction.get("result"),
                },
            })
    return passing


def _run_groq_enriched(doc: dict[str, Any]) -> dict[str, Any]:
    from app.groq_agent import run_groq_match_analysis

    return run_groq_match_analysis(doc)


def _run_groq_synthesis(ranked: list[dict[str, Any]], target_odds: float, max_total_odds: float) -> dict[str, Any]:
    from app.llm import get_llm

    prompt = (
        "You are an expert football betting slip analyst. Read these completed Groq enriched analyses. "
        "Return strict JSON with selected_picks (array of objects with match_id) and synthesis_reasoning. "
        "Prefer confirmed picks where the prediction engine and Groq agree. Rank by conviction_score. "
        f"Select enough picks to reach target odds {target_odds} without exceeding max odds {max_total_odds}. "
        "If target cannot be reached, return the best available picks.\n\n"
        + json.dumps(ranked[:20], ensure_ascii=False)
    )
    response = get_llm().invoke([
        {"role": "system", "content": "Return only valid JSON."},
        {"role": "user", "content": prompt},
    ])
    raw = response.content if hasattr(response, "content") else str(response)
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def _analysis_doc(doc: dict[str, Any], engine_pick: dict[str, Any], similar: list[dict[str, Any]]) -> dict[str, Any]:
    prompt_context = {
        "prediction_engine_best_pick": engine_pick,
        "similar_matches": [
            {
                "match_name": item.get("match_name"),
                "final_score": item.get("final_score"),
                "prediction_made": item.get("prediction_made"),
                "similarity_dimension": item.get("similarity_dimension"),
                "similarity_score": item.get("similarity_score"),
            }
            for item in similar
        ] if len(similar) >= 2 else [],
    }
    return {**doc, "ai_enriched_context": prompt_context, "web_context": {**(doc.get("web_context") or {}), "snippets": [{"snippet": json.dumps(prompt_context)[:900]}]}}


def _similar_matches(doc: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    from app.similar_matches import find_similar_matches

    return find_similar_matches(doc, limit=limit)


def _best_engine_pick(match_id: str) -> dict[str, Any] | None:
    history = list_prediction_history(limit=5, match_id=match_id).get("predictions") or []
    for row in history:
        pick = row.get("best_pick") or _best_pick(row.get("picks") or [])
        if pick:
            return {
                "type": pick.get("type") or pick.get("pick_type") or row.get("pick_type"),
                "selection": pick.get("selection") or row.get("selection"),
                "confidence": pick.get("confidence") or row.get("confidence"),
                "reason": pick.get("reason") or row.get("reason"),
                "signals": row.get("signals") or pick.get("signals") or [],
                "odds": _pick_decimal_odds(pick),
            }
    return None


def _best_pick_from_doc(doc: dict[str, Any]) -> dict[str, Any] | None:
    prediction = doc.get("prediction") or {}
    return prediction.get("best_pick") or _best_pick(prediction.get("picks") or [])


def _best_pick(picks: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [pick for pick in picks if pick.get("type") != "no_bet"]
    if not usable:
        return None
    return max(usable, key=lambda pick: int(pick.get("confidence") or 0))


def _pick_decimal_odds(pick: dict[str, Any] | None) -> float:
    pick = pick or {}
    stake = pick.get("stake") if isinstance(pick.get("stake"), dict) else {}
    odds = _to_float(stake.get("decimal_odds")) or _to_float(pick.get("odds")) or _to_float(pick.get("decimal_odds"))
    if odds and odds > 1:
        return odds
    confidence = max(1, min(95, _to_int(pick.get("confidence"), 55))) / 100
    return round(1 / confidence, 3)


def _select_by_odds(candidates: list[dict[str, Any]], target_odds: float, max_total_odds: float) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    combined = 1.0
    for item in candidates:
        match_id = str(item.get("match_id") or "")
        odds = float(item.get("odds") or 1)
        if match_id in used or combined * odds > max_total_odds:
            continue
        selected.append(item)
        used.add(match_id)
        combined *= odds
        if combined >= target_odds:
            break
    return selected


def _trim_to_ceiling(selected: list[dict[str, Any]], max_total_odds: float) -> list[dict[str, Any]]:
    items = list(selected)
    while len(items) > 1 and _combined_odds(items) > max_total_odds:
        items.sort(key=lambda item: float(item.get("conviction_score") or 0))
        items.pop(0)
    return items


def _combined_odds(items: list[dict[str, Any]]) -> float:
    if not items:
        return 0.0
    return math.prod(float(item.get("odds") or item.get("estimated_odds") or 1) for item in items)


def _same_outcome(left: Any, right: Any) -> bool:
    return _normalise_selection(left) == _normalise_selection(right)


def _normalise_selection(value: Any) -> str:
    text = str(value or "").lower().strip()
    text = re.sub(r"[^a-z0-9+ .-]+", "", text)
    aliases = {
        "home": "home",
        "home win": "home",
        "1": "home",
        "away": "away",
        "away win": "away",
        "2": "away",
        "draw": "draw",
        "x": "draw",
        "over 2.5": "over_2_5",
        "over 25": "over_2_5",
        "under 2.5": "under_2_5",
        "under 25": "under_2_5",
        "yes": "yes",
        "no": "no",
    }
    return aliases.get(text, text.replace(" ", "_"))


def _odds_dimension(target_implied: tuple[float, float, float], odds: Any) -> bool:
    if not isinstance(odds, dict):
        return False
    values = [odds.get("home"), odds.get("draw"), odds.get("away")]
    for target, raw in zip(target_implied, values):
        decimal = _to_float(raw)
        if decimal and decimal > 1 and abs(target - (1 / decimal)) <= 0.08:
            return True
    return False


def _team_name(doc: dict[str, Any], side: str) -> str:
    value = doc.get(f"{side}_team")
    if isinstance(value, dict):
        return str(value.get("name") or "")
    if value:
        return str(value)
    name = str(doc.get("sportybet_name") or doc.get("name") or "")
    parts = re.split(r"\s+v(?:s)?\.?\s+", name, flags=re.I)
    if len(parts) == 2:
        return parts[0 if side == "home" else 1].strip()
    return ""


def _total_goals(score: Any) -> int | None:
    if not score:
        return None
    match = re.search(r"(\d+)\D+(\d+)", str(score))
    if not match:
        return None
    return int(match.group(1)) + int(match.group(2))


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default
