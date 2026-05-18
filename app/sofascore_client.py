import random
import time

from curl_cffi import requests

SOFASCORE_TOURNAMENT_URL = "https://www.sofascore.com/api/v1/unique-tournament/{tournament_id}/scheduled-events/{date}"
SOFASCORE_ALL_URL = "https://www.sofascore.com/api/v1/sport/football/scheduled-events/{date}"
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

_session = requests.Session(impersonate="chrome124")


def _get(url: str) -> requests.Response:
    """Persistent session with cookie warm-up retry on failure."""
    global _session
    try:
        resp = _session.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp
    except Exception:
        # rebuild session, warm cookies via homepage, then retry once
        _session = requests.Session(impersonate="chrome124")
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


def fetch_all_scheduled_events(date: str) -> list[dict]:
    url = SOFASCORE_ALL_URL.format(date=date)
    events = _get(url).json().get("events", [])
    return [_parse_event(e) for e in events]


def fetch_live_events() -> list[dict]:
    events = _get(SOFASCORE_LIVE_URL).json().get("events", [])
    return [_parse_event(e) for e in events]


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
        "home_last_matches": safe(lambda tid: (
            fetch_team_history(tid, 0).get("events", []) +
            fetch_team_history(tid, 1).get("events", [])
        ), home_id),
        "away_last_matches": safe(lambda tid: (
            fetch_team_history(tid, 0).get("events", []) +
            fetch_team_history(tid, 1).get("events", [])
        ), away_id),
        "home_featured_players": safe(fetch_featured_players, home_id),
        "away_featured_players": safe(fetch_featured_players, away_id),
        "odds_featured": safe(fetch_odds_featured, event_id),
        "standings": safe(fetch_standings, tournament_id, season_id),
    }


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
