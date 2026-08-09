"""Layer 2 — Auto-bet consumer.

The auto-bet system is a *consumer + learner*, not a re-predictor.

Pipeline (3 layers)
-------------------
1. Prediction Production — the manual bot (Bot 1 / Bot 2) and the AI queue job
   (``job_ai_prediction_queue``) write predictions into the prediction store.
   This is the ONLY place LLM work happens.
2. Bet Consumption        — this module.  It reads already-produced predictions,
   applies the research-driven good/bad gate (``research_filter.evaluate_pick``,
   exposed through ``upcoming_prediction_candidates``), and builds a lightweight
   SportyBet booking slip.  **No LLM calls.**
3. Learning               — ``job_regenerate_research_stats`` rebuilds the
   ``research_stats`` table from settled outcomes; those stats feed the dynamic
   BLOCK / CAUTION / TRUST rules back into ``evaluate_pick``, so the gate (and
   therefore the auto-bet) improves over time.

Why this module exists
----------------------
Previously the bet path re-ran the full LLM pipeline (``enriched_match_analysis``
per candidate + Groq synthesis) on every request.  That duplicated Prediction
Production work and made auto-bet slow.  This consumer reuses stored predictions
instead, which is exactly what the auto-bet is supposed to do: check the
predictions that already exist, make the booking, and let the research module
decide which picks are good or bad.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _build_analysis_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Convert a stored prediction candidate into the analysis shape that
    ``synthesize_sure_picks`` expects — WITHOUT any LLM call.

    Every field is taken from data that was already produced by the prediction
    jobs (manual bot + AI queue).  Nothing here talks to an LLM.
    """
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
        "market_signal": candidate.get("market_signal"),
        "btts": pick.get("btts"),
        "over_2_5": pick.get("over_2_5"),
        "source": candidate.get("source") or pick.get("source") or "stored_prediction",
        "status": "success",
        "confirmed": bool(candidate.get("confirmed")),
        "similar_matches_used": int(candidate.get("similar_matches_used") or 0),
    }


def run_auto_bet(
    target_odds: float = 1.80,
    max_total_odds: float | None = None,
    stake: int = 100,
    candidate_limit: int = 50,
    request_code: bool = False,
) -> dict[str, Any]:
    """Consume stored predictions, apply the research good/bad gate, and build a
    booking slip.  Performs **no LLM re-prediction**.

    Returns a dict with ``status`` and, on success, a SportyBet ``booking_payload``.
    """
    from app.ai.ai_betbuilder import upcoming_prediction_candidates, synthesize_sure_picks
    from app.data_clients.sportybet_booking import build_booking_payload, request_share_code

    # Layer 2: read stored predictions that already passed the research filter.
    # upcoming_prediction_candidates applies _research_filter_candidate (the
    # evaluate_pick good/bad gate) — no LLM involved here.
    candidates = upcoming_prediction_candidates(limit=candidate_limit)
    n_candidates = len(candidates)
    if not candidates:
        return {
            "status": "no_candidates",
            "message": "No stored predictions passed the research filter",
            "mode": "consumer",
            "candidates_considered": n_candidates,
            "selections": [],
        }

    # Build analyses from stored data only (no enriched_match_analysis / LLM).
    analyses = [_build_analysis_from_candidate(c) for c in candidates]

    # Deterministic synthesis — research conviction is already applied inside
    # synthesize_sure_picks (research_conviction_adj / optimal_profile_score).
    # The deterministic flag skips the LLM synthesis call entirely.
    try:
        synthesis = synthesize_sure_picks(
            analyses,
            target_odds=target_odds,
            max_total_odds=max_total_odds or target_odds * 3.0,
            deterministic=True,
        )
    except Exception as exc:
        return {
            "status": "synthesis_failed",
            "message": str(exc),
            "mode": "consumer",
            "candidates_considered": n_candidates,
            "selections": [],
        }

    selected = synthesis.get("ranked_picks") or []
    if not selected:
        return {
            "status": "no_selection",
            "message": "Research gate left no bookable picks",
            "mode": "consumer",
            "selections": [],
        }

    # Keep only the fields the booking contract needs.
    selections = [{
        "sportybet_id": item.get("match_id") or item.get("sportybet_id"),
        "type": item.get("type") or item.get("pick_type"),
        "selection": item.get("selection"),
        "marketId": item.get("marketId"),
        "outcomeId": item.get("outcomeId"),
    } for item in selected]

    try:
        booking_payload = build_booking_payload(selections, stake=stake)
    except Exception as exc:
        return {
            "status": "booking_failed",
            "message": str(exc),
            "mode": "consumer",
            "candidates_considered": n_candidates,
            "selections": selections,
        }

    result: dict[str, Any] = {
        "status": "success",
        "mode": "consumer",  # no LLM re-prediction
        "research_gate": "applied",  # evaluate_pick via upcoming_prediction_candidates
        "target_odds": target_odds,
        "combined_odds": synthesis.get("combined_odds"),
        "avg_confidence": synthesis.get("avg_confidence"),
        "confirmed_count": synthesis.get("confirmed_count"),
        "candidates_considered": len(candidates),
        "booking_payload": booking_payload,
        "selections": selected,
    }
    if request_code:
        try:
            result["share_code"] = request_share_code(booking_payload)
        except Exception as exc:  # booking still succeeded; code is optional
            result["share_code_error"] = str(exc)
    return result
