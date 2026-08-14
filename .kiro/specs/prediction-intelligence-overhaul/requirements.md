# Requirements Document

## Introduction

This document specifies the requirements for a comprehensive overhaul of the prediction intelligence system. The system currently suffers from four categories of defects: (1) critical stubs and broken feedback loops that produce zero learning value, (2) hard-coded numeric biases that should be replaced by data-driven learned values, (3) incomplete wiring between team/competition intelligence tables and the live prediction pipeline, and (4) minor signal-quality and cache hygiene gaps. This overhaul addresses all four categories end-to-end, with verifiable acceptance criteria and property-based correctness tests.

---

## Glossary

- **SignalAggregator**: The class in `app/enrichment/signal_aggregator.py` that normalises, deduplicates, and aggregates prediction signals into home/draw/away probabilities.
- **Self_Learner**: The module `app/monitoring/self_learner.py` that runs after every grading cycle and updates all learning tables.
- **Learning Cycle**: The execution of `run_learning_cycle()`, which reads graded prediction history and rewrites signal weights, league accuracy, model weights, and related tables.
- **Ensemble**: The weighted combination of Dixon-Coles, ELO, Poisson, Rules, and optional LLM model outputs performed by `app/models/ensemble.py`.
- **Contextual_Intelligence**: The module `app/enrichment/contextual_intelligence.py` that applies match-context adjustments (±points) to confidence scores.
- **Enriched_Prediction**: The main prediction orchestrator in `app/enrichment/enriched_prediction.py`.
- **Competition_Special**: The module `app/competition/competition_special.py` that manages TOP_30_COMPETITIONS and the competition analysis cycle.
- **league_accuracy**: The SQLite table that stores per-league, per-pick-type win rates, average confidence, and calibration gaps.
- **signal_weights**: The SQLite table that stores per-signal win-rate-derived weight adjustments.
- **signal_combination_memory**: The SQLite table that stores win rates for specific signal-pattern combinations.
- **learned_model_weights**: The SQLite table that stores learned weights for each ensemble model.
- **tournament_preferences**: The SQLite table that stores enrichment-queue priority (0–7) per league, where lower values mean the league is processed first.
- **model_bias_corrections**: The SQLite table that stores home/draw/away overconfidence multipliers.
- **team_competitions**: The SQLite table tracking per-team, per-competition match and prediction accuracy stats.
- **signal_outcomes**: The SQLite table recording per-signal win/loss results for each graded match.
- **ai_analysis_feedback**: A new SQLite table (to be created) storing AI analysis quality signals used to calibrate the LLM model weight.
- **user_behavior_outcomes**: A new SQLite table (to be created) storing per-prediction user agreement and outcome data used to calibrate the `user_pick_signal` impact.
- **context_penalty_adjustments**: A new SQLite table (to be created) storing learned per-context-tag penalty overrides.
- **league_outcome_distribution**: A new SQLite table (to be created) storing per-league home/draw/away base rate distributions.
- **system_events**: A new SQLite table (to be created) used for drift alerts and other system-health events.
- **EARS**: Easy Approach to Requirements Syntax — a structured natural-language requirements pattern.
- **graded prediction**: A prediction row with `graded_at IS NOT NULL` and `result IN ('win', 'loss')`.
- **pick_type**: The market type of a prediction (e.g. `match_result`, `goals`, `value_bet`).
- **league_key**: The normalised lowercase-underscore identifier derived from a league name, used as a foreign key across learning tables.
- **user_pick_signal**: A signal injected into predictions when a user has expressed a preference for a pick, currently applying a hardcoded +4 (agree) or -2 (disagree) impact.

---

## Requirements

---

### Requirement 1: AI Analysis Feedback Loop

**User Story:** As a system operator, I want the AI competition-analysis output to feed back into the LLM model weight, so that the LLM model's contribution is sized according to whether its analysis was predictively useful and not left as a static stub returning zero.

#### Acceptance Criteria

1. WHEN `run_learning_cycle()` is called and `_incorporate_ai_analysis(conn, rows)` executes, THE Self_Learner SHALL query the `competition_analysis` table for AI-generated round summaries that correspond to matches in the graded rows.
2. WHEN a graded prediction row has a corresponding `competition_analysis` entry from the same competition and round, THE Self_Learner SHALL compare the AI analysis's predicted winner or confidence direction against the actual match outcome.
3. WHEN the comparison in criterion 2 is evaluated, THE Self_Learner SHALL write one row per analysed match to the `ai_analysis_feedback` table, recording: `match_id`, `competition_key`, `analysis_correct` (boolean), `analysis_confidence_direction` (home/draw/away), `actual_result`, `created_at`.
4. THE Self_Learner SHALL create the `ai_analysis_feedback` table if it does not exist, with columns: `id` (integer primary key autoincrement), `match_id` (text), `competition_key` (text), `analysis_correct` (integer 0/1), `analysis_confidence_direction` (text), `actual_result` (text), `created_at` (text), and a unique index on `(match_id, competition_key)`.
5. WHEN at least 10 rows exist in `ai_analysis_feedback`, THE Self_Learner SHALL compute the AI analysis win rate as `SUM(analysis_correct) / COUNT(*)` and write or update a row in `learned_model_weights` for `model_name = 'llm'` using the same blend formula as other models.
6. WHEN `_incorporate_ai_analysis` completes successfully, THE Self_Learner SHALL return the count of rows written to `ai_analysis_feedback` as an integer greater than zero when matches were found.
7. IF the `competition_analysis` table does not exist or contains no rows matching the graded set, THEN THE Self_Learner SHALL return 0 without raising an exception.

---

### Requirement 2: User Behavior Feedback Loop

**User Story:** As a system operator, I want user pick-agreement signals to feed back into the system's calibration, so that the hardcoded `user_pick_signal` impact values (+4 agree, -2 disagree) are replaced by learned values that reflect actual user predictive accuracy.

#### Acceptance Criteria

1. WHEN `run_learning_cycle()` is called and `_incorporate_user_behavior(conn, rows)` executes, THE Self_Learner SHALL scan all graded prediction rows whose `signals_json` contains a signal named `user_pick_signal`.
2. WHEN a graded row with a `user_pick_signal` is found, THE Self_Learner SHALL extract the signal's `impact` field to determine user agreement (positive impact = user agreed with model, negative = user disagreed).
3. THE Self_Learner SHALL create the `user_behavior_outcomes` table if it does not exist, with columns: `id` (integer primary key autoincrement), `match_id` (text), `pick_type` (text), `user_agreed` (integer 0/1), `result` (text), `created_at` (text), and a unique index on `(match_id, pick_type)`.
4. WHEN a graded `user_pick_signal` row is processed, THE Self_Learner SHALL upsert a row into `user_behavior_outcomes` recording: `match_id`, `pick_type`, `user_agreed` (1 if impact > 0, else 0), `result`.
5. WHEN `user_behavior_outcomes` contains at least 15 rows where `user_agreed = 1`, THE Self_Learner SHALL compute `agree_win_rate = SUM(result='win' AND user_agreed=1) / SUM(user_agreed=1)` and write the learned agree-impact to a new `user_behavior_calibration` key in `learned_model_weights` or a dedicated config key, such that the agree impact is `round((agree_win_rate - 0.5) * 8, 1)` clamped to `[0, 6]`.
6. WHEN `user_behavior_outcomes` contains at least 15 rows where `user_agreed = 0`, THE Self_Learner SHALL compute `disagree_win_rate` similarly and write the learned disagree-impact as `round((0.5 - disagree_win_rate) * 4, 1)` clamped to `[-4, 0]`.
7. WHEN `_incorporate_user_behavior` completes, THE Self_Learner SHALL return the count of rows written to `user_behavior_outcomes`.
8. IF no rows with `user_pick_signal` exist in the graded set, THEN THE Self_Learner SHALL return 0 without raising an exception.

---

### Requirement 3: Signal Correlation Deduplication

**User Story:** As a prediction engineer, I want correlated signals from different sources deduplicated before aggregation, so that a single piece of evidence (e.g. H2H home dominance) does not accumulate triple weight just because three data sources all emitted it.

#### Acceptance Criteria

1. WHEN `SignalAggregator.add_signals()` is called with a list of signals, THE SignalAggregator SHALL group incoming signals by their resolved SIGNAL_CATEGORIES category.
2. WHEN two or more signals in the batch share the same category AND have different `source` values, THE SignalAggregator SHALL retain only the signal with the highest absolute `strength` value for that category and discard the others.
3. WHEN a signal is discarded by deduplication, THE SignalAggregator SHALL increment an internal `_dropped_duplicates` counter for the current aggregation pass.
4. WHEN `calculate_probabilities()` is called after deduplication, THE SignalAggregator SHALL include a `dropped_duplicate_count` key in the returned dict, equal to the value of `_dropped_duplicates`.
5. WHERE duplicate signals exist within a single source (same source AND same category), THE SignalAggregator SHALL retain all of them (deduplication is cross-source only).
6. WHEN deduplication occurs, THE SignalAggregator SHALL log a DEBUG-level message stating the category, the dropped source names, and the retained signal name.

---

### Requirement 4: Data Drift Detection and Emergency Recovery

**User Story:** As a system operator, I want the learning cycle to detect when a league's win rate has collapsed, so that the system automatically deprioritises that league and creates an alert before bad predictions accumulate further.

#### Acceptance Criteria

1. WHEN `run_learning_cycle()` executes, THE Self_Learner SHALL perform a drift detection pass after updating `league_accuracy` and `tournament_preferences`.
2. THE Self_Learner SHALL create a `system_events` table if it does not exist, with columns: `id` (integer primary key autoincrement), `event_type` (text), `league_key` (text), `pick_type` (text), `detail_json` (text), `created_at` (text).
3. WHEN a `league_key` + `pick_type` combination has at least 10 graded rows in the last 7 calendar days AND the computed `win_rate` for those rows is less than 0.40, THE Self_Learner SHALL set that league's `tournament_preferences.priority` to 7 (avoid) immediately, overriding any previously computed priority.
4. WHEN drift is detected per criterion 3, THE Self_Learner SHALL insert one row into `system_events` with `event_type = 'drift_detected'`, the `league_key`, `pick_type`, and a `detail_json` containing: `win_rate`, `samples`, `days_window: 7`, `action: 'priority_set_to_7'`.
5. WHEN one or more drift events are detected, THE Self_Learner SHALL call `clear_learned_parameter_cache()` to force fresh parameter loading for subsequent predictions.
6. WHEN `run_learning_cycle()` completes, THE Self_Learner SHALL include a `drift_events` key in its return dict containing the count of leagues that triggered drift detection.
7. IF a league previously had `priority = 7` due to drift and its 7-day win rate subsequently recovers above 0.45, THEN THE Self_Learner SHALL recalculate its priority using the standard mapping and insert a `system_events` row with `event_type = 'drift_recovery'`.

---

### Requirement 5: Learned Parameter Cache Invalidation

**User Story:** As a prediction engineer, I want the learned parameter cache to be invalidated reliably after every successful learning cycle, so that predictions never use stale weights from a failed or pre-cycle DB state.

#### Acceptance Criteria

1. WHEN `run_learning_cycle()` successfully commits the first `db_conn` block (signal weights, league accuracy, model weights, bias corrections), THE Self_Learner SHALL call `clear_learned_parameter_cache()` immediately after that commit.
2. WHEN `run_learning_cycle()` successfully commits the second `db_conn` block (thresholds, combinations, tournament preferences), THE Self_Learner SHALL call `clear_learned_parameter_cache()` again to ensure the cache reflects the fully updated state.
3. WHEN `clear_learned_parameter_cache()` is called, THE Learned_Parameters module SHALL clear the `lru_cache` on `get_learned_ensemble_weights`, `get_market_regime_params`, `get_pick_generator_thresholds`, `get_prediction_agent_params`, `get_calibration_gap_thresholds`, `get_league_goal_average`, `get_frontend_engine_params`, `get_frontend_api_limits`, and `get_engine_learning_limits`.
4. WHEN any DB write in `run_learning_cycle()` raises an exception and the transaction is rolled back, THE Self_Learner SHALL NOT call `clear_learned_parameter_cache()` for that block, preserving the previously valid cached state.
5. THE `_graded_rows()` function in `learned_parameters.py` SHALL have a time-to-live (TTL) mechanism such that its `lru_cache` result is treated as stale after 3600 seconds and is re-fetched on the next call.
6. WHEN `_graded_rows()` TTL has elapsed, THE Learned_Parameters module SHALL re-execute the graded history SQL query rather than returning the stale cached tuple.

---

### Requirement 6: Data-Driven Away Baseline

**User Story:** As a prediction engineer, I want the away-win baseline used when all signals favour away to be derived from actual per-league data, so that leagues with 30% away-win rates are not treated the same as leagues with 45% away-win rates.

#### Acceptance Criteria

1. WHEN `SignalAggregator.calculate_probabilities()` enters the `all_favor_away` branch, THE SignalAggregator SHALL attempt to read an away-win rate for the current `league_key` from `league_accuracy`.
2. WHEN `league_accuracy` contains a row for the `league_key` with `pick_type = 'match_result'` and `samples >= 20`, THE SignalAggregator SHALL compute `away_baseline = away_wins / total` derived from that league's selection-level win data, rather than using the constant `0.54`.
3. WHEN no league-specific data with at least 20 samples exists, THE SignalAggregator SHALL fall back to the global away-win rate computed across all `league_accuracy` rows with `pick_type = 'match_result'`.
4. WHEN no global data exists at all, THE SignalAggregator SHALL fall back to the hardcoded constant `0.54` as the final default.
5. THE SignalAggregator SHALL populate the `league_outcome_distribution` table during `run_learning_cycle()`, writing one row per league with columns: `league_key` (text primary key), `home_rate` (real), `draw_rate` (real), `away_rate` (real), `samples` (integer), `last_updated` (text).
6. WHEN `league_outcome_distribution` is populated, THE Self_Learner SHALL compute the rates from graded history rows grouped by `league_key`, using only rows with `pick_type = 'match_result'` and at least 20 samples.

---

### Requirement 7: Data-Driven Mixed-Signal Base Probabilities

**User Story:** As a prediction engineer, I want the base probabilities in the mixed-signal ("else") branch of the signal aggregator to reflect per-league outcome distributions, so that leagues with 35% draw rates are not evaluated against a 30% draw assumption.

#### Acceptance Criteria

1. WHEN `SignalAggregator.calculate_probabilities()` enters the mixed-signal branch, THE SignalAggregator SHALL attempt to read per-league base rates from the `league_outcome_distribution` table for the current `league_key`.
2. WHEN `league_outcome_distribution` contains a row for `league_key` with `samples >= 20`, THE SignalAggregator SHALL replace the static base probabilities `(home=0.45, draw=0.30, away=0.25)` with the learned `(home_rate, draw_rate, away_rate)` values from that row.
3. WHEN no league-specific row exists in `league_outcome_distribution`, THE SignalAggregator SHALL fall back to the global rates across all leagues in `league_outcome_distribution`.
4. WHEN no rows exist in `league_outcome_distribution`, THE SignalAggregator SHALL fall back to the static constants `(home=0.45, draw=0.30, away=0.25)`.
5. WHEN learned base probabilities are used, THE SignalAggregator SHALL include a `base_probs_source` key in the returned dict with value `'learned'`, `'global_fallback'`, or `'static_fallback'` to indicate which source was used.

---

### Requirement 8: Learned Context Penalty Adjustments

**User Story:** As a prediction engineer, I want the context penalty values in `contextual_intelligence.py` to be overridable by learned data, so that if "friendly" matches in a specific context have historically had strong win rates, the penalty is reduced rather than remaining fixed.

#### Acceptance Criteria

1. THE Self_Learner SHALL create a `context_penalty_adjustments` table if it does not exist, with columns: `context_tag` (text not null), `league_key` (text not null default `'__global__'`), `penalty_override` (real), `samples` (integer), `win_rate` (real), `last_updated` (text), primary key `(context_tag, league_key)`.
2. WHEN `run_learning_cycle()` executes, THE Self_Learner SHALL aggregate graded predictions grouped by their `context_json` context tags (from `prediction_candidate_history.context_json`) and league, computing the win rate per `(context_tag, league_key)` pair.
3. WHEN a `(context_tag, league_key)` pair has at least 10 graded samples, THE Self_Learner SHALL write a `penalty_override` value computed as `round((0.5 - win_rate) * 12, 1)` clamped to `[-10, 4]`, where a higher win rate produces a less negative (or positive) override.
4. WHEN `_match_context()` in `contextual_intelligence.py` computes the adjustment for a context tag, THE Contextual_Intelligence module SHALL query `context_penalty_adjustments` for the current league and context tag before applying the hardcoded value.
5. WHEN a `context_penalty_adjustments` row exists for the `(context_tag, league_key)` pair with at least 10 samples, THE Contextual_Intelligence module SHALL use `penalty_override` instead of the hardcoded value for that tag.
6. WHEN no learned row exists for the specific league but a `__global__` row exists with at least 10 samples, THE Contextual_Intelligence module SHALL use the global override.
7. WHEN neither a league-specific nor global override exists, THE Contextual_Intelligence module SHALL fall back to the existing hardcoded penalties as defaults.

---

### Requirement 9: Non-Empty Ensemble Base Weights

**User Story:** As a prediction engineer, I want the ensemble model to produce meaningful non-neutral predictions from day one, so that `total_weight = 0.0` and a 33/33/33 fallback never occur when learned weights have not yet accumulated.

#### Acceptance Criteria

1. THE `_BASE_WEIGHTS` dict in `app/models/ensemble.py` SHALL be non-empty and SHALL contain exactly the keys `dixon_coles`, `elo`, `poisson`, `rules`, and `llm` with values `0.30`, `0.25`, `0.15`, `0.20`, and `0.10` respectively, summing to `1.00`.
2. WHEN `_get_weights()` is called and `get_learned_weights()` returns an empty dict (no learned weights available), THE Ensemble module SHALL return `_BASE_WEIGHTS` rather than an empty dict.
3. WHEN `ensemble_prediction()` is called and `_get_weights()` returns `_BASE_WEIGHTS`, THE Ensemble module SHALL use these default weights to produce a non-neutral result whose best probability is strictly greater than `0.34` when any non-trivial model output is present.
4. WHEN `total_weight == 0.0` despite `_BASE_WEIGHTS` being non-empty (because no model produced valid output), THE Ensemble module SHALL return the existing neutral `33/33/33` fallback with `limited_signal: True`.
5. WHEN learned weights become available and `_get_weights()` returns them, THE Ensemble module SHALL use learned weights in preference to `_BASE_WEIGHTS`.

---

### Requirement 10: Direction-Aware Signal Category Fallback

**User Story:** As a prediction engineer, I want `_category_for_signal()` to correctly distinguish home-side from away-side signals when a signal name is not found in SIGNAL_CATEGORIES, so that `away_recent_history` does not incorrectly map to `home_form`.

#### Acceptance Criteria

1. WHEN `_category_for_signal()` is called with a signal name not present in any `SIGNAL_CATEGORIES` keyword set, THE SignalAggregator SHALL inspect the signal name for the substrings `"away"` and `"home"` before assigning a fallback category.
2. WHEN the signal name contains `"away"` and also contains `"form"`, `"recent_history"`, or `"team_watcher"`, THE SignalAggregator SHALL return `"away_form"` as the fallback category.
3. WHEN the signal name contains `"away"` and also contains `"table"`, `"standing"`, or `"league_strength"`, THE SignalAggregator SHALL return `"away_table"` as the fallback category.
4. WHEN the signal name contains `"away"` and also contains `"goal"`, THE SignalAggregator SHALL return `"away_goal_pressure"` as the fallback category.
5. WHEN the signal name contains `"away"` and also contains `"odds"` or `"market"`, THE SignalAggregator SHALL return `"away_odds"` as the fallback category.
6. WHEN the signal name contains `"home"` (and no `"away"`), THE SignalAggregator SHALL apply the equivalent home-side mapping: `"home_form"`, `"home_table"`, `"home_goal_pressure"`, or `"home_odds"`.
7. WHEN the signal name contains neither `"away"` nor `"home"`, THE SignalAggregator SHALL continue with the existing fallback logic unchanged.

---

### Requirement 11: Team Prediction History Signals

**User Story:** As a prediction engineer, I want the team-level prediction accuracy stored in `team_competitions` to influence confidence adjustments for upcoming matches, so that teams with a poor historical prediction record generate a risk signal and teams with strong records generate a boost signal.

#### Acceptance Criteria

1. WHEN `predict_enriched_match()` in `enriched_prediction.py` computes the ensemble prediction, THE Enriched_Prediction module SHALL query `team_competitions` for both the home and away team using their normalised team keys.
2. WHEN a team's `prediction_total >= 10` AND `prediction_correct / prediction_total < 0.40` in `team_competitions`, THE Enriched_Prediction module SHALL add a signal named `team_prediction_history_risk` with `impact = -3` to the prediction's signals list for that team.
3. WHEN a team's `prediction_total >= 10` AND `prediction_correct / prediction_total >= 0.60` in `team_competitions`, THE Enriched_Prediction module SHALL add a signal named `team_prediction_history_boost` with `impact = +2` to the prediction's signals list for that team.
4. WHEN both home and away teams qualify for signals under criteria 2 or 3, THE Enriched_Prediction module SHALL add both signals independently.
5. WHEN `prediction_total < 10`, THE Enriched_Prediction module SHALL not add either signal for that team.
6. WHEN the `team_competitions` table does not exist or the query raises an exception, THE Enriched_Prediction module SHALL continue without the team history signals and SHALL NOT raise an exception.

---

### Requirement 12: Consistent Signal Outcomes Writes

**User Story:** As a prediction engineer, I want every graded prediction to write a `signal_outcomes` row for each decision signal, so that the signal win-rate learner operates on a complete and consistent dataset rather than a partial subset.

#### Acceptance Criteria

1. WHEN a prediction is graded and `result IN ('win', 'loss')`, THE Self_Learner SHALL verify that `signal_outcomes` contains at least one row for that `match_id`.
2. WHEN a graded prediction lacks `signal_outcomes` rows, THE Self_Learner SHALL back-fill them during `run_learning_cycle()` using `_decision_signals_for_row()` to identify the decision signals.
3. WHEN back-filling `signal_outcomes`, THE Self_Learner SHALL write one row per decision signal per graded prediction with columns: `signal_name`, `match_id`, `tournament` (from `league_name`), `country` (from `country_name`), `result`, `created_at`.
4. THE Self_Learner SHALL create the `signal_outcomes` table if it does not exist, with at minimum the columns required by criteria 3, and a unique index on `(signal_name, match_id)` to prevent duplicate writes.
5. WHEN `signal_outcomes` already contains a row for `(signal_name, match_id)`, THE Self_Learner SHALL skip that row (upsert with no-op on conflict) to preserve the original write.
6. WHEN a prediction path grades a match and calls the grading function, THE grading function SHALL write `signal_outcomes` rows using the decision signals identified from `signals_json`, ensuring the back-fill in criterion 2 is only a safety net, not the primary write path.

---

### Requirement 13: H2H Signal Synthesis from SofaScore Data

**User Story:** As a prediction engineer, I want the H2H signal to be computed directly from SofaScore's `last_meetings` data during prediction rather than relying entirely on external injection, so that H2H signal categories always have a value derived from real match data.

#### Acceptance Criteria

1. WHEN `_rules_prediction()` in `enriched_prediction.py` is called and `sofascore_detail.last_meetings` is present and contains at least 3 entries, THE Enriched_Prediction module SHALL compute H2H dominance from those entries.
2. WHEN H2H dominance is computed, THE Enriched_Prediction module SHALL count wins for the home team, wins for the away team, and draws across all meetings up to a maximum of 10 most recent entries.
3. WHEN the home team has strictly more wins than the away team in the last meetings AND the home dominance ratio (`home_wins / total_meetings`) >= 0.5, THE Enriched_Prediction module SHALL emit an `h2h_home` signal with `strength = round(home_wins / total_meetings, 2)`.
4. WHEN the away team has strictly more wins than the home team in the last meetings AND the away dominance ratio >= 0.5, THE Enriched_Prediction module SHALL emit an `h2h_away` signal with `strength = round(away_wins / total_meetings, 2)`.
5. WHEN neither team dominates (abs(home_wins - away_wins) <= 1 OR total_meetings < 3), THE Enriched_Prediction module SHALL emit an `h2h_draw` signal with `strength = round(draws / total_meetings, 2)`.
6. WHEN an H2H signal (`h2h_home`, `h2h_away`, or `h2h_draw`) has already been injected by an external source with higher strength than the computed value, THE Enriched_Prediction module SHALL keep the externally injected signal and discard the computed one.
7. IF `sofascore_detail` is absent or `last_meetings` is absent or has fewer than 3 entries, THEN THE Enriched_Prediction module SHALL not emit any H2H signal from this path and SHALL proceed without error.

---

### Requirement 14: Competition Round Analysis Injection

**User Story:** As a prediction engineer, I want recent AI-generated competition round analysis to be attached to upcoming match predictions, so that insights about current league form and standings momentum are accessible to the rules engine.

#### Acceptance Criteria

1. WHEN `apply_known_competition_context()` in `competition_special.py` processes an upcoming match document, THE Competition_Special module SHALL query the `competition_analysis` table for the most recent row matching the match's `competition_key`.
2. WHEN a `competition_analysis` row is found and its `created_at` timestamp is within 7 calendar days of the current UTC time, THE Competition_Special module SHALL attach it to the match document as `doc["competition_round_analysis"]`.
3. WHEN `competition_round_analysis` is absent or older than 7 days, THE Competition_Special module SHALL not attach it and SHALL proceed without error.
4. WHEN `_rules_prediction()` in `enriched_prediction.py` is called and `doc["competition_round_analysis"]` is present, THE Enriched_Prediction module SHALL extract form and standings insight from the analysis.
5. WHEN the competition round analysis indicates a clear form leader (one team ranked in the top 2 of recent form for that competition), THE Enriched_Prediction module SHALL add a `competition_momentum` signal with `impact = +1` favouring that team's direction.
6. WHEN the analysis does not provide a clear form leader or the field is absent, THE Enriched_Prediction module SHALL not add a `competition_momentum` signal.
7. WHEN `competition_round_analysis` is present, THE Enriched_Prediction module SHALL include a `competition_context_applied: true` flag in the prediction's audit output.

---

### Requirement 15: Tournament Priority Influence on Confidence

**User Story:** As a prediction engineer, I want the enrichment queue priority for a league to apply a small confidence modifier, so that predictions for well-understood leagues receive a small confidence boost and predictions for poorly-performing leagues receive a penalty.

#### Acceptance Criteria

1. WHEN `SignalAggregator._calculate_confidence()` is called, THE SignalAggregator SHALL read the current `tournament_preferences.priority` for the active `league_key` from the database.
2. WHEN the retrieved priority is 0 or 1 (high performing league), THE SignalAggregator SHALL add `+0.05` to the calculated confidence value before clamping.
3. WHEN the retrieved priority is 6 or 7 (poor performing or avoid), THE SignalAggregator SHALL subtract `0.10` from the calculated confidence value before clamping.
4. WHEN the retrieved priority is 2, 3, 4, or 5, THE SignalAggregator SHALL apply no confidence modifier from tournament priority.
5. WHEN the `tournament_preferences` table does not exist or no row exists for the `league_key`, THE SignalAggregator SHALL apply no confidence modifier (treat as priority 4).
6. WHEN the confidence modifier is applied, THE SignalAggregator SHALL still clamp the final confidence to the range `[0.1, 0.95]`.

---

### Requirement 16: Signal Stats Cache Size Guard

**User Story:** As a platform engineer, I want the `_SIGNAL_STATS_BATCH_CACHE` in `signal_aggregator.py` to be bounded in size, so that memory does not grow unbounded across a long-running process.

#### Acceptance Criteria

1. WHEN `prefetch_signal_stats()` or `global_signal_stats()` would cause `_SIGNAL_STATS_BATCH_CACHE` to exceed 1000 entries, THE SignalAggregator module SHALL clear the cache before writing the new entries.
2. WHEN the cache is cleared due to size overflow, THE SignalAggregator module SHALL log a WARNING-level message stating the cache was cleared due to size overflow and including the current entry count.
3. WHEN `reset_signal_stats_cache()` is called, THE SignalAggregator module SHALL unconditionally clear the cache regardless of size.
4. THE `reset_signal_stats_cache()` function SHALL be called at the start of each new prediction pass in `enriched_prediction.py` to ensure per-pass isolation.

---

### Requirement 17: Lineup Detection Key Normalisation

**User Story:** As a prediction engineer, I want lineup detection to check all known lineup key names across data sources, so that the missing-lineup confidence penalty is not applied when lineup data is present under a non-standard key.

#### Acceptance Criteria

1. WHEN `_match_context()` in `contextual_intelligence.py` evaluates whether lineup data is available, THE Contextual_Intelligence module SHALL check all of the following keys on the document: `lineups`, `starting_xi`, `confirmed_lineups`, `home_lineup`, `away_lineup`.
2. WHEN `sofascore_detail` is present on the document, THE Contextual_Intelligence module SHALL also check `sofascore_detail.lineups`, `sofascore_detail.starting_xi`, and `sofascore_detail.confirmed_lineups`.
3. WHEN any of the keys in criteria 1 or 2 is present and non-empty (a non-null, non-empty value), THE Contextual_Intelligence module SHALL consider lineup data available and SHALL NOT apply the `-2` lineup-window adjustment.
4. WHEN none of the keys in criteria 1 or 2 are present or all are empty, AND the `lineup_window` time condition is met, THE Contextual_Intelligence module SHALL apply the `-2` adjustment as currently implemented.

---

### Requirement 18: Confidence Calibration Band Separation

**User Story:** As a prediction analyst, I want the 90–99% confidence range to have its own calibration bucket, so that overconfident very-high-confidence predictions are separately measured and not conflated with the 80–89% bucket.

#### Acceptance Criteria

1. WHEN the confidence calibrator in `app/enrichment/confidence_calibrator.py` assigns a calibration band, THE Confidence_Calibrator SHALL use the following bands: `[0,10)`, `[10,20)`, `[20,30)`, `[30,40)`, `[40,50)`, `[50,60)`, `[60,70)`, `[70,80)`, `[80,90)`, `[90,100)`.
2. WHEN a confidence value is in the range `[80, 90)`, THE Confidence_Calibrator SHALL assign `band_low = 80`.
3. WHEN a confidence value is in the range `[90, 100)`, THE Confidence_Calibrator SHALL assign `band_low = 90`.
4. WHEN existing `confidence_calibration` rows have `band_low = 80` and cover predictions with confidence >= 90, THE Self_Learner or calibrator SHALL migrate those rows to the correct band during the next write cycle.
5. WHEN reading calibration data for a prediction with confidence >= 90, THE Confidence_Calibrator SHALL query `band_low = 90`, not `band_low = 80`.

---

## Correctness Properties

The following properties MUST be verifiable with property-based tests using the Hypothesis library (or equivalent). These tests capture the semantic invariants that must hold regardless of input variation.

### Property 1: Probability Sum Invariant

**Target:** `SignalAggregator.calculate_probabilities()`

FOR ALL non-empty signal lists and any league key, THE SignalAggregator SHALL return a result where `home_prob + draw_prob + away_prob` is within `±0.001` of `1.0`.

- **Pattern:** Invariant
- **Test approach:** Generate arbitrary lists of signals with random names, directions, and strength values. Assert that the returned probabilities sum to 1.0 within floating-point tolerance after every code path (all-home, all-away, mixed).

### Property 2: Away Baseline is Data-Driven When Data Exists

**Target:** `SignalAggregator.calculate_probabilities()` when `all_favor_away = True`

WHEN a league has at least 20 graded away-win samples in `league_accuracy`, THE SignalAggregator SHALL use a baseline that differs from the constant `0.54` by at least `0.001` when the league's actual away-win rate differs from `0.54` by at least `0.01`.

- **Pattern:** Model-based testing (learned model vs. constant)
- **Test approach:** Seed the database with synthetic league data at known away-win rates (e.g. 0.30, 0.40, 0.48, 0.60). Run `calculate_probabilities()` with all-away signals and assert the returned `away_prob` reflects the seeded rate, not `0.54`.

### Property 3: Signal Deduplication Leaves One Per Category Per Source

**Target:** `SignalAggregator.add_signals()` followed by `calculate_probabilities()`

AFTER `add_signals()` with a list containing multiple signals of the same category from different sources, THE SignalAggregator SHALL have at most one signal per category in its internal `signals` list.

- **Pattern:** Invariant (deduplication completeness)
- **Test approach:** Generate signal batches where 2–5 signals share the same resolved category but have different `source` values. Assert that after `add_signals()`, no two entries in `self.signals` share the same category AND have different source values.

### Property 4: Learning Cycle Idempotency

**Target:** `run_learning_cycle()`

WHEN `run_learning_cycle()` is called twice in sequence with the same graded history (no new rows added between calls), THE Self_Learner SHALL produce identical values in `signal_weights`, `league_accuracy`, `learned_model_weights`, and `signal_combination_memory` after both calls.

- **Pattern:** Idempotence (f(x) = f(f(x)))
- **Test approach:** Load a fixed set of synthetic graded rows into the DB. Run the cycle twice. Assert the table contents (weights, win_rates, samples) are byte-for-byte identical after the second run compared to after the first. Timestamps are excluded from the equality check.

### Property 5: Cache Invalidation Correctness

**Target:** `get_learned_ensemble_weights()` after `run_learning_cycle()`

WHEN `run_learning_cycle()` completes, THE Learned_Parameters module SHALL return values from `get_learned_ensemble_weights()` that are consistent with the `learned_model_weights` table contents written in that cycle (not values from a previous cycle).

- **Pattern:** Round-trip (write → read → compare)
- **Test approach:** Run a learning cycle with known synthetic data, observe the written `learned_model_weights` rows. Call `get_learned_ensemble_weights()` immediately after. Assert the returned dict matches the DB rows within `±0.0001`. Confirm that a subsequent cache-only call (without a DB write) returns the same values.

### Property 6: Drift Detection Coverage

**Target:** `run_learning_cycle()` — drift detection branch

FOR ALL leagues where the 7-day win rate is below `0.40` with at least 10 samples, THE Self_Learner SHALL set `tournament_preferences.priority = 7` after `run_learning_cycle()` completes.

- **Pattern:** Metamorphic (if win_rate < threshold → priority must equal 7)
- **Test approach:** Seed the DB with per-league graded rows at various win rates and sample counts. Run the cycle. Assert that every (league_key, pick_type) combination meeting the threshold conditions has `priority = 7` in `tournament_preferences`, and every combination not meeting them has `priority != 7`.

### Property 7: Base Weight Fallback Produces Non-Neutral Output

**Target:** `ensemble_prediction()` when no learned weights exist

WHEN `get_learned_weights()` returns `{}` and `_BASE_WEIGHTS` is the non-empty default, THE Ensemble module SHALL return a prediction whose best probability is strictly greater than `0.34` for any input where at least one model produces non-trivial output.

- **Pattern:** Error condition / non-neutral invariant
- **Test approach:** Clear the `learned_model_weights` table. Call `ensemble_prediction()` with arbitrary non-null Dixon-Coles, ELO, or Poisson outputs. Assert `max(home_win, draw, away_win) > 34.0` and `limited_signal` is absent or `False`.

### Property 8: Team Prediction History Signal Presence

**Target:** `predict_enriched_match()` — team history signal injection

FOR ALL matches where a participating team has `prediction_total >= 10` and `prediction_correct / prediction_total < 0.40` in `team_competitions`, THE prediction output SHALL include a `team_prediction_history_risk` signal with `impact <= -3`.

- **Pattern:** Model-based / metamorphic (team accuracy below threshold → risk signal must appear)
- **Test approach:** Seed `team_competitions` with a team at < 40% accuracy. Invoke the team history signal lookup logic. Assert the returned signals list contains `team_prediction_history_risk` with the correct impact. Assert no such signal appears when `prediction_total < 10`.

---

### Requirement 19: Temporal Decay in Learning Cycle

**User Story:** As a prediction engineer, I want historical wins and losses to be weighted by their age so that recent evidence counts more than year-old evidence, instead of treating a result from 2024 the same as a result from last week.

#### Acceptance Criteria

1. WHEN `run_learning_cycle()` computes win rates for signal_weights, league_accuracy, and learned_model_weights, THE Self_Learner SHALL apply a per-row decay weight of `DECAY_FACTOR ** age_in_weeks` where `age_in_weeks = max(0, days_since_created / 7.0)` and `DECAY_FACTOR = 0.92`.
2. WHEN two graded rows have identical outcomes (both wins) but different ages, the older row SHALL contribute less to the aggregate win rate than the newer row.
3. WHEN `age_in_weeks = 0` (row created today), the decay weight SHALL be exactly `1.0` (no decay).
4. WHEN `age_in_weeks = 52` (one year old), the decay weight SHALL be approximately `0.014` (`0.92^52`).
5. THE weighted win rate computation SHALL use: `weighted_wins = sum(weight_i for rows where result='win')` and `weighted_total = sum(weight_i for all rows)`, producing `win_rate = weighted_wins / weighted_total`.
6. WHEN all rows are equally recent (same created_at), the decay-weighted win rate SHALL equal the unweighted win rate within `±0.001`.
7. THE `DECAY_FACTOR` constant SHALL remain configurable at the module level (not hardcoded inside functions).

---

### Requirement 20: Confidence-Weighted Learning

**User Story:** As a prediction engineer, I want high-confidence outcomes to influence signal weights more than low-confidence coin-flip outcomes, so that a 90% confidence loss penalises a signal more than a 55% confidence loss.

#### Acceptance Criteria

1. WHEN `_tally()` or the equivalent win-rate aggregation function processes graded rows, THE Self_Learner SHALL multiply each row's win/loss contribution by `confidence_weight = row["confidence"] / 100.0`.
2. WHEN `row["confidence"]` is NULL or missing, THE Self_Learner SHALL use a default `confidence_weight = 0.5`.
3. WHEN two rows have the same outcome but different confidence values (e.g. 90 vs 55), the row with higher confidence SHALL contribute more to the aggregate win/loss tally.
4. THE confidence weighting SHALL be applied in addition to (multiplied with) the temporal decay weight from R19, producing a combined row weight of `decay_weight * confidence_weight`.
5. WHEN all rows have `confidence = 100`, the confidence-weighted win rate SHALL equal the unweighted win rate within `±0.001`.
6. WHEN all rows have `confidence = 50`, the confidence-weighted win rate SHALL equal the unweighted win rate within `±0.001`.
7. THE combined weighting (temporal decay × confidence) SHALL be applied consistently to signal_weights, league_accuracy, and learned_model_weights computations.

---

### Requirement 21: Symmetric League Adjustment Caps

**User Story:** As a prediction engineer, I want the league accuracy adjustment caps to be symmetric so that the system does not structurally penalise underperforming leagues 25% more harshly than it rewards overperforming leagues.

#### Acceptance Criteria

1. THE league accuracy adjustment in `app/enrichment/enriched_prediction.py` SHALL use symmetric caps such that the magnitude of the maximum positive adjustment equals the magnitude of the maximum negative adjustment.
2. WHEN the league accuracy boost cap is raised to `+10`, THE maximum positive league adjustment SHALL be `+10` points.
3. WHEN the league accuracy penalty cap is `-10`, THE maximum negative league adjustment SHALL be `-10` points (no change to negative side).
4. THE `max_boost` and `max_penalty` constants (or equivalent inline values) SHALL be equal in absolute magnitude.
5. WHEN a league has >= 65% win rate, the confidence adjustment SHALL not exceed `+10`.
6. WHEN a league has < 50% win rate, the confidence adjustment SHALL not be more negative than `-10`.

---

### Requirement 22: Confidence-Weighted Bias Correction

**User Story:** As a prediction engineer, I want the bias correction multiplier floor to scale with actual loss rate rather than applying a flat 28% suppression at the trigger threshold, so that the correction is proportional to the degree of miscalibration.

#### Acceptance Criteria

1. WHEN computing the bias correction multiplier in `app/monitoring/self_learner.py`, THE Self_Learner SHALL use a dynamic floor formula `multiplier_floor = max(0.72, 1.0 - (loss_rate - 0.50) * 1.4)` rather than the hardcoded floor of `0.72`.
2. WHEN `loss_rate = 0.58` (the current trigger threshold), the computed `multiplier_floor` SHALL be approximately `0.888` (`1.0 - 0.112`), meaning only 11.2% suppression instead of 28%.
3. WHEN `loss_rate = 0.65`, the computed `multiplier_floor` SHALL be approximately `0.79` (`1.0 - 0.21`), not clamped below `0.72`.
4. WHEN `loss_rate = 0.71` or higher, the computed `multiplier_floor` SHALL be `0.72` (the absolute floor).
5. THE `overconfidence >= 0.08` trigger condition SHALL remain unchanged.
6. WHEN the `loss_rate` flag triggers but `overconfidence` does not, THE multiplier_floor SHALL still use the dynamic formula.

---

### Requirement 23: Signal Combination Memory Sample Guard Increase

**User Story:** As a prediction engineer, I want the minimum sample threshold for signal combination weights to be raised from 5 to 12, so that combination weights are not computed from insufficient data that may overfit to noise.

#### Acceptance Criteria

1. WHEN `_learn_signal_combinations()` in `app/monitoring/self_learner.py` evaluates whether to write a combination weight, THE Self_Learner SHALL require at least 12 graded samples for a signal combination before computing or updating its weight.
2. WHEN a signal combination has fewer than 12 samples, THE Self_Learner SHALL skip writing that combination and SHALL NOT delete any existing row for that combination.
3. WHEN a signal combination has exactly 12 samples, THE Self_Learner SHALL write the combination weight.
4. WHEN a signal combination has more than 12 samples, THE Self_Learner SHALL write the combination weight as before.
5. THE minimum sample threshold for signal combinations SHALL be defined as a named constant `MIN_COMBINATION_SAMPLES = 12` at the module level, separate from the signal weight threshold `MIN_SAMPLES = 15` and league threshold `MIN_LEAGUE_SAMPLES = 5`.

---

### Requirement 24: Cross-League Transfer Buckets

**User Story:** As a prediction engineer, I want signal weights to fall back to a country-level bucket before falling back to global, so that leagues with sparse data can share learning from other competitions in the same country rather than immediately falling back to a global average.

#### Acceptance Criteria

1. WHEN `run_learning_cycle()` writes signal weights, THE Self_Learner SHALL also compute and write per-country signal weights using `(signal_name, country_key)` as the lookup key, where `country_key` is derived by normalising `row["country_name"]` using `_norm_league()` or equivalent.
2. WHEN writing country-level signal weights, THE Self_Learner SHALL require at least `MIN_LEAGUE_SAMPLES` (5) samples per `(signal_name, country_key)` pair.
3. WHEN a country-level weight is written, THE Self_Learner SHALL store it in the `signal_weights` table using `league_key = country_key` (the normalised country name), so the existing schema is reused.
4. WHEN `enriched_prediction.py` or the signal weight lookup resolves a weight for `(signal_name, league_key)`, THE lookup SHALL fall back to `(signal_name, country_key)` when no league-specific row exists (or the league has fewer than `MIN_LEAGUE_SAMPLES` samples).
5. WHEN no country-level weight exists, THE lookup SHALL fall back to `(signal_name, "__global__")` as currently implemented.
6. THE fallback chain SHALL be: league-specific → country-level → global → default weight.
7. WHEN computing country-level weights, THE Self_Learner SHALL aggregate all graded rows for the same `country_key` (regardless of specific league) to compute the country-level win rate for that signal.
