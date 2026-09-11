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

from app.utils.primitives import _first_present_key as _first_present

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
# Desktop ("pc") per-competition events feed -- discovered via live network
# inspection of sportybet.com's own competition filter UI. Unlike
# SPORTYBET_POST_URL (a site-wide "top ~300 by order" homepage feed that
# only surfaces popular leagues), this takes an explicit tournamentId and
# returns that competition's own full fixture list. Confirmed live: the
# tournamentId values SportyBet uses here are the same raw Sportradar
# tournament numbers already stored as unique_tournament_id in
# competition_special.TOP_30_COMPETITIONS (e.g. sr:tournament:17 ==
# Premier League on both SofaScore and SportyBet) -- no name/date
# guessing needed for these curated competitions.
SPORTYBET_PC_EVENTS_URL = "https://www.sportybet.com/api/ng/factsCenter/pcEvents"
# Per-team endpoints -- discovered from sportybet.com's mobile team page
# (e.g. /ng/m/team-pages/sr:sport:1/sr:competitor:2814/Football/Espanyol/Spain).
# competitor_id is SportyBet/Sportradar's own team id -- NOT confirmed to
# equal SofaScore's team id. Any caller that wants to use a SofaScore team
# id here must verify the returned team name matches before trusting it.
SPORTYBET_TEAM_INFO_URL      = "https://www.sportybet.com/api/ng/factsCenter/teams/sr:competitor:{competitor_id}"
SPORTYBET_TEAM_FIXTURES_URL  = "https://www.sportybet.com/api/ng/factsCenter/teams/sr:competitor:{competitor_id}/fixtures"
SPORTYBET_TEAM_RESULTS_URL   = "https://www.sportybet.com/api/ng/factsCenter/teams/sr:competitor:{competitor_id}/results"
SPORTYBET_TEAM_NEWS_URL      = "https://www.sportybet.com/api/ng/factsCenter/teams/sr:competitor:{competitor_id}/news"

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

    The homepage visit is done in a background thread so it never blocks
    the main scheduler job. If it fails, we still proceed with the API call
    (the session may work without cookies on some requests).
    """
    global _session_warmed
    if _session_warmed:
        return
    # Mark as warmed immediately so concurrent callers don't all try at once.
    # The background thread will finish the actual warmup.
    _session_warmed = True

    import threading

    def _do_warm() -> None:
        try:
            ua = random.choice(_USER_AGENTS)
            _get_session().get(
                SPORTYBET_HOME_URL,
                headers=_browser_headers(ua, referer="https://www.google.com/"),
                timeout=8,
            )
            time.sleep(random.uniform(0.2, 0.5))
        except Exception:
            pass  # warmup failure is non-fatal

    threading.Thread(target=_do_warm, daemon=True).start()


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

def _post_with_retry(url: str, payload: dict, max_retries: int = 4, timeout: int = 20) -> dict:
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

            response = _get_session().post(url, json=payload, headers=headers, timeout=timeout)

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


def _post_json_with_retry(url: str, payload: list | dict, max_retries: int = 4, timeout: int = 20) -> dict:
    """Same retry/rotation logic as _post_with_retry, for endpoints (like
    pcEvents) whose body is a JSON array rather than a dict -- _t can't be
    injected as a dict key on a list payload, and the live-captured
    requests for this endpoint never send a _t cache-buster anyway."""
    global _session, _session_warmed

    _warm_session()

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            ua = random.choice(_USER_AGENTS)
            headers = _browser_headers(ua)

            response = _get_session().post(url, json=payload, headers=headers, timeout=timeout)

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
        f"SportyBet POST (json) failed after {max_retries} attempts: {last_exc}"
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


# ── List result cache ─────────────────────────────────────────────────────────
# Prevents the scheduler from hammering SportyBet when a job fires faster than
# the API can respond. The live ingest runs every 30s but the HTTP call alone
# can take 8-20s when rate-limited. Without a cache, the next run starts a
# fresh call that overlaps with the previous one.
#
# TTL rules:
#   live (isLive=True):     20 seconds — fast enough to see score changes
#   upcoming (isLive=False): 90 seconds — upcoming list barely changes between calls
#   all (isLive=None):       45 seconds — mixed mode
#
# The cache lives in-process so it resets on restart (safe, no stale data risk).
_MATCH_LIST_CACHE_TTL = {
    True:  20,   # live
    False: 90,   # upcoming
    None:  45,   # all
}
_match_list_cache: dict[str | bool | None, tuple[float, list]] = {}


def _match_list_cache_get(key: bool | None) -> list | None:
    entry = _match_list_cache.get(key)
    if entry and (time.monotonic() - entry[0]) < _MATCH_LIST_CACHE_TTL.get(key, 45):
        return entry[1]
    return None


def _match_list_cache_set(key: bool | None, data: list) -> None:
    _match_list_cache[key] = (time.monotonic(), data)


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_matches_post(is_live: Optional[bool] = True, bypass_cache: bool = False) -> list[dict]:
    # ── Return cached result if fresh enough ──────────────────────────────────
    # List endpoints (live/upcoming) don't need a fresh HTTP call every time the
    # scheduler fires. The cache TTL is short enough to not miss meaningful changes.
    if not bypass_cache:
        cached = _match_list_cache_get(is_live)
        if cached is not None:
            return cached

    payload = {
        "sportId": "sr:sport:1",
        "pageSize": 300,
    }
    if is_live is not None:
        payload["isLive"] = is_live

    # Use 2 retries + 8s timeout for list fetches (not detail).
    # Worst case: 2 × (8s + backoff) ≈ 20s max — well within the 30s scheduler interval.
    # Previously: 4 retries × 20s timeout = up to 97s, causing job overlap.
    data = _post_with_retry(SPORTYBET_POST_URL, payload, max_retries=2, timeout=8)
    result = parse_events_response(data)
    _match_list_cache_set(is_live, result)
    return result


def parse_events_response(data: dict[str, Any]) -> list[dict]:
    """Normalize legacy and current SportyBet event envelopes."""
    payload = data.get("data", data) if isinstance(data, dict) else {}
    if not isinstance(payload, dict):
        payload = {"events": payload} if isinstance(payload, list) else {}
    tournaments = payload.get("tournaments") or payload.get("tournamentList") or payload.get("eventGroups") or []
    if isinstance(tournaments, dict):
        tournaments = list(tournaments.values())
    matches = []
    for tournament in tournaments:
        if not isinstance(tournament, dict):
            continue
        group = {
            "name": tournament.get("name") or tournament.get("tournamentName"),
            "categoryName": (
                tournament.get("sport", {}).get("category", {}).get("name")
                or tournament.get("category", {}).get("name")
                or tournament.get("categoryName")
            ),
        }
        for event in tournament.get("events") or tournament.get("eventList") or []:
            matches.append(_parse_event(event, group))
    for event in payload.get("events") or payload.get("eventList") or []:
        if not isinstance(event, dict):
            continue
        sport = event.get("sport") or {}
        category = sport.get("category") or event.get("category") or {}
        matches.append(_parse_event(event, {
            "name": (category.get("tournament") or {}).get("name") or event.get("tournamentName"),
            "categoryName": category.get("name") or event.get("categoryName"),
        }))
    return matches


_TOURNAMENT_EVENTS_CACHE_TTL = 60
_tournament_events_cache: dict[str, tuple[float, list]] = {}


def fetch_tournament_events(
    tournament_id: str | int,
    market_ids: str = "1,18,10,29,11,26,36,14",
    bypass_cache: bool = False,
) -> list[dict]:
    """Fetch every SportyBet event for ONE competition via the pcEvents
    endpoint, keyed by the raw Sportradar tournament id (e.g. 17 for
    Premier League) -- the same unique_tournament_id already stored in
    competition_special.TOP_30_COMPETITIONS. No name/date matching needed
    to find the right competition; unlike fetch_matches_post's site-wide
    ~300-event feed, this returns the competition's own full list so niche
    curated leagues aren't starved by more popular ones crowding them out.
    """
    tid = str(tournament_id)
    cache_key = f"{tid}:{market_ids}"
    if not bypass_cache:
        cached = _tournament_events_cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < _TOURNAMENT_EVENTS_CACHE_TTL:
            return cached[1]

    payload = [{
        "sportId": "sr:sport:1",
        "marketId": market_ids,
        "tournamentId": [[f"sr:tournament:{tid}"]],
    }]
    data = _post_json_with_retry(SPORTYBET_PC_EVENTS_URL, payload, max_retries=2, timeout=8)
    tournaments = data.get("data") if isinstance(data, dict) else None
    matches: list[dict] = []
    if isinstance(tournaments, list):
        for tournament in tournaments:
            if not isinstance(tournament, dict):
                continue
            group = {"name": tournament.get("name"), "categoryName": None}
            for event in tournament.get("events") or []:
                parsed = _parse_event(event, group)
                # pcEvents nests the real category (country) under each
                # event's own sport.category, not on the tournament group.
                if not parsed.get("category"):
                    event_category = ((event.get("sport") or {}).get("category") or {})
                    parsed["category"] = event_category.get("name")
                parsed["sofascore_id"] = None  # never assume identity across providers
                matches.append(parsed)
    _tournament_events_cache[cache_key] = (time.monotonic(), matches)
    return matches


def fetch_team_info(competitor_id: str | int) -> dict[str, Any]:
    """Raw SportyBet team profile for sr:competitor:<competitor_id>.

    competitor_id here is SportyBet/Sportradar's own team id. It is NOT
    confirmed to equal SofaScore's team id for the same team -- callers
    using a SofaScore id as a guess must verify the returned team name
    before trusting any data keyed off it.
    """
    url = SPORTYBET_TEAM_INFO_URL.format(competitor_id=competitor_id)
    try:
        data = _get_with_retry(url, {}, max_retries=2)
    except Exception:
        return {}
    return data.get("data") or {} if isinstance(data, dict) else {}


def _unwrap_team_list(payload: Any) -> list[dict]:
    """Unwrap the nested shapes SportyBet's team endpoints use.

    Confirmed live via curl (2026-09-06) for /teams/{id}/fixtures: the
    outer "data" is {"flag", "hasNextPage", "data": [...]} -- the actual
    match list sits under a SECOND nested "data" key, not "events"/"list"/
    "posts" as originally guessed. Checking all of them keeps this working
    if /results or /news turn out to use a different one of those names.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "events", "list", "posts"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return inner
    return []


def _parse_team_fixture(item: dict) -> dict:
    """Normalize one /teams/{id}/fixtures or /results entry.

    Confirmed live via curl (2026-09-06): this endpoint's match shape is
    flat fixtureXxx fields plus nested homeTeam/awayTeam/tournament dicts
    -- NOT the same shape _parse_event() expects from pcEvents /
    wapConfigurableEventsByOrder, so it needs its own normalizer.
    """
    home_team = item.get("homeTeam") or {}
    away_team = item.get("awayTeam") or {}
    tournament = item.get("tournament") or {}
    home_name = item.get("fixtureHomeTeamName") or home_team.get("name")
    away_name = item.get("fixtureAwayTeamName") or away_team.get("name")
    return {
        "id": item.get("eventId"),
        "name": f"{home_name} vs {away_name}",
        "home_team": home_name,
        "away_team": away_name,
        "team_ids": {
            "home": item.get("fixtureHomeTeamId") or home_team.get("id"),
            "away": item.get("fixtureAwayTeamId") or away_team.get("id"),
        },
        "tournament": item.get("fixtureTournamentName") or tournament.get("name"),
        "tournament_id": item.get("fixtureTournamentId") or tournament.get("id"),
        "start_time": item.get("fixtureStartTime"),
        "status": item.get("eventStatus"),
        "match_status": item.get("eventMatchStatus"),
        "score": item.get("eventScore"),
        "raw_event": item,
    }


def _team_endpoint(
    url_template: str,
    competitor_id: str | int,
    tournament_ids: list[str] | None,
    size: int,
    parser: Any = None,
) -> list[dict]:
    url = url_template.format(competitor_id=competitor_id)
    params: dict[str, Any] = {"size": size}
    if tournament_ids:
        params["tournamentIds"] = ",".join(f"sr:tournament:{t}" if not str(t).startswith("sr:") else str(t) for t in tournament_ids)
    try:
        data = _get_with_retry(url, params, max_retries=2)
    except Exception:
        return []
    if isinstance(data, dict) and data.get("bizCode") not in (None, 10000):
        # Confirmed live via curl (2026-09-06): SportyBet rejects /fixtures
        # and /results calls that omit tournamentIds with HTTP 200 +
        # bizCode 19000 "Invalid", data: {} -- NOT an empty-but-valid
        # result. Without this check that looked identical to "team has
        # no fixtures/results" to every caller, since _get_with_retry only
        # raises on HTTP-level errors and treats this as a normal 200.
        print(f"[sportybet] team endpoint rejected ({url}): bizCode={data.get('bizCode')} msg={data.get('message')}")
        return []
    payload = data.get("data") if isinstance(data, dict) else None
    items = _unwrap_team_list(payload)
    if parser:
        return [parser(item) for item in items if isinstance(item, dict)]
    return items


def fetch_team_fixtures(competitor_id: str | int, tournament_ids: list[str] | None = None, size: int = 10) -> list[dict]:
    """Upcoming matches for one SportyBet team id -- see fetch_team_info's
    caveat about competitor_id not being a confirmed SofaScore-id match.

    tournament_ids is effectively REQUIRED, not optional: confirmed live
    via curl (2026-09-06) that SportyBet rejects this call with bizCode
    19000 "Invalid" when it's omitted (e.g. GET .../fixtures?size=10 alone
    fails; adding &tournamentIds=sr:tournament:17 succeeds). Callers must
    pass the specific tournament(s) they care about."""
    return _team_endpoint(SPORTYBET_TEAM_FIXTURES_URL, competitor_id, tournament_ids, size, parser=_parse_team_fixture)


def fetch_team_results(competitor_id: str | int, tournament_ids: list[str] | None = None, size: int = 10) -> list[dict]:
    """Past match results for one SportyBet team id.

    Same tournament_ids requirement as fetch_team_fixtures -- confirmed
    live via curl (2026-09-06): omitting it returns bizCode 19000
    "Invalid", not an empty-but-valid result."""
    return _team_endpoint(SPORTYBET_TEAM_RESULTS_URL, competitor_id, tournament_ids, size, parser=_parse_team_fixture)


def _parse_team_news_item(item: dict) -> dict:
    """Normalize one /teams/{id}/news entry.

    Confirmed live via curl (2026-09-06): every sampled item was source
    ``video`` -- a highlight-clip caption from a match already played, NOT
    a text injury/transfer article. Several were tagged to this team only
    because it was the opponent in that clip's match (e.g. a Real Madrid
    player quote tagged "Espanyol" because Real Madrid played Espanyol).
    ``tagged_teams`` is kept so callers can see every team a clip
    references, not just assume it's really about the team it was fetched
    for -- feeding this to an LLM without that context risks it reading a
    stray opponent soundbite as this team's own squad news.
    """
    tags = item.get("tags") or {}
    return {
        "id": item.get("id"),
        "source_type": item.get("source"),  # e.g. "video" -- not text-article by default
        "title": item.get("title") or item.get("headline"),
        "description": item.get("description"),
        "byline": item.get("byLine"),
        "published_time": item.get("publishedTime"),  # epoch ms
        "tagged_teams": [t.get("name") for t in (tags.get("team") or []) if isinstance(t, dict) and t.get("name")],
        "tagged_league": [t.get("name") for t in (tags.get("league") or []) if isinstance(t, dict) and t.get("name")],
        "slug": item.get("slug"),
        "raw_item": item,
    }


def fetch_team_news(competitor_id: str | int, size: int = 10) -> list[dict]:
    """Team news/highlight items from SportyBet's own team page.

    Raw provider content, mostly video-clip captions about matches already
    played rather than forward-looking injury/lineup news -- see
    _parse_team_news_item's docstring. Callers feeding this to the LLM must
    label it as unverified reported content, note it may be about an
    opponent rather than this team, and never treat it as established fact
    (see app/enrichment/team_news.py)."""
    return _team_endpoint(SPORTYBET_TEAM_NEWS_URL, competitor_id, None, size, parser=_parse_team_news_item)


def fetch_live_matches_post() -> list[dict]:
    return fetch_matches_post(is_live=True)


def fetch_upcoming_matches_post() -> list[dict]:
    return fetch_matches_post(is_live=False)


def fetch_live_and_upcoming_matches_post() -> list[dict]:
    live = fetch_live_matches_post()
    upcoming = fetch_upcoming_matches_post()
    seen = {match.get("id") for match in live}
    return live + [match for match in upcoming if match.get("id") not in seen]


def fetch_match_info(match_id: str, bypass_cache: bool = False) -> dict[str, Any]:
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
            matches = fetch_matches_post(is_live=is_live, bypass_cache=bypass_cache)
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
        "id":         event.get("eventId") or event.get("id"),
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
    home_team = event.get("homeTeam") or {}
    away_team = event.get("awayTeam") or {}
    home_name = event.get("homeTeamName") or home_team.get("name")
    away_name = event.get("awayTeamName") or away_team.get("name")
    score_value = event.get("setScore") or event.get("score") or ""
    if isinstance(score_value, dict):
        home_score = score_value.get("home") or score_value.get("homeScore")
        away_score = score_value.get("away") or score_value.get("awayScore")
    else:
        score_parts = str(score_value).split(":")
        home_score = score_parts[0] if len(score_parts) == 2 else None
        away_score = score_parts[1] if len(score_parts) == 2 else None
    event_category = (event.get("sport") or {}).get("category") or {}
    event_tournament = event_category.get("tournament") or {}

    return {
        "id":           event.get("eventId") or event.get("id"),
        "name":         f"{home_name} vs {away_name}",
        "home_team":    home_name,
        "away_team":    away_name,
        "score":        {"home": home_score, "away": away_score},
        "period_scores": event.get("gameScore") or event.get("periodScores"),
        "period":       event.get("matchStatus") or event.get("period"),
        "played_seconds": event.get("playedSeconds"),
        "status":       event.get("status"),
        "home_red_cards": _first_present(event, "homeRedCards", "homeRedCard", "homeTeamRedCards"),
        "away_red_cards": _first_present(event, "awayRedCards", "awayRedCard", "awayTeamRedCards"),
        "start_time":   event.get("estimateStartTime") or event.get("startTime"),
        "tournament":   group.get("name") or event_tournament.get("name"),
        "category":     group.get("categoryName") or event_category.get("name"),
        "venue":        event.get("fixtureVenue", {}).get("name"),
        "team_ids": {
            "home": event.get("homeTeamId") or home_team.get("id"),
            "away": event.get("awayTeamId") or away_team.get("id"),
        },
        "team_icons": {
            "home": event.get("homeTeamIcon"),
            "away": event.get("awayTeamIcon"),
        },
        "sporty_metadata": {
            "game_id": event.get("gameId"),
            "event_source": event.get("eventSource") or {},
            "booking_status": event.get("bookingStatus"),
            "product_status": event.get("productStatus"),
            "banned": bool(event.get("banned")),
            "market_count": len(event.get("markets") or event.get("marketList") or []),
            "comments_count": event.get("commentsNum"),
            "match_tracker_available": not bool(event.get("matchTrackerNotAllowed")),
            "top_team": bool(event.get("topTeam")),
            "topic_id": event.get("topicId"),
        },
        "markets":      [_parse_market(m) for m in event.get("markets") or event.get("marketList") or []],
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
        "group_id": market.get("groupId"),
        "title": market.get("title"),
        "guide": market.get("marketGuide"),
        "is_banned": bool(market.get("banned")),
        "available_score": market.get("availableScore"),
        "last_odds_change_time": market.get("lastOddsChangeTime"),
        "early_payout_markets": market.get("earlyPayoutMarkets") or [],
        "selections": [
            {
                "id":          o.get("id"),
                "name":        o.get("desc"),
                "odds":        o.get("odds"),
                "is_active":   o.get("isActive"),
                "probability": o.get("probability"),
                "void_probability": o.get("voidProbability"),
            }
            for o in market.get("outcomes") or market.get("selections") or []
        ],
    }


