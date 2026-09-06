"""
Bet Builder — Manual (Deterministic)
=====================================
Ranks candidates using stored prediction data only.  No LLM calls.

Entry points
------------
- rank_picks_deterministic(analyses, *, target_odds, max_total_odds) -> dict
- run_manual_bet(...)  -> dict   (full pipeline: candidates → rank → book)
"""
from __future__ import annotations

import logging
from typing import Any

from app.bet_builder.core import (
    upcoming_prediction_candidates,
    score_pick,
    select_by_odds,
    trim_to_ceiling,
    combined_odds,
    pick_decimal_odds,
    track_suggested_slip,
    _best_pick,
    _rank_analyses,
    _to_int,
    _to_float,
)

logger = logging.getLogger(__name__)


def rank_picks_deterministic(
    analyses: list[dict[str, Any]],
    *,
    target_odds: float,
    max_total_odds: float,
) -> dict[str, Any]:
    """
    Score and rank analyses without any LLM call.

    Conviction is derived entirely from stored prediction data, research
    conviction adjustments, league accuracy history, and signal combinations.
    """
    clean = [
        item for item in analyses
        if item.get("status") == "success" or item.get("llm_recommendation")
    ]
    if len(clean) < 1:
        raise ValueError("At least one completed analysis is required")

    ranked = _rank_analyses(analyses, "deterministic", "manual_pick")

    selected = select_by_odds(ranked, target_odds, max_total_odds)
    selected = trim_to_ceiling(selected, max_total_odds)
    if not selected:
        selected = ranked[:1]

    _annotate_reasoning(selected, llm=False)

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
        "synthesis_reasoning": "Deterministic ranking — no LLM used.",
    }


def run_manual_bet(
    target_odds: float = 1.80,
    max_total_odds: float | None = None,
    stake: int = 100,
    candidate_limit: int = 50,
    request_code: bool = False,
) -> dict[str, Any]:
    """
    Full manual pipeline: fetch candidates → deterministic rank → book.
    No LLM calls at any stage.
    """
    from app.data_clients.sportybet_booking import build_booking_payload, request_share_code

    _max = max_total_odds or target_odds * 3.0
    candidates = upcoming_prediction_candidates(limit=candidate_limit)
    if not candidates:
        return {
            "status": "no_candidates",
            "message": "No upcoming ungraded predictions available for today/tomorrow that pass all filters (date, time, live status, odds, research gate, buffer data).",
            "mode": "manual",
            "candidates_considered": 0,
            "selections": [],
        }

    analyses = [_candidate_to_analysis(c) for c in candidates]

    try:
        synthesis = rank_picks_deterministic(analyses, target_odds=target_odds, max_total_odds=_max)
    except Exception as exc:
        return {
            "status": "synthesis_failed",
            "message": str(exc),
            "mode": "manual",
            "candidates_considered": len(candidates),
            "selections": [],
        }

    selected = synthesis.get("ranked_picks") or []
    if not selected:
        return {
            "status": "no_selection",
            "message": "Research gate left no bookable picks",
            "mode": "manual",
            "selections": [],
        }

    tracked = track_suggested_slip(
        selected,
        mode="manual",
        combined_odds_value=synthesis.get("combined_odds"),
        avg_confidence=synthesis.get("avg_confidence"),
        extra_request={"target_odds": target_odds, "candidate_limit": candidate_limit},
    )

    selections = [{
        "sportybet_id": i.get("sportybet_id") or i.get("match_id"),
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
            "mode": "manual",
            "candidates_considered": len(candidates),
            "selections": selections,
            "betbuilder_id": tracked.get("id") if tracked else None,
        }

    result: dict[str, Any] = {
        "status": "success",
        "mode": "manual",
        "research_gate": "applied",
        "target_odds": target_odds,
        "combined_odds": synthesis.get("combined_odds"),
        "avg_confidence": synthesis.get("avg_confidence"),
        "confirmed_count": synthesis.get("confirmed_count"),
        "candidates_considered": len(candidates),
        "booking_payload": booking_payload,
        "selections": selected,
        "betbuilder_id": tracked.get("id") if tracked else None,
    }
    if request_code:
        try:
            result["share_code"] = request_share_code(booking_payload)
        except Exception as exc:
            result["share_code_error"] = str(exc)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _candidate_to_analysis(candidate: dict[str, Any]) -> dict[str, Any]:
    """Convert a stored prediction candidate into the analysis shape."""
    pick = candidate.get("best_pick") or {}
    return {
        "match_id": candidate.get("match_id") or candidate.get("sportybet_id"),
        "sportybet_id": candidate.get("sportybet_id") or candidate.get("match_id"),
        "match_name": candidate.get("match_name") or candidate.get("name"),
        "league_name": candidate.get("league_name") or candidate.get("tournament"),
        "country_name": candidate.get("country_name") or candidate.get("category"),
        "llm_recommendation": pick.get("selection"),
        "llm_confidence": int(pick.get("confidence") or 0),
        "prediction_engine_pick": pick,
        "key_factors": pick.get("key_factors") or candidate.get("key_factors"),
        "market_signal": candidate.get("market_signal") or pick.get("market_signal"),
        "btts": candidate.get("btts") if candidate.get("btts") is not None else pick.get("btts"),
        "over_2_5": candidate.get("over_2_5") if candidate.get("over_2_5") is not None else pick.get("over_2_5"),
        "source": candidate.get("source") or pick.get("source") or "stored_prediction",
        "status": "success",
        "confirmed": bool(candidate.get("confirmed")),
        "similar_matches_used": int(candidate.get("similar_matches_used") or 0),
    }


def _annotate_reasoning(picks: list[dict[str, Any]], *, llm: bool) -> None:
    for item in picks:
        if not item.get("synthesis_reasoning"):
            if item.get("confirmed"):
                item["synthesis_reasoning"] = "Prediction engine consensus pick."
            else:
                item["synthesis_reasoning"] = "High-conviction deterministic pick."
