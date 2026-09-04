"""
LLM provider abstraction.
Uses OpenRouter for all LLM calls.
Set OPENROUTER_API_KEY in .env to enable OpenRouter.

Plain urllib client -- no langchain. Reuses the same bounded-timeout
machinery as app.ai.ai_router (_bounded / _openrouter_url), which carries
the fix for the Aug 2026 DNS-hang incident: a plain urlopen() call can hang
past its stated timeout because socket timeouts don't cover DNS resolution.
See app.ai.ai_router._bounded for the full story.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request as urllib_request
from dataclasses import dataclass
from urllib.error import HTTPError


@dataclass
class _LLMResponse:
    content: str


def _do_chat_call(url: str, payload: bytes, headers: dict, timeout: int) -> str:
    req = urllib_request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {body[:300]}") from exc
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


class _OpenRouterChat:
    """Minimal OpenRouter chat client exposing the .invoke(messages) shape
    the rest of the codebase already expects (a drop-in for the langchain
    ChatOpenAI calls this used to wrap).

    messages: list of {"role": "system"|"user"|"assistant", "content": str} dicts.
    """

    def __init__(self, model: str, api_key: str, timeout: int, temperature: float = 0):
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature

    def invoke(self, messages: list[dict]) -> _LLMResponse:
        from app.ai.ai_router import _bounded, _openrouter_url  # reuse the proven bounded client

        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "temperature": self.temperature,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://predictx.app",
            "X-Title": "PredictX",
        }
        url = _openrouter_url() + "/chat/completions"
        # Give the DNS-hang guard a few seconds of margin over the socket
        # timeout, same reasoning as app.ai.ai_router._call_llm.
        content = _bounded(_do_chat_call, url, payload, headers, self.timeout, timeout=self.timeout + 10)
        return _LLMResponse(content=content)


def get_llm() -> _OpenRouterChat:
    """
    Returns an OpenRouter chat client with an .invoke(messages) method.
    Uses OpenRouter (openai/deepseek-v3-240709 by default) if OPENROUTER_API_KEY
    is set, otherwise raises so callers can fall back to the rules engine.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set — OpenRouter LLM unavailable")

    return _OpenRouterChat(
        model=os.getenv("OPENROUTER_MODEL", "openai/deepseek-v3-240709"),
        api_key=api_key,
        # Without an explicit timeout, a hung OpenRouter response can stall
        # a caller (e.g. the live bet builder's slip-synthesis pass)
        # indefinitely. The default here is deliberately generous, not
        # tight: OPENROUTER_MODEL is "openrouter/free" (see predictx/.env)
        # -- a shared, rate-limited free-tier route that routinely takes
        # tens of seconds, well beyond what a paid model would. A short
        # timeout tuned for paid-model latency would make this fall back
        # to the deterministic path on most calls, quietly defeating the
        # point of the "AI" mode rather than just being a safety net.
        timeout=int(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "60")),
        temperature=0,
    )


def get_fast_llm() -> _OpenRouterChat:
    """Lighter/faster model for tasks like fuzzy match disambiguation."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    return _OpenRouterChat(
        model=os.getenv("OPENROUTER_FAST_MODEL", "openai/deepseek-v3-240709"),
        api_key=api_key,
        # Same free-tier reasoning as get_llm() above -- 12s was tuned for a
        # paid model and would fail most calls against openrouter/free.
        timeout=int(os.getenv("OPENROUTER_FAST_TIMEOUT_SECONDS", "45")),
        temperature=0.1,
    )


def is_openrouter_available() -> bool:
    """Check if OpenRouter is configured without raising."""
    try:
        get_llm()
        return True
    except Exception:
        return False
