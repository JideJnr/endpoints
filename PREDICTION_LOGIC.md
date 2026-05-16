# PredictX Prediction Logic Notes

This project should grow from a transparent rule engine into a learning model. The rules below are intentionally small and explainable so each prediction can be tracked against final results.

## 1. Market Steam Confirmation

If a team already has a form/table/history edge and its odds have shortened from the opening price, treat that as confirmation. This does not mean "low odds always win"; it means the market moved toward the same side the model already liked.

Signals used:
- Current implied probability from odds
- Opening implied probability from `initial_fractional_value`
- Direction and size of movement
- Agreement with form/table edge

Agent behavior:
- Adds `market_steam` signal when probability moves by at least 3.5 percentage points
- Pushes the predicted side only when odds movement agrees with the underlying team edge

## 2. Live Chase / Late Goal Pressure

Late goals are more likely when the match is still close and one team must chase. A strong favorite losing or drawing after 58 minutes should increase live goal pressure, especially in leagues already profiled as goal-friendly.

Signals used:
- Minute
- Current score difference
- Pregame/form strength edge
- Recent goal pressure
- League late-goal profile

Agent behavior:
- Adds `live_chase_pressure` when the game is live, after 58 minutes, and the score difference is 0 or 1
- Promotes `Next goal / Over 0.5 live` when the chasing pressure is strong
- Uses league memory to raise or lower confidence once the system has observed resolved late-goal snapshots for that league

## 2b. League Memory Table

The agent stores every live snapshot it sees, not only late-game situations. Each snapshot keeps:
- League
- Minute and minute bucket
- Score and score state
- Favorite side and favorite probability when odds are available
- Red-card state
- Teams and source

When the same match is later observed as finished, the agent resolves every snapshot into outcomes:
- Next goal happened
- Over 0.5, 1.5, 2.5, and 3.5 final goals
- Home/draw/away result
- Favorite won
- Favorite recovered from drawing or losing
- Red-card team conceded after the snapshot

The older late-goal table is still kept as a focused shortcut for:
- Minute is 70 or later
- Score difference is 0 or 1

Example learned stat:
- "LaLiga late goals after 70' with score diff <= 1: 8 hits from 13 observed snapshots = 61.5%"
- "LaLiga, 71-80, favorite drawing: next goal happened in 58% of snapshots"

Memory is stored locally in `data/predictx_memory.sqlite3`.

## 2c. Aggregation And Duplicate Control

Raw snapshots are useful for recent inspection, but they should not grow forever. Resolved snapshots are rolled into `snapshot_aggregates`, grouped by:
- League
- Minute bucket
- Score state
- Red-card state
- Favorite side

This keeps long-term learning even after old raw snapshots are deleted.

Duplicate protection:
- Timeline snapshots dedupe by source, match id, minute bucket, score, and red-card state
- Focused late-goal snapshots dedupe by time bucket, score total, and score difference
- Matches get a normalized fingerprint from league, home team, away team, and start time
- If a finished match appears live again, it is flagged as a possible replay and excluded from learning snapshots
- If different sources report the same fixture, it is logged in `match_duplicates`

Predictions and settled results should be retained longer than raw snapshots because they are the training record for model accuracy.

## 3. Red Card State

A red card should change the match state immediately. The model does not simply pick against the team with the red card; it checks whether the card hit the favorite, underdog, leading team, or chasing team.

Signals used:
- Home/away red card count
- Model edge before card
- Current score and minute

Agent behavior:
- Adds a `red_card` pick when card counts differ
- Warns when the favorite is weakened rather than blindly backing the original favorite

## 4. League Strength And Cross-League Context

Recent form is not treated equally across every country or division. A first-division English result is weighted above a first-division Croatian result, and second/third divisions are reduced again. This keeps the model from thinking two similar form lines mean the same thing when the opposition level was different.

Signals used:
- Country and division from recent team history tournament names
- Competition strength for Champions League, Europa League, Conference League, and similar cross-border cups
- Lower-context markers like U19, reserves, SRL, women, or virtual matches

Agent behavior:
- Adds `league_strength_edge` to the home/away power score
- Exposes home and away recent average league-strength scores in the prediction signals
- Keeps the impact capped so league quality helps the model but does not overpower real form, odds, red cards, or live state

## 5. Team H2H Context

When Sofascore supplies direct H2H, the model now reads team duel wins/draws and turns that into a small edge. H2H is useful context, not a full prediction by itself.

Signals used:
- Home wins
- Away wins
- Draws
- Sample size

Agent behavior:
- Adds `h2h_edge` only when at least two direct meetings are available
- Caps H2H impact because old or tiny samples can mislead

## 6. AI Brain Supervisor

The AI brain sits above the rule engine and reviews the existing signals. It is not allowed to invent news or override missing data. For a free setup, it tries a local Ollama model first:

- Default URL: `http://localhost:11434/api/chat`
- Default model: `llama3.2:3b`
- Override model with `PREDICTX_AI_MODEL`
- Override URL with `PREDICTX_OLLAMA_URL`

For Render or any hosted deployment, it can use Hugging Face Inference Providers:

- Set `HF_TOKEN` or `HUGGINGFACE_HUB_TOKEN`
- Optional: set `PREDICTX_AI_PROVIDER=huggingface`
- Optional: set `PREDICTX_HF_MODEL=Qwen/Qwen2.5-7B-Instruct:fastest`
- Default endpoint: `https://router.huggingface.co/v1/chat/completions`

Provider order in `auto` mode is Hugging Face if a token exists, then Ollama, then the deterministic supervisor. The supervisor can approve, pass, or mark caution, then apply a small confidence adjustment.

## 7. Web Context Search

The enrichment agent can search DuckDuckGo for match previews before prediction. It uses `ddgs` for the search and `trafilatura` to extract readable text from the top result pages.

Limits:
- Top 3 search results
- 1,500 characters per scraped page
- ASCII-only text to keep prompts clean
- Short timeouts so one slow site does not block prediction

Agent behavior:
- `/run/enrich` stores `web_context` with snippets and scraped preview text
- `/run/predict` passes stored `web_context` into the AI brain
- `/models/web-context?home=...&away=...&tournament=...` tests search directly

Render requirements:
- `ddgs`
- `trafilatura`

Optional setting:
- `PREDICTX_SEARCH_BACKENDS=duckduckgo`

## Research Sources

- Opta Analyst: expected goals measure chance quality using historical shots and context: https://theanalyst.com/2023/08/what-is-expected-goals-xg
- Stochastic modelling of football matches: red cards reduce the affected team's goal intensity by more than 30%, and losing teams increase goal rate while chasing: https://arxiv.org/abs/2312.04338
- Closing line value concept and odds snapshots are commonly used for market calibration: https://www.thestatsapi.com/odds-api/clv
