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

Description: Returns currently live football matches from SportyBet Nigeria using the GET feed. Includes scores, match time, and all available betting markets.

Response: Same shape as endpoint #3.

---

## 3. SportyBet All Live & Upcoming Matches

**GET** `/sporty/live/all`

Description: Returns all live and upcoming matches from SportyBet Nigeria using the POST feed (`wapConfigurableEventsByOrder`). Returns more matches than `/sporty/live` including matches not yet started.

Response:
```json
{
  "status": "success",
  "count": "<total number of matches>",
  "matches": [
    {
      "id": "<sportybet event id e.g. sr:match:12345678>",
      "name": "<home team> vs <away team>",
      "home_team": "<home team name>",
      "away_team": "<away team name>",
      "score": { "home": "<goals>", "away": "<goals>" },
      "period_scores": ["<H1 score>", "<H2 score>"],
      "period": "<current period e.g. H1, H2, Not start>",
      "played_seconds": "<elapsed time e.g. 47:52>",
      "status": "<status code>",
      "start_time": "<unix timestamp in ms>",
      "tournament": "<tournament name>",
      "category": "<country>",
      "venue": "<stadium name>",
      "markets": [ ]
    }
  ]
}
```

---

## 3b. SportyBet Upcoming Matches

**GET** `/sporty/upcoming`

Description: Returns upcoming football matches from SportyBet Nigeria using the POST feed.

Response: Same match shape as `/sporty/live/all`.

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

## 15. Prediction Agent — SportyBet Live

**GET** `/agent/sporty/live-predictions`

Description: Returns live SportyBet matches with ranked prediction picks, confidence, and explainable signals.

Response:
```json
{
  "status": "success",
  "count": 1,
  "predictions": [
    {
      "match_id": "sr:match:123",
      "name": "Home vs Away",
      "source": "sportybet",
      "minute": 72,
      "score": { "home": "1", "away": "1" },
      "signals": [
        { "name": "late_goal_window", "value": "72' with 2 goals", "impact": 8 }
      ],
      "picks": [
        { "type": "live_goals", "selection": "Late goal watch", "confidence": 60, "reason": "league profile and late match state" }
      ]
    }
  ]
}
```

---

## 16. Prediction Agent — SportyBet Upcoming

**GET** `/agent/sporty/upcoming-predictions`

Description: Returns upcoming SportyBet matches with prematch prediction picks based on available odds and markets.

Response: Same prediction shape as `/agent/sporty/live-predictions`.

---

## 17. Prediction Agent — SportyBet All

**GET** `/agent/sporty/all-predictions`

Description: Returns both live and upcoming SportyBet predictions.

Response: Same prediction shape as `/agent/sporty/live-predictions`.

---

## 18. Prediction Agent — SofaScore Date

**GET** `/agent/sofascore/predictions/{date}`

Query params:
- `tournament_id` optional, defaults to Premier League when `all_matches=false`
- `all_matches` optional boolean, default `false`
- `limit` optional integer from 1 to 100, default `20`
- `include_history` optional boolean, default `true`

Description: Returns SofaScore match predictions with recent team history, form, standings, odds, live state, and explainable signals where available.

---

## 19. Prediction Agent — SofaScore Event

**GET** `/agent/sofascore/event/{event_id}/prediction`

Query params:
- `date` optional `YYYY-MM-DD`, defaults to today
- `include_history` optional boolean, default `true`

Description: Returns one SofaScore event prediction with ranked picks and the signals behind them.

---

## 20. Prediction Agent — League Memory

**GET** `/agent/memory/leagues`

Description: Returns all learned league late-goal memory stats.

**GET** `/agent/memory/leagues/{league}`

Description: Returns memory for one league, including sample count, late-goal hits, and late-goal rate.

Response:
```json
{
  "status": "success",
  "memory": {
    "league_key": "laliga",
    "league_name": "LaLiga",
    "samples": 13,
    "late_goals": 8,
    "late_goal_rate": 0.615
  }
}
```

---

**GET** `/agent/memory/snapshots`

Query params:
- `league` optional
- `minute_bucket` optional, e.g. `71-80`
- `score_state` optional, e.g. `favorite_drawing`, `favorite_losing`, `favorite_leading`, `draw`
- `min_samples` optional integer, default `1`

Description: Returns full timeline memory grouped by league, minute bucket, and score state.

Response:
```json
{
  "status": "success",
  "snapshots": [
    {
      "league_key": "laliga",
      "league_name": "LaLiga",
      "minute_bucket": "71-80",
      "score_state": "favorite_drawing",
      "samples": 20,
      "next_goal_rate": 0.55,
      "over_1_5_rate": 0.8,
      "over_2_5_rate": 0.45,
      "favorite_recovered_rate": 0.6,
      "red_card_team_conceded_rate": null
    }
  ]
}
```

---

**GET** `/agent/memory/duplicates`

Description: Returns matches flagged as duplicates, cross-source duplicates, or possible SportyBet replays.

**POST** `/agent/memory/maintenance`

Query params:
- `raw_retention_days` optional integer, default `30`
- `odds_retention_days` optional integer, default `60`

Description: Aggregates resolved snapshots into long-term league stats, deletes old raw snapshots/odds snapshots, and vacuums SQLite. Prediction history is not deleted.

---

## 21. Prediction Agent — Memory Observation

**POST** `/agent/memory/observe?source=manual`

Description: Records one match observation. Every live observation creates a timeline snapshot. Live matches after 70 minutes with score difference <= 1 also create the focused late-goal snapshot. Finished observations resolve all existing snapshots.

**POST** `/agent/memory/sofascore/{date}`

Query params:
- `tournament_id` optional, defaults to Premier League when `all_matches=false`
- `all_matches` optional boolean, default `false`

Description: Fetches SofaScore matches for a date and records them into memory.

**POST** `/agent/memory/sporty/live`

Description: Fetches current SportyBet live matches and records late-game snapshots into memory.

---

## 22. UI Platform Endpoints

These root endpoints are intended for the frontend pages.

### Logic

**GET** `/logic`

Description: Exposes all active prediction logic, signals, and current snapshot memory groups.

### Matches

**GET** `/matches?date=YYYY-MM-DD`

Description: Date-based match browser. Uses SofaScore when available and falls back to memory.

**GET** `/matches/live`

Description: Live matches with score, period, markets, and memory observation side effect.

**GET** `/matches/memory`

Query params:
- `limit` optional
- `league` optional
- `source` optional

Description: Lists matches stored in local memory.

**GET** `/matches/duplicates`

Description: Lists duplicate/replay detections.

**GET** `/matches/{match_id}`

Description: Returns one memory match with snapshots and prediction history.

### Countries & Leagues

**GET** `/countries`

Description: Lists countries inferred from stored leagues.

**GET** `/countries/{country_id}`

Description: Country detail with leagues.

**GET** `/leagues/{league_id}`

Description: League memory, snapshot groups, derived standings, and recent stored matches.

### Teams & Players

**GET** `/teams/{team_id}`

Description: Team profile from SofaScore history with recent stats and matches.

**GET** `/players/{player_id}`

Description: Stable player profile placeholder until a player provider is wired.

### Predictions

**GET** `/predictions?date=YYYY-MM-DD`

Description: All predictions for a day. Records prediction history.

**GET** `/predictions/{match_id}`

Description: Prediction for a specific match, or latest stored prediction if upstream lookup fails.

**GET** `/predictions/suggestions`

Description: Curated high-confidence picks.

**GET** `/predictions/value-bets`

Description: Picks backed by market/odds signals.

**GET** `/predictions/history`

Description: Prediction history stored locally.

### Bet Builder

**POST** `/betbuilder`

Body:
```json
{
  "selections": [
    { "match_id": "123", "selection": "Over 1.5", "confidence": 70, "odds": 1.55 }
  ]
}
```

Description: Returns combined odds and combined confidence, then stores the built bet.

**GET** `/betbuilder/history`

Description: Past built bets.

### Engines

**GET** `/engines`

Description: Lists prediction bots/engines.

**GET** `/engines/{engine_id}`

Description: Engine detail and recent prediction history.

**POST** `/engines/{engine_id}/start`

Description: Marks an engine as running.

**POST** `/engines/{engine_id}/stop`

Description: Marks an engine as stopped.

**GET** `/engines/metrics`

Description: Engine analytics dashboard metrics. Win-rate fields are `null` until prediction grading is added.

### Maintenance

**POST** `/maintenance/memory`

Description: Root alias for `/agent/memory/maintenance`.

---

## 23. Enrichment, Models, And Odds Movement

These endpoints incorporate the second project's pipeline without requiring MongoDB, Chroma, LangChain, or Groq.

**POST** `/run/enrich`

Query params:
- `date` optional `YYYY-MM-DD`
- `force` optional boolean
- `limit` optional integer

Description: Fetches SportyBet matches, fetches SofaScore fixtures, fuzzy-matches both feeds, stores enriched documents locally, and snapshots odds.

**POST** `/run/predict`

Query params:
- `date` optional `YYYY-MM-DD`
- `limit` optional integer

Description: Predicts from enriched documents when available, otherwise falls back to direct SofaScore predictions. Stores prediction history.

**POST** `/run/bot2`

Description: Runs Bot 2 value selector over stored prediction history.

**GET** `/bot2/picks`

Description: Returns Bot 2's curated value picks.

**GET** `/odds/movement/{match_id}`

Description: Opening-vs-current odds movement and sharp-money signal from local odds snapshots.

**GET** `/odds/movements`

Description: Odds movement for all tracked matches, optionally filtered by `date`.

**GET** `/models/poisson?home_team_id=1&away_team_id=2`

Description: Runs the Poisson goal model for home/draw/away, over 2.5, BTTS, and top scorelines.

**GET** `/models/schedule?home_team_id=1&away_team_id=2`

Description: Compares both teams using strength-of-schedule analysis.

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
