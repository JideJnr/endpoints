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
import time
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError

from app.config.config import get_settings

logger = logging.getLogger(__name__)

# How long an availability check result is trusted before re-checking.
# Asymmetric on purpose: a model that just proved itself healthy doesn't need
# re-pinging every call, but a model that failed should be retried soon --
# previously a single bad/slow ping at process start-up (a 5s-timeout ping to
# OpenRouter's free tier, which is often slow or rate-limited) cached
# "unavailable" with NO expiry, so the LLM was silently skipped for the
# entire remaining lifetime of the process. That's the bug this fixes.
_AVAILABILITY_TTL_OK_SECONDS = 300.0
_AVAILABILITY_TTL_FAIL_SECONDS = 60.0

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
        self._availability_cache: dict[str, tuple[bool, float]] = {}
        self._last_provider: str | None = None

    # ── Public call surface ────────────────────────────────────────────────

    def call_analysis(self, prompt: str) -> str:
        """
        Full match analysis → JSON prediction.
        Primary: OpenRouter free model
        """
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
        from app.ai.llm_pipeline import run_llm_pipeline
        return run_llm_pipeline(doc, attach_brain=True)

    def call_pipeline_batch(self, docs: list[dict[str, Any]], limit: int = 50) -> dict[str, Any]:
        """
        Batch small-context pipeline predictions.
        """
        from app.ai.llm_pipeline import run_llm_pipeline_batch
        return run_llm_pipeline_batch(docs, limit=limit, attach_brain=True)

    # ── Availability helpers ───────────────────────────────────────────────

    def is_available(self, model: str) -> bool:
        cached = self._availability_cache.get(model)
        now = time.monotonic()
        if cached is not None:
            result, checked_at = cached
            ttl = _AVAILABILITY_TTL_OK_SECONDS if result else _AVAILABILITY_TTL_FAIL_SECONDS
            if now - checked_at < ttl:
                return result
        result = self._check_openrouter(model)
        self._availability_cache[model] = (result, now)
        return result

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

    def _dispatch(self, prompt: str, task: str, timeout: int) -> str:
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
        from app.ai.ai_router import _call_llm
        return _call_llm(model, prompt, timeout=timeout)

    @staticmethod
    def _check_openrouter(model: str) -> bool:
        try:
            from app.ai.ai_router import is_llm_available
            return is_llm_available(model)
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


# ── Core LLM call (single source of truth) ────────────────────────────────────


def _openrouter_url() -> str:
    settings = get_settings()
    return settings.openrouter_base_url.rstrip("/")


def _bounded(fn, *args, timeout: float, **kwargs):
    """
    Run fn(*args, **kwargs) with a hard wall-clock deadline.

    Why this exists: `urllib.request.urlopen(req, timeout=N)` only bounds the
    socket connect/read calls. DNS resolution (getaddrinfo) happens BEFORE the
    socket exists and is not covered by that timeout at all in the stdlib --
    on a flaky resolver (observed in practice on Windows) a call can hang far
    past its stated timeout with no way to interrupt it from inside the call
    itself. This previously took down the whole app for 4+ minutes from a
    single stuck OpenRouter request (see /matches/{id}/ai-analysis incident).

    Running the call in its own single-worker executor lets the CALLER give up
    after `timeout` seconds and raise, even if the underlying call is still
    stuck. The stuck thread itself can't be force-killed (Python limitation)
    and keeps running in the background until it eventually finishes or the
    process exits, but it no longer blocks this request or anything else.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except _FutureTimeoutError as exc:
            raise TimeoutError(f"{getattr(fn, '__name__', fn)} exceeded {timeout}s deadline") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _do_ping(url: str, payload: bytes, headers: dict, model: str | None) -> bool:
    req = urllib_request.Request(url, data=payload, headers=headers, method="POST")
    with urllib_request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if model:
        return True
    return bool(data.get("choices"))


def is_llm_available(model: str | None = None) -> bool:
    """Check if OpenRouter is reachable (API key is set)."""
    settings = get_settings()
    if not settings.openrouter_api_key:
        return False
    try:
        url = _openrouter_url() + "/chat/completions"
        payload = json.dumps({
            "model": settings.openrouter_model,
            "messages": [{"role": "user", "content": "ping"}],
            "temperature": 0,
            "max_tokens": 1,
        }).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "HTTP-Referer": "https://predictx.app",
            "X-Title": "PredictX",
        }
        # Hard 15s wall-clock deadline (12s urlopen timeout + margin) even if
        # DNS resolution itself is what's stuck.
        return _bounded(_do_ping, url, payload, headers, model, timeout=15)
    except Exception:
        return False


def _do_llm_call(url: str, payload: bytes, headers: dict, timeout: int) -> str:
    req = urllib_request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {body[:300]}") from exc
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def _call_llm(model: str, prompt: str, timeout: int = 60) -> str:
    """Call OpenRouter chat completions (OpenAI-compatible)."""
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set in .env")

    url = _openrouter_url() + "/chat/completions"
    payload = json.dumps({
        "model": settings.openrouter_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "HTTP-Referer": "https://predictx.app",
        "X-Title": "PredictX",
    }
    # Give the DNS-hang guard a few seconds of margin over the caller's own
    # urlopen timeout so a normal slow-but-legitimate response isn't cut off
    # early by the outer deadline.
    return _bounded(_do_llm_call, url, payload, headers, timeout, timeout=timeout + 10)


# ── Module-level singleton ─────────────────────────────────────────────────────
# Import and reuse this instead of constructing a new AIRouter each call.

_router: AIRouter | None = None


def get_router() -> AIRouter:
    global _router
    if _router is None:
        _router = AIRouter()
    return _router

