from curl_cffi import requests

SOFASCORE_TOURNAMENT_URL = "https://www.sofascore.com/api/v1/unique-tournament/{tournament_id}/scheduled-events/{date}"
SOFASCORE_ALL_URL = "https://www.sofascore.com/api/v1/sport/football/scheduled-events/{date}"
SOFASCORE_TEAM_HISTORY_URL = "https://www.sofascore.com/api/v1/team/{team_id}/events/last/{page}"
SOFASCORE_STANDINGS_URL = "https://www.sofascore.com/api/v1/tournament/{tournament_id}/season/{season_id}/standings/total"
SOFASCORE_H2H_URL = "https://www.sofascore.com/api/v1/event/{event_id}/h2h"
SOFASCORE_PREGAME_FORM_URL = "https://www.sofascore.com/api/v1/event/{event_id}/pregame-form"
SOFASCORE_MANAGERS_URL = "https://www.sofascore.com/api/v1/event/{event_id}/managers"
SOFASCORE_FEATURED_PLAYERS_URL = "https://www.sofascore.com/api/v1/team/{team_id}/featured-players"
SOFASCORE_ODDS_URL = "https://www.sofascore.com/api/v1/event/{event_id}/odds/1/all"
SOFASCORE_ODDS_FEATURED_URL = "https://www.sofascore.com/api/v1/event/{event_id}/odds/1/featured"

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def fetch_scheduled_events(date: str, tournament_id: int = 17) -> list[dict]:
    url = SOFASCORE_TOURNAMENT_URL.format(tournament_id=tournament_id, date=date)
    response = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=10)
    response.raise_for_status()
    events = response.json().get("events", [])
    return [_parse_event(e) for e in events]


def fetch_all_scheduled_events(date: str) -> list[dict]:
    url = SOFASCORE_ALL_URL.format(date=date)
    response = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=10)
    response.raise_for_status()
    events = response.json().get("events", [])
    return [_parse_event(e) for e in events]


def fetch_team_history(team_id: int, page: int = 0) -> dict:
    url = SOFASCORE_TEAM_HISTORY_URL.format(team_id=team_id, page=page)
    response = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=10)
    response.raise_for_status()
    data = response.json()
    return {
        "has_next_page": data.get("hasNextPage", False),
        "events": [_parse_event(e) for e in data.get("events", [])],
    }


def fetch_standings(tournament_id: int, season_id: int) -> list[dict]:
    url = SOFASCORE_STANDINGS_URL.format(tournament_id=tournament_id, season_id=season_id)
    response = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=10)
    response.raise_for_status()
    standings = response.json().get("standings", [])
    if not standings:
        return []
    return [_parse_standing_row(row) for row in standings[0].get("rows", [])]


def fetch_event_detail(event: dict) -> dict:
    event_id = event["id"]
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
        "pregame_form": safe(fetch_pregame_form, event_id),
        "managers": safe(fetch_managers, event_id),
        "home_featured_players": safe(fetch_featured_players, home_id),
        "away_featured_players": safe(fetch_featured_players, away_id),
        "odds_featured": safe(fetch_odds_featured, event_id),
        "standings": safe(fetch_standings, tournament_id, season_id),
    }


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
    response = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=10)
    response.raise_for_status()
    d = response.json()
    return {
        "team_duel": d.get("teamDuel"),
        "manager_duel": d.get("managerDuel"),
    }


def fetch_pregame_form(event_id: int) -> dict:
    url = SOFASCORE_PREGAME_FORM_URL.format(event_id=event_id)
    response = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=10)
    response.raise_for_status()
    d = response.json()
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
    response = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=10)
    response.raise_for_status()
    d = response.json()
    return {
        "home_manager": _parse_manager(d.get("homeManager", {})),
        "away_manager": _parse_manager(d.get("awayManager", {})),
    }


def fetch_featured_players(team_id: int) -> list[dict]:
    url = SOFASCORE_FEATURED_PLAYERS_URL.format(team_id=team_id)
    response = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=10)
    response.raise_for_status()
    featured = response.json().get("featuredPlayers", {})
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
    response = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=10)
    response.raise_for_status()
    markets = response.json().get("markets", [])
    return [_parse_odds_market(m) for m in markets]


def fetch_odds_featured(event_id: int) -> dict:
    url = SOFASCORE_ODDS_FEATURED_URL.format(event_id=event_id)
    response = requests.get(url, headers=HEADERS, impersonate="chrome124", timeout=10)
    response.raise_for_status()
    d = response.json()
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
    }
