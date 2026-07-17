from __future__ import annotations

import logging
import threading
from collections import Counter
from typing import Any


_LOGGER = logging.getLogger("predictx.health")
_LOCK = threading.Lock()
_COUNTERS: Counter[str] = Counter()


def record_health_event(component: str, event: str, exc: Exception | None = None, **context: Any) -> None:
    key = f"{component}.{event}"
    with _LOCK:
        _COUNTERS[key] += 1
    message = f"{key}: {exc}" if exc else key
    _LOGGER.warning(message, extra={"component": component, "event": event, "context": context})


def health_counter_snapshot() -> dict[str, int]:
    with _LOCK:
        return dict(_COUNTERS)
