"""
Bet Builder Router
==================
Two clearly separated endpoints:

  POST /betbuilder/manual   — deterministic, no LLM, fast
  POST /betbuilder/llm      — LLM-enriched per match, slower

Both share the same request/response contract so the frontend can swap
between them without any schema changes.
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

    Reads stored predictions, applies the research filter gate, scores and
    ranks picks using conviction scoring, and builds a SportyBet booking slip.
    No LLM calls — fast and always available.
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
    LLM-powered bet builder.

    Enriches each candidate with a per-match OpenRouter/Groq analysis, then
    ranks using conviction scoring with an optional LLM synthesis pass.
    Slower than /manual but uses live LLM reasoning per match.
    """
    from app.bet_builder.llm_builder import run_llm_bet

    return run_llm_bet(
        target_odds=body.target_odds,
        max_total_odds=body.max_total_odds,
        stake=body.stake,
        candidate_limit=body.candidate_limit,
        request_code=body.request_code,
    )
