"""
AI Router
---------
Single source of truth for all LLM dispatch in PredictX.

Priority chain
~~~~~~~~~~~~~~
  1. OpenRouter Pipeline — small-context multi-stage pipeline (form, H2H, odds,
                           standings, models specialists → final synthesis)
  2. OpenRouter free model — primary for match analysis, general reasoning, JSON output
  3. Rules engine         — deterministic fallback, never fails

Each call type declares which model is its *primary* so the router dispatches
to the right strength first, then cascades down the chain automatically.

Usage
~~~~~
    from app.ai.ai_router import AIRouter

    router = AIRouter()

    # Match analysis (JSON prediction) — pipeline primary, then OpenRouter
    text = router.call_analysis(prompt)

    # Step reasoning (H2H, form, evidence) — OpenRouter primary
    text = router.call_reasoning(prompt)

    # Supervisor review (compact JSON) — OpenRouter primary
    text = router.call_review(messages)

    # Full pipeline prediction — uses small-context multi-stage approach
    result = router.call_pipeline(doc)

    # Availability checks
    model = router.best_available()          # first ready model name or None
    router.is_available("openrouter/free")  # True/False
    router.status()                          # full status dict for the API
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config.config import get_settings

logger = logging.getLogger(__name__)

# ── Model catalogue ────────────────────────────────────────────────────────

# Ordered by preference. The router walks this list top-to-bottom.
OPENROUTER_MODELS: list[dict[str, Any]] = [
    {
        "model": "openrouter/free",
        "label": "OpenRouter Free",
        "strength": "general",
        "primary_for": ["analysis", "review", "supervisor", "pipeline"],
        "emoji": "🥇",
    },
]

# Call types that prefer openrouter as primary
_OPENROUTER_PRIMARY_TASKS = {"reasoning", "h2h", "form", "evidence", "step"}


class AIRouter:
    """
    Centralised AI dispatcher.

    Instantiate once per request or reuse as a module-level singleton.
    All availability checks are lazy — nothing is imported until a call is made.
    """

    def __init__(self, timeout_analysis: int = 30, timeout_reasoning: int = 30, timeout_review: int = 30) -> None:
        self._timeout_analysis = timeout_analysis
        self._timeout_reasoning = timeout_reasoning
        self._timeout_review = timeout_review
        self._availability_cache: dict[str, bool] = {}
        self._last_provider: str | None = None

    # ── Public call surface ────────────────────────────────────────────────

    def call_analysis(self, prompt: str) -> str:
        """
        Full match analysis → JSON prediction.
        Primary: OpenRouter Pipeline  →  OpenRouter free model
        """
        # Try pipeline first if we have a doc context (passed via prompt prefix)
        pipeline_result = self._try_pipeline_from_prompt(prompt)
        if pipeline_result is not None:
            return pipeline_result
        return self._dispatch(prompt, task="analysis", timeout=self._timeout_analysis)

    def call_reasoning(self, prompt: str) -> str:
        """
        Step-by-step evidence reasoning (H2H, form, odds, similar matches).
        Primary: OpenRouter free model
        """
        return self._dispatch(prompt, task="reasoning", timeout=self._timeout_reasoning)

    def call_review(self, messages: list[dict[str, str]]) -> str:
        """
        Supervisor / brain review using a messages list (system + user).
        Primary: OpenRouter free model
        """
        return self._dispatch_messages(messages, task="review", timeout=self._timeout_review)

    def call_auto(self, prompt: str, task: str = "analysis") -> str:
        """Generic call — task hint selects the primary model."""
        return self._dispatch(prompt, task=task, timeout=self._timeout_analysis)

    def last_provider(self) -> str | None:
        """Return the provider used by the most recent successful dispatch."""
        return self._last_provider

    def call_pipeline(self, doc: dict[str, Any]) -> dict[str, Any]:
        """
        Full small-context multi-stage OpenRouter pipeline prediction.
        Returns a prediction dict (not just text) with specialist results.
        """
        from app.ai.ollama_pipeline import run_ollama_pipeline
        return run_ollama_pipeline(doc, attach_brain=True)

    def call_pipeline_batch(self, docs: list[dict[str, Any]], limit: int = 50) -> dict[str, Any]:
        """
        Batch small-context pipeline predictions.
        """
        from app.ai.ollama_pipeline import run_ollama_pipeline_batch
        return run_ollama_pipeline_batch(docs, limit=limit, attach_brain=True)

    # ── Availability helpers ───────────────────────────────────────────────

    def is_available(self, model: str) -> bool:
        if model not in self._availability_cache:
            self._availability_cache[model] = self._check_openrouter(model)
        return self._availability_cache[model]

    def is_pipeline_available(self) -> bool:
        """Check if the small-context pipeline is available (OpenRouter key set)."""
        return self.best_available() is not None

    def best_available(self) -> str | None:
        """Return the name of the first ready OpenRouter model, or None."""
        for entry in OPENROUTER_MODELS:
            if self.is_available(entry["model"]):
                return entry["model"]
        return None

    def any_available(self) -> bool:
        return self.best_available() is not None

    def status(self) -> dict[str, Any]:
        """Full availability status — used by the API /ai/status endpoint."""
        models = []
        for entry in OPENROUTER_MODELS:
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
            "openrouter_models": models,
            "openrouter_pipeline_available": self.is_pipeline_available(),
            "any_available": self.any_available(),
            "primary_model": self.best_available(),
            "chain": [e["model"] for e in OPENROUTER_MODELS],
        }

    def invalidate_cache(self) -> None:
        """Force re-check on next call (useful after model changes)."""
        self._availability_cache.clear()

    # ── Dispatch internals ─────────────────────────────────────────────────

    def _ordered_chain(self, task: str) -> list[str]:
        """
        Return model names in dispatch order for this task type.
        """
        chain = [e["model"] for e in OPENROUTER_MODELS]
        return chain

    def _try_pipeline_from_prompt(self, prompt: str) -> str | None:
        """
        If the prompt contains a JSON doc prefix (sent by openrouter_agent),
        try running the full pipeline instead of a single large-context call.
        Returns the pipeline result as a JSON string, or None if pipeline not applicable.
        """
        # Check if prompt starts with a JSON object (doc summary format)
        prompt_stripped = prompt.strip()
        if not prompt_stripped.startswith("{"):
            return None

        try:
            # Try to parse the prefix as JSON to extract doc info
            # The prompt format is: SYSTEM_PROMPT + "\n\n" + doc_summary
            # We look for the doc summary part
            parts = prompt.split("\n\n", 1)
            if len(parts) < 2:
                return None

            doc_summary = parts[1]
            # Try to extract key fields from the summary
            doc = {}
            for line in doc_summary.split("\n"):
                if "=" in line:
                    key, _, value = line.partition("=")
                    doc[key.strip()] = value.strip()

            # Only use pipeline if we have minimal match info
            if not doc.get("sportybet_name") and not doc.get("name"):
                return None

            # Run pipeline
            result = self.call_pipeline(doc)
            if result.get("status") == "predicted":
                return json.dumps(result.get("reasoning") or result)
            return None
        except Exception:
            return None

    def _dispatch(self, prompt: str, task: str, timeout: int) -> str:
        # Try pipeline first for analysis tasks if doc context is present
        if task == "analysis":
            pipeline_result = self._try_pipeline_from_prompt(prompt)
            if pipeline_result is not None:
                return pipeline_result

        for model in self._ordered_chain(task):
            if not self.is_available(model):
                logger.debug("ai_router: %s unavailable, skipping", model)
                continue
            try:
                result = self._call_openrouter(model, prompt, timeout)
                self._last_provider = "openrouter"
                logger.debug("ai_router: %s succeeded for task=%s", model, task)
                return result
            except Exception as exc:
                logger.warning("ai_router: %s failed for task=%s: %s", model, task, exc)
                # Invalidate so next call re-checks availability
                self._availability_cache.pop(model, None)

        raise RuntimeError(f"ai_router: all providers exhausted for task={task}")

    def _dispatch_messages(self, messages: list[dict[str, str]], task: str, timeout: int) -> str:
        """Dispatch a messages list (system+user) — converts to prompt for OpenRouter."""
        # Build a flat prompt from messages for OpenRouter (which uses chat completions)
        prompt = "\n\n".join(
            f"[{m.get('role', 'user').upper()}]\n{m.get('content', '')}"
            for m in messages
        )
        for model in self._ordered_chain(task):
            if not self.is_available(model):
                continue
            try:
                result = self._call_openrouter(model, prompt, timeout)
                self._last_provider = "openrouter"
                logger.debug("ai_router: %s succeeded for task=%s (messages)", model, task)
                return result
            except Exception as exc:
                logger.warning("ai_router: %s failed for task=%s: %s", model, task, exc)
                self._availability_cache.pop(model, None)

        raise RuntimeError(f"ai_router: all providers exhausted for task={task}")

    # ── Provider calls ─────────────────────────────────────────────────────

    def _call_openrouter(self, model: str, prompt: str, timeout: int) -> str:
        from app.ai.ollama_agent import _call_llm
        return _call_llm(model, prompt, timeout=timeout)

    @staticmethod
    def _check_openrouter(model: str) -> bool:
        try:
            from app.ai.ollama_agent import is_ollama_available
            return is_ollama_available(model)
        except Exception:
            return False


# ── JSON parsing helpers (shared across all callers) ──────────────────────────

def parse_json_response(raw: str) -> dict[str, Any]:
    """
    Extract a JSON object from a model response.
    Strips </think>...</think> blocks (OpenRouter) and markdown fences.
    Raises json.JSONDecodeError if no valid JSON found.
    """
    # Strip OpenRouter chain-of-thought blocks
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

