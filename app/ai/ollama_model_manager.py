"""
OpenRouter model manager — no-op replacement for Ollama model manager.

OpenRouter is a cloud API — models are always available and don't need
preloading or keep-alive pings. This module provides the same interface
as the Ollama model manager but does nothing, so callers don't break.
"""
from __future__ import annotations

from app.config.config import get_settings


def preload_model(model: str) -> bool:
    """No-op — OpenRouter models are always available."""
    return True


def preload_all_models() -> dict[str, bool]:
    """No-op — OpenRouter models are always available."""
    settings = get_settings()
    model = settings.openrouter_model
    return {model: True}


def is_model_loaded(model: str) -> bool:
    """OpenRouter models are always available."""
    return True


def start_keep_alive(interval_seconds: int = 120) -> None:
    """No-op — OpenRouter doesn't need keep-alive."""
    pass


def stop_keep_alive() -> None:
    """No-op — OpenRouter doesn't need keep-alive."""
    pass


def get_model_status() -> dict[str, object]:
    """Return OpenRouter model status."""
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
