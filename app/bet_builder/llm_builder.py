"""
Bet Builder - LLM Assisted
==========================
Ranks stored prediction-engine candidates with learned conviction scores,
then optionally uses an LLM synthesis pass to select the final slip.

This module does not create match predictions or run per-match analysis.
Unified upcoming/live jobs own prediction generation; bet builder only
combines already available picks.

Entry points
------------
- rank_picks_llm(analyses, *, target_odds, max_total_odds) -> dict
- run_llm_bet(...) -> dict   (candidates -> rank -> book)
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any

from app.bet_builder.core import (
    upcoming_prediction_candidates,
    select_by_odds,
    trim_to_ceiling,
    combined_odds,
    track_suggested_slip,
    _rank_analyses,
)

logger = logging.getLogger(__name__)


def rank_picks_llm(
    analyses: list[dict[str, Any]],
    *,
    target_odds: float,
    max_total_odds: float,
    skip_llm_synthesis: bool = False,
) -> dict[str, Any]:
    """
    Score and rank stored prediction analyses.

    Attempts a slip-level LLM synthesis pass to select the final combined bet.
    Falls back to deterministic selection if the LLM is unavailable or
    skip_llm_synthesis=True.
    """
    clean = [
        item for item in analyses
        if item.get("status") == "success" or item.get("llm_recommendation")
    ]
    if len(clean) < 1:
        raise ValueError("At least one stored prediction is required")

    ranked = _rank_analyses(analyses, "llm_synthesis", "stored_pick")

    selected = []
    reasoning = ""
    if not skip_llm_synthesis:
        try:
            model_plan = _run_llm_synthesis(ranked, target_odds, max_total_odds)
            selected_ids = {
                str(i.get("match_id") or "") for i in model_plan.get("selected_picks") or []
            }
            selected = [i for i in ranked if str(i.get("match_id") or "") in selected_ids]
            reasoning = (
                model_plan.get("synthesis_reasoning")
                or "LLM selected from stored prediction-engine picks."
            )
        except Exception as exc:
            logger.warning("LLM synthesis failed, falling back to deterministic: %s", exc)
            reasoning = f"Deterministic fallback after LLM synthesis failed: {exc}"

    if not selected:
        selected = select_by_odds(ranked, target_odds, max_total_odds)
        if not reasoning:
            reasoning = "Deterministic selection used."

    selected = trim_to_ceiling(selected, max_total_odds)
    if not selected:
        selected = ranked[:1]

    _annotate_reasoning(selected)

    total = combined_odds(selected)
    avg_conf = (
        round(sum(float(i.get("llm_confidence") or 0) for i in selected) / len(selected))
        if selected else 0
    )
    return {
        "status": "success",
        "ranked_picks": selected,
        "combined_odds": round(total, 3),
        "avg_confidence": avg_conf,
        "confirmed_count": sum(1 for i in selected if i.get("confirmed")),
        "no_consensus": not any(i.get("confirmed") for i in selected),
        "target_not_met": total < target_odds,
        "synthesis_reasoning": reasoning,
    }


def run_llm_bet(
    target_odds: float = 5.0,
    max_total_odds: float | None = None,
    stake: int = 100,
    candidate_limit: int | None = None,
    request_code: bool = False,
) -> dict[str, Any]:
    """
    Full LLM-assisted pipeline: fetch stored candidates -> rank -> book.

    The LLM, when available, only synthesizes the final slip from prediction
    engine picks. It does not predict individual matches.
    """
    from app.bet_builder.manual_builder import _candidate_to_analysis
    from app.data_clients.sportybet_booking import build_booking_payload, request_share_code

    _max = max_total_odds or target_odds * 3.0
    _limit = candidate_limit or _estimate_candidate_limit(target_odds)
    candidates = upcoming_prediction_candidates(limit=_limit)

    if not candidates:
        return {
            "status": "no_candidates",
            "message": "No upcoming ungraded predictions available for today/tomorrow that pass all filters (date, time, live status, odds, research gate, buffer data).",
            "mode": "llm",
            "candidate_count": 0,
        }

    analyses = [_candidate_to_analysis(candidate) for candidate in candidates]

    if not analyses:
        return {
            "status": "error",
            "message": "No stored prediction candidates could be converted",
            "mode": "llm",
            "candidates_considered": len(candidates),
            "predictions_available": 0,
        }

    try:
        synthesis = rank_picks_llm(analyses, target_odds=target_odds, max_total_odds=_max)
    except Exception as exc:
        return {
            "status": "synthesis_failed",
            "message": str(exc),
            "mode": "llm",
            "candidates_considered": len(candidates),
            "selections": [],
        }

    selected = synthesis.get("ranked_picks") or []
    if not selected:
        return {
            "status": "no_selection",
            "message": "Stored prediction gate left no bookable picks",
            "mode": "llm",
            "selections": [],
        }

    tracked = track_suggested_slip(
        selected,
        mode="llm",
        combined_odds_value=synthesis.get("combined_odds"),
        avg_confidence=synthesis.get("avg_confidence"),
        extra_request={"target_odds": target_odds, "candidate_limit": _limit},
    )

    selections = [{
        "sportybet_id": i.get("match_id") or i.get("sportybet_id"),
        "type": i.get("type") or i.get("pick_type"),
        "selection": i.get("selection"),
        "marketId": i.get("marketId"),
        "outcomeId": i.get("outcomeId"),
    } for i in selected]

    try:
        booking_payload = build_booking_payload(selections, stake=stake, force_refresh=False)
    except Exception as exc:
        return {
            "status": "booking_failed",
            "message": str(exc),
            "mode": "llm",
            "candidates_considered": len(candidates),
            "selections": selections,
            "betbuilder_id": tracked.get("id") if tracked else None,
        }

    result: dict[str, Any] = {
        "status": "success",
        "mode": "llm",
        "llm_powered": True,
        "llm_scope": "slip_synthesis_only",
        "research_gate": "applied",
        "target_odds": target_odds,
        "combined_odds": synthesis.get("combined_odds"),
        "avg_confidence": synthesis.get("avg_confidence"),
        "confirmed_count": synthesis.get("confirmed_count"),
        "candidates_considered": len(candidates),
        "predictions_available": len(analyses),
        "booking_payload": booking_payload,
        "selections": selected,
        "synthesis_reasoning": synthesis.get("synthesis_reasoning"),
        "betbuilder_id": tracked.get("id") if tracked else None,
    }
    if request_code:
        try:
            result["share_code"] = request_share_code(booking_payload)
        except Exception as exc:
            result["share_code_error"] = str(exc)
    return result


def _run_llm_synthesis(
    ranked: list[dict[str, Any]],
    target_odds: float,
    max_total_odds: float,
) -> dict[str, Any]:
    from app.ai.llm import get_llm

    prompt = (
        "You are an expert football betting slip analyst. "
        "Return strict JSON with selected_picks (array of objects with match_id) "
        "and synthesis_reasoning. "
        "Use only the provided stored prediction-engine picks. "
        "Do not create new match predictions or change a pick selection. "
        "Rank by conviction_score, confidence, odds fit, and market availability. "
        "Strongly prefer picks with positive league_accuracy_boost. "
        "Avoid picks with league_accuracy_boost below -5 unless no alternatives exist. "
        f"Select enough picks to reach target odds {target_odds} "
        f"without exceeding max odds {max_total_odds}. "
        "If target cannot be reached, return the best available picks.\n\n"
        + json.dumps(ranked[:100], ensure_ascii=False)
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


def _estimate_candidate_limit(target_odds: float) -> int:
    estimated_legs = max(6, int(math.log(max(target_odds, 2)) / math.log(1.65)) + 2)
    return min(200, max(10, estimated_legs * 3))


def _annotate_reasoning(picks: list[dict[str, Any]]) -> None:
    for item in picks:
        if not item.get("synthesis_reasoning"):
            if item.get("confirmed"):
                item["synthesis_reasoning"] = "Prediction engine consensus pick."
            else:
                item["synthesis_reasoning"] = "Included as a high-conviction stored pick."
