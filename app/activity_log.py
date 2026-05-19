from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any


_MAX_EVENTS = 120
_events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
_lock = Lock()
_current: dict[str, Any] = {
    "status": "idle",
    "message": "System is idle",
    "job": "idle",
    "updated_at": datetime.now(timezone.utc).isoformat(),
}


def record_activity(
    message: str,
    *,
    job: str = "system",
    status: str = "info",
    match_id: str | None = None,
    match_name: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a tiny in-process activity event for the settings dashboard."""
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "job": job,
        "status": status,
        "message": message,
        "match_id": match_id,
        "match_name": match_name,
        "details": details or {},
    }
    with _lock:
        _events.appendleft(event)
        global _current
        _current = event
    return event


def mark_idle(message: str = "System is idle") -> None:
    record_activity(message, job="idle", status="idle")


def get_activity(limit: int = 30) -> dict[str, Any]:
    limit = max(1, min(int(limit or 30), _MAX_EVENTS))
    with _lock:
        return {
            "current": dict(_current),
            "events": list(_events)[:limit],
        }
