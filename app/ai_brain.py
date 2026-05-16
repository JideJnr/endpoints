from __future__ import annotations

import json
import os
from typing import Any
from urllib import error, request


DEFAULT_OLLAMA_MODEL = "llama3.2:3b"
OLLAMA_URL = os.getenv("PREDICTX_OLLAMA_URL", "http://localhost:11434/api/chat")
HF_ROUTER_URL = os.getenv("PREDICTX_HF_URL", "https://router.huggingface.co/v1/chat/completions")
DEFAULT_HF_MODEL = "Qwen/Qwen2.5-7B-Instruct:fastest"


def oversee_prediction(prediction: dict[str, Any], detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Optional AI supervisor. Falls back to deterministic review when no local model is running."""
    prompt_payload = _compact_prediction(prediction, detail)
    provider = os.getenv("PREDICTX_AI_PROVIDER", "auto").strip().lower()
    providers = ["huggingface", "ollama"] if provider == "auto" else [provider]
    for name in providers:
        ai = _provider_review(name, prompt_payload)
        if ai:
            return ai
    return _rule_review(prediction)


def _provider_review(provider: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if provider in {"hf", "huggingface", "hugging-face"}:
        return _huggingface_review(payload)
    if provider == "ollama":
        return _ollama_review(payload)
    return None


def _huggingface_review(payload: dict[str, Any]) -> dict[str, Any] | None:
    token = _hf_token()
    if not token:
        return None
    model = os.getenv("PREDICTX_HF_MODEL", DEFAULT_HF_MODEL)
    body = {
        "model": model,
        "messages": _review_messages(payload),
        "temperature": 0.1,
        "max_tokens": 350,
    }
    try:
        req = request.Request(
            HF_ROUTER_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError, error.URLError):
        return None

    choices = data.get("choices") or []
    content = (((choices[0] if choices else {}).get("message") or {}).get("content") or "").strip()
    parsed = _parse_json_object(content)
    if not parsed:
        return None
    return _review_result("huggingface", model, parsed)


def _ollama_review(payload: dict[str, Any]) -> dict[str, Any] | None:
    model = os.getenv("PREDICTX_AI_MODEL", DEFAULT_OLLAMA_MODEL)
    body = {
        "model": model,
        "stream": False,
        "messages": _review_messages(payload),
        "options": {"temperature": 0.1},
    }
    try:
        req = request.Request(
            OLLAMA_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError, error.URLError):
        return None

    content = ((data.get("message") or {}).get("content") or "").strip()
    parsed = _parse_json_object(content)
    if not parsed:
        return None
    return _review_result("ollama", model, parsed)


def _rule_review(prediction: dict[str, Any]) -> dict[str, Any]:
    best = (prediction.get("picks") or [{}])[0]
    signals = prediction.get("signals") or []
    confidence = _to_int(best.get("confidence"), 50)
    negative = [signal for signal in signals if _to_float(signal.get("impact")) is not None and signal.get("impact") < -5]
    positive = [signal for signal in signals if _to_float(signal.get("impact")) is not None and signal.get("impact") > 5]
    risks = []
    if best.get("type") == "no_bet" or confidence < 58:
        status = "pass"
        risks.append("rule engine did not find enough edge")
    elif len(negative) >= 2:
        status = "caution"
        risks.append("multiple negative signals disagree with the top pick")
    else:
        status = "approved"
    if not any(signal.get("name") == "h2h_edge" for signal in signals):
        risks.append("limited direct H2H evidence")
    if not any(signal.get("name") == "league_strength_edge" for signal in signals):
        risks.append("limited cross-league context")

    adjustment = min(5, len(positive) * 2) - min(6, len(negative) * 3)
    return {
        "provider": "rules",
        "model": "deterministic-supervisor",
        "status": status,
        "verdict": best.get("selection"),
        "confidence_adjustment": adjustment,
        "risks": risks,
        "reasons": [signal.get("name") for signal in signals[:5]],
    }


def _compact_prediction(prediction: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "match": prediction.get("name"),
        "tournament": prediction.get("tournament"),
        "score": prediction.get("score"),
        "minute": prediction.get("minute"),
        "top_picks": (prediction.get("picks") or [])[:3],
        "signals": (prediction.get("signals") or [])[:10],
        "features": prediction.get("features"),
        "h2h": (detail or {}).get("h2h"),
        "web_context": (detail or {}).get("web_context") or prediction.get("web_context"),
        "poisson": prediction.get("poisson"),
        "strength_of_schedule": prediction.get("strength_of_schedule"),
    }


def _review_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are PredictX AI Brain, a cautious football prediction reviewer. "
                "Use the supplied rule-engine signals only. Do not invent team news, injuries, or odds. "
                "Return only compact JSON with keys: status, verdict, confidence_adjustment, risks, reasons. "
                "confidence_adjustment must be an integer from -8 to 8."
            ),
        },
        {"role": "user", "content": json.dumps(payload, separators=(",", ":"))},
    ]


def _review_result(provider: str, model: str, parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "status": parsed.get("status") or "reviewed",
        "verdict": parsed.get("verdict"),
        "confidence_adjustment": _bounded_int(parsed.get("confidence_adjustment"), -8, 8),
        "risks": _as_list(parsed.get("risks")),
        "reasons": _as_list(parsed.get("reasons")),
    }


def _hf_token() -> str | None:
    return (
        os.getenv("HF_TOKEN")
        or os.getenv("HUGGINGFACE_HUB_TOKEN")
        or os.getenv("HUGGING_FACE_HUB_TOKEN")
    )


def _parse_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except ValueError:
            return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _bounded_int(value: Any, low: int, high: int) -> int:
    return max(low, min(high, _to_int(value, 0)))


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
