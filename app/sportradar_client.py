from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

try:
    from curl_cffi import requests as http_requests
    _USE_CFFI = True
except ImportError:
    import requests as http_requests  # type: ignore
    _USE_CFFI = False


SPORTRADAR_ALIAS = os.getenv("SPORTRADAR_WIDGET_ALIAS", "sportybet2")
SPORTRADAR_LANGUAGE = os.getenv("SPORTRADAR_WIDGET_LANGUAGE", "en")
SPORTRADAR_S5_BASE = os.getenv("SPORTRADAR_S5_BASE", "https://s5.sir.sportradar.com")
SPORTRADAR_WIDGET_CLIENT_ID = os.getenv(
    "SPORTRADAR_WIDGET_CLIENT_ID",
    "638846b93b23ecfc94ce1a6d45b1dbe6",
)
SPORTRADAR_TIMEOUT_SECONDS = float(os.getenv("SPORTRADAR_TIMEOUT_SECONDS", "4"))
SPORTRADAR_MAX_RESPONSE_BYTES = int(os.getenv("SPORTRADAR_MAX_RESPONSE_BYTES", "300000"))


def fetch_match_intelligence(match_id: Any, *, include_standings: bool = True) -> dict[str, Any]:
    """
    Fetch the Sportradar/SIR widget data SportyBet uses for match stats.

    This is an auxiliary provider. It must never block normal enrichment:
    callers should treat available=false as a provider miss, not a hard failure.
    """
    normalized_id = normalize_match_id(match_id)
    fetched_at = datetime.now(timezone.utc).isoformat()
    if not normalized_id:
        return {
            "source": "sportradar_sir_widget",
            "available": False,
            "match_id": None,
            "fetched_at": fetched_at,
            "error": "missing_match_id",
            "attempts": [],
        }

    match_url = _s5_url(f"match/{normalized_id}")
    result = _fetch_json(match_url)
    detail: dict[str, Any] = {
        "source": "sportradar_sir_widget",
        "provider_alias": SPORTRADAR_ALIAS,
        "client_id": SPORTRADAR_WIDGET_CLIENT_ID,
        "match_id": normalized_id,
        "available": bool(result.get("ok")),
        "fetched_at": fetched_at,
        "endpoint": match_url,
        "attempts": [result],
    }

    if not result.get("ok"):
        detail["error"] = result.get("error") or result.get("status")
        return detail

    payload = result.get("data")
    detail["match"] = payload
    detail["summary"] = summarize_match_payload(payload)

    season_id = _find_first(payload, ("_seasonid", "seasonId", "season_id", "season.id", "season._id"))
    if include_standings and season_id is not None:
        standings_url = _s5_url(f"season/{season_id}/standings")
        standings_result = _fetch_json(standings_url)
        detail["attempts"].append(standings_result)
        if standings_result.get("ok"):
            detail["standings"] = standings_result.get("data")
            detail["summary"]["has_standings"] = True
        else:
            detail["standings_error"] = standings_result.get("error") or standings_result.get("status")

    return detail


def normalize_match_id(match_id: Any) -> str:
    value = str(match_id or "").strip()
    if value.startswith("sr:match:"):
        value = value.rsplit(":", 1)[-1]
    return value


def summarize_match_payload(payload: Any) -> dict[str, Any]:
    keys = set(_walk_keys(payload, max_depth=4))
    return {
        "has_payload": payload is not None,
        "top_level_keys": sorted(payload.keys())[:40] if isinstance(payload, dict) else [],
        "has_h2h": any("h2h" in key.lower() or "headtohead" in key.lower() for key in keys),
        "has_form": any("form" in key.lower() or "lastmatch" in key.lower() for key in keys),
        "has_lineups": any("lineup" in key.lower() for key in keys),
        "has_statistics": any("stat" in key.lower() for key in keys),
        "has_standings": any("standing" in key.lower() or "table" in key.lower() for key in keys),
        "has_probability": any("probab" in key.lower() for key in keys),
    }


def _s5_url(postfix: str) -> str:
    return f"{SPORTRADAR_S5_BASE.rstrip('/')}/{SPORTRADAR_ALIAS}/{SPORTRADAR_LANGUAGE}/{postfix.lstrip('/')}"


def _fetch_json(url: str) -> dict[str, Any]:
    try:
        kwargs: dict[str, Any] = {
            "headers": _headers(),
            "timeout": SPORTRADAR_TIMEOUT_SECONDS,
        }
        if _USE_CFFI:
            kwargs["impersonate"] = "chrome124"
        response = http_requests.get(url, **kwargs)
        text = response.text[:SPORTRADAR_MAX_RESPONSE_BYTES]
        item: dict[str, Any] = {
            "url": url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "bytes": len(response.content or b""),
            "ok": 200 <= response.status_code < 300,
        }
        if not item["ok"]:
            item["body_preview"] = text[:500]
            return item
        try:
            item["data"] = response.json()
        except Exception:
            item["data"] = json.loads(text)
        return item
    except Exception as exc:
        return {
            "url": url,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.sportybet.com/ng/m/sport/football/",
        "Origin": "https://www.sportybet.com",
    }


def _find_first(payload: Any, paths: tuple[str, ...]) -> Any:
    for path in paths:
        value = _path_get(payload, path)
        if value is not None:
            return value
    return None


def _path_get(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _walk_keys(value: Any, *, max_depth: int, prefix: str = "") -> list[str]:
    if max_depth <= 0:
        return []
    if isinstance(value, dict):
        keys: list[str] = []
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            keys.append(name)
            keys.extend(_walk_keys(child, max_depth=max_depth - 1, prefix=name))
        return keys
    if isinstance(value, list):
        keys = []
        for child in value[:5]:
            keys.extend(_walk_keys(child, max_depth=max_depth - 1, prefix=prefix))
        return keys
    return []
