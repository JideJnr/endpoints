"""
Multi-stage OpenRouter prediction pipeline.
Same structure as before — HTTP call goes to OpenRouter.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.config.config import get_settings

from app.utils.primitives import _safe_float
from app.ai.ai_router import _call_llm

logger = logging.getLogger(__name__)

# kept for any external imports
_SPECIALIST_MODEL = "openrouter"
_REASONING_MODEL = "openrouter"
_FINAL_MODEL = "openrouter"

_TIMEOUT_SPECIALIST = 30
_TIMEOUT_FINAL = 45


# ── Prompts ────────────────────────────────────────────────────────────────────

_FORM_PROMPT = """You are a football form analyst. Given the last 5 match results for home and away teams, assess which team has better current form.

HOME_FORM: {home_form}
AWAY_FORM: {away_form}

Return ONLY valid JSON:
{{"advantage":"home|away|neutral","confidence":0-100,"reasoning":"one sentence","key_factor":"one phrase"}}"""

_H2H_PROMPT = """You are a football H2H analyst. Given the head-to-head record between two teams, assess which team has the historical edge.

HOME_WINS: {home_wins}
AWAY_WINS: {away_wins}
DRAWS: {draws}

Return ONLY valid JSON:
{{"advantage":"home|away|neutral","confidence":0-100,"reasoning":"one sentence","key_factor":"one phrase"}}"""

_ODDS_PROMPT = """You are a football odds analyst. Given the 1x2 decimal odds for a match, assess the market signal.

HOME_ODDS: {home_odds}
DRAW_ODDS: {draw_odds}
AWAY_ODDS: {away_odds}

Return ONLY valid JSON:
{{"advantage":"home|away|neutral","confidence":0-100,"reasoning":"one sentence","key_factor":"one phrase","market_signal":"sharp_HOME|sharp_AWAY|stable|unavailable"}}"""

_STANDINGS_PROMPT = """You are a football standings analyst. Given the league positions and points of two teams, assess which team is stronger in the league.

HOME: position={home_pos} points={home_pts}
AWAY: position={away_pos} points={away_pts}

Return ONLY valid JSON:
{{"advantage":"home|away|neutral","confidence":0-100,"reasoning":"one sentence","key_factor":"one phrase"}}"""

_MODEL_PROMPT = """You are a football model ensemble analyst. Given the probability outputs from statistical models, assess the consensus.

POISSON: home={p_home:.1f}% draw={p_draw:.1f}% away={p_away:.1f}%
DIXON_COLES: home={d_home:.1f}% draw={d_draw:.1f}% away={d_away:.1f}%
ELO: home={e_home:.1f}% draw={e_draw:.1f}% away={e_away:.1f}%

Return ONLY valid JSON:
{{"advantage":"home|away|neutral|draw","confidence":0-100,"reasoning":"one sentence","key_factor":"one phrase","consensus":"strong|moderate|weak"}}"""

_FINAL_SYNTHESIS_PROMPT = """You are a football prediction expert. Synthesize the specialist analyses and model outputs into a final prediction.

MATCH: {match}
COMPETITION: {competition}

SPECIALIST ANALYSES:
{specialist_summary}

MODEL OUTPUTS:
{model_summary}

MEMORY CONTEXT:
{memory_context}

Rules:
- Only predict if confidence >= 60%, otherwise set status to "low_confidence"
- Trust strong consensus from specialists
- If specialists disagree, lower confidence

Return ONLY valid JSON:
{{
  "status": "predicted | low_confidence | skipped",
  "prediction": "Home Win | Away Win | Draw",
  "confidence": 0-100,
  "value_bet": true/false,
  "btts": "Yes | No | Unknown",
  "over_2_5": "Yes | No | Unknown",
  "market_signal": "sharp HOME | sharp AWAY | stable | unavailable",
  "key_factors": ["<factor 1>", "<factor 2>", "<factor 3>"],
  "reasoning": {{
    "form": "<one sentence>",
    "h2h": "<one sentence>",
    "standings": "<one sentence>",
    "odds_signal": "<one sentence>",
    "models": "<one sentence>",
    "verdict": "<one sentence final summary>"
  }}
}}"""


# ── Core LLM call (imported from ai_router) ───────────────────────────────────

def is_llm_available(model: str | None = None) -> bool:
    """Check if OpenRouter is configured (API key is set)."""
    from app.ai.ai_router import is_llm_available as _is_available
    return _is_available(model)


def _parse_safe(raw: str) -> dict[str, Any] | None:
    try:
        text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception:
        return None


def _build_memory_context(doc: dict[str, Any]) -> dict[str, Any]:
    """Build a memory block from the self-learner and CLV data.

    Returns a dict (not a string) so callers can pick the fields they need.
    Use _format_memory_context() to convert to a prompt string.
    """
    context: dict[str, Any] = {}
    try:
        from app.monitoring.self_learner import (
            get_signal_weights,
            get_league_accuracy,
            get_top_signals,
            get_learned_weights,
            get_learning_summary,
        )
        league = doc.get("tournament") or doc.get("category") or ""
        if isinstance(league, dict):
            league = league.get("name") or ""

        # Signal weights for this league
        signal_weights = get_signal_weights(league)
        if signal_weights:
            context["signal_weights"] = signal_weights

        # League accuracy profile
        league_acc = get_league_accuracy(league)
        if league_acc.get("known"):
            context["league_accuracy"] = league_acc

        # Top performing signals globally
        top_signals = get_top_signals(limit=5)
        if top_signals:
            context["top_signals_globally"] = [
                {"signal": s["signal"], "win_rate": s["win_rate"], "verdict": s["verdict"]}
                for s in top_signals[:5]
            ]

        summary = get_learning_summary()
        cold = summary.get("bottom_signals") or []
        if cold:
            context["cold_signals_globally"] = [
                {"signal": s.get("signal"), "win_rate": s.get("win_rate"), "samples": s.get("samples")}
                for s in cold[:5]
            ]

        # Current model weights (auto-tuned)
        context["model_weights"] = get_learned_weights()

    except Exception:
        pass

    try:
        from app.risk.clv import get_clv_summary
        clv = get_clv_summary(days=14)
        avg_clv = clv.get("avg_clv_percent")
        if avg_clv is not None:
            context["clv_14d"] = {
                "avg_clv_percent": avg_clv,
                "edge_quality": clv.get("edge_quality"),
                "positive_clv_rate": clv.get("positive_clv_rate"),
            }
    except Exception:
        pass

    try:
        from app.enrichment.confidence_calibrator import get_calibration_table
        cal = get_calibration_table()
        if cal:
            context["calibration"] = [
                {"band": c.get("band"), "win_rate": c.get("win_rate"), "samples": c.get("samples")}
                for c in (cal if isinstance(cal, list) else [])
                if (c.get("samples") or 0) >= 10
            ][:6]
    except Exception:
        pass

    return context


def _format_memory_context(context: dict[str, Any]) -> str:
    """Convert a memory context dict to a human-readable prompt string."""
    if not context:
        return "No memory context available"
    parts = []
    weights = context.get("signal_weights") or {}
    if weights:
        top = sorted(weights.items(), key=lambda x: x[1], reverse=True)[:3]
        parts.append(f"Top signals: {', '.join(f'{k}={v:.0%}' for k, v in top)}")
    acc = context.get("league_accuracy") or {}
    if acc.get("known"):
        parts.append(f"League accuracy: {acc.get('win_rate', 0):.0%} ({acc.get('samples', 0)} samples)")
    clv = context.get("clv_14d") or {}
    if clv.get("avg_clv_percent") is not None:
        parts.append(f"CLV 14d: {clv['avg_clv_percent']:+.1f}%")
    return "\n".join(parts) or "No memory context available"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _aggregate_specialists(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise specialist results into a prompt-ready string."""
    lines: list[str] = []
    votes: dict[str, int] = {"home": 0, "away": 0, "neutral": 0, "draw": 0}
    for r in results:
        name = r.get("specialist") or r.get("name") or "unknown"
        adv = str(r.get("advantage") or r.get("status") or "neutral").lower()
        conf = r.get("confidence") or 0
        reason = r.get("reasoning") or r.get("error") or ""
        lines.append(f"- {name}: {adv} ({conf}%) — {reason}")
        if adv in votes:
            votes[adv] += 1
    return {
        "specialist_summary": "\n".join(lines) or "No specialist data",
        "votes": votes,
    }


def _build_model_summary(doc: dict[str, Any]) -> str:
    """Build a one-line summary of statistical model outputs from the doc."""
    parts: list[str] = []
    poisson = doc.get("poisson") or {}
    if poisson.get("probabilities"):
        p = poisson["probabilities"]
        parts.append(f"Poisson: H={p.get('home_win',0):.0f}% D={p.get('draw',0):.0f}% A={p.get('away_win',0):.0f}%")
    dixon = doc.get("dixon_coles") or {}
    if dixon.get("probabilities"):
        d = dixon["probabilities"]
        parts.append(f"Dixon: H={d.get('home_win',0):.0f}% D={d.get('draw',0):.0f}% A={d.get('away_win',0):.0f}%")
    elo = doc.get("elo") or {}
    if elo.get("home_win_probability"):
        parts.append(f"ELO: H={elo.get('home_win_probability',0):.0f}% A={elo.get('away_win_probability',0):.0f}%")
    return "; ".join(parts) or "No model outputs"


def run_form_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    home_form = [m.get("result", "?") for m in (doc.get("home_last_matches") or [])[:5]]
    away_form = [m.get("result", "?") for m in (doc.get("away_last_matches") or [])[:5]]
    prompt = _FORM_PROMPT.format(
        home_form=", ".join(home_form) or "N/A",
        away_form=", ".join(away_form) or "N/A",
    )
    try:
        raw = _call_llm(_SPECIALIST_MODEL, prompt, timeout=_TIMEOUT_SPECIALIST)
        result = _parse_safe(raw) or {}
        result["specialist"] = "form"
        return result
    except Exception as exc:
        return {"specialist": "form", "status": "error", "error": str(exc)}


def run_h2h_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    h2h = doc.get("h2h") or {}
    duel = h2h.get("teamDuel") or h2h.get("team_duel") or {}
    prompt = _H2H_PROMPT.format(
        home_wins=duel.get("homeWins", 0),
        away_wins=duel.get("awayWins", 0),
        draws=duel.get("draws", 0),
    )
    try:
        raw = _call_llm(_SPECIALIST_MODEL, prompt, timeout=_TIMEOUT_SPECIALIST)
        result = _parse_safe(raw) or {}
        result["specialist"] = "h2h"
        return result
    except Exception as exc:
        return {"specialist": "h2h", "status": "error", "error": str(exc)}


def run_odds_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    odds = doc.get("odds_1x2") or {}
    prompt = _ODDS_PROMPT.format(
        home_odds=odds.get("home", "N/A"),
        draw_odds=odds.get("draw", "N/A"),
        away_odds=odds.get("away", "N/A"),
    )
    try:
        raw = _call_llm(_SPECIALIST_MODEL, prompt, timeout=_TIMEOUT_SPECIALIST)
        result = _parse_safe(raw) or {}
        result["specialist"] = "odds"
        return result
    except Exception as exc:
        return {"specialist": "odds", "status": "error", "error": str(exc)}


def run_standings_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    standings = doc.get("standings") or []
    home_row = next((r for r in standings if "home" in str(r.get("team") or "").lower()), {})
    away_row = next((r for r in standings if "away" in str(r.get("team") or "").lower()), {})
    prompt = _STANDINGS_PROMPT.format(
        home_pos=home_row.get("position", "N/A"),
        home_pts=home_row.get("points", "N/A"),
        away_pos=away_row.get("position", "N/A"),
        away_pts=away_row.get("points", "N/A"),
    )
    try:
        raw = _call_llm(_SPECIALIST_MODEL, prompt, timeout=_TIMEOUT_SPECIALIST)
        result = _parse_safe(raw) or {}
        result["specialist"] = "standings"
        return result
    except Exception as exc:
        return {"specialist": "standings", "status": "error", "error": str(exc)}


def run_model_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    poisson = (doc.get("poisson") or {}).get("probabilities") or {}
    dixon = (doc.get("dixon_coles") or {}).get("probabilities") or {}
    elo = doc.get("elo") or {}
    prompt = _MODEL_PROMPT.format(
        p_home=float(poisson.get("home_win") or 33),
        p_draw=float(poisson.get("draw") or 33),
        p_away=float(poisson.get("away_win") or 33),
        d_home=float(dixon.get("home_win") or 33),
        d_draw=float(dixon.get("draw") or 33),
        d_away=float(dixon.get("away_win") or 33),
        e_home=float(elo.get("home_win_probability") or 33),
        e_draw=float(max(5, 30 - abs(float(elo.get("home_win_probability") or 33) - 50) * 0.4)),
        e_away=float(elo.get("away_win_probability") or 33),
    )
    try:
        raw = _call_llm(_SPECIALIST_MODEL, prompt, timeout=_TIMEOUT_SPECIALIST)
        result = _parse_safe(raw) or {}
        result["specialist"] = "models"
        return result
    except Exception as exc:
        return {"specialist": "models", "status": "error", "error": str(exc)}


def run_final_synthesis(
    doc: dict[str, Any],
    specialist_results: list[dict[str, Any]],
    model_summary: str,
    memory_context: str,
) -> dict[str, Any]:
    aggregation = _aggregate_specialists(specialist_results)
    prompt = _FINAL_SYNTHESIS_PROMPT.format(
        match=doc.get("sportybet_name") or doc.get("name") or "Unknown vs Unknown",
        competition=doc.get("tournament") or doc.get("category") or "Unknown",
        specialist_summary=aggregation["specialist_summary"],
        model_summary=model_summary or "No model outputs",
        memory_context=memory_context or "No memory context",
    )
    try:
        raw = _call_llm(_FINAL_MODEL, prompt, timeout=_TIMEOUT_FINAL)
        result = _parse_safe(raw)
        if not result:
            return {"status": "error", "message": "Failed to parse synthesis JSON", "raw": raw[:200]}
        raw_conf = _safe_float(result.get("confidence"), 0)
        confidence = round(raw_conf) if raw_conf > 1 else round(raw_conf * 100)
        return {
            "status": result.get("status") or "predicted",
            "recommendation": result.get("prediction"),
            "confidence": confidence,
            "value_bet": bool(result.get("value_bet")),
            "key_factors": result.get("key_factors") or [],
            "reasoning": result.get("reasoning") or {},
            "market_signal": result.get("market_signal"),
            "btts": result.get("btts"),
            "over_2_5": result.get("over_2_5"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "openrouter_pipeline",
            "model": get_settings().openrouter_model,
            "specialist_results": specialist_results,
            "aggregation": aggregation,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ── Main entry points ──────────────────────────────────────────────────────────

def run_llm_pipeline(doc: dict[str, Any], attach_brain: bool = True) -> dict[str, Any]:
    """Run the full multi-stage OpenRouter prediction pipeline.

    NOTE: attach_brain is accepted for backward-compatibility but no longer
    fires a brain-review LLM call here. The canonical brain review is
    oversee_prediction() in ai_brain.py, called by _attach_brain_review() in
    prediction_flow.py after the prebuilt_prediction is recorded. Removing it
    here eliminates the duplicate second brain-review call that this path
    previously fired before the result reached prediction_flow.py's own brain
    attachment step.
    """
    settings = get_settings()
    if not settings.openrouter_api_key:
        return {"status": "openrouter_unavailable", "message": "OPENROUTER_API_KEY is not set in .env"}

    logger.info("[pipeline] running specialists via OpenRouter (%s)", settings.openrouter_model)
    specialist_results: list[dict[str, Any]] = []
    for name, runner in [
        ("form", run_form_specialist),
        ("h2h", run_h2h_specialist),
        ("odds", run_odds_specialist),
        ("standings", run_standings_specialist),
        ("models", run_model_specialist),
    ]:
        try:
            result = runner(doc)
            specialist_results.append(result)
            logger.info("[pipeline] %s: %s", name, result.get("status"))
        except Exception as exc:
            logger.warning("[pipeline] %s failed: %s", name, exc)
            specialist_results.append({"status": "error", "specialist": name, "error": str(exc)})

    model_summary = _build_model_summary(doc)
    memory_context_dict = _build_memory_context(doc)
    memory_context = _format_memory_context(memory_context_dict)

    logger.info("[pipeline] running final synthesis")
    final = run_final_synthesis(doc, specialist_results, model_summary, memory_context)

    return {
        "status": final.get("status", "error"),
        "recommendation": final.get("recommendation"),
        "confidence": final.get("confidence"),
        "value_bet": final.get("value_bet"),
        "key_factors": final.get("key_factors") or [],
        "reasoning": final.get("reasoning") or {},
        "market_signal": final.get("market_signal"),
        "btts": final.get("btts"),
        "over_2_5": final.get("over_2_5"),
        "generated_at": final.get("generated_at"),
        "source": "openrouter_pipeline",
        "model": settings.openrouter_model,
        "specialist_results": specialist_results,
        "aggregation": final.get("aggregation") or {},
        "brain_review": None,  # brain review now handled by ai_brain.oversee_prediction only
    }


def run_llm_pipeline_batch(
    docs: list[dict[str, Any]],
    limit: int = 50,
    attach_brain: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.openrouter_api_key:
        return {"status": "openrouter_unavailable", "message": "OPENROUTER_API_KEY is not set in .env"}
    docs = docs[:limit]
    predictions: list[dict[str, Any]] = []
    errors = value_bets = 0
    for i, doc in enumerate(docs, 1):
        name = doc.get("sportybet_name") or doc.get("name") or "unknown"
        logger.info("[pipeline_batch] [%d/%d] %s", i, len(docs), name)
        try:
            pred = run_llm_pipeline(doc, attach_brain=attach_brain)
            pred["sportybet_id"] = doc.get("sportybet_id")
            pred["match_id"] = doc.get("sportybet_id")
            predictions.append(pred)
            if pred.get("status") == "error":
                errors += 1
            elif pred.get("status") == "predicted" and pred.get("value_bet"):
                value_bets += 1
        except Exception as exc:
            errors += 1
            predictions.append({"status": "error", "message": str(exc), "sportybet_id": doc.get("sportybet_id")})
    return {
        "status": "success",
        "total": len(predictions),
        "predicted": sum(1 for p in predictions if p.get("status") == "predicted"),
        "skipped": sum(1 for p in predictions if p.get("status") in ("skipped", "low_confidence")),
        "errors": errors,
        "value_bets": value_bets,
        "predictions": predictions,
    }
