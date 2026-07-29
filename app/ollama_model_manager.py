"""
DeepSeek model manager — no-op shim kept for interface compatibility.
DeepSeek is a cloud API; models are always available and need no preloading.
"""
from __future__ import annotations

from app.config import get_settings


def preload_model(model: str) -> bool:
    return True


def preload_all_models() -> dict[str, bool]:
    return {get_settings().deepseek_model: True}


def is_model_loaded(model: str) -> bool:
    return True


def start_keep_alive(interval_seconds: int = 120) -> None:
    pass


def stop_keep_alive() -> None:
    pass


def get_model_status() -> dict[str, object]:
    settings = get_settings()
    return {
        "models": {
            settings.deepseek_model: {
                "loaded": True,
                "loaded_at": "",
                "uptime_seconds": 0,
            },
        },
        "keep_alive_running": False,
        "keep_alive_duration": "",
        "provider": "deepseek",
    }
