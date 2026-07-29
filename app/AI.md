# AI & DeepSeek Layer

These modules add LLM-based reasoning on top of the statistical prediction engine. DeepSeek is optional — all of these fall back gracefully when `DEEPSEEK_API_KEY` is not set.

---

## `deepseek_agent.py`

The primary DeepSeek integration. Sends a compact match summary to `deepseek-chat` and returns a structured JSON prediction.

### Single match analysis: `run_deepseek_match_analysis(doc)`

Used by the `/matches/{id}/ai-analysis` endpoint and the AI bet builder.

**What it sends (< 300 tokens):**
- Match name, tournament, date, period, score
- Home + away: W/L/D form (last 5), pregame rating, league standing
- H2H record (home wins / away wins / draws)
- 1x2 odds
- One web context snippet (150 chars)

**What it returns:**
```json
{
  "status": "predicted | low_confidence | skipped",
  "recommendation": "Home Win | Away Win | Draw",
  "confidence": 72,
  "value_bet": true,
  "market_signal": "sharp HOME | stable | unavailable",
  "btts": "Yes | No | Unknown",
  "over_2_5": "Yes | No | Unknown",
  "key_factors": ["...", "..."],
  "reasoning": {
    "form": "...", "h2h": "...", "standings": "...",
    "odds_signal": "...", "verdict": "..."
  }
}
```

**Design decision:** Uses a direct `llm.invoke()` call (no LangChain agent) so tool schemas are never injected into the context. This keeps requests within the 12,000 TPM on-demand limit.

### Batch predictions: `run_deepseek_predictions(match_date, docs, limit)`

Runs single-match analysis over up to 50 enriched docs for a given date. Used for batch pre-analysis jobs.

---

## `llm.py`

LLM provider abstraction.

`get_llm()` — returns a configured LangChain LLM client. Provider selection order:
1. **DeepSeek** (if `DEEPSEEK_API_KEY` set) — fastest, lowest latency
2. **HuggingFace** (if `HF_TOKEN` set) — good for Render/cloud deployments
3. **Ollama** (local) — free, needs local install
4. **Deterministic supervisor** — rules-only fallback

`is_deepseek_available()` — returns `True` when `DEEPSEEK_API_KEY` is set and non-empty.

**Env vars:**
- `DEEPSEEK_API_KEY` — DeepSeek API key
- `HF_TOKEN` / `HUGGINGFACE_HUB_TOKEN` — HuggingFace token
- `PREDICTX_AI_PROVIDER` — force a provider (`deepseek | huggingface | ollama | auto`)
- `PREDICTX_HF_MODEL` — HF model ID (default `Qwen/Qwen2.5-7B-Instruct:fastest`)
- `PREDICTX_AI_MODEL` — Ollama model (default `llama3.2:3b`)
- `PREDICTX_AI_TIMEOUT_SECONDS` — request timeout (default 15)

---

## `ai_brain.py`

The AI supervisor that reviews completed predictions and optionally applies a small confidence adjustment.

`oversee_prediction(prediction, doc)` — sends the prediction result + key signals to the LLM and asks it to approve, flag caution, or request a small confidence change. Not called by default — only when `attach_brain=True` in `predict_and_record_enriched`.

The brain cannot override the risk manager or invent data. It can only nudge confidence ±10 points.

---

## `ai_betbuilder.py`

AI-powered bet slip builder. Combines prediction engine picks with DeepSeek analysis to build or validate a slip.

Designed to be called by the `/betbuilder/auto` endpoint when AI mode is enabled. Runs per-match DeepSeek analysis on candidates, then synthesises a final slip recommendation.

---

## `agent_tools.py`

LangChain tool definitions used by the agent executor (`agentic_prediction.py` and `deepseek_agent._build_agent()`).

Available tools:
- `poisson_model(home_team_id, away_team_id)` — run Poisson model
- `get_odds_movement(sportybet_id)` — fetch odds movement data
- `strength_of_schedule(home_team_id, away_team_id)` — SoS scores
- `get_current_time()` — current UTC time (used to check if match has started)

---

## `agentic_prediction.py`

Plan-before-act executor for multi-step enrichment + prediction.

`run_agentic_match_prediction(match_id)` — the agent:
1. Checks match freshness and readiness
2. Decides which enrichment steps are needed
3. Executes them (enrich_context, refresh_live_context, etc.)
4. Runs prediction
5. Returns structured result with completed/skipped step breakdown

Used by the `/predictions/refresh` endpoint and the autopilot guardian.

---

## `chat_agent.py`

Conversational agent for match Q&A. Allows users to ask questions about a specific match in natural language. Uses the same LLM abstraction as the DeepSeek agent.

---

## `bot2.py`

Bot2 prediction variant with a different pick selection and confidence approach. Used for A/B-style comparison against the primary prediction engine.
