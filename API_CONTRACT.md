# PredictX Football Stats Agent — API Contract

Base URL: `http://localhost:8000` (local) | `https://predictx.onrender.com` (production)

---

## 1. Health Check

**GET** `/health`

Description: Confirms the service is running.

Response:
```json
{
  "status": "ok"
}
```

---

## 2. SportyBet Live Matches

**GET** `/sporty/live`

Description: Returns all currently live football matches from SportyBet Nigeria, including scores, match time, and all available betting markets.

Response:
```json
{
  "status": "success",
  "count": 10,
  "matches": [
    {
      "id": "sr:match:67091962",
      "name": "Velez Sarsfield vs Union de Santa Fe",
      "home_team": "Velez Sarsfield",
      "away_team": "Union de Santa Fe",
      "score": { "home": "2", "away": "1" },
      "period_scores": ["2:1", "0:0"],
      "period": "H2",
      "played_seconds": "47:52",
      "status": 1,
      "start_time": 1777326300000,
      "tournament": "Primera LPF",
      "category": "Argentina",
      "venue": "Jose Amalfitani",
      "markets": [
        {
          "id": "1",
          "name": "1X2",
          "specifier": null,
          "status": 0,
          "group": "Main",
          "selections": [
            { "id": "1", "name": "Home", "odds": "1.29", "is_active": 1, "probability": "0.7318800000" },
            { "id": "2", "name": "Draw", "odds": "4.10", "is_active": 1, "probability": "0.2057400000" },
            { "id": "3", "name": "Away", "odds": "12.00", "is_active": 1, "probability": "0.0623800000" }
          ]
        }
      ]
    }
  ]
}
```

---

## 3. SofaScore Scheduled Events

**GET** `/sofascore/scheduled/{date}`

Path params:
- `date` — format `YYYY-MM-DD`

Query params:
- `tournament_id` (optional) — SofaScore unique tournament ID. Defaults to `17` (Premier League)

Description: Returns all scheduled/completed football matches for a given date and tournament from SofaScore.

Response:
```json
{
  "status": "success",
  "date": "2025-04-25",
  "count": 1,
  "events": [
    {
      "id": 12436550,
      "slug": "everton-chelsea",
      "name": "Chelsea vs Everton",
      "home_team": {
        "id": 38,
        "name": "Chelsea",
        "short_name": "Chelsea",
        "code": "CHE"
      },
      "away_team": {
        "id": 48,
        "name": "Everton",
        "short_name": "Everton",
        "code": "EVE"
      },
      "score": {
        "home": 1,
        "away": 0,
        "home_ht": 1,
        "away_ht": 0
      },
      "status": {
        "code": 100,
        "description": "Ended",
        "type": "finished"
      },
      "tournament": {
        "id": 17,
        "name": "Premier League"
      },
      "season": "Premier League 24/25",
      "round": 34,
      "venue": "Stamford Bridge",
      "start_timestamp": 1745667000,
      "winner_code": 1
    }
  ]
}
```

---

## 4. SofaScore All Football Matches

**GET** `/sofascore/scheduled/{date}/all`

Path params:
- `date` — format `YYYY-MM-DD`

Description: Returns all football matches across all tournaments and leagues for a given date from SofaScore. Same event shape as endpoint #3.

Response:
```json
{
  "status": "success",
  "date": "2026-05-04",
  "count": 450,
  "events": [ ]
}
```

---

## 5. SofaScore Team Match History

**GET** `/sofascore/team/{team_id}/history`

Path params:
- `team_id` — SofaScore team ID e.g. `14` (Nottingham Forest)

Query params:
- `page` (optional) — pagination page, `0` = most recent. Defaults to `0`

Description: Returns a team's last 30 matches per page. Use `has_next_page` to paginate further back.

Response:
```json
{
  "status": "success",
  "team_id": 14,
  "page": 0,
  "has_next_page": true,
  "events": [
    {
      "id": "<event id>",
      "slug": "<url slug>",
      "name": "<home team> vs <away team>",
      "home_team": { "id": "<id>", "name": "<name>", "short_name": "<short>", "code": "<code>" },
      "away_team": { "id": "<id>", "name": "<name>", "short_name": "<short>", "code": "<code>" },
      "score": { "home": "<goals>", "away": "<goals>", "home_ht": "<ht goals>", "away_ht": "<ht goals>" },
      "status": { "code": "<code>", "description": "<label>", "type": "<type>" },
      "tournament": { "id": "<id>", "name": "<name>" },
      "season": "<season label>",
      "round": "<round number>",
      "venue": "<stadium name>",
      "start_timestamp": "<unix timestamp>",
      "winner_code": "<1=home, 2=away, 3=draw>"
    }
  ]
}
```

---

## 6. SofaScore League Standings

**GET** `/sofascore/standings/{tournament_id}/{season_id}`

Path params:
- `tournament_id` — SofaScore tournament ID, found in any event under `tournament.tournament_id`
- `season_id` — SofaScore season ID, found in any event under `season_id`

Description: Returns the full league table for a given tournament and season. Both IDs are available directly from the `/sofascore/scheduled/{date}/all` response.

Response:
```json
{
  "status": "success",
  "tournament_id": "<tournament id>",
  "season_id": "<season id>",
  "standings": [
    {
      "position": "<league position>",
      "team": {
        "id": "<team id>",
        "name": "<team name>",
        "short_name": "<short name>",
        "code": "<3-letter code>"
      },
      "played": "<matches played>",
      "wins": "<wins>",
      "draws": "<draws>",
      "losses": "<losses>",
      "goals_for": "<goals scored>",
      "goals_against": "<goals conceded>",
      "goal_diff": "<goal difference e.g. +5 or -3>",
      "points": "<total points>",
      "promotion": "<promotion zone label e.g. Champions League or null>"
    }
  ]
}
```

---

## 7. SofaScore Event H2H

**GET** `/sofascore/event/{event_id}/h2h`

Description: Returns head-to-head duel summary between the two teams and managers for a given event.

Response:
```json
{
  "status": "success",
  "event_id": "<event id>",
  "team_duel": { "homeWins": "<int>", "awayWins": "<int>", "draws": "<int>" },
  "manager_duel": { "homeWins": "<int>", "awayWins": "<int>", "draws": "<int>" }
}
```

---

## 8. SofaScore Pregame Form

**GET** `/sofascore/event/{event_id}/pregame-form`

Description: Returns recent form, league position, and avg rating for both teams going into the match.

Response:
```json
{
  "status": "success",
  "event_id": "<event id>",
  "label": "<form label e.g. Pts>",
  "home_team": { "avg_rating": "<float>", "position": "<int>", "value": "<string>", "form": ["W", "L", "D", "W", "W"] },
  "away_team": { "avg_rating": "<float>", "position": "<int>", "value": "<string>", "form": ["D", "W", "L", "W", "D"] }
}
```

---

## 9. SofaScore Event Managers

**GET** `/sofascore/event/{event_id}/managers`

Description: Returns the home and away managers for a given event.

Response:
```json
{
  "status": "success",
  "event_id": "<event id>",
  "home_manager": { "id": "<id>", "name": "<full name>", "short_name": "<short name>" },
  "away_manager": { "id": "<id>", "name": "<full name>", "short_name": "<short name>" }
}
```

---

## 10. SofaScore Team Featured Players

**GET** `/sofascore/team/{team_id}/featured-players`

Description: Returns the featured/standout players for a team based on recent performance.

Response:
```json
{
  "status": "success",
  "team_id": "<team id>",
  "players": [
    {
      "id": "<player id>",
      "name": "<full name>",
      "short_name": "<short name>",
      "position": "<G/D/M/F>",
      "jersey_number": "<number>",
      "rating": "<sofascore rating>"
    }
  ]
}
```

---

## 11. SofaScore Event Odds

**GET** `/sofascore/event/{event_id}/odds`

Description: Returns all available pre-match odds markets for a given event in fractional format.

Response:
```json
{
  "status": "success",
  "event_id": "<event id>",
  "markets": [
    {
      "market_name": "<e.g. Full time>",
      "market_group": "<e.g. 1X2>",
      "market_period": "<e.g. Full-time>",
      "suspended": "<bool>",
      "is_live": "<bool>",
      "choices": [
        {
          "name": "<1, X or 2>",
          "fractional_value": "<e.g. 2/7>",
          "initial_fractional_value": "<opening odds>",
          "change": "<1=drifted, -1=shortened, 0=unchanged>"
        }
      ]
    }
  ]
}
```

---

## 12. SofaScore Event Featured Odds

**GET** `/sofascore/event/{event_id}/odds/featured`

Description: Returns the 3 key odds markets for a match — 1X2, Asian Handicap, and Full Time. Lighter alternative to `/odds` when only headline odds are needed.

Response:
```json
{
  "status": "success",
  "event_id": "<event id>",
  "has_more_odds": "<bool>",
  "default": {
    "market_name": "<e.g. Full time>",
    "market_group": "<e.g. 1X2>",
    "market_period": "<e.g. Full-time>",
    "suspended": "<bool>",
    "is_live": "<bool>",
    "choices": [
      {
        "name": "<1, X or 2>",
        "fractional_value": "<current odds e.g. 14/25>",
        "initial_fractional_value": "<opening odds>",
        "change": "<1=drifted, -1=shortened, 0=unchanged>"
      }
    ]
  },
  "asian": "<same shape as default, Asian Handicap market>",
  "full_time": "<same shape as default, Full Time 1X2 market>"
}
```

---

## 13. SofaScore Event Full Detail

**GET** `/sofascore/event/{event_id}/detail`

Path params:
- `event_id` — SofaScore event ID, from `/sofascore/scheduled/{date}/all`

Query params:
- `date` (optional) — format `YYYY-MM-DD`. Defaults to today. Used to locate the event in the schedule.

Description: Returns everything about a match in one call — base event info, h2h, pregame form, managers, featured players for both teams, featured odds, and league standings. Any section that is unavailable (e.g. cup matches have no standings, lower leagues may have no featured players) returns `null` instead of failing.

Response:
```json
{
  "status": "success",
  "id": "<event id>",
  "name": "<home> vs <away>",
  "home_team": { "id": "<id>", "name": "<name>", "short_name": "<short>", "code": "<code>" },
  "away_team": { "id": "<id>", "name": "<name>", "short_name": "<short>", "code": "<code>" },
  "score": { "home": "<int>", "away": "<int>", "home_ht": "<int>", "away_ht": "<int>" },
  "status": { "code": "<int>", "description": "<label>", "type": "<type>" },
  "tournament": { "id": "<id>", "tournament_id": "<id>", "name": "<name>" },
  "season": "<season label>",
  "season_id": "<int>",
  "round": "<int>",
  "venue": "<stadium or null>",
  "start_timestamp": "<unix timestamp>",
  "winner_code": "<1=home, 2=away, 3=draw, null=not played>",
  "h2h": { "team_duel": { "homeWins": "<int>", "awayWins": "<int>", "draws": "<int>" }, "manager_duel": "<same or null>" },
  "pregame_form": {
    "label": "<Pts>",
    "home_team": { "avg_rating": "<float>", "position": "<int>", "value": "<string>", "form": ["W","L","D","W","W"] },
    "away_team": { "avg_rating": "<float>", "position": "<int>", "value": "<string>", "form": ["D","W","L","D","W"] }
  },
  "managers": {
    "home_manager": { "id": "<id>", "name": "<name>", "short_name": "<short>" },
    "away_manager": { "id": "<id>", "name": "<name>", "short_name": "<short>" }
  },
  "home_featured_players": "<list of players or null>",
  "away_featured_players": "<list of players or null>",
  "odds_featured": {
    "has_more_odds": "<bool>",
    "default": "<1X2 market or null>",
    "asian": "<Asian Handicap market or null>",
    "full_time": "<Full Time market or null>"
  },
  "standings": "<league table rows or null>"
}
```

---

## 14. API Contract

**GET** `/contract`

Description: Returns this contract document as plain text. Always reflects the current state of all available endpoints.

Response: Plain text markdown document.

---

## Standard Error Response

All endpoints return this shape on failure:

```json
{
  "detail": "Error message describing what went wrong"
}
```

HTTP status codes:
- `200` — success
- `502` — upstream SportyBet API call failed
