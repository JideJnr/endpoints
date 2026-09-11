"""
Shared helper for giving a call a hard wall-clock deadline when the call
has no reliable timeout of its own.

Why this exists: something like `urllib.request.urlopen(req, timeout=N)`
only bounds the socket connect/read calls -- DNS resolution (getaddrinfo)
happens BEFORE the socket exists and isn't covered by that timeout at all
in the stdlib. On a flaky resolver a call can hang far past its stated
timeout with no way to interrupt it from inside the call itself. This is
exactly what took the whole app down for 4+ minutes from a single stuck
OpenRouter request (see app/ai/ai_router.py::_bounded, written for the
/matches/{id}/ai-analysis incident). Promoted here 2026-09-09 after the
same class of bug was found hanging the live bet-builder endpoints
(app/bet_builder/core.py, app/bet_builder/live_builder.py) -- "no response
at all" for a live bet request, root cause was an unbounded wait inside
the request's own live-data refresh, not an actual error.
"""
from __future__ import annotations

from typing import Any, Callable, TypeVar

T = TypeVar("T")


def bounded_call(fn: Callable[..., T], *args: Any, timeout: float, **kwargs: Any) -> T:
    """
    Run fn(*args, **kwargs) with a hard wall-clock deadline.

    Running the call in its own single-worker executor lets the CALLER give
    up after `timeout` seconds and raise TimeoutError, even if the
    underlying call is still stuck. The stuck thread itself can't be
    force-killed (a Python limitation) and keeps running in the background
    until it eventually finishes or the process exits, but it no longer
    blocks this request -- or anything else waiting on this call.
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
