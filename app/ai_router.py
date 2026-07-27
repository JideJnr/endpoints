"""
AI Router
---------
Single source of truth for all LLM dispatch in PredictX.

Priority chain
~~~~~~~~~~~~~~
  1. qwen3:8b       — primary for match analysis, general reasoning, JSON output
  2. deepseek-r1:8b — primary for deep step-by-step reasoning tasks (H2H, form,
                      evidence chains); fallback for qwen3 when unavailable
  3. Groq           — cloud fallback when both local models are unavailable
                      (rate-limited; used only as last resort)
  4. Rules engine   — deterministic fallback, never fails

Each call type declares which model is its *primary* so the router dispatches
to the right strength first, then cascades down the chain automatically.

Usage
~~~~~
    from app.ai_router import AIRouter

    router = AIRouter()

    # Match analysis (JSON prediction) — qwen3 primary
    text = router.call_analysis(prompt)

    # Step reasoning (H2H, form, evidence) — deepseek primary
    text = router.call_reasoning(prompt)

    # Supervisor review (compact JSON) — qwen3 primary
    text = router.call_review(messages)

    # Availability checks
    model = router.best_available()          # first ready model name or None
    router.is_available("qwen3:8b")          # True/False
    router.status()                          # full status dict for the API
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── Model catalogue ────────────────────────────────────────────────────────────

# Ordered by preference. The router walks this list top-to-bottom.
OLLAMA_CHAIN: list[dict[str, Any]] = [
    {
        "model": "qwen3:8b",
        "label": "Qwen3 8B",
        "strength": "general",
        "primary_for": ["analysis", "review", "supervisor"],
        "emoji": "🥇",
    },
    {
        "model": "deepseek-r1:8b",
        "label": "DeepSeek-R1 8B",
        "strength": "reasoning",
        "primary_for": ["reasoning", "h2h", "form", "evidence"],
        "emoji": "🥈",
    },
]

# Call types that prefer deepseek as primary
_DEEPSEEK_PRIMARY_TASKS = {"reasoning", "h2h", "form", "evidence", "step"}


class AIRouter:
    """
    Centralised AI dispatcher.

    Instantiate once per request or reuse as a module-level singleton.
    All availability checks are lazy — nothing is imported until a call is made.
    """

    def __init__(self, timeout_analysis: int = 60, timeout_reasoning: int = 30, timeout_review: int = 20) -> None:
        self._timeout_analysis = timeout_analysis
        self._timeout_reasoning = timeout_reasoning
        self._timeout_review = timeout_review
        self._availability_cache: dict[str, bool] = {}

    # ── Public call surface ────────────────────────────────────────────────────

    def call_analysis(self, prompt: str) -> str:
        """
        Full match analysis → JSON prediction.
        Primary: qwen3:8b  →  deepseek-r1:8b  →  Groq
        """
        return self._dispatch(prompt, task="analysis", timeout=self._timeout_analysis)

    def call_reasoning(self, prompt: str) -> str:
        """
        Step-by-step evidence reasoning (H2H, form, odds, similar matches).
        Primary: deepseek-r1:8b  →  qwen3:8b  →  Groq
        """
        return self._dispatch(prompt, task="reasoning", timeout=self._timeout_reasoning)

    def call_review(self, messages: list[dict[str, str]]) -> str:
        """
        Supervisor / brain review using a messages list (system + user).
        Primary: qwen3:8b  →  deepseek-r1:8b  →  Groq
        """
        return self._dispatch_messages(messages, task="review", timeout=self._timeout_review)

    def call_auto(self, prompt: str, task: str = "analysis") -> str:
        """Generic call — task hint selects the primary model."""
        return self._dispatch(prompt, task=task, timeout=self._timeout_analysis)

    # ── Availability helpers ───────────────────────────────────────────────────

    def is_available(self, model: str) -> bool:
        if model not in self._availability_cache:
            self._availability_cache[model] = self._check_ollama(model)
        return self._availability_cache[model]

    def best_available(self) -> str | None:
        """Return the name of the first ready Ollama model, or None."""
        for entry in OLLAMA_CHAIN:
            if self.is_available(entry["model"]):
                return entry["model"]
        return None

    def is_groq_available(self) -> bool:
        if "groq" not in self._availability_cache:
            try:
                from app.llm import is_groq_available
                self._availability_cache["groq"] = is_groq_available()
            except Exception:
                self._availability_cache["groq"] = False
        return self._availability_cache["groq"]

    def any_available(self) -> bool:
        return self.best_available() is not None or self.is_groq_available()

    def status(self) -> dict[str, Any]:
        """Full availability status — used by the API /ai/status endpoint."""
        models = []
        for entry in OLLAMA_CHAIN:
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
            "ollama_models": models,
            "groq_available": self.is_groq_available(),
            "any_available": self.any_available(),
            "primary_model": self.best_available(),
            "chain": [e["model"] for e in OLLAMA_CHAIN] + (["groq"] if self.is_groq_available() else []),
        }

    def invalidate_cache(self) -> None:
        """Force re-check on next call (useful after ollama pull)."""
        self._availability_cache.clear()

    # ── Dispatch internals ─────────────────────────────────────────────────────

    def _ordered_chain(self, task: str) -> list[str]:
        """
        Return model names in dispatch order for this task type.
        deepseek-primary tasks swap the first two entries.
        """
        chain = [e["model"] for e in OLLAMA_CHAIN]
        if task in _DEEPSEEK_PRIMARY_TASKS and len(chain) >= 2:
            chain[0], chain[1] = chain[1], chain[0]
        return chain

    def _dispatch(self, prompt: str, task: str, timeout: int) -> str:
        for model in self._ordered_chain(task):
            if not self.is_available(model):
                logger.debug("ai_router: %s unavailable, skipping", model)
                continue
            try:
                result = self._call_ollama(model, prompt, timeout)
                logger.debug("ai_router: %s succeeded for task=%s", model, task)
                return result
            except Exception as exc:
                logger.warning("ai_router: %s failed for task=%s: %s", model, task, exc)
                # Invalidate so next call re-checks availability
                self._availability_cache.pop(model, None)

        # Groq final fallback
        if self.is_groq_available():
            try:
                result = self._call_groq(prompt, timeout)
                logger.debug("ai_router: groq succeeded for task=%s", task)
                return result
            except Exception as exc:
                logger.warning("ai_router: groq failed for task=%s: %s", task, exc)
                self._availability_cache.pop("groq", None)

        raise RuntimeError(f"ai_router: all providers exhausted for task={task}")

    def _dispatch_messages(self, messages: list[dict[str, str]], task: str, timeout: int) -> str:
        """Dispatch a messages list (system+user) — converts to prompt for Ollama."""
        # Build a flat prompt from messages for Ollama (which uses /api/generate)
        prompt = "\n\n".join(
            f"[{m.get('role', 'user').upper()}]\n{m.get('content', '')}"
            for m in messages
        )
        for model in self._ordered_chain(task):
            if not self.is_available(model):
                continue
            try:
                result = self._call_ollama(model, prompt, timeout)
                logger.debug("ai_router: %s succeeded for task=%s (messages)", model, task)
                return result
            except Exception as exc:
                logger.warning("ai_router: %s failed for task=%s: %s", model, task, exc)
                self._availability_cache.pop(model, None)

        # Groq handles messages natively
        if self.is_groq_available():
            try:
                result = self._call_groq_messages(messages, timeout)
                logger.debug("ai_router: groq succeeded for task=%s (messages)", task)
                return result
            except Exception as exc:
                logger.warning("ai_router: groq failed for task=%s: %s", task, exc)
                self._availability_cache.pop("groq", None)

        raise RuntimeError(f"ai_router: all providers exhausted for task={task}")

    # ── Provider calls ─────────────────────────────────────────────────────────

    def _call_ollama(self, model: str, prompt: str, timeout: int) -> str:
        from app.ollama_agent import _call_ollama
        return _call_ollama(model, prompt, timeout=timeout)

    def _call_groq(self, prompt: str, timeout: int) -> str:
        from app.llm import get_llm
        response = get_llm().invoke([{"role": "user", "content": prompt}])
        return str(response.content if hasattr(response, "content") else response).strip()

    def _call_groq_messages(self, messages: list[dict[str, str]], timeout: int) -> str:
        from app.llm import get_llm
        response = get_llm().invoke(messages)
        return str(response.content if hasattr(response, "content") else response).strip()

    @staticmethod
    def _check_ollama(model: str) -> bool:
        try:
            from app.ollama_agent import is_ollama_available
            return is_ollama_available(model)
        except Exception:
            return False


# ── JSON parsing helpers (shared across all callers) ──────────────────────────

def parse_json_response(raw: str) -> dict[str, Any]:
    """
    Extract a JSON object from a model response.
    Strips <think>...</think> blocks (DeepSeek-R1) and markdown fences.
    Raises json.JSONDecodeError if no valid JSON found.
    """
    # Strip DeepSeek-R1 chain-of-thought blocks
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # Strip markdown fences
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Extract first {...} block
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


# ── Module-level singleton ─────────────────────────────────────────────────────
# Import and reuse this instead of constructing a new AIRouter each call.

_router: AIRouter | None = None


def get_router() -> AIRouter:
    global _router
    if _router is None:
        _router = AIRouter()
    return _router
