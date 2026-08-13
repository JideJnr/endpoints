"""
Bet Builder Router
==================
Two clearly separated endpoints:

  POST /betbuilder/manual   - deterministic stored-pick builder
  POST /betbuilder/llm      - stored picks plus optional slip synthesis

Both endpoints consume predictions already produced by unified upcoming/live.
They do not generate per-match predictions.
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
