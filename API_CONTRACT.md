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

## 4. API Contract

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
