"""
Multi-stage small-context Ollama prediction pipeline.

Design
------
Instead of sending one large prompt with the entire match document, we:
  1. Run specialist agents — each gets a tiny, focused prompt about ONE aspect
  2. Aggregate all specialist results into a compact summary
  3. Send the aggregated summary + deterministic model outputs to a final synthesis call
  4. Optionally run a brain review on the final prediction

Each specialist call uses < 200 tokens of context and returns a compact JSON.
The final synthesis call uses < 500 tokens of aggregated context.

This approach:
  - Keeps every LLM call fast (sub-second on CPU for 8B models)
  - Reduces hallucination risk (each model focuses on one thing)
  - Makes the pipeline resilient (if one specialist fails, others still work)
  - Enables parallel execution of independent specialists
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib import request as urllib_request

from app.config import get_settings

logger = logging.getLogger(__name__)

# ── Model assignment ────────────────────────────────────────────────────────────
# qwen3:8b: fast, good at JSON, general analysis
# deepseek-r1:8b: reasoning, step-by-step analysis

_SPECIALIST_MODEL = "qwen3:8b"
# Use qwen3:8b for all specialists on CPU — deepseek-r1:8b is too slow for real-time
_REASONING_MODEL = "qwen3:8b"
_FINAL_MODEL = "qwen3:8b"

# ── Timeout settings (seconds) ──────────────────────────────────────────────────
# Local 8B models on CPU need generous timeouts.
_TIMEOUT_SPECIALIST = 120
_TIMEOUT_REASONING = 180
_TIMEOUT_FINAL = 240
_TIMEOUT_BRAIN = 120


# ── Specialist prompts ─────────────────────────────────────────────────────────

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

_MODEL_PROMPT = """You are a football model ensemble analyst. Given the probability outputs from statistical models (Poisson, Dixon-Coles, Elo), assess the consensus.

POISSON: home={p_home:.1f}% draw={p_draw:.1f}% away={p_away:.1f}%
DIXON_COLES: home={d_home:.1f}% draw={d_draw:.1f}% away={d_away:.1f}%
ELO: home={e_home:.1f}% draw={e_draw:.1f}% away={e_away:.1f}%

Return ONLY valid JSON:
{{"advantage":"home|away|neutral|draw","confidence":0-100,"reasoning":"one sentence","key_factor":"one phrase","consensus":"strong|moderate|weak"}}"""


# ── Final synthesis prompt ─────────────────────────────────────────────────────

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
- Consider all specialist inputs but trust strong consensus
- If specialists disagree, lower confidence

Return ONLY valid JSON:
{{
  "match": "<home> vs <away>",
  "status": "predicted | low_confidence | skipped",
  "prediction": "Home Win | Away Win | Draw",
  "odds": "<decimal odds string>",
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


# ── Brain review prompt ────────────────────────────────────────────────────────

_BRAIN_REVIEW_PROMPT = """You are PredictX AI Brain, a cautious football prediction reviewer with memory.

You receive the current match prediction AND a memory_context block showing what has historically worked.

PREDICTION:
{prediction_summary}

MEMORY CONTEXT:
{memory_context}

Rules:
- Use memory_context to calibrate confidence: if a signal has 65%+ win rate historically, trust it more
- If a signal has <40% win rate, flag it as a risk
- If CLV is positive, the system is beating the market — be less conservative
- Do not invent team news, injuries, or odds
- confidence_adjustment must be an integer from -8 to +8

Return ONLY valid JSON:
{{
  "status": "approved | caution | pass",
  "verdict": "<same as prediction or adjusted>",
  "confidence_adjustment": -8 to 8,
  "risks": ["<risk 1>", "<risk 2>"],
  "reasons": ["<reason 1>", "<reason 2>"]
}}"""


# ── Helper functions ───────────────────────────────────────────────────────────

def _ollama_url() -> str:
    settings = get_settings()
    return settings.ollama_url.replace("/api/chat", "").rstrip("/")


def is_ollama_available(model: str | None = None) -> bool:
    """Check if Ollama is reachable and the model is available."""
    try:
        url = _ollama_url() + "/api/tags"
        req = urllib_request.Request(url, method="GET")
        with urllib_request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if model:
            names = [m.get("name", "") for m in (data.get("models") or [])]
            return any(model in name for name in names)
        return True
    except Exception:
        return False


async def _call_ollama_async(model: str, prompt: str, timeout: int = 60) -> str:
    """
    Async-safe Ollama call.
    Runs the blocking urllib request in a thread pool to avoid blocking the event loop.
    Works in both async and sync contexts.
    """
    def _sync_call() -> str:
        url = _ollama_url() + "/api/generate"
        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": 256},
        }).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        return raw.get("response", "")

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_call)


def _call_ollama(model: str, prompt: str, timeout: int = 60) -> str:
    """Sync wrapper for backward compatibility."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're in an async context (e.g., FastAPI request handler)
            # Can't use asyncio.run() or run_until_complete()
            # Schedule the coroutine and wait for it with a future
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, _call_ollama_async(model, prompt, timeout))
                return future.result(timeout=timeout + 10)
        return loop.run_until_complete(_call_ollama_async(model, prompt, timeout))
    except RuntimeError:
        # Fallback: run in a new event loop (for sync contexts)
        return asyncio.run(_call_ollama_async(model, prompt, timeout))


def _parse_response(raw: str) -> dict[str, Any]:
    """Extract JSON from model response, stripping <think> blocks and markdown fences."""
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _parse_response_safe(raw: str) -> dict[str, Any] | None:
    """Like _parse_response but returns None instead of raising."""
    try:
        return _parse_response(raw)
    except Exception:
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── Specialist extractors ──────────────────────────────────────────────────────

def _extract_form_data(doc: dict[str, Any]) -> dict[str, str]:
    """Extract form strings from doc for the form specialist."""
    detail = doc.get("sofascore_detail") or {}
    home_history = detail.get("home_last_matches") or doc.get("home_last_matches") or []
    away_history = detail.get("away_last_matches") or doc.get("away_last_matches") or []

    def _wld(history: list, team_id: Any) -> str:
        finished = [m for m in (history or []) if (m.get("status") or {}).get("type") == "finished"][:5]
        out = []
        for m in finished:
            s = m.get("score") or {}
            h_id = (m.get("home_team") or {}).get("id")
            is_home = str(h_id) == str(team_id) if h_id else True
            gf = s.get("home", 0) if is_home else s.get("away", 0)
            ga = s.get("away", 0) if is_home else s.get("home", 0)
            try:
                gf, ga = int(gf), int(ga)
                out.append("W" if gf > ga else "D" if gf == ga else "L")
            except Exception:
                out.append("?")
        return "".join(out) or "N/A"

    home = detail.get("home_team") or doc.get("home_team") or {}
    away = detail.get("away_team") or doc.get("away_team") or {}
    home_id = home.get("id") if isinstance(home, dict) else None
    away_id = away.get("id") if isinstance(away, dict) else None

    return {
        "home_form": _wld(home_history, home_id),
        "away_form": _wld(away_history, away_id),
    }


def _extract_h2h_data(doc: dict[str, Any]) -> dict[str, Any]:
    """Extract H2H data from doc for the H2H specialist."""
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
    """Extract odds data from doc for the odds specialist."""
    markets = doc.get("sportybet_markets") or doc.get("markets") or []
    detail = doc.get("sofascore_detail") or {}

    home_odds = draw_odds = away_odds = None

    # Try SportyBet markets first
    for mkt in markets:
        n = (mkt.get("name") or "").lower()
        if mkt.get("id") == "1" or "1x2" in n or "match result" in n:
            sels = mkt.get("selections") or []
            for s in sels[:3]:
                name = (s.get("name") or "").lower()
                odds = _safe_float(s.get("odds"))
                if "home" in name or "1" in name:
                    home_odds = odds
                elif "draw" in name or "x" in name:
                    draw_odds = odds
                elif "away" in name or "2" in name:
                    away_odds = odds
            break

    # Fallback to SofaScore odds
    if home_odds is None:
        choices = ((detail.get("odds_featured") or {}).get("default") or {}).get("choices") or []
        for c in choices[:3]:
            name = (c.get("name") or "").lower()
            odds = _safe_float(c.get("fractional_value"))
            if "home" in name:
                home_odds = odds
            elif "draw" in name:
                draw_odds = odds
            elif "away" in name:
                away_odds = odds

    return {
        "home_odds": home_odds,
        "draw_odds": draw_odds,
        "away_odds": away_odds,
        "available": home_odds is not None and away_odds is not None,
    }


def _extract_standings_data(doc: dict[str, Any]) -> dict[str, Any]:
    """Extract standings data from doc for the standings specialist."""
    detail = doc.get("sofascore_detail") or {}
    standings = detail.get("standings") or doc.get("standings") or []

    home = detail.get("home_team") or doc.get("home_team") or {}
    away = detail.get("away_team") or doc.get("away_team") or {}
    home_name = (home.get("name") or "") if isinstance(home, dict) else str(home or "")
    away_name = (away.get("name") or "") if isinstance(away, dict) else str(away or "")

    home_pos = away_pos = "?"
    home_pts = away_pts = "?"
    for row in standings:
        tn = (row.get("team") or {}).get("name") or ""
        if home_name and home_name.lower() in tn.lower():
            home_pos = row.get("position", "?")
            home_pts = row.get("points", "?")
        if away_name and away_name.lower() in tn.lower():
            away_pos = row.get("position", "?")
            away_pts = row.get("points", "?")

    return {
        "home_pos": home_pos,
        "home_pts": home_pts,
        "away_pos": away_pos,
        "away_pts": away_pts,
        "available": home_pos != "?" and away_pos != "?",
    }


def _extract_model_data(doc: dict[str, Any]) -> dict[str, Any]:
    """Extract model outputs from doc for the model specialist."""
    prediction = doc.get("prediction") or {}
    models = prediction.get("models") or {}

    poisson = models.get("poisson") or {}
    dixon = models.get("dixon_coles") or {}
    elo = models.get("elo") or {}

    def _probs(model: dict[str, Any]) -> dict[str, float]:
        probs = model.get("probabilities") or model.get("probabilities") or {}
        return {
            "home": _safe_float(probs.get("home_win"), 33.3),
            "draw": _safe_float(probs.get("draw"), 33.3),
            "away": _safe_float(probs.get("away_win"), 33.3),
        }

    p = _probs(poisson)
    d = _probs(dixon)
    e = _probs(elo)

    return {
        "poisson": p,
        "dixon_coles": d,
        "elo": e,
        "available": bool(poisson or dixon or elo),
    }


# ── Specialist runners ─────────────────────────────────────────────────────────

def _run_specialist(model: str, prompt: str, timeout: int) -> dict[str, Any]:
    """Run a single specialist agent and return parsed result."""
    try:
        raw = _call_ollama(model, prompt, timeout=timeout)
        result = _parse_response_safe(raw)
        if result:
            return {"status": "success", "result": result}
        return {"status": "error", "error": "Failed to parse JSON response", "raw": raw[:200]}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def run_form_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    """Analyze team form using a small-context Ollama call."""
    data = _extract_form_data(doc)
    if not data.get("home_form") and not data.get("away_form"):
        return {"status": "skipped", "reason": "No form data available"}

    prompt = _FORM_PROMPT.format(**data)
    result = _run_specialist(_SPECIALIST_MODEL, prompt, _TIMEOUT_SPECIALIST)
    if result.get("status") == "success":
        return {**result, "specialist": "form", "model": _SPECIALIST_MODEL}
    return result


def run_h2h_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    """Analyze H2H record using a small-context Ollama call."""
    data = _extract_h2h_data(doc)
    if not data.get("available"):
        return {"status": "skipped", "reason": "No H2H data available"}

    prompt = _H2H_PROMPT.format(**data)
    result = _run_specialist(_REASONING_MODEL, prompt, _TIMEOUT_REASONING)
    if result.get("status") == "success":
        return {**result, "specialist": "h2h", "model": _REASONING_MODEL}
    return result


def run_odds_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    """Analyze odds/market signal using a small-context Ollama call."""
    data = _extract_odds_data(doc)
    if not data.get("available"):
        return {"status": "skipped", "reason": "No odds data available"}

    prompt = _ODDS_PROMPT.format(**data)
    result = _run_specialist(_SPECIALIST_MODEL, prompt, _TIMEOUT_SPECIALIST)
    if result.get("status") == "success":
        return {**result, "specialist": "odds", "model": _SPECIALIST_MODEL}
    return result


def run_standings_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    """Analyze league standings using a small-context Ollama call."""
    data = _extract_standings_data(doc)
    if not data.get("available"):
        return {"status": "skipped", "reason": "No standings data available"}

    prompt = _STANDINGS_PROMPT.format(**data)
    result = _run_specialist(_SPECIALIST_MODEL, prompt, _TIMEOUT_SPECIALIST)
    if result.get("status") == "success":
        return {**result, "specialist": "standings", "model": _SPECIALIST_MODEL}
    return result


def run_model_specialist(doc: dict[str, Any]) -> dict[str, Any]:
    """Analyze model ensemble outputs using a small-context Ollama call."""
    data = _extract_model_data(doc)
    if not data.get("available"):
        return {"status": "skipped", "reason": "No model outputs available"}

    p = data["poisson"]
    d = data["dixon_coles"]
    e = data["elo"]
    prompt = _MODEL_PROMPT.format(
        p_home=p["home"], p_draw=p["draw"], p_away=p["away"],
        d_home=d["home"], d_draw=d["draw"], d_away=d["away"],
        e_home=e["home"], e_draw=e["draw"], e_away=e["away"],
    )
    result = _run_specialist(_SPECIALIST_MODEL, prompt, _TIMEOUT_SPECIALIST)
    if result.get("status") == "success":
        return {**result, "specialist": "models", "model": _SPECIALIST_MODEL}
    return result


# ── Aggregation ────────────────────────────────────────────────────────────────

def _aggregate_specialists(specialist_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate specialist results into a compact summary for final synthesis."""
    summary_parts = []
    advantage_counts = {"home": 0, "away": 0, "draw": 0, "neutral": 0}
    total_confidence = 0
    confidence_count = 0

    for result in specialist_results:
        if result.get("status") != "success":
            continue
        r = result.get("result") or {}
        adv = r.get("advantage", "neutral")
        conf = _safe_int(r.get("confidence"), 50)
        reasoning = r.get("reasoning", "")
        key_factor = r.get("key_factor", "")
        specialist = result.get("specialist", "unknown")

        advantage_counts[adv] = advantage_counts.get(adv, 0) + 1
        total_confidence += conf
        confidence_count += 1

        summary_parts.append(
            f"[{specialist.upper()}] {adv.upper()} (conf={conf}%): {reasoning} | {key_factor}"
        )

    # Determine consensus advantage
    consensus_advantage = max(advantage_counts, key=advantage_counts.get) if advantage_counts else "neutral"
    avg_confidence = round(total_confidence / confidence_count) if confidence_count else 50

    return {
        "specialist_summary": "\n".join(summary_parts) if summary_parts else "No specialist data available",
        "consensus_advantage": consensus_advantage,
        "avg_confidence": avg_confidence,
        "specialist_count": len(summary_parts),
        "advantage_breakdown": advantage_counts,
    }


def _build_model_summary(doc: dict[str, Any]) -> str:
    """Build a compact model summary for the final synthesis."""
    prediction = doc.get("prediction") or {}
    models = prediction.get("models") or {}

    parts = []
    for name, model_data in models.items():
        if not model_data or model_data.get("error"):
            continue
        probs = model_data.get("probabilities") or {}
        if probs:
            parts.append(
                f"{name}: home={_safe_float(probs.get('home_win'), 33):.1f}% "
                f"draw={_safe_float(probs.get('draw'), 33):.1f}% "
                f"away={_safe_float(probs.get('away_win'), 33):.1f}%"
            )

    return "\n".join(parts) if parts else "No model outputs available"


def _build_memory_context(doc: dict[str, Any]) -> str:
    """Build a compact memory context for the final synthesis."""
    try:
        from app.self_learner import get_signal_weights, get_league_accuracy, get_top_signals
        from app.clv import get_clv_summary

        league = doc.get("tournament") or doc.get("category") or ""
        if isinstance(league, dict):
            league = league.get("name") or ""

        parts = []
        signal_weights = get_signal_weights(league)
        if signal_weights:
            top = sorted(signal_weights.items(), key=lambda x: x[1], reverse=True)[:3]
            parts.append(f"Top signals: {', '.join(f'{k}={v:.0%}' for k, v in top)}")

        league_acc = get_league_accuracy(league)
        if league_acc.get("known"):
            parts.append(f"League accuracy: {league_acc.get('win_rate', 0):.0%} ({league_acc.get('samples', 0)} samples)")

        clv = get_clv_summary(days=14)
        avg_clv = clv.get("avg_clv_percent")
        if avg_clv is not None:
            parts.append(f"CLV 14d: {avg_clv:+.1f}%")

        return "\n".join(parts) if parts else "No memory context available"
    except Exception:
        return "Memory context unavailable"


# ── Final synthesis ────────────────────────────────────────────────────────────

def run_final_synthesis(
    doc: dict[str, Any],
    specialist_results: list[dict[str, Any]],
    model_summary: str,
    memory_context: str,
) -> dict[str, Any]:
    """Run the final synthesis stage: gather all results and produce final prediction."""
    if not is_ollama_available():
        return {"status": "ollama_unavailable", "message": "Ollama is not running"}

    aggregation = _aggregate_specialists(specialist_results)

    match_name = doc.get("sportybet_name") or doc.get("name") or "Unknown vs Unknown"
    competition = doc.get("tournament") or doc.get("category") or "Unknown"

    prompt = _FINAL_SYNTHESIS_PROMPT.format(
        match=match_name,
        competition=competition,
        specialist_summary=aggregation.get("specialist_summary", ""),
        model_summary=model_summary or "No model outputs",
        memory_context=memory_context or "No memory context",
    )

    try:
        raw = _call_ollama(_FINAL_MODEL, prompt, timeout=_TIMEOUT_FINAL)
        result = _parse_response_safe(raw)
        if not result:
            return {"status": "error", "message": "Failed to parse final synthesis JSON", "raw": raw[:200]}

        # Normalize confidence
        raw_confidence = _safe_float(result.get("confidence"), 0)
        confidence = round(raw_confidence) if raw_confidence > 1 else round(raw_confidence * 100)

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
            "source": "ollama_pipeline",
            "model": _FINAL_MODEL,
            "specialist_results": specialist_results,
            "aggregation": aggregation,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ── Brain review ───────────────────────────────────────────────────────────────

def run_brain_review(prediction: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """Run AI brain review using small-context Ollama call."""
    if not is_ollama_available():
        return {"status": "ollama_unavailable", "provider": "rules"}

    # Build compact prediction summary
    pred_summary = {
        "match": prediction.get("name"),
        "prediction": prediction.get("picks", [{}])[0] if prediction.get("picks") else {},
        "top_signals": [s.get("name") for s in (prediction.get("signals") or [])[:5]],
        "confidence": prediction.get("picks", [{}])[0].get("confidence") if prediction.get("picks") else None,
    }

    memory_context = _build_memory_context(doc)

    prompt = _BRAIN_REVIEW_PROMPT.format(
        prediction_summary=json.dumps(pred_summary, default=str),
        memory_context=memory_context,
    )

    try:
        raw = _call_ollama(_SPECIALIST_MODEL, prompt, timeout=_TIMEOUT_BRAIN)
        result = _parse_response_safe(raw)
        if not result:
            return {"status": "error", "provider": "rules", "message": "Failed to parse brain review JSON"}

        return {
            "status": result.get("status") or "approved",
            "provider": "ollama",
            "model": _SPECIALIST_MODEL,
            "verdict": result.get("verdict"),
            "confidence_adjustment": _safe_int(result.get("confidence_adjustment"), 0),
            "risks": result.get("risks") or [],
            "reasons": result.get("reasons") or [],
            "memory_context_used": bool(memory_context and memory_context != "Memory context unavailable"),
        }
    except Exception as exc:
        return {"status": "error", "provider": "rules", "message": str(exc)}


# ── Main pipeline entry point ──────────────────────────────────────────────────

def run_ollama_pipeline(doc: dict[str, Any], attach_brain: bool = True) -> dict[str, Any]:
    """
    Run the full small-context multi-stage Ollama prediction pipeline.

    Stages:
      1. Run all available specialists (form, H2H, odds, standings, models)
      2. Aggregate specialist results
      3. Run final synthesis
      4. Optionally run brain review

    Returns a prediction dict compatible with the existing prediction flow.
    """
    if not is_ollama_available():
        return {
            "status": "ollama_unavailable",
            "message": "Ollama is not running. Start it with: ollama serve",
        }

    # ── Stage 1: Specialists ────────────────────────────────────────────────────
    logger.info("[ollama_pipeline] running specialist agents")
    specialist_results: list[dict[str, Any]] = []

    specialists = [
        ("form", run_form_specialist),
        ("h2h", run_h2h_specialist),
        ("odds", run_odds_specialist),
        ("standings", run_standings_specialist),
        ("models", run_model_specialist),
    ]

    for name, runner in specialists:
        try:
            result = runner(doc)
            specialist_results.append(result)
            if result.get("status") == "success":
                logger.info("[ollama_pipeline] %s specialist: %s", name, result.get("result", {}).get("advantage"))
            else:
                logger.debug("[ollama_pipeline] %s specialist skipped: %s", name, result.get("reason"))
        except Exception as exc:
            logger.warning("[ollama_pipeline] %s specialist failed: %s", name, exc)
            specialist_results.append({"status": "error", "specialist": name, "error": str(exc)})

    # ── Stage 2: Aggregation ───────────────────────────────────────────────────
    aggregation = _aggregate_specialists(specialist_results)
    model_summary = _build_model_summary(doc)
    memory_context = _build_memory_context(doc)

    # ── Stage 3: Final synthesis ───────────────────────────────────────────────
    logger.info("[ollama_pipeline] running final synthesis (consensus=%s)", aggregation.get("consensus_advantage"))
    final = run_final_synthesis(doc, specialist_results, model_summary, memory_context)

    # ── Stage 4: Brain review ──────────────────────────────────────────────────
    brain = None
    if attach_brain and final.get("status") == "predicted":
        try:
            brain = run_brain_review(final, doc)
            logger.info("[ollama_pipeline] brain review: %s (adj=%+d)",
                        brain.get("status"), brain.get("confidence_adjustment", 0))
        except Exception as exc:
            logger.warning("[ollama_pipeline] brain review failed: %s", exc)

    # ── Build final prediction dict ────────────────────────────────────────────
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
        "source": "ollama_pipeline",
        "model": _FINAL_MODEL,
        "specialist_results": specialist_results,
        "aggregation": aggregation,
        "brain_review": brain,
    }


def run_ollama_pipeline_batch(
    docs: list[dict[str, Any]],
    limit: int = 50,
    attach_brain: bool = True,
) -> dict[str, Any]:
    """
    Run the small-context pipeline over a batch of enriched match documents.

    Returns a summary dict with predictions list.
    """
    if not is_ollama_available():
        return {"status": "ollama_unavailable", "message": "Ollama is not running. Start it with: ollama serve"}

    docs = docs[:limit]
    print(f"[ollama_pipeline] predicting {len(docs)} matches")

    predictions = []
    value_bets = 0
    errors = 0

    for i, doc in enumerate(docs, 1):
        name = doc.get("sportybet_name") or doc.get("name") or "unknown"
        print(f"[ollama_pipeline] [{i}/{len(docs)}] {name}")
        try:
            pred = run_ollama_pipeline(doc, attach_brain=attach_brain)
            pred["sportybet_id"] = doc.get("sportybet_id")
            pred["match_id"] = doc.get("sportybet_id")
            predictions.append(pred)

            if pred.get("status") == "error":
                errors += 1
            elif pred.get("status") == "predicted":
                if pred.get("value_bet"):
                    value_bets += 1
        except Exception as exc:
            errors += 1
            predictions.append({
                "status": "error",
                "message": str(exc),
                "sportybet_id": doc.get("sportybet_id"),
                "match_id": doc.get("sportybet_id"),
            })

    return {
        "status": "success",
        "total": len(predictions),
        "predicted": len([p for p in predictions if p.get("status") == "predicted"]),
        "skipped": len([p for p in predictions if p.get("status") in ("skipped", "low_confidence")]),
        "errors": errors,
        "value_bets": value_bets,
        "predictions": predictions,
    }
