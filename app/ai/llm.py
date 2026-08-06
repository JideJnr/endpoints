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
    )


def is_openrouter_available() -> bool:
    """Check if OpenRouter is configured without raising."""
    try:
        get_llm()
        return True
    except Exception:
        return False
