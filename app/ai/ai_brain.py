from __future__ import annotations

import json
from typing import Any
from urllib import error, request

from app.config.config import get_settings


DEFAULT_OLLAMA_MODEL = "llama3.2:3b"
DEFAULT_HF_MODEL = "Qwen/Qwen2.5-7B-Instruct:fastest"


def oversee_prediction(prediction: dict[str, Any], detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    AI supervisor with memory-aware reasoning.
    Routes through AIRouter (qwen3 → openrouter → groq) then falls back to
    deterministic rules when no model is available.
    """
    safe_detail = detail if isinstance(detail, dict) else {}
    match_context = _build_match_context(prediction, safe_detail)
    memory_context = _build_memory_context(prediction)
    prompt_payload = _compact_prediction(prediction, safe_detail, memory_context, match_context)
    # Try AIRouter first (covers ollama + groq in one call)
    from app.ai.ai_router import get_router
    if get_router().any_available():
        ai = _router_review(prompt_payload)
        if ai:
            ai["memory_context_used"] = bool(memory_context)
            return ai
    # HuggingFace as secondary cloud option
    settings = get_settings()
    if settings.hf_token_present:
        ai = _huggingface_review(prompt_payload)
        if ai:
            ai["memory_context_used"] = bool(memory_context)
            return ai
    return _rule_review(prediction, memory_context)


def _build_match_context(prediction: dict[str, Any], detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Attach competition and team watcher context to the AI review payload."""
    context: dict[str, Any] = {}
    match_doc: dict[str, Any] = {}
    if isinstance(detail, dict):
        match_doc.update(detail)
    if isinstance(prediction, dict):
        match_doc.update(prediction)

    try:
        from app.competition.competition_special import apply_known_competition_context
        apply_known_competition_context(match_doc)
        context["known_competition"] = match_doc.get("known_competition")
        context["competition_special"] = match_doc.get("competition_special")
        context["competition_intelligence"] = match_doc.get("competition_intelligence")
    except Exception:
        pass

    try:
        from app.team_watcher.team_watcher import team_context_for_match
        team_watchers = team_context_for_match(match_doc)
        context["team_watchers"] = team_watchers
    except Exception:
        pass

    competition_intelligence = context.get("competition_intelligence") or {}
    if isinstance(competition_intelligence, dict):
        ai_watchers = competition_intelligence.get("ai_team_watchers")
        if ai_watchers is not None:
            context["ai_team_watchers"] = ai_watchers
    return context


def _build_memory_context(prediction: dict[str, Any]) -> dict[str, Any]:
    """
    Build a memory block from the self-learner and CLV data.
    This gives the AI brain historical awareness:
      - Which signals are hot/cold right now
      - How accurate the system has been in this league
      - Whether CLV is positive (are we beating the market?)
      - Auto-tuned model weights
    """
    context: dict[str, Any] = {}

    try:
        from app.monitoring.self_learner import get_signal_weights, get_league_accuracy, get_top_signals, get_learned_weights, get_learning_summary
        league = prediction.get("league_name") or prediction.get("tournament") or ""
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


def _provider_review(provider: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if provider in {"hf", "huggingface", "hugging-face"}:
        return _huggingface_review(payload)
    if provider == "ollama":
        return _router_review(payload)  # route through OpenRouter instead
    if provider in {"groq", "auto"}:
        return _router_review(payload)
    return None


def _router_review(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Use AIRouter for supervisor review: qwen3 → openrouter → groq."""
    from app.ai.ai_router import get_router, parse_json_safe
    try:
        messages = _review_messages(payload)
        raw = get_router().call_review(messages)
        parsed = parse_json_safe(raw)
        if not parsed:
            return None
        model = get_router().best_available() or "groq"
        return _review_result("ai_router", model, parsed)
    except Exception:
        return None


def _huggingface_review(payload: dict[str, Any]) -> dict[str, Any] | None:
    settings = get_settings()
    token = _hf_token()
    if not token:
        return None
    model = settings.hf_model or DEFAULT_HF_MODEL
    body = {
        "model": model,
        "messages": _review_messages(payload),
        "temperature": 0.1,
        "max_tokens": 350,
    }
    try:
        req = request.Request(
            settings.hf_url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=settings.ai_timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError, error.URLError):
        return None

    choices = data.get("choices") or []
    content = (((choices[0] if choices else {}).get("message") or {}).get("content") or "").strip()
    parsed = _parse_json_object(content)
    if not parsed:
        return None
    return _review_result("huggingface", model, parsed)



def _rule_review(prediction: dict[str, Any], memory_context: dict[str, Any] | None = None) -> dict[str, Any]:
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

    # Memory-aware adjustment: boost/suppress based on historical signal performance
    if memory_context:
        signal_weights = memory_context.get("signal_weights") or {}
        for sig in signals:
            name = sig.get("name") or ""
            weight_adj = signal_weights.get(name)
            if weight_adj is not None:
                # Positive weight_adj = signal historically reliable → small boost
                adjustment += round(weight_adj * 3)

        # CLV awareness: if we're beating the market, be slightly less conservative
        clv_data = memory_context.get("clv_14d") or {}
        if clv_data.get("avg_clv_percent", 0) > 2:
            adjustment += 1
        elif clv_data.get("avg_clv_percent", 0) < -2:
            adjustment -= 1
            risks.append("recent CLV is negative — system may be overpaying")

    adjustment = max(-8, min(8, adjustment))

    return {
        "provider": "rules",
        "model": "deterministic-supervisor-v2",
        "status": status,
        "verdict": best.get("selection"),
        "confidence_adjustment": adjustment,
        "risks": risks,
        "reasons": [signal.get("name") for signal in signals[:5]],
        "memory_context_used": bool(memory_context),
    }


def _compact_prediction(
    prediction: dict[str, Any],
    detail: dict[str, Any] | None,
    memory_context: dict[str, Any] | None = None,
    match_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "match": prediction.get("name"),
        "tournament": prediction.get("tournament"),
        "score": prediction.get("score"),
        "minute": prediction.get("minute"),
        "top_picks": (prediction.get("picks") or [])[:3],
        "signals": (prediction.get("signals") or [])[:10],
        "features": prediction.get("features"),
        "data_sources": (detail or {}).get("data_sources") or prediction.get("data_sources"),
        "sportybet_detail": (detail or {}).get("sportybet_detail"),
        "h2h": (detail or {}).get("h2h"),
        "web_context": (detail or {}).get("web_context") or prediction.get("web_context"),
        "league_sentiment": (detail or {}).get("league_sentiment") or prediction.get("league_sentiment"),
        "poisson": prediction.get("poisson"),
        "strength_of_schedule": prediction.get("strength_of_schedule"),
    }
    if memory_context:
        payload["memory_context"] = memory_context
    if match_context:
        payload["match_context"] = match_context
    return payload


def _review_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are PredictX AI Brain, a cautious football prediction reviewer with memory. "
                "You receive the current match signals AND a memory_context block showing what has "
                "historically worked, plus a match_context block with competition and team watcher context. "
                "Use memory_context to calibrate your confidence adjustment — if a signal has a 65%+ "
                "win rate historically, trust it more. If a signal has <40% win rate, flag it as a risk. "
                "If CLV is positive, the system is beating the market — be less conservative. "
                "Use the supplied rule-engine signals only. Do not invent team news, injuries, or odds. "
                "If web_context.open_router_analysis contains sentiment data, consider it: "
                "positive home sentiment boosts home confidence, negative sentiment reduces it. "
                "If web_context.open_router_analysis contains probability data (implied_home_win, etc.), "
                "use it as a supporting signal but do not override model outputs. "
                "If league_sentiment is present, use it as broad context: "
                "negative league sentiment means more upsets are likely, "
                "positive sentiment reinforces form-based predictions. "
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
    import os

    return os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")


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



