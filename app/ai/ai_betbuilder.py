"""
AI Bet Builder — LLM enrichment layer.

Responsibilities
----------------
- enriched_match_analysis()  per-match OpenRouter/Groq enrichment
- similarity_gate()          filter similar historical matches
- build_ai_betbuilder()      legacy entry point (delegates to llm_builder)

Shared candidate/scoring logic lives in ``app.bet_builder.core``.
Re-exported here so existing callers don't break.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from app.storage.buffer import get_buffered_match
from app.storage.league_memory import list_prediction_history

# Re-exports for backward compatibility
from app.bet_builder.core import (                          # noqa: F401
    upcoming_prediction_candidates,
    pick_decimal_odds as _pick_decimal_odds,
    extract_odds_profile as _extract_betbuilder_odds_profile,
    _best_pick,
    _same_outcome,
    _to_float,
    _to_int,
)
from app.bet_builder.manual_builder import rank_picks_deterministic  # noqa: F401

from app.utils.match_helpers import _team_name

_ANALYSIS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 10 * 60


# ---------------------------------------------------------------------------
# Per-match LLM enrichment
# ---------------------------------------------------------------------------

def enriched_match_analysis(
    sportybet_id: str, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Enrich a single match with an OpenRouter/Groq LLM analysis.

    Cached for ``_CACHE_TTL_SECONDS``.  Returns a status='error' dict on any
    LLM failure so callers can skip gracefully.
    """
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
    llm_input = _analysis_doc(doc, engine_pick, gated)
    analysis = _run_openrouter_enriched(llm_input)
    if analysis.get("status") in {"openrouter_unavailable", "agent_build_failed", "error"}:
        return analysis

    llm_selection = analysis.get("recommendation") or analysis.get("llm_recommendation")
    confirmed = _same_outcome(engine_pick.get("selection"), llm_selection)
    result = {
        "status": "success",
        "sportybet_id": cache_key,
        "match_id": cache_key,
        "match_name": doc.get("sportybet_name") or doc.get("match_name") or doc.get("name") or cache_key,
        "league_name": doc.get("tournament") or doc.get("league_name"),
        "country_name": doc.get("category") or doc.get("country_name"),
        "llm_recommendation": llm_selection,
        "llm_confidence": _to_int(analysis.get("confidence"), 0),
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


# ---------------------------------------------------------------------------
# Similarity gate
# ---------------------------------------------------------------------------

def similarity_gate(
    doc: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    from app.enrichment.similar_matches import _extract_target_odds_implied

    target_implied = _extract_target_odds_implied(doc)
    team_terms = [t for t in (_team_name(doc, "home"), _team_name(doc, "away")) if len(t) >= 4]
    scored = [i for i in candidates if float(i.get("similarity_score") or 0) >= 0.40]
    totals = [t for t in (_total_goals(i.get("final_score")) for i in scored) if t is not None]
    avg_total = sum(totals) / len(totals) if totals else None

    passing = []
    for item in scored:
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


# ---------------------------------------------------------------------------
# Legacy entry point — delegates to llm_builder
# ---------------------------------------------------------------------------

def build_ai_betbuilder(payload: dict[str, Any]) -> dict[str, Any]:
    """Legacy entry point.  Delegates to ``bet_builder.llm_builder.run_llm_bet``."""
    from app.bet_builder.llm_builder import run_llm_bet

    return run_llm_bet(
        target_odds=max(1.01, _to_float(payload.get("target_odds")) or 5.0),
        max_total_odds=_to_float(payload.get("max_total_odds")),
        stake=int(payload.get("stake") or 100),
        candidate_limit=_to_int(payload.get("candidate_limit"), 0) or None,
    )


# ---------------------------------------------------------------------------
# synthesize_sure_picks — backward-compat shim
# ---------------------------------------------------------------------------

def synthesize_sure_picks(
    analyses: list[dict[str, Any]],
    *,
    target_odds: float,
    max_total_odds: float,
    deterministic: bool = False,
) -> dict[str, Any]:
    """Backward-compatible shim.

    Routes to ``rank_picks_deterministic`` or ``rank_picks_llm`` based on the
    ``deterministic`` flag.
    """
    if deterministic:
        return rank_picks_deterministic(
            analyses, target_odds=target_odds, max_total_odds=max_total_odds
        )
    from app.bet_builder.llm_builder import rank_picks_llm
    return rank_picks_llm(
        analyses, target_odds=target_odds, max_total_odds=max_total_odds
    )


# ---------------------------------------------------------------------------
# Internal LLM helpers
# ---------------------------------------------------------------------------

def _run_openrouter_enriched(doc: dict[str, Any]) -> dict[str, Any]:
    from app.ai.llm_analysis import run_llm_match_analysis
    return run_llm_match_analysis(doc)


def _analysis_doc(
    doc: dict[str, Any],
    engine_pick: dict[str, Any],
    similar: list[dict[str, Any]],
) -> dict[str, Any]:
    prompt_context = {
        "prediction_engine_best_pick": engine_pick,
        "similar_matches": [
            {
                "match_name": i.get("match_name"),
                "final_score": i.get("final_score"),
                "prediction_made": i.get("prediction_made"),
                "similarity_dimension": i.get("similarity_dimension"),
                "similarity_score": i.get("similarity_score"),
            }
            for i in similar
        ] if len(similar) >= 2 else [],
    }
    return {
        **doc,
        "ai_enriched_context": prompt_context,
        "web_context": {
            **(doc.get("web_context") or {}),
            "snippets": [{"snippet": json.dumps(prompt_context)[:900]}],
        },
    }


def _similar_matches(doc: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    from app.enrichment.similar_matches import find_similar_matches
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


# ---------------------------------------------------------------------------
# Geometry / text helpers (used by similarity_gate)
# ---------------------------------------------------------------------------


def _total_goals(score: str | None) -> float | None:
    """Parse a score string like '2-1' and return total goals."""
    if not score or '-' not in str(score):
        return None
    parts = str(score).split('-', 1)
    try:
        return float(parts[0]) + float(parts[1])
    except (ValueError, TypeError):
        return None


def _odds_dimension(
    target_implied: tuple[float, float, float], odds: Any
) -> bool:
    if not isinstance(odds, dict):
        return False
    for target, raw in zip(target_implied, [odds.get("home"), odds.get("draw"), odds.get("away")]):
        decimal = _to_float(raw)
        if decimal and decimal > 1 and abs(target - (1 / decimal)) <= 0.08:
            return True
    return False


