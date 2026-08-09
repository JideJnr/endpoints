"""
LLM model manager — provider-agnostic stub.

Cloud LLM providers (OpenRouter, etc.) don't need preloading or keep-alive pings.
This module provides the same interface as the old Ollama model manager but does
nothing, so callers don't break.
"""
from __future__ import annotations

from app.config.config import get_settings


def preload_model(model: str) -> bool:
    """No-op — cloud LLM models are always available."""
    return True


def preload_all_models() -> dict[str, bool]:
    """No-op — cloud LLM models are always available."""
    settings = get_settings()
    model = settings.openrouter_model
    return {model: True}


def is_model_loaded(model: str) -> bool:
    """Cloud LLM models are always available."""
    return True


def start_keep_alive(interval_seconds: int = 120) -> None:
    """No-op — cloud LLM providers don't need keep-alive."""
    pass


def stop_keep_alive() -> None:
    """No-op — cloud LLM providers don't need keep-alive."""
    pass


def get_model_status() -> dict[str, object]:
    """Return LLM model status."""
    settings = get_settings()
    return {
        "models": {
            settings.openrouter_model: {
                "loaded": True,
                "loaded_at": "",
                "uptime_seconds": 0,
            },
        },
        "keep_alive_running": False,
        "keep_alive_duration": "",
        "provider": "openrouter",
    }
