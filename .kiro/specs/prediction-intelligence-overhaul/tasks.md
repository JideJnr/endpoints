# Implementation Plan: Prediction Intelligence Overhaul

## Overview

Implement 18 requirements across six source files in four severity tiers. The implementation order follows hard dependency constraints: new tables must be created before they are read, cache infrastructure must be fixed before drift detection runs, and property-based tests are the final group after all implementation is complete.

The design document, requirements, and source code are treated as the authoritative reference throughout. Every task references specific requirement sub-clauses and identifies which file(s) to modify.

---

## Tasks

---

## Tier 1 — Critical Stubs and Broken Feedback Loops (R1–R5)

These tasks fix zero-value feedback paths and broken cache infrastructure. They must be completed before any other tier because downstream learning tables depend on them and the drift-detection work (R4) requires the `system_events` table created here.

- [ ] 1. Add `ai_analysis_feedback`, `user_behavior_outcomes`, and `system_events` tables to `_init_learner_tables()`
  - In `app/monitoring/self_learner.py`, add three new `CREATE TABLE IF NOT EXISTS` blocks inside `_init_learner_tables(conn)`:
    - `ai_analysis_feedback` with columns: `id` (INTEGER PK AUTOINCREMENT), `match_id` (TEXT NOT NULL), `competition_key` (TEXT NOT NULL), `analysis_correct` (INTEGER NOT NULL DEFAULT 0), `analysis_confidence_direction` (TEXT), `actual_result` (TEXT), `created_at` (TEXT NOT NULL DEFAULT current_timestamp), UNIQUE(`match_id`, `competition_key`), and an index on `(competition_key)`.
    - `user_behavior_outcomes` with columns: `id` (INTEGER PK AUTOINCREMENT), `match_id` (TEXT NOT NULL), `pick_type` (TEXT NOT NULL), `user_agreed` (INTEGER NOT NULL DEFAULT 0), `result` (TEXT NOT NULL), `created_at` (TEXT NOT NULL DEFAULT current_timestamp), UNIQUE(`match_id`, `pick_type`).
    - `system_events` with columns: `id` (INTEGER PK AUTOINCREMENT), `event_type` (TEXT NOT NULL), `league_key` (TEXT), `pick_type` (TEXT), `detail_json` (TEXT), `created_at` (TEXT NOT NULL DEFAULT current_timestamp), and an index on `(event_type, created_at DESC)`.
  - _Requirements: R1.4, R2.3, R4.2_

- [ ] 2. Implement `_incorporate_ai_analysis(conn, rows)` — replace stub
  - [ ] 2.1 Write the full body of `_incorporate_ai_analysis(conn, rows)` in `app/monitoring/self_learner.py`
    - For each graded row, derive `competition_key` from `league_name` via `_norm_league()` and extract `round_name` from `audit_json` if available.
    - Query `competition_analysis` for the most recent row matching `competition_key` within 30 days (catch `sqlite3.OperationalError` if the table is absent and return 0).
    - Parse `analysis_text` (JSON) to extract `top_table`; determine `analysis_confidence_direction` from which team (home/away) is at rank 1; compare against `row["selection"]` to derive `analysis_correct` (1/0).
    - Upsert into `ai_analysis_feedback` with `ON CONFLICT(match_id, competition_key) DO NOTHING`.
    - When `ai_analysis_feedback` has >= 10 rows: compute `ai_win_rate = SUM(analysis_correct) / COUNT(*)`, apply the same blend formula as other models, and upsert `learned_model_weights` for `model_name = 'llm'`.
    - Return the count of rows upserted to `ai_analysis_feedback`.
    - _Requirements: R1.1, R1.2, R1.3, R1.5, R1.6, R1.7_
  - [ ] 2.2 Write unit test for `_incorporate_ai_analysis` with no `competition_analysis` table
    - Confirm the function returns 0 and does not raise when the table is missing.
    - _Requirements: R1.7_

- [ ] 3. Implement `_incorporate_user_behavior(conn, rows)` — replace stub
  - [ ] 3.1 Write the full body of `_incorporate_user_behavior(conn, rows)` in `app/monitoring/self_learner.py`
    - Iterate graded rows, parse `signals_json`, filter for `name == 'user_pick_signal'`.
    - Extract `impact`; set `user_agreed = 1` if `impact > 0` else `0`.
    - Upsert into `user_behavior_outcomes` with `ON CONFLICT(match_id, pick_type) DO NOTHING`.
    - After batch upsert: if `SUM(user_agreed=1) >= 15`, compute `agree_win_rate` and write `round((agree_win_rate - 0.5) * 8, 1)` clamped to `[0, 6]` to a `user_behavior_calibration` key in `learned_model_weights`.
    - If `SUM(user_agreed=0) >= 15`, compute `disagree_win_rate` and write `round((0.5 - disagree_win_rate) * 4, 1)` clamped to `[-4, 0]`.
    - Return count of rows written to `user_behavior_outcomes`.
    - _Requirements: R2.1, R2.2, R2.4, R2.5, R2.6, R2.7, R2.8_
  - [ ] 3.2 Write unit test for `_incorporate_user_behavior` when no `user_pick_signal` rows exist
    - Confirm the function returns 0 and does not raise.
    - _Requirements: R2.8_

- [ ] 4. Implement signal deduplication in `SignalAggregator` (R3)
  - [ ] 4.1 Add `_dropped_duplicates: int = 0` instance attribute in `SignalAggregator.__init__()` in `app/enrichment/signal_aggregator.py`
    - _Requirements: R3.3_
  - [ ] 4.2 Rewrite `add_signals()` to group incoming signals by `(category, source)` and deduplicate cross-source
    - After normalizing each signal, group by resolved `category`.
    - For each category, collect all incoming entries. Within the same `source`, allow all through unchanged.
    - Across different sources in the same category, retain only the entry with the highest `abs(strength)`; discard the rest.
    - For each discarded signal, increment `self._dropped_duplicates` and log a DEBUG-level message: `"[signal_aggregator] dedup: category={category} dropped={dropped_sources} kept={kept_name}"`.
    - Same-source duplicate signals (same source AND same category) are passed through as-is (R3.5).
    - _Requirements: R3.1, R3.2, R3.3, R3.5, R3.6_
  - [ ] 4.3 Add `"dropped_duplicate_count": self._dropped_duplicates` to the dict returned by `calculate_probabilities()`
    - _Requirements: R3.4_
  - [ ] 4.4 Write unit tests for deduplication behaviour
    - Case 1: two signals with same category, different sources → only the stronger is retained.
    - Case 2: two signals with same category, same source → both are retained.
    - Case 3: `dropped_duplicate_count` is correctly reflected in the `calculate_probabilities()` return value.
    - _Requirements: R3.1–R3.5_

- [ ] 5. Fix TTL cache for `_graded_rows()` in `learned_parameters.py` (R5)
  - [ ] 5.1 Replace the `@lru_cache(maxsize=1)` decorator on `_graded_rows()` with a module-level time-aware cache in `app/monitoring/learned_parameters.py`
    - Declare three module-level variables: `_GRADED_ROWS_CACHE: tuple[dict, ...] | None = None`, `_GRADED_ROWS_FETCHED_AT: float = 0.0`, `_GRADED_ROWS_TTL: int = 3600`.
    - Inside `_graded_rows()`: if `_GRADED_ROWS_CACHE is not None` and `time.monotonic() - _GRADED_ROWS_FETCHED_AT < _GRADED_ROWS_TTL`, return the cached value immediately.
    - Otherwise, re-execute the `_GRADED_SQL` query; on exception, set `_GRADED_ROWS_CACHE = ()` and return it.
    - _Requirements: R5.5, R5.6_
  - [ ] 5.2 Update `clear_learned_parameter_cache()` to also reset `_GRADED_ROWS_CACHE = None` and `_GRADED_ROWS_FETCHED_AT = 0.0`
    - This forces an immediate re-fetch on the next call.
    - _Requirements: R5.3_
  - [ ] 5.3 Write unit test for TTL re-fetch behaviour
    - Confirm that `_graded_rows()` re-queries the DB after the TTL elapses (mock `time.monotonic` to fast-forward time).
    - Confirm that calling `clear_learned_parameter_cache()` immediately invalidates the cache.
    - _Requirements: R5.5, R5.6_

- [ ] 6. Fix cache invalidation placement in `run_learning_cycle()` (R5)
  - In `app/monitoring/self_learner.py`, move `clear_learned_parameter_cache()` to be called immediately after the **first** `conn.commit()` (the block that writes `signal_weights`, `league_accuracy`, `learned_model_weights`, `bias_corrections`).
  - Retain the existing `clear_learned_parameter_cache()` call after the **second** `conn.commit()` (thresholds, combinations, tournament preferences).
  - Wrap each call in a `try/except Exception` so a cache-clear error never rolls back a successful DB commit.
  - When any DB write raises an exception and the transaction is rolled back, do NOT call `clear_learned_parameter_cache()` for that block.
  - _Requirements: R5.1, R5.2, R5.4_

- [ ] 7. Implement drift detection `_detect_and_handle_drift(conn, rows)` and wire into learning cycle (R4)
  - Prerequisite: tasks 1 (system_events table) and 6 (cache invalidation) must be complete before this task.
  - [ ] 7.1 Write `_detect_and_handle_drift(conn, rows)` in `app/monitoring/self_learner.py`
    - Filter `rows` to those from the last 7 calendar days using `created_at`.
    - Group by `(league_key, pick_type)`. For each group with `len >= 10`, compute `win_rate = wins / total`.
    - **Drift:** if `win_rate < 0.40`, `UPDATE tournament_preferences SET priority = 7` for that `league_key`; insert a `system_events` row with `event_type='drift_detected'`, `detail_json={"win_rate":..., "samples":..., "days_window":7, "action":"priority_set_to_7"}`.
    - **Recovery:** if the league previously had `priority = 7` due to drift AND its 7-day `win_rate >= 0.45`, recompute priority using the standard mapping; insert a `system_events` row with `event_type='drift_recovery'`.
    - After any drift event, call `clear_learned_parameter_cache()`.
    - Return the count of drift events detected.
    - _Requirements: R4.1, R4.3, R4.4, R4.5, R4.7_
  - [ ] 7.2 Call `_detect_and_handle_drift(conn, rows)` in `run_learning_cycle()` after `update_tournament_preferences()` and add `"drift_events"` key to the return dict
    - _Requirements: R4.1, R4.6_
  - [ ] 7.3 Write unit tests for drift detection
    - Case 1: league with 7-day win_rate < 0.40 and >= 10 samples → priority forced to 7 and `system_events` row inserted.
    - Case 2: league with < 10 samples → priority unchanged.
    - Case 3: league recovers above 0.45 → priority recalculated and `drift_recovery` event written.
    - _Requirements: R4.3, R4.4, R4.7_

- [ ] 8. Checkpoint — Tier 1 complete
  - Ensure all tests pass, ask the user if questions arise.

---

## Tier 2 — Hard-Coded Biases → Data-Driven (R6–R10)

These tasks replace hardcoded numeric constants with values learned from graded history. R6 and R7 both read from `league_outcome_distribution`, which is populated within `run_learning_cycle()` — the population function and the read-back helpers must be implemented in the same task group.

- [ ] 9. Add `league_outcome_distribution` and `context_penalty_adjustments` tables to `_init_learner_tables()`
  - In `app/monitoring/self_learner.py`, add to `_init_learner_tables(conn)`:
    - `league_outcome_distribution` with columns: `league_key` (TEXT PRIMARY KEY), `home_rate` (REAL NOT NULL DEFAULT 0.45), `draw_rate` (REAL NOT NULL DEFAULT 0.30), `away_rate` (REAL NOT NULL DEFAULT 0.25), `samples` (INTEGER NOT NULL DEFAULT 0), `last_updated` (TEXT NOT NULL DEFAULT current_timestamp).
    - `context_penalty_adjustments` with columns: `context_tag` (TEXT NOT NULL), `league_key` (TEXT NOT NULL DEFAULT `'__global__'`), `penalty_override` (REAL), `samples` (INTEGER NOT NULL DEFAULT 0), `win_rate` (REAL), `last_updated` (TEXT NOT NULL DEFAULT current_timestamp), PRIMARY KEY `(context_tag, league_key)`.
  - _Requirements: R6.5, R8.1_

- [ ] 10. Populate `league_outcome_distribution` in `run_learning_cycle()` (R6 step 5–6)
  - Prerequisite: task 9 (table must exist).
  - [ ] 10.1 Write `_populate_league_outcome_distribution(conn, rows)` in `app/monitoring/self_learner.py`
    - Filter `rows` to those with `pick_type == 'match_result'`.
    - Group by `league_key`. For each league with >= 20 samples, compute `home_rate`, `draw_rate`, `away_rate` from the `selection` field (normalise selections to `'home'`, `'draw'`, `'away'` using `_selection_side()` or equivalent).
    - Upsert into `league_outcome_distribution`. Return count of rows written.
    - _Requirements: R6.5, R6.6_
  - [ ] 10.2 Call `_populate_league_outcome_distribution(conn, rows)` as step 10 in `run_learning_cycle()` and add `"league_outcome_distribution_updates"` key to the return dict
    - _Requirements: R6.5_

- [ ] 11. Replace hardcoded away baseline with `_get_away_baseline()` in `signal_aggregator.py` (R6)
  - Prerequisite: task 10 (table populated before prediction path reads it).
  - [ ] 11.1 Write `_get_away_baseline(league_key: str) -> float` helper in `app/enrichment/signal_aggregator.py`
    - Step 1: query `league_outcome_distribution` for `league_key` with `samples >= 20`; return `away_rate`.
    - Step 2: fall back to the global average `away_rate` across all rows in `league_outcome_distribution`.
    - Step 3: fall back to the hardcoded constant `0.54`.
    - _Requirements: R6.1, R6.2, R6.3, R6.4_
  - [ ] 11.2 In `calculate_probabilities()`, replace the hardcoded `away_baseline = 0.54` with a call to `_get_away_baseline(self.league_key)`
    - _Requirements: R6.1, R6.2_
  - [ ] 11.3 Write unit tests for `_get_away_baseline()`
    - League present in `league_outcome_distribution` with away_rate = 0.38 → returns ≈ 0.38.
    - No league row but global rows present → returns global average.
    - No rows at all → returns 0.54.
    - _Requirements: R6.2, R6.3, R6.4_

- [ ] 12. Replace hardcoded mixed-signal base probabilities with `_get_base_probs()` in `signal_aggregator.py` (R7)
  - Prerequisite: task 10 (table populated).
  - [ ] 12.1 Write `_get_base_probs(league_key: str) -> tuple[float, float, float, str]` in `app/enrichment/signal_aggregator.py`
    - Returns `(home_rate, draw_rate, away_rate, source)` where `source` is `'learned'`, `'global_fallback'`, or `'static_fallback'`.
    - Step 1: query `league_outcome_distribution` for `league_key` with `samples >= 20` → return `(home_rate, draw_rate, away_rate, 'learned')`.
    - Step 2: fall back to global average across all rows in `league_outcome_distribution` → `'global_fallback'`.
    - Step 3: fall back to `(0.45, 0.30, 0.25, 'static_fallback')`.
    - _Requirements: R7.1, R7.2, R7.3, R7.4_
  - [ ] 12.2 In `calculate_probabilities()` mixed-signal (`else`) branch, replace `home_prob = 0.45 + ...` / `away_prob = 0.25 + ...` / `draw_prob = 0.30 + ...` base constants with values from `_get_base_probs(self.league_key)`
    - Add `"base_probs_source"` key to the returned dict, always present.
    - _Requirements: R7.2, R7.4, R7.5_
  - [ ] 12.3 Write unit tests for `_get_base_probs()`
    - League row with known rates → returned base probs match those rates and `base_probs_source == 'learned'`.
    - No league row → global fallback path and `base_probs_source == 'global_fallback'`.
    - No rows at all → `base_probs_source == 'static_fallback'`.
    - _Requirements: R7.2, R7.3, R7.4, R7.5_

- [ ] 13. Implement `_learn_context_penalties()` and populate `context_penalty_adjustments` in the learning cycle (R8)
  - Prerequisite: task 9 (`context_penalty_adjustments` table created).
  - [ ] 13.1 Write `_learn_context_penalties(conn, rows)` in `app/monitoring/self_learner.py`
    - For each graded row in `rows`, parse `context_json["match_context"]["tags"]` (a list of strings).
    - Group by `(context_tag, league_key)`. For each pair with >= 10 samples, compute `penalty_override = round((0.5 - win_rate) * 12, 1)` clamped to `[-10, 4]`.
    - Upsert into `context_penalty_adjustments`. Return count of rows written.
    - _Requirements: R8.1, R8.2, R8.3_
  - [ ] 13.2 Call `_learn_context_penalties(conn, rows)` inside `run_learning_cycle()` and add `"context_penalty_updates"` key to the return dict
    - _Requirements: R8.2_

- [ ] 14. Wire learned context penalties into `contextual_intelligence.py` (R8)
  - Prerequisite: task 13 (table populated).
  - [ ] 14.1 Write `_learned_penalty_for_tag(tag: str, league_key: str) -> float | None` helper in `app/enrichment/contextual_intelligence.py`
    - Query `context_penalty_adjustments` for `(tag, league_key)` with `samples >= 10` → return `penalty_override`.
    - Fall back to `(tag, '__global__')` with `samples >= 10` → return global override.
    - Return `None` to signal that the hardcoded value should be used.
    - Wrap in `try/except Exception` and return `None` on error.
    - _Requirements: R8.4, R8.6, R8.7_
  - [ ] 14.2 In `_match_context()`, before applying each hardcoded tag adjustment, call `_learned_penalty_for_tag(tag, league_key)` and use the returned override when not `None`
    - The `league_key` is derived from `doc.get("tournament")` or equivalent.
    - _Requirements: R8.4, R8.5, R8.6, R8.7_
  - [ ] 14.3 Write unit tests for learned context penalty override
    - Tag with a league-specific row → uses `penalty_override`.
    - Tag with only a global row → uses global override.
    - Tag with no rows → falls back to hardcoded value.
    - _Requirements: R8.5, R8.6, R8.7_

- [ ] 15. Fix `_BASE_WEIGHTS` in `ensemble.py` and update `_get_weights()` fallback (R9)
  - This task is standalone — no prerequisites beyond the file itself.
  - [ ] 15.1 Replace `_BASE_WEIGHTS: dict[str, float] = {}` in `app/models/ensemble.py` with:
    ```python
    _BASE_WEIGHTS: dict[str, float] = {
        "dixon_coles": 0.30,
        "elo":         0.25,
        "poisson":     0.15,
        "rules":       0.20,
        "llm":         0.10,
    }
    ```
    - _Requirements: R9.1_
  - [ ] 15.2 In `_get_weights()`, update the fallback path so that when `get_learned_weights()` returns an empty dict, the function returns `_BASE_WEIGHTS` instead of `{}`
    - The `total_weight == 0.0` neutral fallback (`33/33/33` with `limited_signal: True`) is preserved for the case where `_BASE_WEIGHTS` is non-empty but no model produces valid output.
    - _Requirements: R9.2, R9.3, R9.4, R9.5_
  - [ ] 15.3 Write unit tests for `_BASE_WEIGHTS` and `_get_weights()` fallback
    - Assert `_BASE_WEIGHTS` keys, values, and sum to 1.00.
    - Assert `_get_weights()` returns `_BASE_WEIGHTS` when `learned_model_weights` is empty.
    - Assert `ensemble_prediction()` returns `max(probs.values()) > 34.0` when called with non-trivial model output and no learned weights.
    - _Requirements: R9.1, R9.2, R9.3_

- [ ] 16. Fix direction-aware category fallback in `_category_for_signal()` (R10)
  - This task is standalone — no prerequisites.
  - [ ] 16.1 Rewrite `_category_for_signal(name: str) -> str` in `app/enrichment/signal_aggregator.py`
    - Keep the existing exact-match loop (step 1).
    - Add step 2 direction-aware fallback: check `"away"` **before** `"home"` because `"away"` is more specific.
      - `has_away = "away" in name`; `has_home = "home" in name and not has_away`
      - Prefix = `"away"` or `"home"` based on the above.
      - `"form"`, `"recent_history"`, `"team_watcher"`, `"wd"`, `"last"` in name → `f"{prefix}_form"`
      - `"table"`, `"standing"`, `"position"`, `"league_strength"` in name → `f"{prefix}_table"`
      - `"goal"`, `"attack"`, `"scoring"`, `"pressure"` in name → `f"{prefix}_goal_pressure"`
      - `"odds"`, `"market"`, `"steam"` in name → `f"{prefix}_odds"`
      - `"defense"`, `"conceding"`, `"clean"` in name → `f"{prefix}_defense"`
    - Retain the original non-directional fallbacks unchanged as step 3.
    - _Requirements: R10.1, R10.2, R10.3, R10.4, R10.5, R10.6, R10.7_
  - [ ] 16.2 Write unit tests for direction-aware category fallback
    - `"away_recent_history"` → `"away_form"` (not `"home_form"`).
    - `"away_table_position"` → `"away_table"`.
    - `"home_goal_pressure_strong"` → `"home_goal_pressure"`.
    - Signal with neither `"home"` nor `"away"` → existing behaviour unchanged.
    - _Requirements: R10.2, R10.3, R10.6, R10.7_

- [ ] 17. Checkpoint — Tier 2 complete
  - Ensure all tests pass, ask the user if questions arise.

---

## Tier 3 — Incomplete Team/Competition Wiring (R11–R15)

These tasks connect existing intelligence tables to the live prediction pipeline. Each task is largely independent; R14 requires both `competition_special.py` and `enriched_prediction.py` changes.

- [ ] 18. Add `signal_outcomes` table to `_init_learner_tables()` and implement `_backfill_signal_outcomes()` (R12)
  - [ ] 18.1 Add `signal_outcomes` table creation to `_init_learner_tables(conn)` in `app/monitoring/self_learner.py`
    - Columns: `id` (INTEGER PK AUTOINCREMENT), `signal_name` (TEXT NOT NULL), `match_id` (TEXT NOT NULL), `tournament` (TEXT), `country` (TEXT), `result` (TEXT), `created_at` (TEXT), UNIQUE(`signal_name`, `match_id`).
    - _Requirements: R12.4_
  - [ ] 18.2 Write `_backfill_signal_outcomes(conn, rows)` in `app/monitoring/self_learner.py`
    - For each graded row, check if `signal_outcomes` contains at least one row for `match_id`.
    - If absent, call `_decision_signals_for_row(row)` and upsert one row per decision signal with `ON CONFLICT(signal_name, match_id) DO NOTHING`.
    - Return count of rows written.
    - _Requirements: R12.1, R12.2, R12.3, R12.5_
  - [ ] 18.3 Call `_backfill_signal_outcomes(conn, rows)` as the final step in `run_learning_cycle()` and add `"signal_outcome_backfills"` key to the return dict
    - _Requirements: R12.6_

- [ ] 19. Inject team prediction history signals in `enriched_prediction.py` (R11)
  - [ ] 19.1 Write helper `_team_history_signals(doc, detail) -> list[dict]` in `app/enrichment/enriched_prediction.py`
    - Derive `home_key` and `away_key` from `detail.get("home_team")["name"]` and `detail.get("away_team")["name"]` using a normalisation function.
    - Derive `comp_key` from the document.
    - Query `team_competitions` for each team key + competition key.
    - If `prediction_total >= 10` and `prediction_correct / prediction_total < 0.40`: add `team_prediction_history_risk` signal with `impact = -3`.
    - If `prediction_total >= 10` and `prediction_correct / prediction_total >= 0.60`: add `team_prediction_history_boost` signal with `impact = +2`.
    - If `prediction_total < 10`: add no signal for that team.
    - Wrap in `try/except Exception`; return `[]` on any error.
    - _Requirements: R11.1, R11.2, R11.3, R11.4, R11.5, R11.6_
  - [ ] 19.2 Call `_team_history_signals(doc, detail)` after `ensemble_prediction()` in `predict_enriched_match()` and append the returned signals to the prediction's signals list
    - _Requirements: R11.1_
  - [ ] 19.3 Write unit tests for `_team_history_signals()`
    - Team with `prediction_total >= 10` and accuracy < 0.40 → risk signal present with `impact = -3`.
    - Team with `prediction_total >= 10` and accuracy >= 0.60 → boost signal present with `impact = +2`.
    - Team with `prediction_total < 10` → no signal.
    - DB error → returns `[]` without raising.
    - _Requirements: R11.2, R11.3, R11.5, R11.6_

- [ ] 20. Implement H2H signal synthesis from SofaScore `last_meetings` (R13)
  - This task is standalone within `enriched_prediction.py` — no table prerequisites.
  - [ ] 20.1 Write `_compute_h2h_signals(detail: dict) -> list[dict]` in `app/enrichment/enriched_prediction.py`
    - Take up to 10 most recent entries from `detail.get("last_meetings") or []`; return `[]` if fewer than 3 entries.
    - Derive `home_id` from `detail.get("home_team", {}).get("id")`.
    - Iterate meetings: for each, read `homeScore.current`, `awayScore.current`, and `homeTeam.id`. Correctly attribute wins to `home_wins` or `away_wins` based on whether the meeting's home team is the current home team.
    - Count `draws` for ties. Compute `total = home_wins + draws + away_wins`; return `[]` if `total < 3`.
    - `home_ratio = home_wins / total`; `away_ratio = away_wins / total`.
    - If `home_wins > away_wins` and `home_ratio >= 0.5`: emit `{"name": "h2h_home", "value": round(home_ratio, 2), "impact": round(home_ratio * 4), "source": "sofascore_last_meetings"}`.
    - If `away_wins > home_wins` and `away_ratio >= 0.5`: emit `{"name": "h2h_away", ...}`.
    - Otherwise: emit `{"name": "h2h_draw", "value": round(draws / total, 2), "impact": 1, "source": "sofascore_last_meetings"}`.
    - Return `[]` on any exception.
    - _Requirements: R13.1, R13.2, R13.3, R13.4, R13.5, R13.7_
  - [ ] 20.2 In `_rules_prediction(doc, detail)`, call `_compute_h2h_signals(detail)` and, before appending each signal, check if an existing signal with the same `name` and equal or higher `abs(impact)` already exists; skip the computed signal if so
    - _Requirements: R13.6_
  - [ ] 20.3 Write unit tests for `_compute_h2h_signals()`
    - Fewer than 3 meetings → returns `[]`.
    - Home dominance >= 0.5 → returns `h2h_home` signal with correct strength.
    - Away dominance >= 0.5 → returns `h2h_away` signal.
    - Balanced → returns `h2h_draw` signal.
    - Existing higher-impact H2H signal → computed signal is discarded.
    - _Requirements: R13.3, R13.4, R13.5, R13.6, R13.7_

- [ ] 21. Attach competition round analysis in `competition_special.py` and wire into `enriched_prediction.py` (R14)
  - [ ] 21.1 In `apply_known_competition_context()` in `app/competition/competition_special.py`, after setting `doc["known_competition"]`, query `competition_analysis` for the most recent row matching the match's `competition_key`
    - Use `db_conn(timeout=5)` inside `try/except Exception`.
    - If a row is found and its `created_at` is within 7 calendar days of current UTC, set `doc["competition_round_analysis"] = analysis`.
    - If absent or older than 7 days, do not set the key and continue without error.
    - _Requirements: R14.1, R14.2, R14.3_
  - [ ] 21.2 In `_rules_prediction(doc, detail)` in `app/enrichment/enriched_prediction.py`, after existing signal computation, check `doc.get("competition_round_analysis")`
    - If present: parse `analysis_text` as JSON, extract `top_table`, find if home or away team appears in the top 2 entries by team name match.
    - If a clear form leader is found (one team in top 2), append a `competition_momentum` signal with `impact = +1` for that team's direction.
    - Set `audit["competition_context_applied"] = True` whenever `competition_round_analysis` is present, regardless of whether a momentum signal was added.
    - Wrap the entire block in `try/except Exception`.
    - _Requirements: R14.4, R14.5, R14.6, R14.7_
  - [ ] 21.3 Write unit tests for competition round analysis wiring
    - `apply_known_competition_context()` attaches `competition_round_analysis` when a recent row exists; does not attach when row is older than 7 days.
    - `_rules_prediction()` with a valid `competition_round_analysis` sets `audit["competition_context_applied"] = True`.
    - No clear form leader → no `competition_momentum` signal.
    - _Requirements: R14.2, R14.3, R14.5, R14.6, R14.7_

- [ ] 22. Add tournament priority confidence modifier to `SignalAggregator._calculate_confidence()` (R15)
  - [ ] 22.1 At the end of `_calculate_confidence()` in `app/enrichment/signal_aggregator.py`, add a tournament priority modifier block
    - Query `tournament_preferences` for `self.league_key` inside `try/except Exception` (no-op on error or missing table).
    - Priority 0 or 1 → add `+0.05` to `confidence`.
    - Priority 6 or 7 → subtract `0.10` from `confidence`.
    - Priority 2–5 → no modification.
    - No row or error → no modification (treat as priority 4).
    - After modification, clamp `confidence = max(0.1, min(0.95, confidence))`.
    - _Requirements: R15.1, R15.2, R15.3, R15.4, R15.5, R15.6_
  - [ ] 22.2 Write unit tests for tournament priority confidence modifier
    - Priority 0 → confidence increases by 0.05.
    - Priority 7 → confidence decreases by 0.10.
    - Priority 4 → confidence unchanged.
    - Missing `tournament_preferences` table → no error, confidence unchanged.
    - _Requirements: R15.2, R15.3, R15.4, R15.5_

- [ ] 23. Checkpoint — Tier 3 complete
  - Ensure all tests pass, ask the user if questions arise.

---

## Tier 4 — Signal Quality and Cache Hygiene (R16–R18)

These tasks are independent of each other and of the earlier tiers. They improve operational quality with no table prerequisites.

- [ ] 24. Add cache size guard to `prefetch_signal_stats()` and `global_signal_stats()` (R16)
  - In `app/enrichment/signal_aggregator.py`, before writing any new entries to `_SIGNAL_STATS_BATCH_CACHE` in `prefetch_signal_stats()` and `global_signal_stats()`:
    - Check `if len(_SIGNAL_STATS_BATCH_CACHE) >= 1000`.
    - If true, log a WARNING: `"[signal_aggregator] cache overflow: clearing %d entries before writing new batch", len(_SIGNAL_STATS_BATCH_CACHE)` and call `_SIGNAL_STATS_BATCH_CACHE.clear()`.
    - `reset_signal_stats_cache()` remains unconditional — it always clears.
  - Verify that `reset_signal_stats_cache()` is called at the start of each prediction pass in `enriched_prediction.py` (add the call if missing).
  - _Requirements: R16.1, R16.2, R16.3, R16.4_

- [ ] 25. Normalise lineup detection across all key names (R17)
  - In `app/enrichment/contextual_intelligence.py`, add a module-level constant:
    ```python
    _LINEUP_KEYS = ("lineups", "starting_xi", "confirmed_lineups", "home_lineup", "away_lineup")
    ```
  - Write a helper `_has_lineup_data(doc: dict) -> bool`:
    - Check each key in `_LINEUP_KEYS` directly on `doc`; return `True` if any is present and non-empty (truthy).
    - Check `sofascore_detail = doc.get("sofascore_detail") or {}`; check `"lineups"`, `"starting_xi"`, `"confirmed_lineups"` on it.
    - Return `False` if none found.
  - In `_match_context()`, replace the existing `lineup_window` check:
    - Before (current): `not doc.get("lineups") and not (doc.get("sofascore_detail") or {}).get("lineups")`
    - After: `not _has_lineup_data(doc)`
  - _Requirements: R17.1, R17.2, R17.3, R17.4_

- [ ] 26. Separate the 80–89% and 90–99% calibration bands in `confidence_calibrator.py` (R18)
  - In `app/enrichment/confidence_calibrator.py`:
  - [ ] 26.1 In `rebuild_calibration()`, replace `min(80, (confidence / 10) * 10) as band_low` in both the per-pick_type query and the `'__global__'` query with:
    ```sql
    case
        when confidence >= 90 then 90
        when confidence >= 80 then 80
        else (confidence / 10) * 10
    end as band_low
    ```
    - _Requirements: R18.1, R18.2, R18.3, R18.4_
  - [ ] 26.2 In `calibrate_confidence()`, replace `band_low = min(80, (raw_confidence // 10) * 10)` with:
    ```python
    if raw_confidence >= 90:
        band_low = 90
    elif raw_confidence >= 80:
        band_low = 80
    else:
        band_low = (raw_confidence // 10) * 10
    ```
    - _Requirements: R18.1, R18.2, R18.3, R18.5_
  - [ ] 26.3 Write unit tests for calibration band separation
    - `rebuild_calibration()` with predictions at confidence 85 and 95 produces separate rows for `band_low=80` and `band_low=90`.
    - `calibrate_confidence(pick_type, 85)` queries `band_low=80`.
    - `calibrate_confidence(pick_type, 92)` queries `band_low=90`.
    - _Requirements: R18.2, R18.3, R18.5_

- [ ] 27. Checkpoint — Tier 4 complete
  - Ensure all tests pass, ask the user if questions arise.

---

## Tier 5 — Property-Based Tests and Integration Tests (P1–P10)

All implementation tasks above must be complete before writing property-based tests. Tests use the Hypothesis library with `@settings(max_examples=150)` minimum. Each test is tagged with `Feature: prediction-intelligence-overhaul, Property N: <description>`.

- [ ] 28. Write property-based test suite in `tests/test_prediction_intelligence_pbt.py`
  - Create the test file with shared fixtures: an in-memory SQLite DB helper, `SIGNAL_NAMES` sample set, and a synthetic graded-row factory.
  - [ ] 28.1 Write P1 — Probability Sum Invariant
    - Generate: arbitrary lists of signals (1–20 items) with random names from `SIGNAL_NAMES`, `value: float[-10, 10]`, `source: text`.
    - Assert: `abs(home_prob + draw_prob + away_prob - 1.0) < 0.001` for every generated input after every code path (all-home, all-away, mixed).
    - **Property 1** — **Validates: Requirements R6, R7**
  - [ ] 28.2 Write P2 — Away Baseline is Data-Driven When Data Exists
    - Seed the test DB with synthetic `league_outcome_distribution` rows at known `away_rate` values (e.g. 0.30, 0.40, 0.48, 0.60).
    - Generate: all-away signal lists, league_key matching the seeded league.
    - Assert: `abs(away_prob - seeded_away_rate) < abs(away_prob - 0.54)` when the seeded rate differs from 0.54 by at least 0.01.
    - **Property 2** — **Validates: Requirements R6.2, R6.3**
  - [~] 28.3 Write P3 — Signal Deduplication Leaves At Most One Per Category Per Source Group
    - Generate: signal batches where 2–5 signals share the same resolved category but have different `source` values.
    - Assert: after `add_signals()`, no two entries in `agg.signals` share the same `category` AND have different `source` values (i.e., at most one per `(category, source)` combination, and at most one per category across sources).
    - **Property 3** — **Validates: Requirements R3.1, R3.2, R3.3**
  - [~] 28.4 Write P4 — Learning Cycle Idempotency
    - Load a fixed set of synthetic graded rows into an in-memory DB.
    - Run `run_learning_cycle()` twice in sequence.
    - Assert: values in `signal_weights`, `league_accuracy`, `learned_model_weights`, `signal_combination_memory` are identical after the second call compared to the first (exclude `last_updated` timestamps from the comparison).
    - **Property 4** — **Validates: Requirements R1, R2, R4, R5, R6, R8, R12**
  - [~] 28.5 Write P5 — Cache Invalidation Round-Trip
    - Run a learning cycle with known synthetic data; record the written `learned_model_weights` rows.
    - Call `get_learned_ensemble_weights()` immediately after.
    - Assert: returned dict values match DB rows within `±0.0001`.
    - Assert: a subsequent cache-only call (no DB write) returns the same values.
    - **Property 5** — **Validates: Requirements R5.1, R5.2, R5.5**
  - [ ] 28.6 Write P6 — Drift Detection Coverage
    - Generate: per-league graded rows at various win rates and sample counts (min_size=1, max_size=8 leagues).
    - Run `run_learning_cycle()`.
    - Assert: every `(league_key, pick_type)` combination with `win_rate < 0.40` and `samples >= 10` in the last 7 days has `tournament_preferences.priority = 7`.
    - Assert: combinations not meeting the threshold do NOT have `priority = 7` from drift.
    - **Property 6** — **Validates: Requirements R4.3, R4.4, R4.6**
  - [ ] 28.7 Write P7 — Base Weight Fallback Produces Non-Neutral Output
    - Clear the `learned_model_weights` table (empty DB).
    - Generate: arbitrary non-null Dixon-Coles, ELO, or Poisson probability dicts.
    - Call `ensemble_prediction()` with these inputs.
    - Assert: `max(result["probabilities"].values()) > 34.0`.
    - Assert: `result.get("limited_signal")` is absent or `False`.
    - **Property 7** — **Validates: Requirements R9.2, R9.3**
  - [ ] 28.8 Write P8 — Team Prediction History Signal Presence
    - Generate: team_competitions rows with various `prediction_total` and `prediction_correct` values.
    - Call the `_team_history_signals()` helper directly.
    - Assert: when `prediction_total >= 10` and `prediction_correct / prediction_total < 0.40`, the returned list contains `team_prediction_history_risk` with `impact <= -3`.
    - Assert: when `prediction_total < 10`, no signal is returned.
    - **Property 8** — **Validates: Requirements R11.2, R11.5**
  - [ ] 28.9 Write P9 — Direction-Aware Category for Away Signals
    - Generate: signal names by combining `"away_"` prefix with any of the qualifier keywords (`"form"`, `"recent_history"`, `"team_watcher"`, `"table"`, `"standing"`, `"position"`, `"goal"`, `"odds"`, `"market"`, `"defense"`, `"conceding"`, `"clean"`).
    - Call `_category_for_signal(name)` for each.
    - Assert: returned category begins with `"away_"`.
    - **Property 9** — **Validates: Requirement R10**
  - [ ] 28.10 Write P10 — Calibration Band Partition
    - Generate: integer confidence values in `[80, 100)`.
    - For values in `[80, 90)`, assert `calibrate_confidence()` (or the band-low derivation logic) produces `band_low = 80`.
    - For values in `[90, 100)`, assert `band_low = 90`.
    - Assert the two bands are never conflated.
    - **Property 10** — **Validates: Requirements R18.2, R18.3, R18.5**

- [~] 29. Write integration tests in `tests/test_prediction_intelligence_integration.py`
  - Use an in-memory SQLite DB for all integration tests.
  - [ ] 29.1 Full learning cycle populates all five new tables
    - Seed: synthetic graded rows covering `ai_analysis_feedback`, `user_behavior_outcomes`, `league_outcome_distribution`, `context_penalty_adjustments`, `signal_outcomes` write paths.
    - Assert: all five tables contain rows after `run_learning_cycle()` completes.
    - Assert: return dict contains all new keys (`drift_events`, `context_penalty_updates`, `league_outcome_distribution_updates`, `signal_outcome_backfills`).
    - _Requirements: R1, R2, R4, R6, R8, R12_
  - [ ] 29.2 `apply_known_competition_context()` attaches and withholds `competition_round_analysis` correctly
    - Seed: one `competition_analysis` row with `created_at` = now − 3 days; one with `created_at` = now − 10 days.
    - Assert: the 3-day-old row is attached as `doc["competition_round_analysis"]`.
    - Assert: the 10-day-old row is NOT attached.
    - _Requirements: R14.1, R14.2, R14.3_
  - [ ] 29.3 End-to-end: `_rules_prediction()` sets `audit["competition_context_applied"]` when CRA is present
    - Seed a doc with `competition_round_analysis` containing a valid `top_table`.
    - Call `_rules_prediction()` (or the relevant sub-function with a mocked doc).
    - Assert `audit["competition_context_applied"] == True`.
    - _Requirements: R14.7_
  - [ ] 29.4 `rebuild_calibration()` produces separate `band_low=80` and `band_low=90` rows
    - Seed: graded predictions with confidence 85 and 95.
    - Call `rebuild_calibration()`.
    - Assert: `confidence_calibration` contains rows with `band_low=80` and `band_low=90` as separate entries.
    - _Requirements: R18.1, R18.3, R18.4_

- [~] 30. Final checkpoint — All tests pass
  - Ensure all tests pass. Run the full test suite and confirm no regressions across all modified modules. Ask the user if questions arise.

---

## Tier 6 — Learning Calibration Improvements (R19–R24)

These tasks address six learning-quality gaps identified in a post-initial-spec technical audit. They build on the core learning infrastructure established in Tiers 1–3. Tasks 31 and 32 (temporal decay and confidence weighting) both modify `_tally()` and should be implemented together in a single pass.

- [~] 31. Implement temporal decay weighting in `_tally()` (R19)
  - Prerequisite: Tier 1 core learning cycle must be complete.
  - [ ] 31.1 Add a `_decay_weight(created_at: str, now: datetime) -> float` helper in `app/monitoring/self_learner.py`
    - Compute `age_days = max(0, (now - row_dt).days)` and `age_weeks = age_days / 7.0`.
    - Return `DECAY_FACTOR ** age_weeks`.
    - Wrap in `try/except Exception`; return `1.0` on any parse error.
    - The existing module-level `DECAY_FACTOR = 0.92` constant at line ~76 is used directly (do NOT redefine it inside the function).
    - _Requirements: R19.1, R19.3, R19.4, R19.7_
  - [ ] 31.2 Update `_tally()` (or the equivalent win-rate aggregation used for signal_weights, league_accuracy, and learned_model_weights) to use decay-weighted contributions
    - Replace `wins += 1` / `total += 1` with `weighted_wins += decay` / `weighted_total += decay`.
    - Compute `win_rate = weighted_wins / weighted_total` (guard: return `0.5` when `weighted_total == 0`).
    - Pass `now = datetime.utcnow()` into `_tally()` at each call site so the reference time is consistent within a single cycle.
    - _Requirements: R19.1, R19.2, R19.5, R19.6_
  - [ ] 31.3 Write unit tests for temporal decay
    - A row with `age_in_weeks = 0` returns `decay_weight = 1.0`.
    - A row with `age_in_weeks = 52` returns `decay_weight ≈ 0.014` (within ±0.001).
    - Two rows with identical outcomes but ages 1 week apart: older row contributes less.
    - All rows with same `created_at`: weighted win_rate equals unweighted win_rate within ±0.001.
    - _Requirements: R19.2, R19.3, R19.4, R19.6_

- [~] 32. Implement confidence weighting in `_tally()` (R20)
  - Prerequisite: task 31 (temporal decay already in `_tally()`).
  - [ ] 32.1 In `_tally()`, multiply each row's contribution by `confidence_weight = (row.get("confidence") or 50) / 100.0`
    - Combined weight per row: `row_weight = decay_weight * confidence_weight`.
    - NULL or missing confidence uses default `0.5` (→ `confidence_weight = 0.5`).
    - _Requirements: R20.1, R20.2, R20.4_
  - [ ] 32.2 Verify the combined weighting is applied consistently across all three contexts (signal_weights, league_accuracy, learned_model_weights)
    - No separate tally paths should exist that bypass the combined weight.
    - _Requirements: R20.7_
  - [ ] 32.3 Write unit tests for confidence weighting
    - Two rows with same outcome, confidences 90 vs 55: the 90-confidence row contributes more.
    - All rows at `confidence = 100`: weighted win_rate equals unweighted win_rate within ±0.001.
    - All rows at `confidence = 50`: weighted win_rate equals unweighted win_rate within ±0.001.
    - Row with NULL confidence: treated as `confidence = 50`.
    - Combined weight = `decay * (conf/100.0)` verified numerically for a known row.
    - _Requirements: R20.2, R20.3, R20.5, R20.6_

- [~] 33. Make league adjustment caps symmetric in `enriched_prediction.py` (R21)
  - This task is standalone — no prerequisites.
  - [ ] 33.1 In `app/enrichment/enriched_prediction.py`, locate the league accuracy penalty/boost application block (around lines 766–773)
    - Replace the asymmetric inline caps (`min(8, ...)` for boost, `max(-10, ...)` for penalty) with a single named constant `MAX_LEAGUE_ADJUSTMENT = 10`.
    - Apply: `boost = min(MAX_LEAGUE_ADJUSTMENT, ...)` and `penalty = max(-MAX_LEAGUE_ADJUSTMENT, ...)`.
    - _Requirements: R21.1, R21.2, R21.3, R21.4_
  - [ ] 33.2 Write unit tests for symmetric caps
    - A league with >= 65% win rate: adjustment does not exceed `+10`.
    - A league with < 50% win rate: adjustment is not more negative than `-10`.
    - Assert `abs(max_positive_cap) == abs(max_negative_cap)`.
    - _Requirements: R21.5, R21.6_

- [~] 34. Replace hardcoded bias correction floor with dynamic formula (R22)
  - This task is standalone within `self_learner.py`.
  - [ ] 34.1 In `app/monitoring/self_learner.py`, locate the bias correction multiplier floor (around line 947)
    - Replace `multiplier = max(0.72, multiplier)` with:
      ```python
      multiplier_floor = max(0.72, 1.0 - (loss_rate - 0.50) * 1.4)
      multiplier = max(multiplier_floor, multiplier)
      ```
    - Ensure `loss_rate` is in scope at this point (it is the value that triggered the bias correction block).
    - The `overconfidence >= 0.08` trigger condition is not changed.
    - _Requirements: R22.1, R22.5, R22.6_
  - [ ] 34.2 Write unit tests for the dynamic bias floor
    - `loss_rate = 0.58` → `multiplier_floor ≈ 0.888` (within ±0.001).
    - `loss_rate = 0.65` → `multiplier_floor ≈ 0.790` (within ±0.001).
    - `loss_rate = 0.71` → `multiplier_floor = 0.72` (clamped at absolute floor).
    - `loss_rate = 0.80` → `multiplier_floor = 0.72` (absolute floor, not lower).
    - Higher `loss_rate` → lower or equal `multiplier_floor` (monotonicity).
    - _Requirements: R22.2, R22.3, R22.4_

- [~] 35. Raise signal combination memory sample guard to 12 (R23)
  - This task is standalone within `self_learner.py`.
  - [ ] 35.1 Add `MIN_COMBINATION_SAMPLES = 12` as a named constant at module level in `app/monitoring/self_learner.py`
    - Place near the existing `MIN_SAMPLES` and `MIN_LEAGUE_SAMPLES` constants.
    - Do NOT reuse or rename `MIN_SAMPLES` or `MIN_LEAGUE_SAMPLES`.
    - _Requirements: R23.5_
  - [ ] 35.2 In `_learn_signal_combinations()`, replace the existing numeric threshold (5) with `MIN_COMBINATION_SAMPLES`
    - The guard reads: `if len(combo_rows) < MIN_COMBINATION_SAMPLES: continue`
    - Existing `signal_combination_memory` rows for combinations with 5–11 samples are preserved (no DELETE).
    - _Requirements: R23.1, R23.2, R23.3, R23.4_
  - [ ] 35.3 Write unit tests for the raised sample guard
    - Combination with 11 samples: no write to `signal_combination_memory`.
    - Combination with 12 samples: write occurs.
    - Combination with 20 samples: write occurs as before.
    - Existing DB rows for combinations with < 12 samples are not deleted.
    - _Requirements: R23.1, R23.2, R23.3, R23.4_

- [ ] 36. Implement cross-league country-level transfer buckets (R24)
  - Prerequisite: the signal weight write path from Tier 1/2 must be complete.
  - [ ] 36.1 Write `_populate_country_signal_weights(conn, rows, now)` in `app/monitoring/self_learner.py`
    - Group rows by `(signal_name, country_key)` where `country_key = _norm_league(row.get("country_name") or "")`.
    - Skip rows where `country_key` is empty or `None`.
    - For each `(signal_name, country_key)` pair with `len(group) >= MIN_LEAGUE_SAMPLES` (5), compute win rate via `_tally()` (including R19+R20 weighting).
    - Write to `signal_weights` table using `league_key = country_key`.
    - Return count of rows written.
    - _Requirements: R24.1, R24.2, R24.3, R24.7_
  - [ ] 36.2 Call `_populate_country_signal_weights(conn, rows, now)` in `run_learning_cycle()` after writing league-specific signal weights
    - _Requirements: R24.1_
  - [ ] 36.3 Update the signal weight lookup in `enriched_prediction.py` (or the shared lookup helper) to implement the fallback chain
    - Step 1: Query `signal_weights` for `(signal_name, league_key)`.
    - Step 2: If no league row, derive `country_key = _norm_league(country_name)` and query `(signal_name, country_key)`.
    - Step 3: If no country row, query `(signal_name, "__global__")`.
    - Step 4: If no global row, use default weight.
    - _Requirements: R24.4, R24.5, R24.6_
  - [ ] 36.4 Write unit tests for cross-league transfer buckets
    - League with no signal row but matching country row: country weight is returned.
    - Country with no row but global row exists: global weight is returned.
    - Neither country nor global exists: default weight is returned.
    - League row exists: returned without querying country or global.
    - Country-level write: only written when `len(group) >= MIN_LEAGUE_SAMPLES`.
    - _Requirements: R24.2, R24.4, R24.5, R24.6_

- [ ] 37. Checkpoint — Tier 6 complete; write P11–P16 property-based tests
  - Ensure all Tier 6 implementation tasks (31–36) pass their unit tests.
  - [ ] 37.1 Write P11 — Temporal Decay Monotonicity
    - Generate: pairs of rows with identical outcomes and `created_at` values differing by at least 1 day.
    - Assert: `_decay_weight(older_row) < _decay_weight(newer_row)`.
    - Assert: a row with `created_at = today` returns `_decay_weight = 1.0`.
    - **Property 11** — **Validates: Requirements R19.2, R19.3, R19.4**
  - [ ] 37.2 Write P12 — Confidence-Weighted Tally Convergence
    - Generate: sets of rows all having the same confidence value `c` (in range 1–100).
    - Assert: `_tally(rows, now)` (with R19+R20 weighting) equals the unweighted win rate within ±0.001.
    - **Property 12** — **Validates: Requirements R20.5, R20.6**
  - [ ] 37.3 Write P13 — Combined Row Weight Decomposition
    - Generate: arbitrary graded rows with `created_at` and `confidence` values.
    - Assert: `row_weight = _decay_weight(row) * (row["confidence"] / 100.0)` and `0 < row_weight <= 1.0`.
    - **Property 13** — **Validates: Requirements R19.1, R20.1, R20.4**
  - [ ] 37.4 Write P14 — League Adjustment Symmetry
    - Assert: the positive cap constant equals the absolute value of the negative cap constant in the league adjustment code.
    - Generate: arbitrary league accuracy values; assert no adjustment exceeds `+MAX_LEAGUE_ADJUSTMENT` or is more negative than `-MAX_LEAGUE_ADJUSTMENT`.
    - **Property 14** — **Validates: Requirements R21.1, R21.4**
  - [ ] 37.5 Write P15 — Dynamic Bias Floor Monotonicity
    - Generate: loss_rate values in `[0.50, 1.0]`.
    - Assert: `multiplier_floor(r)` is non-increasing as `r` increases (higher loss → lower or equal floor).
    - Assert: `multiplier_floor(r) >= 0.72` for all `r`.
    - **Property 15** — **Validates: Requirements R22.1, R22.2, R22.3, R22.4**
  - [ ] 37.6 Write P16 — Signal Weight Fallback Chain Completeness
    - Seed the DB with: (a) a country row but no league row, (b) a global row but no country or league row, (c) no rows at all.
    - Assert: case (a) returns country weight; case (b) returns global weight; case (c) returns default.
    - Assert: when a league row exists, neither country nor global is queried (chain stops at first match).
    - **Property 16** — **Validates: Requirements R24.4, R24.5, R24.6**
  - Run the full test suite and confirm no regressions. Ask the user if questions arise.

---

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP.
- Each task references specific requirement sub-clauses for full traceability.
- DB table creation tasks (1, 9, 18) must be executed before any task that reads from those tables.
- The `league_outcome_distribution` population (task 10) must be complete before the `_get_away_baseline()` and `_get_base_probs()` helpers (tasks 11, 12) are tested end-to-end.
- Property-based tests (task 28) are the final implementation step for Tiers 1–5; all 18 requirements must be implemented first.
- Tier 6 tasks (31–37) address learning calibration gaps identified in a post-initial-spec audit. Tasks 31 and 32 (R19 and R20) must be implemented together since they both modify the same `_tally()` function. Task 37 (R24) depends on the signal weight write path established in earlier tiers.
- Checkpoints at tasks 8, 17, 23, 27, 30, and 37 are mandatory synchronisation points.
