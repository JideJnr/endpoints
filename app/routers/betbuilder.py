"""
Bet Builder Router
==================
Prematch (upcoming) endpoints:

  POST /betbuilder/manual   - deterministic stored-pick builder
  POST /betbuilder/llm      - stored picks plus optional slip synthesis
  POST /betbuilder/smart    - learned-conviction only, no target odds

Live (in-play) endpoints — same three modes, sourced from currently live
matches instead of upcoming ones:

  POST /betbuilder/live/manual  - "Accept": deterministic, no LLM
  POST /betbuilder/live/smart   - "Suggest": learned conviction, no target
  POST /betbuilder/live/llm     - "AI": LLM-assisted slip synthesis

All endpoints consume predictions already produced by unified upcoming/live.
They do not generate per-match predictions themselves (the live candidate
fetch may trigger an on-demand live prediction for a match with no fresh
stored one, but that's the existing prediction pipeline, not new logic here).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/betbuilder", tags=["betbuilder"])


class BetBuilderRequest(BaseModel):
    target_odds: float = Field(default=1.80, gt=1.0)
    max_total_odds: Optional[float] = Field(default=None, gt=1.0)
    stake: int = Field(default=100, gt=0)
    candidate_limit: int = Field(default=50, gt=0, le=200)
    request_code: bool = False


class SmartBetRequest(BaseModel):
    stake: int = Field(default=100, gt=0)
    candidate_limit: int = Field(default=50, gt=0, le=200)
    request_code: bool = False


@router.post("/manual")
def manual_bet(body: BetBuilderRequest) -> dict[str, Any]:
    """
    Deterministic bet builder.

    Reads stored unified predictions, applies the research filter gate, scores
    and ranks picks using conviction scoring, and builds a SportyBet booking
    slip. No LLM calls.
    """
    from app.bet_builder.manual_builder import run_manual_bet

    return run_manual_bet(
        target_odds=body.target_odds,
        max_total_odds=body.max_total_odds,
        stake=body.stake,
        candidate_limit=body.candidate_limit,
        request_code=body.request_code,
    )


@router.post("/llm")
def llm_bet(body: BetBuilderRequest) -> dict[str, Any]:
    """
    LLM-assisted bet builder.

    Reads stored unified predictions, applies the research filter gate, scores
    them, and optionally uses the LLM only to choose the combined slip. It does
    not generate per-match predictions.
    """
    from app.bet_builder.llm_builder import run_llm_bet

    return run_llm_bet(
        target_odds=body.target_odds,
        max_total_odds=body.max_total_odds,
        stake=body.stake,
        candidate_limit=body.candidate_limit,
        request_code=body.request_code,
    )


@router.post("/smart")
def smart_bet(body: SmartBetRequest) -> dict[str, Any]:
    """
    Smart bet builder — no target odds.

    Reads stored unified predictions, scores them with the same conviction
    logic as the other two modes (learned pick accuracy, per-league
    accuracy, signal-combination history), and includes every pick whose
    conviction clears its own learned bar. Combined odds is whatever
    results from stacking every leg that earns its place -- there's no
    target to hit and nothing trims the slip back down once it grows.

    Returns status "no_smart_bet" (not a weak fallback pick) on a day
    nothing clears the bar. No LLM calls.
    """
    from app.bet_builder.smart_builder import run_smart_bet

    return run_smart_bet(
        stake=body.stake,
        candidate_limit=body.candidate_limit,
        request_code=body.request_code,
    )


# ---------------------------------------------------------------------------
# Live (in-play) — same three modes, sourced from currently live matches
# ---------------------------------------------------------------------------

@router.post("/live/manual")
def live_manual_bet(body: BetBuilderRequest) -> dict[str, Any]:
    """
    Live "Accept" builder — deterministic, no LLM.

    Reads currently in-play matches with a fresh (or freshly generated)
    live prediction from the shared live probability grid, scores and ranks
    them with the same conviction logic as the prematch manual builder, and
    builds a SportyBet booking slip (force-refreshed against live markets).
    """
    from app.bet_builder.live_builder import run_live_manual_bet

    return run_live_manual_bet(
        target_odds=body.target_odds,
        max_total_odds=body.max_total_odds,
        stake=body.stake,
        candidate_limit=body.candidate_limit,
        request_code=body.request_code,
    )


@router.post("/live/smart")
def live_smart_bet(body: SmartBetRequest) -> dict[str, Any]:
    """
    Live "Suggest" builder — no target odds, learned conviction only.

    Includes every currently in-play pick whose conviction clears its own
    learned bar. Returns status "no_smart_bet" rather than forcing a pick
    when nothing live clears the bar right now. No LLM calls.
    """
    from app.bet_builder.live_builder import run_live_smart_bet

    return run_live_smart_bet(
        stake=body.stake,
        candidate_limit=body.candidate_limit,
        request_code=body.request_code,
    )


@router.post("/live/llm")
def live_llm_bet(body: BetBuilderRequest) -> dict[str, Any]:
    """
    Live "AI" builder — LLM-assisted slip synthesis.

    Reads currently in-play matches with a fresh live prediction, scores
    them, and (when available) uses the LLM to choose the combined slip
    from those already-generated picks. It does not predict individual
    matches itself.
    """
    from app.bet_builder.live_builder import run_live_llm_bet

    return run_live_llm_bet(
        target_odds=body.target_odds,
        max_total_odds=body.max_total_odds,
        stake=body.stake,
        candidate_limit=body.candidate_limit,
        request_code=body.request_code,
    )
