"""
LLM provider stub.
DeepSeek has replaced Groq. This module is kept only so existing
imports don't break. All callers should use app.ai_router instead.
"""
from __future__ import annotations


def get_llm():
    raise RuntimeError("DeepSeek has replaced Groq. Use app.ai_router.get_router() instead.")


def get_fast_llm():
    raise RuntimeError("DeepSeek has replaced Groq. Use app.ai_router.get_router() instead.")


def is_deepseek_available() -> bool:
    return False
