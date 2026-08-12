"""
primitives.py
~~~~~~~~~~~~~
Consolidated primitive type-conversion and safe-parsing helpers.

Single source of truth for:
  - _to_int, _to_float, _safe_float, _safe_num
  - _safe_json, _loads
  - _optional_int, _first_present

All modules should import from here instead of defining local copies.
"""
from __future__ import annotations

import json
from typing import Any


# ── Numeric conversion ────────────────────────────────────────────────────────


def _to_int(value: Any, default: int = 0) -> int:
    """Convert *value* to int, returning *default* on failure.

    Handles string floats (``"3.7"`` → ``3``) as well as plain ints.
    """
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float | None:
    """Convert *value* to float, returning ``None`` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    """Convert *value* to float, returning ``None`` for ``None`` / ``""`` / failure."""
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _safe_num(value: Any) -> float | int:
    """Convert *value* to a number (float preferred, int fallback), returning ``0`` on failure."""
    try:
        return float(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    """Convert *value* to int, returning ``None`` for ``None`` / ``""``."""
    if value is None or value == "":
        return None
    return _to_int(value, 0)


# ── JSON / serialisation ──────────────────────────────────────────────────────


def _safe_json(value: Any, fallback: Any) -> Any:
    """Parse *value* as JSON, returning *fallback* on any failure."""
    try:
        return json.loads(value or json.dumps(fallback))
    except Exception:
        return fallback


def _loads(value: Any, default: Any) -> Any:
    """Parse *value* as JSON, returning *default* on any failure."""
    try:
        return json.loads(value or "")
    except Exception:
        return default


# ── First-present helpers ─────────────────────────────────────────────────────


def _first_present(*values: Any) -> Any:
    """Return the first non-``None`` / non-``""`` value from *values*."""
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_present_key(mapping: dict[str, Any], *keys: str) -> Any:
    """Return the first non-``None`` value for *keys* from *mapping*."""
    for key in keys:
        if isinstance(mapping, dict) and mapping.get(key) is not None:
            return mapping.get(key)
    return None
