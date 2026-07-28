"""
Ollama persistent model manager.

Keeps Ollama models resident in memory across multiple requests by:
  1. Pre-loading models at application startup
  2. Setting keep_alive=-1 on all generate calls so models never unload
  3. Running a background keep-alive thread that periodically pings Ollama
     to prevent any internal garbage collection from evicting the model

This eliminates the redundant model reload overhead that causes high latency
on the first prediction request after a model has been idle.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib import request as urllib_request

from app.config import get_settings

logger = logging.getLogger(__name__)

_KEEP_ALIVE_DURATION = "24h"

_model_lock = threading.Lock()
_loaded_models: dict[str, float] = {}
_keep_alive_thread: threading.Thread | None = None
_keep_alive_running = False


def _ollama_url() -> str:
    settings = get_settings()
    return settings.ollama_url.replace("/api/chat", "").rstrip("/")


def preload_model(model: str) -> bool:
    """Pre-load a single Ollama model into memory.

    Sends a minimal generate request with keep_alive=-1 so the model
    stays resident. Returns True if the model is loaded, False otherwise.
    """
    start = time.monotonic()
    try:
        url = _ollama_url() + "/api/generate"
        payload = json.dumps({
            "model": model,
            "prompt": "",
            "stream": False,
            "think": False,
            "keep_alive": _KEEP_ALIVE_DURATION,
            "options": {"temperature": 0, "num_predict": 1},
        }).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = round(time.monotonic() - start, 2)
        with _model_lock:
            _loaded_models[model] = time.monotonic()
        logger.info(
            "[ollama_model_manager] preloaded model=%s in %.2fs status=%s",
            model, elapsed, data.get("status", "unknown"),
        )
        return True
    except Exception as exc:
        elapsed = round(time.monotonic() - start, 2)
        logger.warning(
            "[ollama_model_manager] preload failed model=%s after %.2fs: %s",
            model, elapsed, exc,
        )
        return False


def preload_all_models() -> dict[str, bool]:
    """Pre-load all configured Ollama models at startup."""
    from app.ollama_agent import OLLAMA_MODELS
    results = {}
    for model in OLLAMA_MODELS:
        results[model] = preload_model(model)
    return results


def is_model_loaded(model: str) -> bool:
    """Check if a model is currently resident in Ollama memory."""
    with _model_lock:
        if model not in _loaded_models:
            return False
    try:
        url = _ollama_url() + "/api/tags"
        req = urllib_request.Request(url, method="GET")
        with urllib_request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = [m.get("name", "") for m in (data.get("models") or [])]
        return any(model in name for name in names)
    except Exception:
        return False


def _keep_alive_ping(model: str) -> bool:
    """Send a lightweight keep-alive ping to keep the model resident."""
    try:
        url = _ollama_url() + "/api/generate"
        payload = json.dumps({
            "model": model,
            "prompt": "",
            "stream": False,
            "think": False,
            "keep_alive": _KEEP_ALIVE_DURATION,
            "options": {"temperature": 0, "num_predict": 1},
        }).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read().decode("utf-8"))
        with _model_lock:
            _loaded_models[model] = time.monotonic()
        return True
    except Exception as exc:
        logger.debug("[ollama_model_manager] keep-alive ping failed for %s: %s", model, exc)
        return False


def _keep_alive_loop(interval_seconds: int = 120) -> None:
    """Background thread that periodically pings all loaded models."""
    while _keep_alive_running:
        with _model_lock:
            models = list(_loaded_models.keys())
        for model in models:
            _keep_alive_ping(model)
        time.sleep(interval_seconds)


def start_keep_alive(interval_seconds: int = 120) -> None:
    """Start the background keep-alive thread for loaded models."""
    global _keep_alive_running, _keep_alive_thread
    if _keep_alive_running:
        return
    _keep_alive_running = True
    _keep_alive_thread = threading.Thread(
        target=_keep_alive_loop,
        args=(interval_seconds,),
        daemon=True,
        name="ollama_keep_alive",
    )
    _keep_alive_thread.start()
    logger.info(
        "[ollama_model_manager] keep-alive thread started (interval=%ds)",
        interval_seconds,
    )


def stop_keep_alive() -> None:
    """Stop the background keep-alive thread."""
    global _keep_alive_running
    _keep_alive_running = False
    if _keep_alive_thread is not None:
        _keep_alive_thread.join(timeout=5)
        _keep_alive_thread = None
    logger.info("[ollama_model_manager] keep-alive thread stopped")


def get_model_status() -> dict[str, Any]:
    """Return the status of all managed models."""
    with _model_lock:
        loaded = dict(_loaded_models)
    now = time.monotonic()
    model_info = {}
    for model, loaded_at in loaded.items():
        uptime_seconds = round(now - loaded_at, 1)
        model_info[model] = {
            "loaded": True,
            "loaded_at": datetime.fromtimestamp(loaded_at, tz=timezone.utc).isoformat(),
            "uptime_seconds": uptime_seconds,
        }
    return {
        "models": model_info,
        "keep_alive_running": _keep_alive_running,
        "keep_alive_duration": _KEEP_ALIVE_DURATION,
    }