"""
AI Router
---------
Single source of truth for all LLM dispatch in PredictX.

Priority chain
~~~~~~~~~~~~~~
  1. DeepSeek  -- primary for all tasks (cheap, fast, OpenAI-compatible)
  2. Rules engine -- deterministic fallback, never fails

Usage
~~~~~
    from app.ai_router import AIRouter

    router = AIRouter()

    text = router.call_analysis(prompt)
    text = router.call_reasoning(prompt)
    text = router.call_review(messages)
    result = router.call_pipeline(doc)

    model = router.best_available()   # "deepseek-chat" or None
    router.status()                   # full status dict for the API
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# -- Model catalogue -----------------------------------------------------------

DEEPSEEK_MODELS: list[dict[str, Any]] = [
    {
        "model": "deepseek-chat",
        "label": "DeepSeek Chat (V3)",
        "strength": "general",
        "primary_for": ["analysis", "reasoning", "review", "supervisor", "pipeline"],
        "emoji": "🥇",
    },
]




class AIRouter:
    """
    Centralised AI dispatcher.

    Instantiate once per request or reuse as a module-level singleton.
    All availability checks are lazy -- nothing is imported until a call is made.
    """

    def __init__(self, timeout_analysis: int = 30, timeout_reasoning: int = 30, timeout_review: int = 30) -> None:
        self._timeout_analysis = timeout_analysis
        self._timeout_reasoning = timeout_reasoning
        self._timeout_review = timeout_review
        self._availability_cache: dict[str, bool] = {}

    # -- Public call surface ---------------------------------------------------

    def call_analysis(self, prompt: str) -> str:
        """Full match analysis -> JSON prediction. Primary: DeepSeek."""
        return self._dispatch(prompt, task="analysis", timeout=self._timeout_analysis)

    def call_reasoning(self, prompt: str) -> str:
        """Step-by-step evidence reasoning (H2H, form, odds, similar matches)."""
        return self._dispatch(prompt, task="reasoning", timeout=self._timeout_reasoning)

    def call_review(self, messages: list[dict[str, str]]) -> str:
        """Supervisor / brain review using a messages list (system + user)."""
        return self._dispatch_messages(messages, task="review", timeout=self._timeout_review)

    def call_auto(self, prompt: str, task: str = "analysis") -> str:
        """Generic call -- task hint selects the primary model."""
        return self._dispatch(prompt, task=task, timeout=self._timeout_analysis)

    def call_pipeline(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Full small-context multi-stage pipeline prediction."""
        from app.ollama_pipeline import run_ollama_pipeline
        return run_ollama_pipeline(doc, attach_brain=True)

    def call_pipeline_batch(self, docs: list[dict[str, Any]], limit: int = 50) -> dict[str, Any]:
        """Batch pipeline predictions."""
        from app.ollama_pipeline import run_ollama_pipeline_batch
        return run_ollama_pipeline_batch(docs, limit=limit, attach_brain=True)

    # -- Availability helpers --------------------------------------------------

    def is_available(self, model: str) -> bool:
        if model not in self._availability_cache:
            self._availability_cache[model] = self._check_deepseek()
        return self._availability_cache[model]

    def is_pipeline_available(self) -> bool:
        return self.best_available() is not None

    def best_available(self) -> str | None:
        for entry in DEEPSEEK_MODELS:
            if self.is_available(entry["model"]):
                return entry["model"]
        return None

    def is_deepseek_available(self) -> bool:
        return self._check_deepseek()

    def any_available(self) -> bool:
        return self.best_available() is not None

    def status(self) -> dict[str, Any]:
        """Full availability status -- used by the API /ai/status endpoint."""
        models = []
        for entry in DEEPSEEK_MODELS:
            available = self.is_available(entry["model"])
            models.append({
                "model": entry["model"],
                "label": entry["label"],
                "strength": entry["strength"],
                "primary_for": entry["primary_for"],
                "available": available,
                "emoji": entry["emoji"],
            })
        return {
            "deepseek_models": models,
            "deepseek_available": self.best_available() is not None,
            "any_available": self.any_available(),
            "primary_model": self.best_available(),
            "chain": [e["model"] for e in DEEPSEEK_MODELS],
        }

    def invalidate_cache(self) -> None:
        """Force re-check on next call (useful after model changes)."""
        self._availability_cache.clear()

    # -- Dispatch internals ----------------------------------------------------

    def _ordered_chain(self, task: str) -> list[str]:
        return [e["model"] for e in DEEPSEEK_MODELS]

    def _dispatch(self, prompt: str, task: str, timeout: int) -> str:
        for model in self._ordered_chain(task):
            if not self.is_available(model):
                logger.debug("ai_router: deepseek unavailable, skipping")
                continue
            try:
                result = self._call_deepseek(prompt, timeout)
                logger.debug("ai_router: deepseek succeeded for task=%s", task)
                return result
            except Exception as exc:
                logger.warning("ai_router: deepseek failed for task=%s: %s", task, exc)
                self._availability_cache.pop(model, None)

        raise RuntimeError(f"ai_router: all providers exhausted for task={task}")

    def _dispatch_messages(self, messages: list[dict[str, str]], task: str, timeout: int) -> str:
        prompt = "\n\n".join(
            f"[{m.get('role', 'user').upper()}]\n{m.get('content', '')}"
            for m in messages
        )
        return self._dispatch(prompt, task=task, timeout=timeout)

    # -- Provider calls --------------------------------------------------------

    def _call_deepseek(self, prompt: str, timeout: int) -> str:
        from app.ollama_agent import _call_llm
        settings = get_settings()
        return _call_llm(settings.deepseek_model, prompt, timeout=timeout)

    @staticmethod
    def _check_deepseek() -> bool:
        try:
            settings = get_settings()
            return bool(settings.deepseek_api_key and settings.deepseek_api_key.strip())
        except Exception:
            return False


# -- JSON parsing helpers (shared across all callers) --------------------------

def parse_json_response(raw: str) -> dict[str, Any]:
    """
    Extract a JSON object from a model response.
    Strips <think>...</think> blocks (DeepSeek-R1) and markdown fences.
    Raises json.JSONDecodeError if no valid JSON found.
    """
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise json.JSONDecodeError("No JSON object found", text, 0)


def parse_json_safe(raw: str) -> dict[str, Any] | None:
    """Like parse_json_response but returns None instead of raising."""
    try:
        return parse_json_response(raw)
    except Exception:
        return None


# -- Module-level singleton ----------------------------------------------------

_router: AIRouter | None = None


def get_router() -> AIRouter:
    global _router
    if _router is None:
        _router = AIRouter()
    return _router
