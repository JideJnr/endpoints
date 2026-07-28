"""
Multi-stage OpenRouter prediction pipeline.
Same structure as before — HTTP call swapped from Ollama to OpenRouter.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError

from app.config import get_settings

logger = logging.getLogger(__name__)

# kept for any external imports
_SPECIALIST_MODEL = "openrouter"
_REASONING_MODEL = "openrouter"
_FINAL_MODEL = "openrouter"

_TIMEOUT_SPECIALIST = 30
_TIMEOUT_FINAL = 45
_TIMEOUT_BRAIN = 30


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

_BRAIN_REVIEW_PROMPT = """You are PredictX AI Brain, a cautious football prediction reviewer.

PREDICTION:
{prediction_summary}

MEMORY CONTEXT:
{memory_context}

Rules:
- If a signal has 65%+ historical win rate, trust it more
- If a signal has <40% win rate, flag it as a risk
- confidence_adjustment must be an integer from -8 to +8

Return ONLY valid JSON:
{{
  "status": "approved | caution | pass",
  "verdict": "<same as prediction or adjusted>",
  "confidence_adjustment": 0,
  "risks": ["<risk 1>"],
  "reasons": ["<reason 1>"]
}}"""


# ── Core LLM call ──────────────────────────────────────────────────────────────

def is_ollama_available(model: str | None = None) -> bool:
    """Kept for compatibility — pipeline now uses OpenRouter."""
    return bool(get_settings().openrouter_api_key)


def _call_llm(prompt: str, timeout: int = 30) -> str:
    """Call OpenRouter chat completions (OpenAI-compatible)."""
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set in .env")
    url = settings.openrouter_base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": settings.openrouter_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 512,
    }).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "HTTP-Referer": "https://predictx.app",
            "X-Title": "PredictX",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {body[:300]}") from exc
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


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


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _run_specialist(prompt: str, timeout: int) -> dict[str, Any]:
    try:
        raw = _call_llm(prompt, timeout=timeout)
        result = _parse_safe(raw)
        if result:
            return {"status": "success", "result": result}
        return {"status": "error", "error": "Failed to parse JSON", "raw": raw[:200]}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ── Data extractors ────────────────────────────────────────────────────────────

def _extract_form_data(doc: dict[str, Any]) -> dict[str, str]:
    detail = doc.get("sofascore_detail") or {}
    home_history = detail.get("home_last_matches") or doc.get("home_last_matches") or []
    away_history = detail.get("away_last_matches") or doc.get("away_last_matches") or []

    def _wld(history: list, team_id: Any) -> str:
        finished = [m for m in history if (m.get("status") or {}).get("type") == "finished"][:5]
        out = []
        for m in finished:
            s = m.get("score") or {}
            h_id = (m.get("home_team") or {}).get("id")
            is_home = str(h_id) == str(team_id) if h_id else True
            gf = s.get("home", 0) if is_home else s.get("away", 0)
            ga = s.get("away", 0) if is_home else s.get("home", 0)
            try:
                out.append("W" if int(gf) > int(ga) else "D" if int(gf) == int(ga) else "L")
            except Exception:
                out.append("?")
        return "".join(out) or "N/A"

    home = detail.get("home_team") or doc.get("home_team") or {}
    away = detail.get("away_team") or doc.get("away_team") or {}
    return {
        "home_form": _wld(home_history, home.get("id") if isinstance(home, dict) else None),
        "away_form": _wld(away_history, away.get("id") if isinstance(away, dict) else None),
    }


def _extract_h2h_data(doc: dict[str, Any]) -> dict[str, Any]:
    detail = doc.get("sofascore_detail") or {}
    h2h = detail.get("h2h") or {}
    td = h2h.get("team_duel") or h2h.get("teamDuel") or {}
    return {
        "home_wins": td.get("homeWins", 0),
        "away_wins": td.get("awayWins", 0),
        "draws": td.get("draws", 0),
        "available": bool(td),
    }


def _extract_odds_data(doc: dict[str, Any]) -> dict[str, Any]:
    markets = doc.get("sportybet_markets") or doc.get("markets") or []
    home_odds = draw_odds = away_odds = None
    for mkt in markets:
        n = (mkt.get("name") or "").lower()
        if mkt.get("id") == "1" or "1x2" in n or "match result" in n:
            for s in (mkt.get("selections") or [])[:3]:
                name = (s.get("name") or "").lower()
                odds = _safe_float(s.get("odds"))
                if "home" in name or name == "1":
                    home_odds = odds
                elif "draw" in name or name == "x":
                    draw_odds = odds
                elif "away" in name or name == "2":
                    away_odds = odds
            break
    return {
        "home_odds": home_odds, "draw_odds": draw_odds, "away_odds": away_odds,
        "available": home_odds is not None and away_odds is not None,
    }


def _extract_standings_data(doc: dict[str, Any]) -> dict[str, Any]:
    detail = doc.get("sofascore_detail") or {}
    standings = detail.get("standings") or doc.get("standings") or []
    home = detail.get("home_team") or doc.get("home_team") or {}
    away = detail.get("away_team") or doc.get("away_team") or {}
    home_name = (home.get("name") or "") if isinstance(home, dict) else str(home or "")
    away_name = (away.get("name") or "") if isinstance(away, dict) else str(away or "")
    home_pos = away_pos = home_pts = away_pts = "?"
    for row in standings:
        tn = (row.get("team") or {}).get("name") or ""
        if home_name and home_name.lower() in tn.lower():
            home_pos, home_pts = row.get("position", "?"), row.get("points", "?")
        if away_name and away_name.lower() in tn.lower():
            away_pos, away_pts = row.get("position", "?"), row.get("points", "?")
    return {
        "home_pos": home_pos, "home_pts": home_pts,
        "away_pos": away_pos, "away_pts": away_pts,
        "available": home_pos != "?" and away_pos != "?",
    }


def _extract_model_data(doc: dict[str, Any]) -> dict[str, Any]:
    models = (doc.get("prediction") or {}).get("models") or {}

    def _probs(m: dict[str, Any]) -> dict[str, float]:
        p = m.get("probabilities") or {}
        return {
            "home": _safe_float(p.get("home_win"), 33.3),
            "draw": _safe_float(p.get("draw"), 33.3),
            "away": _safe_float(p.get("away_win"), 33.3),
        }

    poisson = models.get("poisson") or {}
    dixon = models.get("dixon_coles") or {}
    elo = models.get("elo") or {}
    return {
        "poisson": _probs(poisson), "dixon_coles": _probs(dixon), "elo": _probs(elo),
        "available": bool(poisson or dixon or elo),
    }


# ── Specialists ────────────────────────────────────────────────────────────────

def run_form_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    data = _extract_form_data(doc)
    if data["home_form"] == "N/A" and data["away_form"] == "N/A":
        return {"status": "skipped", "reason": "No form data"}
    r = _run_specialist(_FORM_PROMPT.format(**data), _TIMEOUT_SPECIALIST)
    return {**r, "specialist": "form"} if r.get("status") == "success" else r


def run_h2h_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    data = _extract_h2h_data(doc)
    if not data["available"]:
        return {"status": "skipped", "reason": "No H2H data"}
    r = _run_specialist(_H2H_PROMPT.format(**data), _TIMEOUT_SPECIALIST)
    return {**r, "specialist": "h2h"} if r.get("status") == "success" else r


def run_odds_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    data = _extract_odds_data(doc)
    if not data["available"]:
        return {"status": "skipped", "reason": "No odds data"}
    r = _run_specialist(_ODDS_PROMPT.format(**data), _TIMEOUT_SPECIALIST)
    return {**r, "specialist": "odds"} if r.get("status") == "success" else r


def run_standings_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    data = _extract_standings_data(doc)
    if not data["available"]:
        return {"status": "skipped", "reason": "No standings data"}
    r = _run_specialist(_STANDINGS_PROMPT.format(**data), _TIMEOUT_SPECIALIST)
    return {**r, "specialist": "standings"} if r.get("status") == "success" else r


def run_model_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    data = _extract_model_data(doc)
    if not data["available"]:
        return {"status": "skipped", "reason": "No model outputs"}
    p, d, e = data["poisson"], data["dixon_coles"], data["elo"]
    prompt = _MODEL_PROMPT.format(
        p_home=p["home"], p_draw=p["draw"], p_away=p["away"],
        d_home=d["home"], d_draw=d["draw"], d_away=d["away"],
        e_home=e["home"], e_draw=e["draw"], e_away=e["away"],
    )
    r = _run_specialist(prompt, _TIMEOUT_SPECIALIST)
    return {**r, "specialist": "models"} if r.get("status") == "success" else r


# ── Aggregation + memory ───────────────────────────────────────────────────────

def _aggregate_specialists(results: list[dict[str, Any]]) -> dict[str, Any]:
    parts: list[str] = []
    counts: dict[str, int] = {"home": 0, "away": 0, "draw": 0, "neutral": 0}
    total_conf = n = 0
    for res in results:
        if res.get("status") != "success":
            continue
        r = res.get("result") or {}
        adv = r.get("advantage", "neutral")
        conf = _safe_int(r.get("confidence"), 50)
        counts[adv] = counts.get(adv, 0) + 1
        total_conf += conf
        n += 1
        parts.append(f"[{res.get('specialist','?').upper()}] {adv.upper()} (conf={conf}%): {r.get('reasoning','')} | {r.get('key_factor','')}")
    return {
        "specialist_summary": "\n".join(parts) or "No specialist data available",
        "consensus_advantage": max(counts, key=counts.get),
        "avg_confidence": round(total_conf / n) if n else 50,
        "specialist_count": n,
        "advantage_breakdown": counts,
    }


def _build_model_summary(doc: dict[str, Any]) -> str:
    models = (doc.get("prediction") or {}).get("models") or {}
    parts = []
    for name, m in models.items():
        if not m or m.get("error"):
            continue
        p = m.get("probabilities") or {}
        if p:
            parts.append(
                f"{name}: home={_safe_float(p.get('home_win'), 33):.1f}% "
                f"draw={_safe_float(p.get('draw'), 33):.1f}% "
                f"away={_safe_float(p.get('away_win'), 33):.1f}%"
            )
    return "\n".join(parts) or "No model outputs available"


def _build_memory_context(doc: dict[str, Any]) -> str:
    try:
        from app.self_learner import get_signal_weights, get_league_accuracy
        from app.clv import get_clv_summary
        league = doc.get("tournament") or doc.get("category") or ""
        if isinstance(league, dict):
            league = league.get("name") or ""
        parts = []
        weights = get_signal_weights(league)
        if weights:
            top = sorted(weights.items(), key=lambda x: x[1], reverse=True)[:3]
            parts.append(f"Top signals: {', '.join(f'{k}={v:.0%}' for k, v in top)}")
        acc = get_league_accuracy(league)
        if acc.get("known"):
            parts.append(f"League accuracy: {acc.get('win_rate', 0):.0%} ({acc.get('samples', 0)} samples)")
        clv = get_clv_summary(days=14)
        if clv.get("avg_clv_percent") is not None:
            parts.append(f"CLV 14d: {clv['avg_clv_percent']:+.1f}%")
        return "\n".join(parts) or "No memory context available"
    except Exception:
        return "Memory context unavailable"


# ── Final synthesis + brain review ────────────────────────────────────────────

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
        raw = _call_llm(prompt, timeout=_TIMEOUT_FINAL)
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


def run_brain_review(prediction: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    picks = prediction.get("picks") or [{}]
    pred_summary = {
        "match": prediction.get("name"),
        "prediction": picks[0],
        "top_signals": [s.get("name") for s in (prediction.get("signals") or [])[:5]],
        "confidence": picks[0].get("confidence"),
    }
    memory_context = _build_memory_context(doc)
    prompt = _BRAIN_REVIEW_PROMPT.format(
        prediction_summary=json.dumps(pred_summary, default=str),
        memory_context=memory_context,
    )
    try:
        raw = _call_llm(prompt, timeout=_TIMEOUT_BRAIN)
        result = _parse_safe(raw)
        if not result:
            return {"status": "error", "provider": "openrouter", "message": "Failed to parse brain JSON"}
        return {
            "status": result.get("status") or "approved",
            "provider": "openrouter",
            "model": get_settings().openrouter_model,
            "verdict": result.get("verdict"),
            "confidence_adjustment": _safe_int(result.get("confidence_adjustment"), 0),
            "risks": result.get("risks") or [],
            "reasons": result.get("reasons") or [],
        }
    except Exception as exc:
        return {"status": "error", "provider": "openrouter", "message": str(exc)}


# ── Main entry points ──────────────────────────────────────────────────────────

def run_ollama_pipeline(doc: dict[str, Any], attach_brain: bool = True) -> dict[str, Any]:
    """Run the full multi-stage OpenRouter prediction pipeline."""
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
    memory_context = _build_memory_context(doc)

    logger.info("[pipeline] running final synthesis")
    final = run_final_synthesis(doc, specialist_results, model_summary, memory_context)

    brain = None
    if attach_brain and final.get("status") == "predicted":
        try:
            brain = run_brain_review(final, doc)
            logger.info("[pipeline] brain: %s adj=%+d", brain.get("status"), brain.get("confidence_adjustment", 0))
        except Exception as exc:
            logger.warning("[pipeline] brain failed: %s", exc)

    confidence = final.get("confidence")
    if brain and brain.get("confidence_adjustment"):
        confidence = max(1, min(99, (confidence or 50) + brain["confidence_adjustment"]))

    return {
        "status": final.get("status", "error"),
        "recommendation": final.get("recommendation"),
        "confidence": confidence,
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
        "brain_review": brain,
    }


def run_ollama_pipeline_batch(
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
            pred = run_ollama_pipeline(doc, attach_brain=attach_brain)
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
