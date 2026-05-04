from curl_cffi import requests

SOFASCORE_URL = "https://www.sofascore.com/api/v1/unique-tournament/{tournament_id}/scheduled-events/{date}"

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def fetch_scheduled_events(date: str, tournament_id: int = 17) -> list[dict]:
    url = SOFASCORE_URL.format(tournament_id=tournament_id, date=date)
    response = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=10)
    response.raise_for_status()
    events = response.json().get("events", [])
    return [_parse_event(e) for e in events]


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
        "tournament": {
            "id": tournament.get("uniqueTournament", {}).get("id"),
            "name": tournament.get("name"),
        },
        "season": season.get("name"),
        "round": e.get("roundInfo", {}).get("round"),
        "venue": venue.get("name"),
        "start_timestamp": e.get("startTimestamp"),
        "winner_code": e.get("winnerCode"),
    }
