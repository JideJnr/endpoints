"""
LLM provider abstraction.
Uses OpenRouter for all LLM calls.
Set OPENROUTER_API_KEY in .env to enable OpenRouter.
"""
from __future__ import annotations

import os
from typing import Any


def get_llm():
    """
    Returns a LangChain-compatible LLM instance.
    Uses OpenRouter (openai/deepseek-v3-240709) if OPENROUTER_API_KEY is set,
    otherwise raises ImportError so callers can fall back to rules engine.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set — OpenRouter LLM unavailable")

    try:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=os.getenv("OPENROUTER_MODEL", "openai/deepseek-v3-240709"),
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            temperature=0,
            # Without an explicit timeout, a hung OpenRouter response can stall
            # a caller (e.g. the live bet builder's slip-synthesis pass)
            # indefinitely. The default here is deliberately generous, not
            # tight: OPENROUTER_MODEL is "openrouter/free" (see predictx/.env)
            # -- a shared, rate-limited free-tier route that routinely takes
            # tens of seconds, well beyond what a paid model would. A short
            # timeout tuned for paid-model latency would make this fall back
            # to the deterministic path on most calls, quietly defeating the
            # point of the "AI" mode rather than just being a safety net.
            # max_retries=1 avoids langchain's default extra retry (each with
            # its own backoff) compounding that latency further -- callers
            # that need an LLM decision already have a deterministic fallback
            # on any exception, so one honest attempt beats a slow retry.
            timeout=int(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "60")),
            max_retries=1,
        )
    except ImportError as exc:
        raise ImportError(
            "langchain-openai not installed. Run: pip install langchain-openai"
        ) from exc


def get_fast_llm():
    """Lighter/faster model for tasks like fuzzy match disambiguation."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=os.getenv("OPENROUTER_FAST_MODEL", "openai/deepseek-v3-240709"),
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=0.1,
        # Same free-tier reasoning as get_llm() above -- 12s was tuned for a
        # paid model and would fail most calls against openrouter/free.
        timeout=int(os.getenv("OPENROUTER_FAST_TIMEOUT_SECONDS", "45")),
        max_retries=1,
    )


def is_openrouter_available() -> bool:
    """Check if OpenRouter is configured without raising."""
    try:
        get_llm()
        return True
    except Exception:
        return False
