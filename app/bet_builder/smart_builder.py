"""
Bet Builder — Smart (Learned Conviction, No Target)
====================================================
Unlike the manual/LLM builders, this mode does not accept a target odds.
It reads stored unified predictions, scores them with the same conviction
logic (learned pick accuracy, league accuracy, signal-combination history),
and includes every candidate whose conviction clears its own learned bar.
Combined odds is whatever results from stacking every leg that earns its
place — there is no target to hit and no ceiling to trim back down to.

If nothing clears the bar today, this mode returns "no_smart_bet" rather
than falling back to a weak pick — forcing a bet on a day nothing is
actually confident about would defeat the point of a "smart" mode.

Entry points
------------
- rank_picks_smart(analyses) -> dict
- run_smart_bet(...) -> dict   (full pipeline: candidates -> rank -> book)
"""
from __future__ import annotations

import logging
from typing import Any

from app.bet_builder.core import (
    upcoming_prediction_candidates,
    select_by_conviction,
    combined_odds,
    track_suggested_slip,
    _rank_analyses,
)

logger = logging.getLogger(__name__)


def rank_picks_smart(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Score and select analyses using learned conviction only. No LLM call,
    no target odds — every candidate that clears its own learned threshold
    is included.
    """
    clean = [
        item for item in analyses
        if item.get("status") == "success" or item.get("llm_recommendation")
    ]
    if len(clean) < 1:
        raise ValueError("At least one completed analysis is required")

    ranked = _rank_analyses(analyses, "smart", "smart_pick")
    selected = select_by_conviction(ranked)

    _annotate_smart_reasoning(selected)

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
        "synthesis_reasoning": "Learned-conviction selection — no target odds, no LLM used.",
    }


def run_smart_bet(
    stake: int = 100,
    candidate_limit: int = 50,
    request_code: bool = False,
) -> dict[str, Any]:
    """
    Full smart pipeline: fetch candidates -> conviction-only rank -> book.
    No target odds, no LLM calls at any stage.
    """
    from app.data_clients.sportybet_booking import build_booking_payload, request_share_code
    from app.bet_builder.manual_builder import _candidate_to_analysis

    candidates = upcoming_prediction_candidates(limit=candidate_limit)
    if not candidates:
        return {
            "status": "no_candidates",
            "message": "No upcoming ungraded predictions available for today/tomorrow that pass all filters (date, time, live status, odds, research gate, buffer data).",
            "mode": "smart",
            "candidates_considered": 0,
            "selections": [],
        }

    analyses = [_candidate_to_analysis(c) for c in candidates]

    try:
        synthesis = rank_picks_smart(analyses)
    except Exception as exc:
        return {
            "status": "synthesis_failed",
            "message": str(exc),
            "mode": "smart",
            "candidates_considered": len(candidates),
            "selections": [],
        }

    selected = synthesis.get("ranked_picks") or []
    if not selected:
        return {
            "status": "no_smart_bet",
            "message": "Nothing today clears the learned conviction bar for a smart bet. This is by design — the system won't force a pick just to have one.",
            "mode": "smart",
            "candidates_considered": len(candidates),
            "selections": [],
        }

    tracked = track_suggested_slip(
        selected,
        mode="smart",
        combined_odds_value=synthesis.get("combined_odds"),
        avg_confidence=synthesis.get("avg_confidence"),
        extra_request={"candidate_limit": candidate_limit},
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
            "mode": "smart",
            "candidates_considered": len(candidates),
            "selections": selections,
            "betbuilder_id": tracked.get("id") if tracked else None,
        }

    result: dict[str, Any] = {
        "status": "success",
        "mode": "smart",
        "research_gate": "applied",
        "combined_odds": synthesis.get("combined_odds"),
        "avg_confidence": synthesis.get("avg_confidence"),
        "confirmed_count": synthesis.get("confirmed_count"),
        "leg_count": len(selected),
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

def _annotate_smart_reasoning(picks: list[dict[str, Any]]) -> None:
    for item in picks:
        if not item.get("synthesis_reasoning"):
            if item.get("confirmed"):
                item["synthesis_reasoning"] = "Prediction engine consensus pick, clears learned conviction bar."
            else:
                item["synthesis_reasoning"] = "High-conviction pick backed by learned pick/league accuracy."
