import time
import requests
from typing import Optional

SPORTYBET_URL = "https://www.sportybet.com/api/ng/factsCenter/configurableLiveOrPrematchEvents"
SPORTYBET_POST_URL = "https://www.sportybet.com/api/ng/factsCenter/wapConfigurableEventsByOrder"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120",
    "Accept": "application/json",
    "Referer": "https://www.sportybet.com/ng/sport/live",
}

POST_HEADERS = {
    **HEADERS,
    "Content-Type": "application/json",
    "Origin": "https://www.sportybet.com",
}


def fetch_live_matches() -> list[dict]:
    params = {
        "sportId": "sr:sport:1",
        "withTwoUpMarket": "true",
        "withOneUpMarket": "true",
        "_t": int(time.time() * 1000),
    }
    response = requests.get(SPORTYBET_URL, params=params, headers=HEADERS, timeout=10)
    response.raise_for_status()
    groups = response.json().get("data", [])
    matches = []
    for group in groups:
        for event in group.get("events", []):
            matches.append(_parse_event(event, group))
    return matches


def fetch_matches_post(is_live: Optional[bool] = True) -> list[dict]:
    payload = {
        "sportId": "sr:sport:1",
        "pageSize": 300,
        "_t": int(time.time() * 1000),
    }
    if is_live is not None:
        payload["isLive"] = is_live
    response = requests.post(SPORTYBET_POST_URL, json=payload, headers=POST_HEADERS, timeout=15)
    response.raise_for_status()
    tournaments = response.json().get("data", {}).get("tournaments", [])
    matches = []
    for tournament in tournaments:
        group = {
            "name": tournament.get("name"),
            "categoryName": tournament.get("sport", {}).get("category", {}).get("name"),
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


def _parse_event(event: dict, group: dict) -> dict:
    score_parts = (event.get("setScore") or "").split(":")
    home_score = score_parts[0] if len(score_parts) == 2 else None
    away_score = score_parts[1] if len(score_parts) == 2 else None

    return {
        "id": event.get("eventId"),
        "name": f"{event.get('homeTeamName')} vs {event.get('awayTeamName')}",
        "home_team": event.get("homeTeamName"),
        "away_team": event.get("awayTeamName"),
        "score": {"home": home_score, "away": away_score},
        "period_scores": event.get("gameScore"),
        "period": event.get("matchStatus"),
        "played_seconds": event.get("playedSeconds"),
        "status": event.get("status"),
        "home_red_cards": _first_present(event, "homeRedCards", "homeRedCard", "homeTeamRedCards"),
        "away_red_cards": _first_present(event, "awayRedCards", "awayRedCard", "awayTeamRedCards"),
        "start_time": event.get("estimateStartTime"),
        "tournament": group.get("name"),
        "category": group.get("categoryName"),
        "venue": event.get("fixtureVenue", {}).get("name"),
        "markets": [_parse_market(m) for m in event.get("markets", [])],
        "raw_event": event,
        "raw_group": group,
    }


def _parse_market(market: dict) -> dict:
    return {
        "id": market.get("id"),
        "name": market.get("desc"),
        "specifier": market.get("specifier"),
        "status": market.get("status"),
        "group": market.get("group"),
        "selections": [
            {
                "id": o.get("id"),
                "name": o.get("desc"),
                "odds": o.get("odds"),
                "is_active": o.get("isActive"),
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
