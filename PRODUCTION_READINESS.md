# PredictX Production Readiness

## Current Architecture

PredictX now has four layers:

1. Data collection from SportyBet, Sofascore, odds markets, memory, and web search.
2. Deterministic prediction logic for form, H2H, league strength, odds, live state, and red cards.
3. AI brain review through Hugging Face, Ollama, or deterministic fallback.
4. API endpoints for mobile and dashboard clients.

## Render Environment

Use the values in `.env.example` as the deployment checklist.

Required for hosted AI:

- `PREDICTX_AI_PROVIDER=huggingface`
- `HF_TOKEN=<your token>`
- `PREDICTX_HF_MODEL=Qwen/Qwen2.5-7B-Instruct:fastest`

Recommended for persistent memory:

- Attach a Render disk.
- Set `PREDICTX_DB_PATH` to a path on that disk, for example `/var/data/predictx_memory.sqlite3`.

Recommended for mobile clients:

- Set `PREDICTX_CORS_ORIGINS` to the real mobile/web app origins.

## Health Endpoints

- `GET /health` confirms the app process is alive.
- `GET /readiness` checks database writability, AI configuration, and web-search settings.
- `GET /config` returns safe public configuration without exposing secrets.

## Match Prediction Endpoint

Use this for a specific match:

```text
GET /matches/{match_id}/prediction?source=sportybet&include_web_context=true
```

Fallback order:

1. Enriched match memory.
2. Current SportyBet live/upcoming feed.
3. Sofascore scheduled events for the supplied date.
4. Saved prediction history.

## AI Logic Readiness

The AI brain is now isolated in `app/ai_brain.py`, so deeper AI logic can be added without rewriting routers. It receives compact context:

- Top rule-engine picks
- Signals
- H2H
- Web search context
- Poisson output
- Strength of schedule
- Match score/minute/tournament

The model is instructed to return JSON only, with:

- `status`
- `verdict`
- `confidence_adjustment`
- `risks`
- `reasons`

## Next Production Step

To improve beyond a 50% success rate, the next major layer should be prediction grading:

1. Store each prediction before kickoff/live state.
2. Resolve it after the final result.
3. Calculate hit rate by market type, league, odds band, confidence band, and AI provider.
4. Feed those results back into confidence calibration.
