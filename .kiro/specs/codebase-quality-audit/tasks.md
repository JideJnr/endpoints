# Codebase Quality Audit — Implementation Tasks

## Overview

Tasks are ordered to minimise risk: test infrastructure first, then each defect category from safest to most impactful. Category 4 (syntax/imports) is done first because the `llm_agent.py` (formerly `groq_agent.py`) syntax error blocks the entire `ai` package — fixing it unblocks all subsequent AI-layer testing. Category 1 (circular imports) follows because stable imports are a precondition for any integration test. Categories 2 and 3 are then safe to execute in parallel streams.

---

## Task List

- [ ] 1 Set up test infrastructure for all four defect categories
  - [~] 1.1 Create `tests/test_circular_imports.py` with the import-graph exploration tests (parametrised over all 18 cycle pairs)
  - [~] 1.2 Create `tests/test_duplicate_functions.py` with the AST-based definition-count tests (parametrised over the 19 true-duplicate entries)
  - [~] 1.3 Create `tests/test_hardcoded_constants.py` with the `Settings` attribute and default-value tests
  - [~] 1.4 Create `tests/test_explicit_imports.py` with the `py_compile` and `pyflakes` subprocess tests for all 16 affected files
  - [~] 1.5 Run the full test suite baseline (`pytest tests/` with `--tb=short`) and record which tests fail — these are the "red" tests the fix must turn green

- [ ] 2 Fix Category 4 — Syntax error and missing explicit imports (unblocks AI package)
  - [~] 2.1 Verify `app/ai/llm_agent.py` (renamed from `groq_agent.py`) compiles cleanly; confirm the syntax error is resolved
  - [~] 2.2 Run `python -m py_compile app/ai/llm_agent.py` and assert exit code 0
  - [~] 2.3 Run `python -m pyflakes app/ai/llm_agent.py` and fix any remaining undefined names
  - [~] 2.4 Run the full audit tool (`_check_imports.py` or `pyflakes app/`) to produce the definitive list of 15 files with missing explicit imports
  - [~] 2.5 For each file in the 15-file list: add explicit `from app.x.y import name` statements; verify with `py_compile` and `pyflakes`
  - [~] 2.6 Re-run `tests/test_explicit_imports.py` — all tests in this file must pass before proceeding

- [ ] 3 Fix Category 1 — Break all 18 circular import cycles
  - [~] 3.1 Audit `app/storage/buffer.py` for any module-level imports of `app.monitoring.self_learner`, `app.storage.mongo_store`, `app.ai.*`, or `app.utils.prediction_flow`; convert each to a lazy import inside the calling function
  - [~] 3.2 Audit `app/storage/mongo_store.py` for module-level imports of `app.storage.buffer`; convert `from app.storage.buffer import _archive_finished_locally` (in `cleanup_buffer`) to a function-scope import if not already
  - [~] 3.3 Audit `app/utils/prediction_flow.py` for module-level imports of `app.ai.ai_brain`, `app.ai.ai_router`, `app.ai.ai_prediction_pipeline`, `app.monitoring.self_learner`; convert any found to lazy imports
  - [~] 3.4 Audit `app/ai/ai_brain.py` — confirm all imports of `app.ai.ai_router` are already function-scope; fix any that are not
  - [~] 3.5 Audit `app/ai/ai_prediction_pipeline.py` — confirm all imports of `app.ai.ai_router`, `app.storage.buffer`, `app.storage.mongo_store`, `app.monitoring.self_learner` are function-scope; fix any that are not
  - [~] 3.6 Audit `app/monitoring/self_learner.py` — confirm it has no module-level back-imports to `ai.*`, `storage.buffer`, `storage.mongo_store`, or `utils.prediction_flow`
  - [~] 3.7 Write and run a cycle-detection script (`tools/check_cycles.py`) that imports each of the 6 hub modules in a clean process and asserts clean import; parametrize over all 18 pairs
  - [~] 3.8 Re-run `tests/test_circular_imports.py` — all 18 cycle tests must pass

- [ ] 4 Fix Category 2 — Consolidate true-duplicate functions (19 functions)
  - [~] 4.1 Identify all files containing each of the 19 true-duplicate functions using the AST scan from task 1.2
  - [~] 4.2 Create `app/utils/doc_helpers.py` as the canonical home for document-inspection helpers (`_context_source`, `_is_live_doc`, `_is_finished_doc`, `_is_not_started_period`, `_date_from_start_time`, `_safe_call`, `_band`, `_impact`) that have no existing canonical home
  - [~] 4.3 Create `app/utils/web_helpers.py` as the canonical home for `_fetch_web`
  - [~] 4.4 Add `_ensure_column`, `_ensure_signal_outcomes_table`, `_ensure_signal_combination_outcomes_table` to `app/storage/db.py` if not already present
  - [~] 4.5 Add `_side_from_selection_and_match`, `_side_from_team_selection`, `_match_sides` to `app/utils/match_helpers.py` if not already present
  - [~] 4.6 For each non-canonical file that defines one of the 19 true-duplicate functions: delete the local definition and add the canonical import at the top of the file
  - [~] 4.7 Run `pytest` to confirm no regressions from step 4.6; fix any import errors that arise
  - [~] 4.8 Re-run `tests/test_duplicate_functions.py` — all 19 tests must report `count_definitions == 1`

- [ ] 5 Fix Category 2 — Rationalise near-duplicate function families (7 families)
  - [~] 5.1 Consolidate `_hf_token` — remove local definitions from `ai_brain.py` and any other file that defines it; update all callers to `from app.config.config import _hf_token`
  - [~] 5.2 Consolidate `_extract_1x2` — create a single parameterised implementation in `app/utils/match_helpers.py` with a `strict: bool = False` parameter; update all 4 call sites
  - [~] 5.3 Consolidate `_data_sources` — create canonical implementation in `app/utils/doc_helpers.py` with `include_meta: bool = True`; update all 3 call sites
  - [~] 5.4 Confirm `_minute_bucket`, `_score_state`, `_snapshot_row` canonical homes in `app/storage/league_memory/_helpers.py`; delete any copies elsewhere and update imports
  - [ ] 5.5 Consolidate `_parse_datetime` — create parameterised canonical in `app/utils/primitives.py` with `tz_aware: bool = True`; update all 3 call sites
  - [ ] 5.6 Run `pytest` to confirm no regressions; re-run `tests/test_duplicate_functions.py`

- [ ] 6 Fix Category 3 — Externalise hardcoded constants to `Settings`
  - [ ] 6.1 Add all new `Settings` fields to the `Settings` dataclass in `app/config/config.py` (ensemble weights, regime thresholds, pick-generator baselines, calibrator thresholds, Poisson avg goals)
  - [ ] 6.2 Add corresponding `os.getenv(ENV_VAR, "original_default")` reads to `get_settings()` body, with exact original literal values as defaults
  - [ ] 6.3 Update `.env.example` with the new environment variable names and their default values (documentation only; does not change runtime behaviour when absent)
  - [ ] 6.4 Refactor `app/models/ensemble.py` — replace `_BASE_WEIGHTS` literal dict with `_default_weights()` that calls `get_settings()`; keep `_BASE_WEIGHTS` as a module-level alias pointing to `_default_weights()` for backward compat
  - [ ] 6.5 Refactor `app/market/regime.py` — replace `TIER_1`–`TIER_4` literal `Regime(...)` constructions with a `_build_tiers()` factory that reads from `get_settings()`; reassign `TIER_1, TIER_2, TIER_3, TIER_4` from the factory call
  - [ ] 6.6 Refactor `app/risk/pick_generator.py` — replace the `0.54` and `0.50` confidence literals in `_fallback_picks` with reads from `get_settings()`
  - [ ] 6.7 Refactor `app/enrichment/confidence_calibrator.py` — replace `moderate_threshold = 10.0` and `severe_threshold = 20.0` fallback literals in `compute_calibration_gap` with reads from `get_settings()`
  - [ ] 6.8 Refactor `app/models/poisson.py` — replace the `1.3` literal divisor (league-average goals) in `run_poisson` with a named variable read from `get_settings().poisson_league_avg_goals`
  - [ ] 6.9 Refactor `app/ai/prediction_agent.py` — identify and externalise any numeric confidence/opponent-weight literals in the agent scoring logic to `Settings`
  - [ ] 6.10 Re-run `tests/test_hardcoded_constants.py` — all tests must pass (default-value assertions and env-override assertions)
  - [ ] 6.11 Run the full `pytest` suite to confirm no regressions in prediction output

- [ ] 7 Write and run property-based tests for preservation checking
  - [ ] 7.1 Write Hypothesis tests in `tests/test_pbt_duplicate_preservation.py` verifying that each canonical duplicate function returns the same result as the original implementation for arbitrary inputs (use the original definition captured before deletion as the oracle)
  - [ ] 7.2 Write Hypothesis tests in `tests/test_pbt_config_defaults.py` verifying that with no environment variables set, all new `Settings` fields equal their original hardcoded values across any number of `invalidate_settings_cache()` / `get_settings()` cycles
  - [ ] 7.3 Write Hypothesis tests in `tests/test_pbt_import_stability.py` verifying that non-cycle module pairs can be imported in any order without error
  - [ ] 7.4 Run all PBT tests (`pytest tests/test_pbt_*.py -v`) and confirm all pass

- [ ] 8 Integration and regression verification
  - [ ] 8.1 Run the full test suite (`pytest tests/ -v --tb=short`) and confirm all tests pass (zero failures)
  - [ ] 8.2 Cold-start smoke test: run `python -c "from app.main import app; print('OK')"` in a clean Python process and assert no error output
  - [ ] 8.3 Prediction smoke test: run a sample prediction through `app/utils/prediction_flow.py` with a known fixture document and assert the output structure is unchanged from the pre-fix baseline
  - [ ] 8.4 Config override smoke test: set `ENSEMBLE_WEIGHT_DIXON_COLES=0.30`, call `invalidate_settings_cache()`, run the ensemble model, and assert `weights_used["dixon_coles"] == 0.30`; then restore the env var and confirm default behaviour
  - [ ] 8.5 Run `python -m pyflakes app/` and confirm zero errors (excluding any intentional `_` suppression patterns)
  - [ ] 8.6 Run `python -m py_compile` on all `app/**/*.py` files and confirm zero compile errors

- [ ] 9 Documentation and cleanup
  - [ ] 9.1 Update `app/utils/README.md` to document the new canonical homes (`doc_helpers.py`, `web_helpers.py`) and list the functions they export
  - [ ] 9.2 Update `app/config/README.md` with a table of all new environment variables, their defaults, and the business context for each
  - [ ] 9.3 Remove any debug output files (`_check_out.txt`, `debug_*.py`, `debug_*.txt`, `dup_output.txt`, `_pyflakes.txt`, `_compile_test.txt`) that were generated during the audit phase and are no longer needed
  - [ ] 9.4 Final review: re-read `bugfix.md` requirements 2.1–2.18 and 3.1–3.9 line by line; verify every clause is satisfied by the completed tasks
