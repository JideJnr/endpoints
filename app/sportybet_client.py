"""
SportyBet Client
----------------
Uses curl_cffi to impersonate a real Chrome browser at the TLS level.
This bypasses Cloudflare/bot-detection that blocks plain requests from
datacenter IPs (Render, Railway, etc.).

Strategy:
  1. curl_cffi with chrome124 impersonation — matches TLS fingerprint
  2. Realistic browser headers including sec-ch-ua, sec-fetch-* etc.
  3. Rotating User-Agent pool
  4. Session cookie warm-up (visit homepage first to get cookies)
  5. Exponential backoff retry on 403/429/5xx
  6. Jitter between requests to avoid rate-limit patterns
  7. Optional residential proxy via SPORTYBET_PROXY env var
     e.g. SPORTYBET_PROXY=http://user:pass@proxy-host:port
"""
from __future__ import annotations

import os
import random
import time
from typing import Any, Optional

try:
    from curl_cffi import requests as cffi_requests
    _USE_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    _USE_CFFI = False

# ── Constants ─────────────────────────────────────────────────────────────────

SPORTYBET_HOME_URL   = "https://www.sportybet.com/ng/"
SPORTYBET_POST_URL   = "https://www.sportybet.com/api/ng/factsCenter/wapConfigurableEventsByOrder"
SPORTYBET_RESULTS_URL = "https://www.sportybet.com/api/ng/factsCenter/eventResultList"

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

_IMPERSONATIONS = ["chrome124", "chrome123", "chrome120", "chrome110"]

# ── Session management ────────────────────────────────────────────────────────

_session: cffi_requests.Session | None = None
_session_warmed = False
_last_ua: str = _USER_AGENTS[0]


def _new_session() -> cffi_requests.Session:
    impersonate = random.choice(_IMPERSONATIONS) if _USE_CFFI else None
    if _USE_CFFI:
        session = cffi_requests.Session(impersonate=impersonate)
    else:
        session = cffi_requests.Session()
    session.trust_env = False
    # Optional residential proxy — set SPORTYBET_PROXY in Render env vars
    # e.g. http://user:pass@residential-proxy-host:port
    proxy = os.getenv("SPORTYBET_PROXY")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def _get_session() -> cffi_requests.Session:
    global _session, _session_warmed, _last_ua
    if _session is None:
        _session = _new_session()
        _session_warmed = False
    return _session


def _warm_session() -> None:
    """
    Visit the SportyBet homepage to pick up cookies and establish a
    realistic browsing session before hitting the API endpoints.
    Only runs once per session lifetime.
    """
    global _session_warmed
    if _session_warmed:
        return
    try:
        ua = random.choice(_USER_AGENTS)
        _last_ua = ua
        _get_session().get(
            SPORTYBET_HOME_URL,
            headers=_browser_headers(ua, referer="https://www.google.com/"),
            timeout=12,
        )
        _session_warmed = True
        time.sleep(random.uniform(0.4, 1.0))
    except Exception:
        _session_warmed = True  # don't retry warm-up on every call


def _browser_headers(ua: str | None = None, referer: str = "https://www.sportybet.com/ng/sport/football") -> dict:
    ua = ua or _last_ua
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer,
        "Origin": "https://www.sportybet.com",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Content-Type": "application/json",
    }


# ── Retry wrapper ─────────────────────────────────────────────────────────────

def _post_with_retry(url: str, payload: dict, max_retries: int = 4) -> dict:
    """
    POST with exponential backoff + session rotation on 403/429.
    On 403: rotate session (new TLS fingerprint + new UA) and retry.
    On 429: wait longer before retry.
    On 5xx: short wait and retry.
    """
    global _session, _session_warmed

    _warm_session()

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            ua = random.choice(_USER_AGENTS)
            headers = _browser_headers(ua)
            payload["_t"] = int(time.time() * 1000)

            response = _get_session().post(url, json=payload, headers=headers, timeout=20)

            if response.status_code == 403:
                # Rotate session entirely — new TLS fingerprint
                _session = _new_session()
                _session_warmed = False
                wait = 2 ** attempt + random.uniform(1, 3)
                print(f"[sportybet] 403 on attempt {attempt + 1}, rotating session, waiting {wait:.1f}s")
                time.sleep(wait)
                _warm_session()
                continue

            if response.status_code == 429:
                wait = 5 * (attempt + 1) + random.uniform(1, 4)
                print(f"[sportybet] 429 rate-limited on attempt {attempt + 1}, waiting {wait:.1f}s")
                time.sleep(wait)
                continue

            response.raise_for_status()
            return response.json()

        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait = 1.5 ** attempt + random.uniform(0.5, 2)
                time.sleep(wait)

    raise RuntimeError(
        f"SportyBet POST failed after {max_retries} attempts: {last_exc}"
    )


def _get_with_retry(url: str, params: dict, max_retries: int = 4) -> dict:
    """GET with same retry/rotation logic."""
    global _session, _session_warmed

    _warm_session()

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            ua = random.choice(_USER_AGENTS)
            headers = _browser_headers(ua)
            params["_t"] = int(time.time() * 1000)

            response = _get_session().get(url, params=params, headers=headers, timeout=20)

            if response.status_code == 403:
                _session = _new_session()
                _session_warmed = False
                wait = 2 ** attempt + random.uniform(1, 3)
                print(f"[sportybet] 403 on attempt {attempt + 1}, rotating session, waiting {wait:.1f}s")
                time.sleep(wait)
                _warm_session()
                continue

            if response.status_code == 429:
                wait = 5 * (attempt + 1) + random.uniform(1, 4)
                time.sleep(wait)
                continue

            response.raise_for_status()
            return response.json()

        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait = 1.5 ** attempt + random.uniform(0.5, 2)
                time.sleep(wait)

    raise RuntimeError(
        f"SportyBet GET failed after {max_retries} attempts: {last_exc}"
    )


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_matches_post(is_live: Optional[bool] = True) -> list[dict]:
    payload = {
        "sportId": "sr:sport:1",
        "pageSize": 300,
    }
    if is_live is not None:
        payload["isLive"] = is_live

    data = _post_with_retry(SPORTYBET_POST_URL, payload)
    tournaments = data.get("data", {}).get("tournaments", [])
    matches = []
    for tournament in tournaments:
        group = {
            "name": tournament.get("name"),
            "categoryName": (
                tournament.get("sport", {}).get("category", {}).get("name")
                or tournament.get("category", {}).get("name")
            ),
        }
        for event in tournament.get("events", []):
            matches.append(_parse_event(event, group))
    return matches


def fetch_live_matches_post() -> list[dict]:
    return fetch_matches_post(is_live=True)


def fetch_upcoming_matches_post() -> list[dict]:
    return fetch_matches_post(is_live=False)


def fetch_live_and_upcoming_matches_post() -> list[dict]:
    live = fetch_live_matches_post()
    upcoming = fetch_upcoming_matches_post()
    seen = {match.get("id") for match in live}
    return live + [match for match in upcoming if match.get("id") not in seen]


def fetch_match_info(match_id: str) -> dict[str, Any]:
    """
    Fetch one SportyBet event by id from the same API endpoint the live/upcoming
    pages use. This is endpoint-only lookup: no page HTML parsing.
    """
    match_id = str(match_id)
    calls = (
        ("live", True),
        ("upcoming", False),
        ("all", None),
    )
    errors: list[str] = []
    for scope, is_live in calls:
        try:
            matches = fetch_matches_post(is_live=is_live)
        except Exception as exc:
            errors.append(f"{scope}: {exc}")
            continue
        match = next((item for item in matches if str(item.get("id") or "") == match_id), None)
        if match:
            payload: dict[str, Any] = {"sportId": "sr:sport:1", "pageSize": 300}
            if is_live is not None:
                payload["isLive"] = is_live
            return {
                "found": True,
                "match_id": match_id,
                "scope": scope,
                "api_endpoint": SPORTYBET_POST_URL,
                "request_payload": payload,
                "match": match,
                "source": "sportybet_endpoint",
            }
    return {
        "found": False,
        "match_id": match_id,
        "scope": None,
        "api_endpoint": SPORTYBET_POST_URL,
        "request_payloads_tried": [
            {"sportId": "sr:sport:1", "pageSize": 300, **({} if is_live is None else {"isLive": is_live})}
            for _, is_live in calls
        ],
        "errors": errors,
        "source": "sportybet_endpoint",
    }


# Backward-compatible aliases for older routers/deploys.
def fetch_live_matches() -> list[dict]:
    return fetch_live_matches_post()


def fetch_upcoming_matches() -> list[dict]:
    return fetch_upcoming_matches_post()


def fetch_live_and_upcoming_matches() -> list[dict]:
    return fetch_live_and_upcoming_matches_post()


def fetch_results(start_time_ms: int, end_time_ms: int, count: int = 200, last_id: str = "") -> list[dict]:
    """Fetch finished match results from SportyBet for a time window."""
    params = {
        "count": count,
        "lastId": last_id,
        "sportId": "sr:sport:1",
        "startTime": start_time_ms,
        "endTime": end_time_ms,
    }
    data = _get_with_retry(SPORTYBET_RESULTS_URL, params)
    tournaments = data.get("data", {}).get("tournaments", [])
    results = []
    for tournament in tournaments:
        sport = tournament.get("events", [{}])[0].get("sport", {}) if tournament.get("events") else {}
        category = sport.get("category", {})
        group = {
            "name": category.get("tournament", {}).get("name") or tournament.get("name"),
            "categoryName": category.get("name"),
        }
        for event in tournament.get("events", []):
            results.append(_parse_result(event, group))
    return results


# ── Parsers ───────────────────────────────────────────────────────────────────

def _parse_result(event: dict, group: dict) -> dict:
    score_parts = (event.get("setScore") or "").split(":")
    home = int(score_parts[0]) if len(score_parts) == 2 and score_parts[0].isdigit() else None
    away = int(score_parts[1]) if len(score_parts) == 2 and score_parts[1].isdigit() else None
    event_category = (event.get("sport") or {}).get("category") or {}
    event_tournament = event_category.get("tournament") or {}
    return {
        "id":         event.get("eventId"),
        "name":       f"{event.get('homeTeamName')} vs {event.get('awayTeamName')}",
        "home_team":  event.get("homeTeamName"),
        "away_team":  event.get("awayTeamName"),
        "score":      {"home": home, "away": away},
        "period":     event.get("matchStatus"),
        "start_time": event.get("estimateStartTime"),
        "tournament": group.get("name") or event_tournament.get("name"),
        "category":   group.get("categoryName") or event_category.get("name"),
        "status":     event.get("status"),
    }


def _parse_event(event: dict, group: dict) -> dict:
    score_parts = (event.get("setScore") or "").split(":")
    home_score = score_parts[0] if len(score_parts) == 2 else None
    away_score = score_parts[1] if len(score_parts) == 2 else None
    event_category = (event.get("sport") or {}).get("category") or {}
    event_tournament = event_category.get("tournament") or {}

    return {
        "id":           event.get("eventId"),
        "name":         f"{event.get('homeTeamName')} vs {event.get('awayTeamName')}",
        "home_team":    event.get("homeTeamName"),
        "away_team":    event.get("awayTeamName"),
        "score":        {"home": home_score, "away": away_score},
        "period_scores": event.get("gameScore"),
        "period":       event.get("matchStatus"),
        "played_seconds": event.get("playedSeconds"),
        "status":       event.get("status"),
        "home_red_cards": _first_present(event, "homeRedCards", "homeRedCard", "homeTeamRedCards"),
        "away_red_cards": _first_present(event, "awayRedCards", "awayRedCard", "awayTeamRedCards"),
        "start_time":   event.get("estimateStartTime"),
        "tournament":   group.get("name") or event_tournament.get("name"),
        "category":     group.get("categoryName") or event_category.get("name"),
        "venue":        event.get("fixtureVenue", {}).get("name"),
        "markets":      [_parse_market(m) for m in event.get("markets", [])],
        "raw_event":    event,
        "raw_group":    group,
    }


def _parse_market(market: dict) -> dict:
    return {
        "id":       market.get("id"),
        "name":     market.get("desc"),
        "specifier": market.get("specifier"),
        "status":   market.get("status"),
        "group":    market.get("group"),
        "selections": [
            {
                "id":          o.get("id"),
                "name":        o.get("desc"),
                "odds":        o.get("odds"),
                "is_active":   o.get("isActive"),
                "probability": o.get("probability"),
            }
            for o in market.get("outcomes", [])
        ],
    }


def _first_present(source: dict, *keys: str):
    for key in keys:
        if key in source:
            return source[key]
    return None
