# Routers

FastAPI route handlers. These are thin — they validate input, call the appropriate core module, and format the response. Business logic lives in the core modules, not here.

---

## Router Map

| File | Prefix / Tag | What it handles |
|------|-------------|-----------------|
| `frontend.py` | (no prefix) | Match detail, similar matches, AI analysis, buffer status, predictions today |
| `platform.py` | `/agent`, `/betbuilder`, `/predictions` | Predictions, value bets, bet builder, performance analytics |
| `sporty.py` | `/mongo` | Manual scan triggers, live priority toggle |
| `sofascore.py` | (no prefix) | SofaScore-specific routes |
| `agent.py` | `/agent` | Agent analytics, signal stats, model explorer |
| `composite.py` | `/composite` | Single-call page loaders (prediction dashboard, analytics dashboard) |
| `pipelines.py` | `/pipelines` | Pipeline enable/disable/preset |
| `scheduler.py` | `/scheduler` | Interval viewing and patching |
| `sofa_pipeline.py` | `/sofa-pipeline` | SofaScore-only pipeline control |
| `mongo.py` | `/mongo` | Manual triggers, cleanup, world cup special |
| `mobile_bridge.py` | `/mobile-bridge` | Accept raw SportyBet packets from Android app |
| `diagnostics.py` | `/system`, `/health` | Health check, readiness, config, audit, authority |

---

## Key Endpoints Reference

### Match endpoints (`frontend.py`)
```
GET  /matches/today                    — list all buffered matches for today
GET  /matches/today/{id}               — full match detail
GET  /matches/live                     — currently live matches
GET  /matches/by-date/{date}           — matches for a specific date
GET  /matches/upcoming-enriched-predicted — upcoming with enrichment + prediction status
GET  /matches/{id}/similar             — similar historical matches (cached 5 min)
POST /matches/{id}/enrich              — trigger enrichment for one match
POST /matches/{id}/predict             — trigger prediction for one match
POST /matches/{id}/ai-analysis         — run Groq AI analysis for one match
POST /matches/{id}/sofascore-match     — manually attach a SofaScore event
GET  /matches/{id}/sofascore-candidates — SofaScore candidate events for matching
```

### Prediction endpoints (`platform.py`)
```
GET  /predictions/today                — all today's predictions (used by Picks Hub)
GET  /predictions/history              — prediction history with grading
POST /predictions/refresh              — re-enrich + re-predict all today's matches
GET  /agent/value-bets                 — value bet filter
GET  /agent/analytics/performance      — win rate, by pick type
GET  /agent/analytics/roi              — ROI calculations
```

### Bet builder (`platform.py`)
```
POST /betbuilder                       — save a manual slip
POST /betbuilder/auto                  — auto-build a slip from constraints
GET  /betbuilder/history               — saved slips with grading
POST /betbuilder/grade                 — re-grade all historical slips
```

### Buffer status (`frontend.py`)
```
GET  /buffer/status                    — counts + scheduler health + activity log
POST /matches/cleanup                  — remove finished + stale rows
POST /matches/purge-ghosts             — remove ghost matches
```

### Pipeline control (`pipelines.py`)
```
GET  /pipelines                        — list all pipelines + enabled status
POST /pipelines/{id}/enable            — enable a pipeline
POST /pipelines/{id}/disable           — disable a pipeline
POST /pipelines/preset/{preset}        — apply cloud/local/off preset
```

### System (`diagnostics.py`)
```
GET  /health                           — simple health check
GET  /readiness                        — startup readiness with checks
GET  /config                           — current settings (no secrets)
GET  /system/activity                  — recent activity log
GET  /system/audit                     — full pipeline audit report
GET  /system/authority                 — correction authority lease status
```

---

## Adding a New Endpoint

1. Decide which router it belongs to (match detail → `frontend.py`, predictions → `platform.py`, admin/triggers → `mongo.py`)
2. Add the route function — keep it thin, delegate to a core module
3. Add a corresponding API function in `football_frontend/src/services/apis/footballApi.ts`
4. No need to touch `main.py` — all routers are already registered there
