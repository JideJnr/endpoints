# PredictX — Prediction Pipelines Reference

> Living document. Update this file whenever a pipeline changes.  
> Each section covers one pipeline: what triggers it, what runs inside it, known issues, and open discussion items.

---

## Table of Contents

1. [Enrichment Worker Pipeline](#1-enrichment-worker-pipeline)
2. [Unified Upcoming Pipeline](#2-unified-upcoming-pipeline)
3. [Unified Live Pipeline](#3-unified-live-pipeline)
4. [Manual / Rules Sub-Pipeline](#4-manual--rules-sub-pipeline)
5. [SofaScore-Only Pipeline](#5-sofascore-only-pipeline)
6. [AI Prediction Queue](#6-ai-prediction-queue)
7. [Competition Special Pipeline](#7-competition-special-pipeline)
8. [Live Priority Lane](#8-live-priority-lane)
9. [Grading & Learning Jobs](#9-grading--learning-jobs)
10. [Signal Inventory](#10-signal-inventory)
11. [Open Discussion Items](#11-open-discussion-items)

---

## 1. Enrichment Worker Pipeline

**File:** `app/storage/buffer.py` → `run_enrichment_worker()`  
**Scheduler job:** `enrich_worker` — every **30 seconds**  
**Entry chain:** `job_enrich_worker()` → `run_enrichment_worker()` → `apply_prediction_state()` → `predict_and_record_enriched()` → `predict_enriched_match()`

### What it does

The primary auto-prediction loop. Picks up a batch of unenriched or stale matches from the buffer, fetches SofaScore detail, and runs the full prediction stack.

### Process steps

| # | Step | Module |
|---|------|--------|
| 1 | `get_unenriched_batch()` — live first, then today, then tomorrow | `storage/buffer.py` |
| 2 | Fetch SofaScore scheduled events for required dates (parallel) | `sofascore_client.py` |
| 3 | `_fuzzy_match()` + `_llm_match()` — match SportyBet row to SofaScore event | `enrichment/enrichment.py` |
| 4 | `fetch_event_detail()` or `fetch_event_detail_live_refresh()` (parallel, 8 workers) | `sofascore_client.py` |
| 5 | `fetch_match_intelligence()` — Sportradar overlay | `sportradar_client.py` |
| 6 | `search_match_context()` + `search_league_sentiment()` — web context | `web_context.py` |
| 7 | `store_enriched()` — write enriched doc to buffer | `storage/buffer.py` |
| 8 | `apply_prediction_state()` — full prediction pipeline (see §4 + below) | `utils/prediction_flow.py` |
| 9 | `record_prediction()` — write to `prediction_history` | `storage/league_memory/crud.py` |

### Inside `predict_enriched_match()`

| # | Step | Purpose |
|---|------|---------|
| 1 | `prediction_readiness()` | Data contract gate — blocks if missing detail/history/markets |
| 2 | `_rules_prediction()` → `predict_sofascore_event()` | Rules engine (see §4) |
| 3 | `run_poisson()`, `run_dixon_coles()`, `elo_prediction()` | Statistical models |
| 4 | `ensemble_prediction()` | Weighted blend of all models |
| 5 | `_value_bets()` | Kelly edge detection against market odds |
| 6 | `_source_quality_signals()` | Data depth signals (sporty/sofa/sportradar availability) |
| 7 | `weighted_finished_match_memory()`, `close_match_strength_context()` | Historical outcome memory |
| 8 | `user_pick_signal` | User behavior overlay |
| 9 | `pattern_signal()` | Odds pattern detection |
| 10 | `get_movement()` | Odds movement / steam signal |
| 11 | `team_watcher_signal()` | TW engine: rules + AI model pick from team profiles |
| 12 | `team_watch_signal()` | Opponent tier edge, goal timing edge, signal combo history |
| 13 | `_consensus_longshot_value_signal()` | Model-market disagreement (longshot value) |
| 14 | `grade_signal_for_match()` | SofaScore grade signal |
| 15 | `get_signal_weights()` | Self-learner signal weight adjustments per league |
| 16 | `_market_selector_picks()` | Final pick selection (1X2, DC, goals, BTTS) |
| 17 | `_live_inplay_picks()` | Live-only markets (next goal, live winner, next team to score) |
| 18 | `_apply_time_decay()` | Live confidence decay by minute |
| 19 | `calibrate_confidence()` | Historical win rate calibration per pick type |
| 20 | `get_league_accuracy()` | League-level accuracy adjustment (±10 max) |
| 21 | `build_contextual_intelligence()` | Contextual risk/boost overlay |
| 22 | `apply_risk_controls()` | Risk management gate |
| 23 | `_curate_picks()` | Final pick curation with role learning and signal policy |
| 24 | `record_prediction()` | Append-only write to prediction history |

### Batch modes

| Mode | Condition | Batch size |
|------|-----------|------------|
| Live only | Live priority toggle ON and live matches exist | 8 |
| Live only (pipeline toggle) | `sportybet_enrich_live` ON, `sportybet_enrich_prematch` OFF | 4 |
| Upcoming only | `sportybet_enrich_prematch` ON, `sportybet_enrich_live` OFF | 4 |
| Both (normal) | Both ON — live first, fall through to upcoming if idle | 4+4 |

### Known issues / discussion
- [ ] Web context fetch (`search_match_context`) runs per-match inside the batch — can slow the cycle when 8 workers all hit the web search API simultaneously
- [ ] `_llm_match` fallback adds latency when fuzzy score is between 0.55–0.62
- [ ] Signal combo history in `team_watch_signal` requires `prediction_json` to be stored on watcher matches — only populated after first observe cycle

---

## 2. Unified Upcoming Pipeline

**File:** `app/scheduling/scheduler.py` → `job_unified_upcoming()`  
**Scheduler job:** `unified_upcoming` — every **5 minutes**  
**Toggle:** `unified_upcoming` pipeline registry key

### What it does

A single-pass pipeline that fetches all SportyBet upcoming matches, matches them against SofaScore in bulk, and predicts. Designed to be more efficient than the enrichment worker for prematch batches.

### Process steps

| # | Step | Detail |
|---|------|--------|
| 1 | `fetch_upcoming_matches_post()` | Fetch all SportyBet upcoming |
| 2 | `ingest_matches()` per date group | Buffer all matches |
| 3 | `snapshot_odds()` per match | Capture opening odds |
| 4 | `fetch_all_scheduled_events()` for today + tomorrow | One SofaScore call per date |
| 5 | `get_unenriched_batch(limit=12, exclude_live=True)` | Only today/tomorrow unmatched |
| 6 | `_resolve_sofascore_match()` | Team watcher exact → fuzzy → search → LLM |
| 7 | `fetch_event_detail()` in parallel (4 workers) | Skip if already enriched |
| 8 | Build enriched doc → `store_enriched()` | Write to buffer |
| 9 | `apply_prediction_state()` | Full prediction pipeline |

### Match resolution priority in `_resolve_sofascore_match()`

1. Team watcher exact match (sporty_team_id or sofascore_team_id)
2. Saved sofascore_id from existing enriched doc
3. Fuzzy match against daily SofaScore feed (threshold 0.72)
4. SofaScore search API fallback (threshold 0.70)
5. LLM match fallback (threshold 0.55–0.72)
6. `no_match` — stored with retry_after_ts

### Known issues / discussion
- [ ] Batch is capped at `UNIFIED_UPCOMING_BATCH_SIZE = 12` per cycle — large backlogs take many cycles to clear
- [ ] SofaScore search fallback (`_search_match_safe`) only runs when best fuzzy score < 0.70 — some valid matches with unusual name formats still miss
- [ ] Prediction runs on ALL stored matches including unmatched ones (SportyBet market signal only) — discuss whether unmatched prematch predictions should be published

---

## 3. Unified Live Pipeline

**File:** `app/scheduling/scheduler.py` → `job_unified_live()`  
**Scheduler job:** `unified_live` — every **60 seconds**  
**Toggle:** `unified_live` pipeline registry key

### What it does

Dedicated live match pipeline. Refreshes scores, matches against SofaScore live feed, fetches lightweight live detail, and re-predicts.

### Process steps

| # | Step | Detail |
|---|------|--------|
| 1 | `fetch_live_matches_post()` | Fetch all SportyBet live |
| 2 | `ingest_matches()` + `patch_live_scores()` | Buffer new + update scores |
| 3 | `snapshot_odds()` per match | Live odds snapshot |
| 4 | `observe_matches("sportybet", matches)` | Team watcher observation |
| 5 | `fetch_live_events()` | One SofaScore live call |
| 6 | `get_unenriched_batch(limit=8, live_only=True)` | Live matches needing enrichment |
| 7a | Already matched + has detail → `fetch_event_detail_live_refresh()` | Lightweight: stats + incidents only (~3 calls vs ~12) |
| 7b | Unmatched → `_fuzzy_match()` → `fetch_event_detail()` | Full detail fetch |
| 8 | Build doc → `store_enriched()` | Write to buffer |
| 9 | `apply_prediction_state()` | Full prediction pipeline (live mode) |

### Live refresh optimisation
Already-matched live matches use `fetch_event_detail_live_refresh()` which fetches only statistics, incidents, and lineups — saving ~9 SofaScore API calls per live match per cycle.

### Known issues / discussion
- [ ] Fuzzy match threshold for live is 0.62 (lower than prematch 0.72) — can produce false matches for similarly named teams
- [ ] `observe_matches` runs on raw SportyBet data before enrichment — team watcher gets sporty IDs but not sofascore IDs until enrichment completes
- [ ] Live prediction cooldown is 2 minutes when SofaScore live stats are available, 3 minutes otherwise — discuss whether this is tight enough for fast-moving matches

---

## 4. Manual / Rules Sub-Pipeline

**File:** `app/ai/prediction_agent.py` → `predict_sofascore_event()`  
**Called by:** enrichment worker, unified pipelines, sofa pipeline — as the rules sub-model inside `_rules_prediction()`

### What it does

The deterministic rules engine. Produces picks and signals from form data, H2H, table position, odds, and team watch. Its output feeds into the ensemble as evidence.

### Process steps

| # | Step | Signal name | Impact range |
|---|------|-------------|-------------|
| 1 | `_team_history_features()` | — | Builds form stats |
| 2 | `_form_edge()` | `recent_history_edge`, `avg_rating_edge` | ±variable |
| 3 | `_league_strength_edge()` | `league_strength_edge` | ±variable |
| 4 | `_h2h_edge()` | `h2h_edge` | ±8 |
| 5 | `_table_edge()` | `league_position_edge` | ±15 (season-stage weighted) |
| 6 | `_odds_edge()` + `_odds_momentum_edge()` | `odds_edge`, `market_steam` | ±variable |
| 7 | `_common_opponent_edge()` | `common_opponent_edge` | ±12 (table-weighted) |
| 8 | `form_trajectory_signal()` | `home_form_trajectory`, `away_form_trajectory` | ±6 |
| 9 | **`team_watch_signal()`** | `team_watch` | ±8 |
| 10 | Sum → `home_power` | — | Drives pick selection |
| 11 | Goal picks | `goal_pressure`, `btts_pressure` | — |
| 12 | Live picks | `live_chase_pressure`, `late_goal_league` | — |
| 13 | `_apply_time_decay()` | — | ×0.35–1.0 multiplier |

### `home_power` thresholds

| `home_power` | Pick generated |
|---|---|
| ≥ 8 | Direct match_result pick (55 + min(25, abs)) confidence |
| 4–7 | Signal aggregator directional pick or no_bet |
| < 4 | No match_result pick from rules |

### `team_watch_signal()` sub-signals

| Sub-signal | Source data | Weight |
|---|---|---|
| `opponent_tier_edge` | `table_gap` on watcher matches — win rate vs stronger/similar/weaker | 50% |
| `goal_timing_edge` | `goal_timing.goal_minutes`, `average_interval_minutes` from `raw_match_json` | 30% |
| `signal_combo_edge` | `prediction_json` on watcher matches — overlap with current active signals | 20% |

### Known issues / discussion
- [ ] `_common_opponent_edge` is expensive — scans all home/away history for shared opponents. Consider caching per match_id
- [ ] `form_trajectory_signal` requires ≥4 finished matches — new teams always return `unknown`
- [ ] `team_watch_signal` signal combo edge requires ≥3 past matches with ≥2 overlapping signal names — sparse early on
- [ ] `_table_edge` is zeroed when season stage is `not_started` — but `beginning` only gets 0.3 weight, discuss if this is conservative enough

---

## 5. SofaScore-Only Pipeline

**File:** `app/sofa_pipeline.py` → `run_sofa_pipeline_cycle()`  
**Scheduler job:** `sofa_pipeline` — every **5 minutes**, only when toggle enabled  
**Toggle:** `sofa_pipeline` engine state (Settings UI)

### What it does

Cloud/Render mode pipeline. Uses SofaScore as the sole data source when SportyBet blocks datacenter IPs. Fetches SofaScore scheduled + live events, enriches, and predicts without any SportyBet data.

### Process steps

| # | Step | Detail |
|---|------|--------|
| 1 | `fetch_all_scheduled_events()` for today | SofaScore scheduled |
| 2 | `fetch_live_events()` | SofaScore live |
| 3 | Enrich from SofaScore detail only | No SportyBet markets |
| 4 | `apply_prediction_state()` | Full prediction pipeline |

### Known issues / discussion
- [ ] No SportyBet markets means `_odds_edge`, `_odds_momentum_edge`, `market_steam`, and `_value_bets` all return 0 — prediction quality is lower
- [ ] `prediction_readiness` allows sofascore-only predictions when `provider_state == "sofascore"` — but confidence calibration was trained on both-source data
- [ ] Discuss: should sofa-only predictions be tagged differently in the frontend to indicate lower data confidence?

---

## 6. AI Prediction Queue

**File:** `app/ai_prediction_pipeline.py` → `job_ai_prediction_queue()`  
**Scheduler job:** `ai_prediction_queue` — every **5 minutes**  
**Toggle:** `ai_prediction_queue` pipeline registry key

### What it does

LLM-based prediction overlay. Picks up matches flagged `ai_prediction_queue_pending = True` and runs them through the Ollama/OpenRouter pipeline for a second-opinion prediction.

### Process steps

| # | Step | Detail |
|---|------|--------|
| 1 | Query buffer for `ai_prediction_queue_pending = True` | Prematch only |
| 2 | `run_ollama_pipeline()` | LLM prediction using OpenRouter |
| 3 | `build_prediction_audit()` | Audit trail |
| 4 | `record_prediction()` | Write to history |

### Known issues / discussion
- [ ] Only runs for prematch (`not item.get("is_live")`) — live AI predictions are not implemented
- [ ] `run_ollama_pipeline` requires `openrouter_api_key` — silently skips if not configured
- [ ] No deduplication check against existing AI predictions for the same match — can produce multiple AI prediction rows per match
- [ ] Discuss: should AI predictions be blended into the main pick or kept as a separate `source = "ai_pipeline"` row?

---

## 7. Competition Special Pipeline

**File:** `app/competition_special.py` → `run_enabled_competition_cycles()`  
**Scheduler job:** `competition_special` — every **5 minutes**  
**Toggle:** per-competition enable flag in competition registry

### What it does

Dedicated SofaScore enrichment + prediction lane for each enabled top-30 competition (e.g. World Cup, Champions League, Premier League). Runs independently of the main enrichment worker so high-priority competitions always get fresh data.

### Process steps

| # | Step | Detail |
|---|------|--------|
| 1 | Load enabled competitions from registry | `competition_registry.py` |
| 2 | Fetch SofaScore events for each competition | Per-competition unique_tournament_id |
| 3 | Match + enrich + predict | Same `apply_prediction_state()` path |
| 4 | Update `team_competitions` stats | `competition_registry.py` |

### Known issues / discussion
- [ ] Competition cycles run sequentially — if 10 competitions are enabled, the job can take >5 minutes and overlap with the next trigger
- [ ] Discuss: should competition special have its own prediction cooldown separate from the main enrichment worker cooldown?

---

## 8. Live Priority Lane

**File:** `app/scheduling/scheduler.py` → `job_live_priority_toggle()`  
**Scheduler job:** `live_priority_toggle` — every **60 seconds**, only when toggle enabled  
**Toggle:** `live_priority_mode` engine state (Settings UI)

### What it does

When enabled, runs a dedicated live-first enrichment pass every minute. Designed for periods with many concurrent live matches where the normal 30-second enrichment worker can't keep up.

### Process steps

| # | Step | Detail |
|---|------|--------|
| 1 | `job_ingest_live(limit=200)` | Fresh SportyBet live fetch |
| 2 | `run_enrichment_worker(batch_size=8, live_only=True, force_live_retry=True, fetch_web_context=False)` | Force-retry all live matches |

### Known issues / discussion
- [ ] `force_live_retry=True` bypasses the `sofascore_retry_after_ts` gate — can hammer SofaScore API during busy periods
- [ ] Web context is disabled (`fetch_web_context=False`) in this lane — live predictions have no web context signals
- [ ] Discuss: should live priority also trigger `team_watch_signal` refresh or is the cached profile sufficient?

---

## 9. Grading & Learning Jobs

These jobs run after predictions are made and settle truth, update learning tables, and improve future predictions.

### 9.1 `grade_overdue_predictions` — every 30 minutes

**File:** `app/storage/league_memory/queries.py` → `grade_overdue_predictions()`

| Step | Detail |
|------|--------|
| 1 | `fetch_results()` from SportyBet (primary) | Matches by sportybet_id |
| 2 | `fetch_all_scheduled_events()` from SofaScore (fallback) | Per date, per match |
| 3 | `fetch_event()` direct fetch for unresolved sofa IDs | |
| 4 | `_grade_match_predictions_by_ids()` | Grades `prediction_history` + `prediction_candidate_history` + `prediction_decision_log` |
| 5 | `grade_orphaned_predictions()` | Matches removed from buffer before grading |
| 6 | `rebuild_calibration()` | Rebuild confidence bands from fresh win/loss data |
| 7 | `run_learning_cycle()` | Update signal weights per league |
| 8 | `optimise_ensemble_weights()` | Rebalance model weights |
| 9 | `grade_betbuilder_history()` | Grade betbuilder slips |

### 9.2 `grade_predictions` — every 6 hours

Full date-range grading pass (last 4 days) plus:
- ELO update (`record_match_result_once`)
- CLV computation (`compute_clv_for_date`)
- Odds pattern grading (`grade_patterns_for_date`)
- Self-learner cycle
- MongoDB cleanup

### 9.3 `prediction_monitor` — every 1 hour

**File:** `app/monitoring/prediction_monitor.py` → `run_prediction_monitor()`

| Step | Detail |
|------|--------|
| 1 | `grade_overdue_predictions()` | Settle any remaining truth |
| 2 | `get_grading_metrics()` | Overall win/loss stats |
| 3 | `_performance_trend()` | 24h vs previous 24h win rate delta |
| 4 | `_mismatch_report()` | Recent losses, repeated losing markets |
| 5 | `_pipeline_health()` | Pending/stale prediction counts |
| 6 | `_rebuild_calibration()` | If graded or trend degrading |
| 7 | `_optimise_weights()` | If graded or trend degrading |
| 8 | `_memory_maintenance()` | Prune old snapshots |

### 9.4 `autopilot_guardian` — every 5 minutes

Self-healing coordinator. Does not own prediction authority. Nudges stale jobs and coordinates catch-up learning. Never generates new predictions directly.

### Known issues / discussion
- [ ] `grade_overdue_predictions` uses `first_seen` (prediction created_at) as kickoff proxy when `start_time` is missing — can grade too early for matches with bad start_time data
- [ ] `grade_orphaned_predictions` uses `created_at` date as match date proxy — can miss matches that were predicted the day before kickoff
- [ ] `run_learning_cycle()` is called from both `grade_overdue_predictions` (every 30 min) and `grade_predictions` (every 6 hrs) — discuss whether 30-minute learning cycles are too frequent and cause oscillation in signal weights

---

## 10. Signal Inventory

All signals that can appear in a prediction's `signals` list, grouped by source.

### Rules engine signals (`predict_sofascore_event`)
| Signal | Impact | Description |
|--------|--------|-------------|
| `recent_history_edge` | ±variable | Form points + goals for/against differential |
| `avg_rating_edge` | ±variable | SofaScore average player rating differential |
| `league_strength_edge` | ±variable | Recent league strength comparison |
| `h2h_edge` | ±8 | Head-to-head win rate |
| `league_position_edge` | ±15 | Table position gap (season-stage weighted) |
| `odds_edge` | ±variable | Implied probability from 1X2 odds |
| `market_steam` | ±10 | Odds shortening from opening |
| `common_opponent_edge` | ±12 | Shared opponent comparison (table-weighted) |
| `home_form_trajectory` | ±6 | Opponent-quality-weighted form trend (home) |
| `away_form_trajectory` | ±6 | Opponent-quality-weighted form trend (away) |
| `late_goal_league` | +7 | High late-goal league flag |
| `goal_pressure` | ±variable | Combined scoring/conceding pressure |
| `live_chase_pressure` | ±variable | Close scoreline + chasing pressure |
| `red_card_state` | ±10 | Red card impact on win probability |
| `league_memory_late_goal` | ±8 | League-level late goal memory |

### Team watch signals
| Signal | Impact | Description |
|--------|--------|-------------|
| `team_watch` | ±8 | Opponent tier edge + goal timing edge + signal combo history |
| `team_watcher_engine` | ±8 | TW engine rules + AI model pick (confidence_impact) |

### Statistical model signals
| Signal | Impact | Description |
|--------|--------|-------------|
| `poisson_model` | ±variable | Poisson goal distribution probabilities |
| `dixon_coles_model` | ±variable | Dixon-Coles adjusted probabilities |
| `elo_model` | ±variable | ELO win probability |
| `ensemble_model` | ±variable | Weighted ensemble output |
| `model_consensus` | 0–6 | Agreement across models |

### Memory signals
| Signal | Impact | Description |
|--------|--------|-------------|
| `finished_database_memory` | ±6 | Historical outcome rates (tournament/country/global) |
| `close_match_strength_memory` | ±5 | Similar-strength historical outcomes |
| `prediction_memory` | ±8 | Past prediction win rate for this pick type/selection |

### Market signals
| Signal | Impact | Description |
|--------|--------|-------------|
| `odds_pattern` | ±variable | Odds pattern detection |
| `odds_progression` | ±5 | Odds movement direction and strength |
| `consensus_longshot_value` | 3–18 | Model-market disagreement on a priced-out side |

### Learning signals
| Signal | Impact | Description |
|--------|--------|-------------|
| `learned_signal_adjustment` | ±8 | Self-learner weight adjustments per league |
| `calibration_gap_severe` | -8 | Confidence exceeds historical win rate by >20pts |
| `calibration_gap_moderate` | -4 | Confidence exceeds historical win rate by 10–20pts |

### Context signals
| Signal | Impact | Description |
|--------|--------|-------------|
| `contextual_intelligence` | ±variable | Contextual risk/boost overlay |
| `risk_management` | -8/−3/+1 | Risk management gate output |
| `user_pick_signal` | +4/−2 | User behavior overlay |
| `web_context` | 0–4 | Web search availability |
| `web_sentiment` | ±variable | OpenRouter sentiment analysis |
| `web_probability` | ±variable | OpenRouter implied probabilities |
| `venue_form_edge` | ±12 | Home/away venue record differential |
| `season_not_started` | -4 | Season hasn't started — standings unreliable |
| `season_beginning` | -2 | Season just started — standings partially reliable |
| `live_inplay_state` | ±variable | Live match state summary |
| `live_statistics_result_edge` | ±variable | Live stats boost (possession, shots on target) |
| `goal_environment` | -6/−2/+2 | Hot/warm/calm goal environment profile |
| `sofascore_grades` | ±variable | SofaScore player grade signal |
| `source_blend_*` | 1–4 | Data source quality indicator |

---

## 11. Open Discussion Items

Items to discuss and resolve. Move to relevant pipeline section once resolved.

### Architecture
- [ ] **Two enrichment paths running in parallel** — `enrich_worker` (every 30s) and `unified_upcoming` (every 5min) both process prematch matches. Are they deduplicating correctly via the prediction cooldown? Should one be deprecated?
- [ ] **`unified_live` vs `enrich_worker` live mode** — both process live matches. `unified_live` is more structured but `enrich_worker` has the live priority toggle. Discuss which should be the canonical live pipeline.
- [ ] **Prediction cooldown values** — prematch: 180 min, live with sofa stats: 2 min, live without: 3 min. Are these right? A match that goes live at minute 0 won't get a new prediction until minute 2 even if the score changes.

### Team Watch
- [ ] **Goal timing data availability** — `goal_timing` is populated by `enrich_match_facts()` from `sofascore_detail`. For matches without SofaScore detail, `goal_timing_edge` always returns 0. Should we fall back to league-level goal timing from `late_goal_snapshots`?
- [ ] **Signal combo edge sparsity** — requires `prediction_json` stored on watcher matches AND ≥2 overlapping signal names AND ≥3 such matches. Most teams will have 0 combo edge for months. Discuss minimum viable threshold.
- [ ] **Opponent tier classification** — currently uses `table_gap` (opponent position minus team position). A gap of -3 to +3 is "similar". Is this the right band? A gap of ±3 in a 20-team league is very different from ±3 in a 10-team league.

### Grading
- [ ] **Learning cycle frequency** — `run_learning_cycle()` fires every 30 minutes via `grade_overdue_predictions`. Signal weights could oscillate if only 1–2 new results come in per cycle. Consider a minimum sample gate before updating weights.
- [ ] **Orphaned prediction grading** — uses `created_at` date as match date proxy. Predictions made the day before kickoff will look for SofaScore events on the wrong date. Should we store `match_date` on `prediction_history` rows?

### Signals
- [ ] **`team_watcher_engine` vs `team_watch_signal`** — two separate team watch signals now exist. `team_watcher_engine` is a standalone pick (rules + AI model). `team_watch_signal` is a pure signal (opponent tier + timing + combo). Discuss whether both should influence `home_power` or only one.
- [ ] **`consensus_longshot_value` gate** — requires ≥15 graded samples with ≥55% win rate. Most selections will never reach this threshold. Discuss whether the gate is too strict for a new system.
