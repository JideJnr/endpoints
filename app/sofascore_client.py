import random
import time
from typing import Dict, List, Optional, Tuple

from curl_cffi import requests

SOFASCORE_TOURNAMENT_URL = "https://www.sofascore.com/api/v1/unique-tournament/{tournament_id}/scheduled-events/{date}"
SOFASCORE_ALL_URL = "https://www.sofascore.com/api/v1/sport/football/scheduled-events/{date}"
SOFASCORE_SEARCH_TOURNAMENT_URL = "https://www.sofascore.com/api/v1/search/all?q={query}"
SOFASCORE_EVENT_URL = "https://www.sofascore.com/api/v1/event/{event_id}"
SOFASCORE_SEARCH_URL = "https://www.sofascore.com/api/v1/search/all?q={query}"
SOFASCORE_LIVE_URL = "https://www.sofascore.com/api/v1/sport/football/events/live"
SOFASCORE_TEAM_HISTORY_URL = "https://www.sofascore.com/api/v1/team/{team_id}/events/last/{page}"
SOFASCORE_STANDINGS_URL = "https://www.sofascore.com/api/v1/tournament/{tournament_id}/season/{season_id}/standings/total"
SOFASCORE_H2H_URL = "https://www.sofascore.com/api/v1/event/{event_id}/h2h"
SOFASCORE_STATISTICS_URL = "https://www.sofascore.com/api/v1/event/{event_id}/statistics"
SOFASCORE_INCIDENTS_URL = "https://www.sofascore.com/api/v1/event/{event_id}/incidents"
SOFASCORE_LINEUPS_URL = "https://www.sofascore.com/api/v1/event/{event_id}/lineups"
SOFASCORE_PREGAME_FORM_URL = "https://www.sofascore.com/api/v1/event/{event_id}/pregame-form"
SOFASCORE_MANAGERS_URL = "https://www.sofascore.com/api/v1/event/{event_id}/managers"
SOFASCORE_FEATURED_PLAYERS_URL = "https://www.sofascore.com/api/v1/team/{team_id}/featured-players"
SOFASCORE_ODDS_URL = "https://www.sofascore.com/api/v1/event/{event_id}/odds/1/all"
SOFASCORE_ODDS_FEATURED_URL = "https://www.sofascore.com/api/v1/event/{event_id}/odds/1/featured"

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
}

_HOME_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ── List endpoint TTL cache ───────────────────────────────────────────────────
# Prevents hammering the same list endpoint repeatedly within a single scheduler
# cycle. The enrichment worker fires every 30 sec; without this cache, every
# cycle re-fetches the full scheduled-events list even when nothing changed.
#
# TTL rules:
#   - scheduled events (prematch): 60 seconds  — prematch lists barely change
#   - live events: 20 seconds                  — live list changes minute-by-minute
#
# Only the list endpoints are cached here. Detail endpoints (statistics, incidents,
# lineups) are NOT cached — they are always fetched fresh per match.

_LIST_CACHE_TTL_SCHEDULED = 60   # seconds
_LIST_CACHE_TTL_LIVE = 20        # seconds
_list_cache: Dict[str, Tuple[float, list]] = {}  # key → (fetched_at, data)


def _list_cache_get(key: str, ttl: float) -> Optional[list]:
    entry = _list_cache.get(key)
    if entry and (time.monotonic() - entry[0]) < ttl:
        return entry[1]
    return None


def _list_cache_set(key: str, data: list) -> None:
    _list_cache[key] = (time.monotonic(), data)

def _new_session() -> requests.Session:
    session = requests.Session(impersonate="chrome124")
    # Some Windows/dev environments set HTTP(S)_PROXY to 127.0.0.1:9.
    # SofaScore requests must bypass that or candidate scans fail before
    # reaching SofaScore at all.
    session.trust_env = False
    return session


_session = _new_session()

TERMINAL_STATUS_TYPES = {
    "abandoned",
    "awarded",
    "canceled",
    "cancelled",
    "finished",
    "interrupted",
    "postponed",
    "suspended",
    "walkover",
}
TERMINAL_STATUS_WORDS = (
    "abandoned",
    "awarded",
    "canceled",
    "cancelled",
    "ended",
    "finished",
    "interrupted",
    "postponed",
    "suspended",
    "walkover",
)
LIVE_STATUS_TYPES = {"inprogress", "live"}


def _status_text(event: dict) -> tuple[str, str]:
    status = event.get("status") or {}
    status_type = str(status.get("type") or "").lower().replace(" ", "").replace("_", "")
    description = str(status.get("description") or "").lower()
    return status_type, description


def is_terminal_event(event: dict) -> bool:
    """True for SofaScore events that should not be matched as upcoming/live."""
    status_type, description = _status_text(event)
    if status_type in TERMINAL_STATUS_TYPES:
        return True
    return any(word in description for word in TERMINAL_STATUS_WORDS)


def is_usable_event_for_mode(event: dict, live: bool = False) -> bool:
    """
    Gate SofaScore candidates by match mode.

    Prematch enrichment should only consider schedulable events. Live enrichment
    should only consider in-play events. Terminal states like postponed/canceled
    are excluded from both so stale SofaScore fixtures cannot be attached.
    """
    status_type, _ = _status_text(event)
    if is_terminal_event(event):
        return False
    if live:
        return status_type in LIVE_STATUS_TYPES
    return status_type not in LIVE_STATUS_TYPES


def _get(url: str) -> requests.Response:
    """Persistent session with cookie warm-up retry on failure."""
    global _session
    try:
        resp = _session.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp
    except Exception:
        # rebuild session, warm cookies via homepage, then retry once
        _session = _new_session()
        try:
            _session.get("https://www.sofascore.com/", headers=_HOME_HEADERS, timeout=10)
        except Exception:
            pass
        resp = _session.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp


def fetch_scheduled_events(date: str, tournament_id: int = 17) -> list[dict]:
    url = SOFASCORE_TOURNAMENT_URL.format(tournament_id=tournament_id, date=date)
    events = _get(url).json().get("events", [])
    return [_parse_event(e) for e in events]


# ── Curated top-league unique-tournament IDs ─────────────────────────────────
# These cover the leagues most commonly seen in SportyBet feeds.
# New IDs are auto-learned from matched buffer matches and persisted in SQLite.
_CORE_TOURNAMENT_IDS: Dict[int, str] = {
    17:    "Premier League",
    8:     "La Liga",
    23:    "Serie A",
    35:    "Bundesliga",
    34:    "Ligue 1",
    7:     "Champions League",
    679:   "Europa League",
    329:   "Conference League",
    37:    "Championship",
    44:    "Eredivisie",
    238:   "Primeira Liga",
    325:   "Super Lig",
    203:   "Saudi Pro League",
    955:   "MLS",
    242:   "Brasileirao",
    390:   "Argentine Primera",
    406:   "Liga MX",
    384:   "Premier League (old)",
    480:   "World Cup",
    679:   "Europa League",
    17015: "Africa Cup of Nations",
    11697: "AFCON Qualifying",
    16736: "Bolivia Division Profesional",
    1116:  "First League",
    41654: "South Australia State League 1",
    71304: "VPL 1",
    160606: "Calcutta Premier Division",
    173887: "U23 Victoria Premier League 1",
}


def _get_learned_tournament_ids() -> Dict[int, str]:
    """Load auto-learned tournament IDs from SQLite."""
    try:
        from app.league_memory import DB_PATH, _init_db
        import sqlite3 as _sqlite3
        _init_db()
        with _sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute("""
                create table if not exists sofa_tournament_ids (
                    tournament_id integer primary key,
                    name text,
                    last_seen text not null default current_timestamp
                )
            """)
            rows = conn.execute("select tournament_id, name from sofa_tournament_ids").fetchall()
            return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def learn_tournament_id(tournament_id: int, name: str) -> None:
    """Persist a newly discovered SofaScore unique-tournament ID."""
    if not tournament_id:
        return
    try:
        from app.league_memory import DB_PATH, _init_db
        import sqlite3 as _sqlite3
        _init_db()
        with _sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute("""
                create table if not exists sofa_tournament_ids (
                    tournament_id integer primary key,
                    name text,
                    last_seen text not null default current_timestamp
                )
            """)
            conn.execute("""
                insert into sofa_tournament_ids (tournament_id, name, last_seen)
                values (?, ?, current_timestamp)
                on conflict(tournament_id) do update set
                    name = excluded.name,
                    last_seen = current_timestamp
            """, (int(tournament_id), str(name or "")))
            conn.commit()
    except Exception:
        pass


def _fetch_tournament_events(tournament_id: int, date: str) -> list[dict]:
    """Fetch scheduled events for one unique-tournament ID, return [] on any error."""
    try:
        url = SOFASCORE_TOURNAMENT_URL.format(tournament_id=tournament_id, date=date)
        resp = _session.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return [_parse_event(e) for e in resp.json().get("events", [])]
    except Exception:
        return []


def fetch_all_scheduled_events(date: str) -> list[dict]:
    """Fetch scheduled events by tournament ID (global endpoint is dead).
    Merges core league IDs + auto-learned IDs from matched buffer matches.
    Fetches all in parallel, deduplicates by event ID.
    """
    cache_key = f"scheduled:{date}"
    cached = _list_cache_get(cache_key, _LIST_CACHE_TTL_SCHEDULED)
    if cached is not None:
        return cached

    # Try global endpoint first — works on some dates/regions
    try:
        url = SOFASCORE_ALL_URL.format(date=date)
        resp = _session.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 200:
            events = [_parse_event(e) for e in resp.json().get("events", [])]
            if events:
                _list_cache_set(cache_key, events)
                return events
    except Exception:
        pass

    # Global endpoint failed — fetch by tournament ID
    from concurrent.futures import ThreadPoolExecutor, as_completed
    all_ids: Dict[int, str] = {**_CORE_TOURNAMENT_IDS, **_get_learned_tournament_ids()}

    seen: set = set()
    events: list[dict] = []

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(_fetch_tournament_events, tid, date): tid for tid in all_ids}
        for future in as_completed(futures):
            for ev in future.result():
                eid = ev.get("id")
                if eid and eid not in seen:
                    seen.add(eid)
                    events.append(ev)
                    # auto-learn the tournament ID from each returned event
                    t = ev.get("tournament") or {}
                    tid = t.get("id")  # this is the unique-tournament id in parsed events
                    tname = t.get("name")
                    if tid and tid not in all_ids:
                        learn_tournament_id(tid, tname)

    _list_cache_set(cache_key, events)
    return events


def search_events(query: str, limit: int = 12) -> list[dict]:
    """Search SofaScore events when scheduled-events misses a fixture."""
    from urllib.parse import quote

    if not query.strip():
        return []
    url = SOFASCORE_SEARCH_URL.format(query=quote(query.strip()))
    results = _get(url).json().get("results", [])
    events = []
    seen = set()
    for item in results:
        if item.get("type") != "event":
            continue
        entity = item.get("entity") or {}
        event_id = entity.get("id")
        if not event_id or event_id in seen:
            continue
        seen.add(event_id)
        events.append(_parse_event(entity))
        if len(events) >= limit:
            break
    return events


def fetch_event(event_id) -> dict:
    """Fetch one SofaScore event directly by id."""
    url = SOFASCORE_EVENT_URL.format(event_id=event_id)
    event = _get(url).json().get("event", {})
    return _parse_event(event) if event else {}


def fetch_live_events() -> list[dict]:
    cache_key = "live"
    cached = _list_cache_get(cache_key, _LIST_CACHE_TTL_LIVE)
    if cached is not None:
        return cached
    events = [_parse_event(e) for e in _get(SOFASCORE_LIVE_URL).json().get("events", [])]
    _list_cache_set(cache_key, events)
    return events


def fetch_team_history(team_id: int, page: int = 0) -> dict:
    # ── Fix 1: serve from cache on page 0, bypass cache for page 1+ ────────────────────────
    if page == 0:
        try:
            from app.league_memory import get_cached_team_history, store_team_history
            cached = get_cached_team_history(team_id)
            if cached is not None:
                return {"has_next_page": True, "events": cached}
        except Exception:
            pass
    url = SOFASCORE_TEAM_HISTORY_URL.format(team_id=team_id, page=page)
    data = _get(url).json()
    events = [_parse_event(e) for e in data.get("events", [])]
    if page == 0 and events:
        try:
            from app.league_memory import store_team_history
            store_team_history(team_id, events)
        except Exception:
            pass
    return {
        "has_next_page": data.get("hasNextPage", False),
        "events": events,
    }


def fetch_standings(tournament_id: int, season_id: int) -> list[dict]:
    url = SOFASCORE_STANDINGS_URL.format(tournament_id=tournament_id, season_id=season_id)
    standings = _get(url).json().get("standings", [])
    if not standings:
        return []
    return [_parse_standing_row(row) for row in standings[0].get("rows", [])]


def fetch_event_detail(event: dict) -> dict:
    time.sleep(random.uniform(0.5, 1.5))  # jitter to avoid rate limiting
    event_id = event["id"]
    if not event.get("season_id") or not ((event.get("tournament") or {}).get("tournament_id")):
        fresh_event = fetch_event(event_id)
        if fresh_event:
            event = {**event, **{k: v for k, v in fresh_event.items() if v not in (None, {}, [])}}
    home_id = event["home_team"]["id"]
    away_id = event["away_team"]["id"]
    tournament_id = event["tournament"]["tournament_id"]
    season_id = event["season_id"]

    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception:
            return None

    return {
        **event,
        "h2h": safe(fetch_h2h, event_id),
        "statistics": safe(fetch_event_statistics, event_id),
        "incidents": safe(fetch_event_incidents, event_id),
        "lineups": safe(fetch_event_lineups, event_id),
        "pregame_form": safe(fetch_pregame_form, event_id),
        "managers": safe(fetch_managers, event_id),
        "home_last_matches": _team_history_events(home_id),
        "away_last_matches": _team_history_events(away_id),
        "home_featured_players": safe(fetch_featured_players, home_id),
        "away_featured_players": safe(fetch_featured_players, away_id),
        "odds_featured": safe(fetch_odds_featured, event_id),
        "standings": safe(fetch_standings, tournament_id, season_id),
    }


def fetch_event_detail_live_refresh(event_id: int, existing_detail: dict) -> dict:
    """
    Lightweight live refresh for an already-enriched match.

    Only fetches the three endpoints that change during a live match:
      - statistics  (possession, shots, xG, corners, attacks)
      - incidents   (goals, cards, substitutions)
      - lineups     (current XI + substitutions made)

    All static data (H2H, managers, team history, featured players, standings,
    pre-game form, odds) is preserved from existing_detail — no API call needed
    since they don't change during the match.

    This replaces the full fetch_event_detail call for live match re-enrichment,
    reducing per-cycle API calls from ~12 down to ~3 per live match.

    Returns the merged detail dict with live fields updated.
    """
    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception:
            return None

    live_stats = safe(fetch_event_statistics, event_id)
    live_incidents = safe(fetch_event_incidents, event_id)
    live_lineups = safe(fetch_event_lineups, event_id)

    # Merge: start from existing detail, overlay only the live-changing fields
    updated = dict(existing_detail)
    if live_stats is not None:
        updated["statistics"] = live_stats
    if live_incidents is not None:
        updated["incidents"] = live_incidents
    if live_lineups is not None:
        updated["lineups"] = live_lineups

    updated["live_refresh_at"] = time.time()
    return updated


def _team_history_events(team_id: Optional[int], pages: int = 2) -> list[dict]:
    """Return all available history pages without dropping page 0 when page 1 is absent."""
    if not team_id:
        return []
    events: list[dict] = []
    seen: set[str] = set()
    for page in range(max(1, pages)):
        try:
            data = fetch_team_history(team_id, page)
        except Exception:
            if page == 0:
                return events
            break
        page_events = data.get("events") or []
        for event in page_events:
            eid = str(event.get("id") or "")
            if eid and eid not in seen:
                events.append(event)
                seen.add(eid)
        if not data.get("has_next_page"):
            break
    return events


def _parse_standing_row(row: dict) -> dict:
    team = row.get("team", {})
    promotion = row.get("promotion", {})
    return {
        "position": row.get("position"),
        "team": {
            "id": team.get("id"),
            "name": team.get("name"),
            "short_name": team.get("shortName"),
            "code": team.get("nameCode"),
        },
        "played": row.get("matches"),
        "wins": row.get("wins"),
        "draws": row.get("draws"),
        "losses": row.get("losses"),
        "goals_for": row.get("scoresFor"),
        "goals_against": row.get("scoresAgainst"),
        "goal_diff": row.get("scoreDiffFormatted"),
        "points": row.get("points"),
        "promotion": promotion.get("text"),
    }


def fetch_h2h(event_id: int) -> dict:
    url = SOFASCORE_H2H_URL.format(event_id=event_id)
    d = _get(url).json()
    return {
        "team_duel": d.get("teamDuel"),
        "manager_duel": d.get("managerDuel"),
    }


def fetch_event_statistics(event_id: int) -> list[dict]:
    url = SOFASCORE_STATISTICS_URL.format(event_id=event_id)
    return _get(url).json().get("statistics", [])


def fetch_event_incidents(event_id: int) -> list[dict]:
    url = SOFASCORE_INCIDENTS_URL.format(event_id=event_id)
    return _get(url).json().get("incidents", [])


def fetch_event_lineups(event_id: int) -> dict:
    url = SOFASCORE_LINEUPS_URL.format(event_id=event_id)
    return _get(url).json()


def fetch_pregame_form(event_id: int) -> dict:
    url = SOFASCORE_PREGAME_FORM_URL.format(event_id=event_id)
    d = _get(url).json()
    return {
        "label": d.get("label"),
        "home_team": {
            "avg_rating": d.get("homeTeam", {}).get("avgRating"),
            "position": d.get("homeTeam", {}).get("position"),
            "value": d.get("homeTeam", {}).get("value"),
            "form": d.get("homeTeam", {}).get("form"),
        },
        "away_team": {
            "avg_rating": d.get("awayTeam", {}).get("avgRating"),
            "position": d.get("awayTeam", {}).get("position"),
            "value": d.get("awayTeam", {}).get("value"),
            "form": d.get("awayTeam", {}).get("form"),
        },
    }


def fetch_managers(event_id: int) -> dict:
    url = SOFASCORE_MANAGERS_URL.format(event_id=event_id)
    d = _get(url).json()
    return {
        "home_manager": _parse_manager(d.get("homeManager", {})),
        "away_manager": _parse_manager(d.get("awayManager", {})),
    }


def fetch_featured_players(team_id: int) -> list[dict]:
    url = SOFASCORE_FEATURED_PLAYERS_URL.format(team_id=team_id)
    featured = _get(url).json().get("featuredPlayers", {})
    seen = set()
    players = []
    for entry in featured.values():
        player = entry.get("player", {})
        pid = player.get("id")
        if pid not in seen:
            seen.add(pid)
            players.append({
                "id": pid,
                "name": player.get("name"),
                "short_name": player.get("shortName"),
                "position": player.get("position"),
                "jersey_number": player.get("jerseyNumber"),
                "rating": entry.get("statistics", {}).get("rating"),
            })
    return players


def fetch_odds(event_id: int) -> list[dict]:
    url = SOFASCORE_ODDS_URL.format(event_id=event_id)
    markets = _get(url).json().get("markets", [])
    return [_parse_odds_market(m) for m in markets]


def fetch_odds_featured(event_id: int) -> dict:
    url = SOFASCORE_ODDS_FEATURED_URL.format(event_id=event_id)
    d = _get(url).json()
    featured = d.get("featured", {})
    return {
        "has_more_odds": d.get("hasMoreOdds"),
        "default": _parse_odds_market(featured["default"]) if "default" in featured else None,
        "asian": _parse_odds_market(featured["asian"]) if "asian" in featured else None,
        "full_time": _parse_odds_market(featured["fullTime"]) if "fullTime" in featured else None,
    }


def _parse_manager(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "name": m.get("name"),
        "short_name": m.get("shortName"),
    }


def _parse_odds_market(m: dict) -> dict:
    return {
        "market_name": m.get("marketName"),
        "market_group": m.get("marketGroup"),
        "market_period": m.get("marketPeriod"),
        "suspended": m.get("suspended"),
        "is_live": m.get("isLive"),
        "choices": [
            {
                "name": c.get("name"),
                "fractional_value": c.get("fractionalValue"),
                "initial_fractional_value": c.get("initialFractionalValue"),
                "change": c.get("change"),
            }
            for c in m.get("choices", [])
        ],
    }


def _parse_event(e: dict) -> dict:
    home = e.get("homeTeam", {})
    away = e.get("awayTeam", {})
    home_score = e.get("homeScore", {})
    away_score = e.get("awayScore", {})
    status = e.get("status", {})
    tournament = e.get("tournament", {})
    season = e.get("season", {})
    venue = e.get("venue", {})

    return {
        "id": e.get("id"),
        "slug": e.get("slug"),
        "name": f"{home.get('name')} vs {away.get('name')}",
        "home_team": {
            "id": home.get("id"),
            "name": home.get("name"),
            "short_name": home.get("shortName"),
            "code": home.get("nameCode"),
        },
        "away_team": {
            "id": away.get("id"),
            "name": away.get("name"),
            "short_name": away.get("shortName"),
            "code": away.get("nameCode"),
        },
        "score": {
            "home": home_score.get("current"),
            "away": away_score.get("current"),
            "home_ht": home_score.get("period1"),
            "away_ht": away_score.get("period1"),
        },
        "status": {
            "code": status.get("code"),
            "description": status.get("description"),
            "type": status.get("type"),
        },
        "home_red_cards": e.get("homeRedCards") or e.get("homeRedCard") or e.get("homeTeamRedCards"),
        "away_red_cards": e.get("awayRedCards") or e.get("awayRedCard") or e.get("awayTeamRedCards"),
        "tournament": {
            "id": tournament.get("uniqueTournament", {}).get("id"),
            "tournament_id": tournament.get("id"),
            "name": tournament.get("name"),
        },
        "season": season.get("name"),
        "season_id": season.get("id"),
        "round": e.get("roundInfo", {}).get("round"),
        "venue": venue.get("name"),
        "start_timestamp": e.get("startTimestamp"),
        "winner_code": e.get("winnerCode"),
        "raw_event": e,
    }
