"""
LLM provider abstraction.
Supports Groq (langchain-groq) with fallback to the existing ai_brain rules engine.
Set GROQ_API_KEY in .env to enable Groq. If not set, falls back gracefully.
"""
from __future__ import annotations

import os
from typing import Any


def get_llm():
    """
    Returns a LangChain-compatible LLM instance.
    Prefers Groq (llama-3.3-70b-versatile) if GROQ_API_KEY is set,
    otherwise raises ImportError so callers can fall back to rules engine.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set — Groq LLM unavailable")

    try:
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            api_key=api_key,
            temperature=0,
        )
    except ImportError as exc:
        raise ImportError(
            "langchain-groq not installed. Run: pip install langchain-groq"
        ) from exc


def get_fast_llm():
    """Lighter/faster model for tasks like fuzzy match disambiguation."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set")
    from langchain_groq import ChatGroq
    return ChatGroq(
        model=os.getenv("GROQ_FAST_MODEL", "llama-3.1-8b-instant"),
        api_key=api_key,
        temperature=0.1,
    )


def is_groq_available() -> bool:
    """Check if Groq is configured without raising."""
    try:
        get_llm()
        return True
    except Exception:
        return False
