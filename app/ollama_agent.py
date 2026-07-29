"""
DeepSeek prediction agent.
Uses DeepSeek's OpenAI-compatible API for fast, cheap inference.

Set DEEPSEEK_API_KEY in .env.
Default model: deepseek-chat (DeepSeek-V3, cheapest/fastest).
For reasoning tasks set DEEPSEEK_MODEL=deepseek-reasoner.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError

from app.config import get_settings

DEEPSEEK_MODELS = {
    "deepseek-chat": {
        "label": "DeepSeek Chat (V3)",
        "role": "Best Overall — cheapest, fast, strong reasoning",
        "emoji": "🥇",
    },
    "deepseek-reasoner": {
        "label": "DeepSeek Reasoner (R1)",
        "role": "Deep reasoning — slower but more thorough",
        "emoji": "🧠",
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


def _deepseek_url() -> str:
    settings = get_settings()
    return settings.deepseek_base_url.rstrip("/")


def is_ollama_available(model: str | None = None) -> bool:
    """Check if DeepSeek API is reachable (API key is set)."""
    settings = get_settings()
    if not settings.deepseek_api_key:
        return False
    try:
        url = _deepseek_url() + "/chat/completions"
        payload = json.dumps({
            "model": settings.deepseek_model,
            "messages": [{"role": "user", "content": "ping"}],
            "temperature": 0,
            "max_tokens": 1,
        }).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.deepseek_api_key}",
            },
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("choices"))
    except Exception:
        return False


def _call_llm(model: str, prompt: str, timeout: int = 60) -> str:
    """Call DeepSeek chat completions (OpenAI-compatible)."""
    settings = get_settings()
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set in .env")

    url = _deepseek_url() + "/chat/completions"
    payload = json.dumps({
        "model": settings.deepseek_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 512,
    }).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.deepseek_api_key}",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek HTTP {exc.code}: {body[:300]}") from exc

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def _parse_response(raw: str) -> dict[str, Any]:
    """Extract JSON from model response, stripping </think> blocks and markdown fences."""
    text = re.sub(r"</think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def run_ollama_match_analysis(
    doc: dict[str, Any],
    model: str = "deepseek/deepseek-chat",
) -> dict[str, Any]:
    """
    Run a single DeepSeek model analysis for one enriched match document.

    Args:
        doc:   enriched match document (same format as deepseek_agent)
        model: DeepSeek model name (ignored — uses configured model)

    Returns:
        analysis dict with status, recommendation, confidence, reasoning, etc.
    """
    if not is_ollama_available():
        return {
            "status": "ollama_unavailable",
            "message": "DeepSeek is not available. Check DEEPSEEK_API_KEY in .env",
            "model": model,
        }

    try:
        from app.competition_special import apply_known_competition_context
        apply_known_competition_context(doc)
    except Exception:
        pass

    try:
        from app.deepseek_agent import _summarise_doc
        summary = _summarise_doc(doc)
    except Exception as exc:
        return {"status": "error", "message": f"Failed to summarise doc: {exc}", "model": model}

    try:
        raw = _call_llm(model, summary)
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

    model_info = DEEPSEEK_MODELS.get(model) or DEEPSEEK_MODELS.get(get_settings().deepseek_model) or {"label": model, "role": "", "emoji": "🤖"}

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
        "source": "deepseek",
        "model": model,
        "model_label": model_info["label"],
        "model_role": model_info["role"],
        "model_emoji": model_info["emoji"],
    }


def run_ollama_all_models(doc: dict[str, Any]) -> dict[str, Any]:
    """
    Run OpenRouter models on one match document.

    Returns a combined result with individual model outputs and a consensus.
    """
    settings = get_settings()
    model = settings.deepseek_model
    results: dict[str, Any] = {}
    results[model] = run_ollama_match_analysis(doc, model=model)

    predictions = [
        r.get("recommendation")
        for r in results.values()
        if r.get("status") == "predicted" and r.get("recommendation")
    ]
    consensus = None
    if len(predictions) == len(results) and len(set(predictions)) == 1:
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
    model: str = "deepseek/deepseek-chat",
) -> dict[str, Any]:
    """
    Run DeepSeek predictions over enriched match documents (batch).

    Args:
        match_date: YYYY-MM-DD, defaults to today
        docs:       pre-loaded enriched docs (skips DB fetch if provided)
        limit:      max matches to predict
        model:      which DeepSeek model to use (ignored — uses configured model)

    Returns:
        summary dict with predictions list
    """
    from datetime import date as dt

    if not is_ollama_available():
        return {"status": "ollama_unavailable", "message": "DeepSeek is not available. Check DEEPSEEK_API_KEY in .env"}

    target_date = match_date or dt.today().isoformat()

    if docs is None:
        from app.league_memory import get_enriched_matches
        docs = get_enriched_matches(target_date, limit=limit)

    if not docs:
        return {"status": "no_matches", "date": target_date, "predictions": []}

    docs = docs[:limit]
    print(f"[deepseek_agent] predicting {len(docs)} matches for {target_date} using {model}")

    predictions = []
    value_bets = 0
    errors = 0

    for i, doc in enumerate(docs, 1):
        name = doc.get("sportybet_name") or doc.get("name") or "unknown"
        print(f"[deepseek_agent] [{i}/{len(docs)}] {name}")
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