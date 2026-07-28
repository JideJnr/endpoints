"""
Ollama local LLM prediction agent.
Supports two models:
  - qwen3:8b   (Best Overall — strong reasoning + coding)
  - deepseek-r1:8b (Best Reasoning — great for Team A vs Team B analysis)

Requires Ollama running locally: https://ollama.com
Pull models first:
  ollama pull qwen3:8b
  ollama pull deepseek-r1:8b

Set PREDICTX_OLLAMA_URL in .env (default: http://localhost:11434)
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib import request as urllib_request

from app.config import get_settings

OLLAMA_MODELS = {
    "qwen3:8b": {
        "label": "Qwen3 8B",
        "role": "Best Overall — excellent reasoning, strong coding, long context",
        "emoji": "🥇",
    },
    "deepseek-r1:8b": {
        "label": "DeepSeek-R1 8B",
        "role": "Best Reasoning — Team A vs Team B analysis, explains predictions",
        "emoji": "🥈",
    },
}

SYSTEM_PROMPT = """You are a football prediction expert. Analyse the match data and output a prediction as valid JSON only — no text outside the JSON block.

Consider: form (W/L/D), H2H record, league standings, 1x2 odds, and any web context provided.
Only predict if confidence >= 0.60, otherwise set status to "low_confidence".

Output format:
{
  "match": "<home> vs <away>",
  "status": "predicted | low_confidence | skipped",
  "prediction": "Home Win | Away Win | Draw",
  "odds": "<decimal odds string>",
  "confidence": <0.0-1.0>,
  "value_bet": <true if confidence>=0.70 and odds>=2.5>,
  "btts": "Yes | No | Unknown",
  "over_2_5": "Yes | No | Unknown",
  "market_signal": "sharp HOME | sharp AWAY | stable | unavailable",
  "key_factors": ["<factor 1>", "<factor 2>", "<factor 3>"],
  "reasoning": {
    "form": "<one sentence>",
    "h2h": "<one sentence>",
    "standings": "<one sentence>",
    "odds_signal": "<one sentence>",
    "verdict": "<one sentence final summary>"
  }
}"""


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


async def _call_ollama_async(model: str, prompt: str, timeout: int = 120) -> str:
    """
    Async-safe Ollama call.
    Runs the blocking urllib request in a thread pool to avoid blocking the event loop.
    """
    def _sync_call() -> str:
        url = _ollama_url() + "/api/generate"
        payload = json.dumps({
            "model": model,
            "prompt": f"{SYSTEM_PROMPT}\n\n{prompt}",
            "stream": False,
            "think": False,
            "keep_alive": "-1",
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


def _call_ollama(model: str, prompt: str, timeout: int = 120) -> str:
    """Sync wrapper for backward compatibility."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're in an async context (e.g., FastAPI request handler)
            # Can't use asyncio.run() or run_until_complete()
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
    # Strip DeepSeek-R1 <think>...</think> reasoning blocks
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # Strip markdown fences
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def run_ollama_match_analysis(
    doc: dict[str, Any],
    model: str = "qwen3:8b",
) -> dict[str, Any]:
    """
    Run a single Ollama model analysis for one enriched match document.

    Args:
        doc:   enriched match document (same format as groq_agent)
        model: ollama model name, e.g. "qwen3:8b" or "deepseek-r1:8b"

    Returns:
        analysis dict with status, recommendation, confidence, reasoning, etc.
    """
    if not is_ollama_available():
        return {
            "status": "ollama_unavailable",
            "message": "Ollama is not running. Start it with: ollama serve",
            "model": model,
        }

    if not is_ollama_available(model):
        return {
            "status": "model_unavailable",
            "message": f"Model {model} not found. Pull it with: ollama pull {model}",
            "model": model,
        }

    try:
        from app.competition_special import apply_known_competition_context
        apply_known_competition_context(doc)
    except Exception:
        pass

    # Reuse the same summariser from groq_agent to keep prompts consistent
    try:
        from app.groq_agent import _summarise_doc
        summary = _summarise_doc(doc)
    except Exception as exc:
        return {"status": "error", "message": f"Failed to summarise doc: {exc}", "model": model}

    try:
        raw = _call_ollama(model, summary)
        result = _parse_response(raw)
    except json.JSONDecodeError as exc:
        return {"status": "error", "message": f"Model returned non-JSON: {exc}", "model": model}
    except Exception as exc:
        return {"status": "error", "message": str(exc), "model": model}

    try:
        raw_confidence = float(result.get("confidence", 0))
        confidence = round(raw_confidence * 100) if raw_confidence <= 1 else round(raw_confidence)
    except (TypeError, ValueError):
        confidence = None

    model_info = OLLAMA_MODELS.get(model, {"label": model, "role": "", "emoji": "🤖"})

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
        "source": "ollama",
        "model": model,
        "model_label": model_info["label"],
        "model_role": model_info["role"],
        "model_emoji": model_info["emoji"],
    }


def run_ollama_all_models(doc: dict[str, Any]) -> dict[str, Any]:
    """
    Run both Ollama models (qwen3:8b + deepseek-r1:8b) on one match document.

    Returns a combined result with individual model outputs and a consensus.
    """
    results: dict[str, Any] = {}
    for model in OLLAMA_MODELS:
        results[model] = run_ollama_match_analysis(doc, model=model)

    # Build consensus: if both models agree on the same prediction, flag it
    predictions = [
        r.get("recommendation")
        for r in results.values()
        if r.get("status") == "predicted" and r.get("recommendation")
    ]
    consensus = None
    if len(predictions) == len(OLLAMA_MODELS) and len(set(predictions)) == 1:
        consensus = predictions[0]

    avg_confidence = None
    confidences = [r.get("confidence") for r in results.values() if isinstance(r.get("confidence"), (int, float))]
    if confidences:
        avg_confidence = round(sum(confidences) / len(confidences))

    return {
        "status": "success",
        "models": results,
        "consensus": consensus,
        "consensus_reached": consensus is not None,
        "avg_confidence": avg_confidence,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def run_ollama_predictions(
    match_date: str | None = None,
    docs: list[dict[str, Any]] | None = None,
    limit: int = 50,
    model: str = "qwen3:8b",
) -> dict[str, Any]:
    """
    Run Ollama predictions over enriched match documents (batch).

    Args:
        match_date: YYYY-MM-DD, defaults to today
        docs:       pre-loaded enriched docs (skips DB fetch if provided)
        limit:      max matches to predict
        model:      which Ollama model to use

    Returns:
        summary dict with predictions list
    """
    from datetime import date as dt

    if not is_ollama_available():
        return {"status": "ollama_unavailable", "message": "Ollama is not running. Start it with: ollama serve"}

    target_date = match_date or dt.today().isoformat()

    if docs is None:
        from app.league_memory import get_enriched_matches
        docs = get_enriched_matches(target_date, limit=limit)

    if not docs:
        return {"status": "no_matches", "date": target_date, "predictions": []}

    docs = docs[:limit]
    print(f"[ollama_agent] predicting {len(docs)} matches for {target_date} using {model}")

    predictions = []
    value_bets = 0
    errors = 0

    for i, doc in enumerate(docs, 1):
        name = doc.get("sportybet_name") or doc.get("name") or "unknown"
        print(f"[ollama_agent] [{i}/{len(docs)}] {name}")
        pred = run_ollama_match_analysis(doc, model=model)
        pred["sportybet_id"] = doc.get("sportybet_id")
        pred["match_id"] = doc.get("sportybet_id")
        predictions.append(pred)

        if pred.get("status") == "error":
            errors += 1
        elif pred.get("status") == "predicted":
            if pred.get("value_bet"):
                value_bets += 1

    return {
        "status": "success",
        "date": target_date,
        "model": model,
        "total": len(predictions),
        "predicted": len([p for p in predictions if p.get("status") == "predicted"]),
        "skipped": len([p for p in predictions if p.get("status") in ("skipped", "low_confidence")]),
        "errors": errors,
        "value_bets": value_bets,
        "predictions": predictions,
    }
