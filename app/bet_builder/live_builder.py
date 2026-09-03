"""
Bet Builder — Live (manual / smart / LLM)
==========================================
Same three modes as the prematch bet builder (manual/deterministic,
smart/learned-conviction, LLM-assisted), but sourced from currently
in-play matches instead of upcoming ones.

Candidate sourcing, scoring, and selection logic are fully shared with the
prematch builders (`app/bet_builder/core.py`) — the only real differences
here are:

- Candidates come from `live_prediction_candidates()`, which trusts a
  stored live prediction only if it's fresh (see
  `LIVE_PREDICTION_FRESHNESS_SECONDS` in core.py) and otherwise generates
  one on demand, right in the request.
- Booking always uses `force_refresh=True` — live markets move every few
  seconds, so booking must re-check current SportyBet markets rather than
  trusting the snapshot taken at candidate-selection time.
- The research-filter gate (`_research_filter_candidate`, applied by the
  prematch builders) is skipped upstream in `live_prediction_candidates()`
  itself, since its per-league rules have no accuracy history yet for
  live/grid pick types.

This module does not create match predictions on its own beyond the
on-demand generation `live_prediction_candidates()` already performs; it
only ranks and books already-available live picks.

Entry points
------------
- run_live_manual_bet(...) -> dict
- run_live_smart_bet(...) -> dict
- run_live_llm_bet(...) -> dict
"""
from __future__ import annotations

import logging
from typing import Any

from app.bet_builder.core import (
    live_prediction_candidates,
    track_suggested_slip,
)
from app.bet_builder.manual_builder import _candidate_to_analysis, rank_picks_deterministic
from app.bet_builder.smart_builder import rank_picks_smart
from app.bet_builder.llm_builder import rank_picks_llm, _estimate_candidate_limit

logger = logging.getLogger(__name__)

_NO_LIVE_CANDIDATES_MESSAGE = (
    "No currently in-play matches have a bettable live prediction right now "
    "(fresh stored data or a fresh on-demand generation, restricted to "
    "winner, over/under, double chance, next-goal-scorer and BTTS)."
)


def run_live_manual_bet(
    target_odds: float = 1.80,
    max_total_odds: float | None = None,
    stake: int = 100,
    candidate_limit: int = 50,
    request_code: bool = False,
) -> dict[str, Any]:
    """
    Full live manual pipeline: fetch in-play candidates -> deterministic
    rank -> book. No LLM calls at any stage.
    """
    from app.data_clients.sportybet_booking import build_booking_payload, request_share_code

    _max = max_total_odds or target_odds * 3.0
    candidates = live_prediction_candidates(limit=candidate_limit)
    if not candidates:
        return {
            "status": "no_candidates",
            "message": _NO_LIVE_CANDIDATES_MESSAGE,
            "mode": "live_manual",
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
            "mode": "live_manual",
            "candidates_considered": len(candidates),
            "selections": [],
        }

    selected = synthesis.get("ranked_picks") or []
    if not selected:
        return {
            "status": "no_selection",
            "message": "No bookable live picks after ranking",
            "mode": "live_manual",
            "selections": [],
        }

    tracked = track_suggested_slip(
        selected,
        mode="live_manual",
        combined_odds_value=synthesis.get("combined_odds"),
        avg_confidence=synthesis.get("avg_confidence"),
        extra_request={"target_odds": target_odds, "candidate_limit": candidate_limit},
    )

    selections = _to_selections(selected)

    try:
        booking_payload = build_booking_payload(selections, stake=stake, force_refresh=True)
    except Exception as exc:
        return {
            "status": "booking_failed",
            "message": str(exc),
            "mode": "live_manual",
            "candidates_considered": len(candidates),
            "selections": selections,
            "betbuilder_id": tracked.get("id") if tracked else None,
        }

    result: dict[str, Any] = {
        "status": "success",
        "mode": "live_manual",
        "is_live": True,
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


def run_live_smart_bet(
    stake: int = 100,
    candidate_limit: int = 50,
    request_code: bool = False,
) -> dict[str, Any]:
    """
    Full live smart pipeline: fetch in-play candidates -> conviction-only
    rank -> book. No target odds, no LLM calls.

    Live/grid pick types have little graded accuracy history yet (they only
    started grading correctly recently), so `select_by_conviction`'s learned
    threshold will often return nothing at first -- this is expected, same
    as the prematch smart mode's "don't force a pick" philosophy.
    """
    from app.data_clients.sportybet_booking import build_booking_payload, request_share_code

    candidates = live_prediction_candidates(limit=candidate_limit)
    if not candidates:
        return {
            "status": "no_candidates",
            "message": _NO_LIVE_CANDIDATES_MESSAGE,
            "mode": "live_smart",
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
            "mode": "live_smart",
            "candidates_considered": len(candidates),
            "selections": [],
        }

    selected = synthesis.get("ranked_picks") or []
    if not selected:
        return {
            "status": "no_smart_bet",
            "message": "Nothing live right now clears the learned conviction bar. This is by design -- the system won't force a live pick just to have one.",
            "mode": "live_smart",
            "candidates_considered": len(candidates),
            "selections": [],
        }

    tracked = track_suggested_slip(
        selected,
        mode="live_smart",
        combined_odds_value=synthesis.get("combined_odds"),
        avg_confidence=synthesis.get("avg_confidence"),
        extra_request={"candidate_limit": candidate_limit},
    )

    selections = _to_selections(selected)

    try:
        booking_payload = build_booking_payload(selections, stake=stake, force_refresh=True)
    except Exception as exc:
        return {
            "status": "booking_failed",
            "message": str(exc),
            "mode": "live_smart",
            "candidates_considered": len(candidates),
            "selections": selections,
            "betbuilder_id": tracked.get("id") if tracked else None,
        }

    result: dict[str, Any] = {
        "status": "success",
        "mode": "live_smart",
        "is_live": True,
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


def run_live_llm_bet(
    target_odds: float = 5.0,
    max_total_odds: float | None = None,
    stake: int = 100,
    candidate_limit: int | None = None,
    request_code: bool = False,
) -> dict[str, Any]:
    """
    Full LLM-assisted live pipeline: fetch in-play candidates -> rank
    (with an LLM slip-synthesis pass, urgency-adjusted for live state) ->
    book.

    The LLM, when available, only synthesizes the final slip from already
    generated live picks -- it does not predict individual matches.
    """
    from app.data_clients.sportybet_booking import build_booking_payload, request_share_code

    _max = max_total_odds or target_odds * 3.0
    _limit = candidate_limit or _estimate_candidate_limit(target_odds)
    candidates = live_prediction_candidates(limit=_limit)

    if not candidates:
        return {
            "status": "no_candidates",
            "message": _NO_LIVE_CANDIDATES_MESSAGE,
            "mode": "live_llm",
            "candidate_count": 0,
        }

    analyses = [_candidate_to_analysis(c) for c in candidates]

    try:
        synthesis = rank_picks_llm(
            analyses,
            target_odds=target_odds,
            max_total_odds=_max,
            skip_llm_synthesis=False,
        )
    except Exception as exc:
        return {
            "status": "synthesis_failed",
            "message": str(exc),
            "mode": "live_llm",
            "candidates_considered": len(candidates),
            "selections": [],
        }

    selected = synthesis.get("ranked_picks") or []
    if not selected:
        return {
            "status": "no_selection",
            "message": "No bookable live picks after ranking",
            "mode": "live_llm",
            "selections": [],
        }

    tracked = track_suggested_slip(
        selected,
        mode="live_llm",
        combined_odds_value=synthesis.get("combined_odds"),
        avg_confidence=synthesis.get("avg_confidence"),
        extra_request={"target_odds": target_odds, "candidate_limit": _limit},
    )

    selections = _to_selections(selected)

    try:
        booking_payload = build_booking_payload(selections, stake=stake, force_refresh=True)
    except Exception as exc:
        return {
            "status": "booking_failed",
            "message": str(exc),
            "mode": "live_llm",
            "candidates_considered": len(candidates),
            "selections": selections,
            "betbuilder_id": tracked.get("id") if tracked else None,
        }

    result: dict[str, Any] = {
        "status": "success",
        "mode": "live_llm",
        "is_live": True,
        "llm_powered": True,
        "llm_scope": "slip_synthesis_only",
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_selections(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "sportybet_id": i.get("match_id") or i.get("sportybet_id"),
        "type": i.get("type") or i.get("pick_type"),
        "selection": i.get("selection"),
        "marketId": i.get("marketId"),
        "outcomeId": i.get("outcomeId"),
    } for i in picks]
