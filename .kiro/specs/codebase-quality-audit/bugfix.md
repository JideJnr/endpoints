# Bugfix Requirements Document

## Introduction

This document captures requirements for resolving four categories of structural and maintenance defects identified during a comprehensive audit of the football prediction application. The issues span the Python backend (`app/`) and TypeScript frontend (`prediction/`, `services/`), and range from runtime-threatening circular import cycles and widespread code duplication to magic-number constants scattered throughout business logic and missing explicit imports. Left unaddressed, these defects create import errors during cold starts, make the codebase unmaintainable, harden configuration values that should be tunable, and produce latent runtime failures from undefined names. The goal is to eliminate these hazards systematically while preserving all existing prediction logic and application behaviour.

---

## Bug Analysis

### Current Behavior (Defect)

**Category 1 — Circular Dependencies (18 cycles)**

1.1 WHEN Python loads any module in the `mongo_store ↔ prediction_flow`, `mongo_store ↔ buffer`, `mongo_store ↔ ai_brain`, `mongo_store ↔ ai_router`, `mongo_store ↔ self_learner`, or `mongo_store ↔ ai_prediction_pipeline` import chains THEN the runtime encounters a circular dependency that can raise `ImportError` or produce partially-initialised module objects.

1.2 WHEN Python loads any module in the `buffer ↔ prediction_flow`, `buffer ↔ ai_brain`, `buffer ↔ ai_router`, `buffer ↔ self_learner`, or `buffer ↔ ai_prediction_pipeline` import chains THEN the runtime encounters a circular dependency for the same reasons as 1.1.

1.3 WHEN Python loads any module in the `prediction_flow ↔ ai_brain`, `prediction_flow ↔ ai_router`, `prediction_flow ↔ self_learner`, or `prediction_flow ↔ ai_prediction_pipeline` import chains THEN the runtime encounters a circular dependency.

1.4 WHEN Python loads any module in the `ai_brain ↔ ai_router`, `ai_brain ↔ self_learner`, or `ai_router ↔ self_learner` import chains THEN the runtime encounters a circular dependency.

1.5 WHEN the application performs a cold start or any involved module is reloaded at runtime THEN one or more of the 18 circular import cycles may cause an `ImportError` or silently bind an incomplete module, breaking the prediction pipeline.

**Category 2 — Duplicate and Near-Duplicate Functions (30+ instances)**

1.6 WHEN a developer needs to fix a bug or change behaviour in any of the 19 confirmed true-duplicate functions (e.g. `_safe_float`, `_safe_json`, `_fraction_to_probability`, `_team_name`, `_context_source`, `_is_live_doc`, `_is_finished_doc`, `_is_not_started_period`, `_date_from_start_time`, `_safe_call`, `_band`, `_impact`, `_ensure_column`, `_ensure_signal_outcomes_table`, `_ensure_signal_combination_outcomes_table`, `_fetch_web`, `_side_from_selection_and_match`, `_side_from_team_selection`, `_match_sides`) THEN the change must be made in two or more locations, and any location that is missed silently diverges.

1.7 WHEN a developer works with any of the near-duplicate function families (`_hf_token` in 3 files, `_extract_1x2` in 4 files, `_data_sources` in 3 files, `_minute_bucket` in 3 files, `_score_state` in 3 files, `_parse_datetime` in 3 files, `_snapshot_row` in 3 files) THEN the copies already have subtly different implementations, causing inconsistent behaviour across the code paths that call each copy.

**Category 3 — Hardcoded Values That Should Be Configurable**

1.8 WHEN the ensemble model selects base weights THEN it uses hardcoded fallback values in `models/ensemble.py` rather than reading from configuration, preventing runtime tuning without a code change.

1.9 WHEN the market regime classifier evaluates a pick THEN it uses hardcoded Elite/Major/Mid/Fringe confidence and edge thresholds in `market/regime.py`, making re-calibration require a code deployment.

1.10 WHEN the pick generator produces a recommendation THEN it uses the hardcoded baseline confidence values `0.50` and `0.54` in `risk/pick_generator.py`, making threshold tuning require a code deployment.

1.11 WHEN the prediction agent scores opponent weight or calculates confidence THEN it uses hardcoded opponent-weight thresholds and confidence formulas in `ai/prediction_agent.py`, preventing calibration without code changes.

1.12 WHEN the confidence calibrator applies fallback thresholds THEN it uses hardcoded values `10.0` and `20.0` in `enrichment/confidence_calibrator.py`.

1.13 WHEN the Poisson model computes expected goals THEN it uses the hardcoded league-average goals constant `1.4` in `models/poisson.py`, making per-league calibration impossible without a code change.

1.14 WHEN the TypeScript prediction engine initialises ELO parameters or Poisson constants THEN it uses hardcoded values `DEFAULT_ELO=1500`, `ELO_K=32`, `ELO_HOME_ADVANTAGE=100`, `maxGoals=10`, and `leagueAvgGoals=1.4` in `prediction/engine.ts`, preventing runtime configuration.

1.15 WHEN the football API service queries data THEN it uses 20+ hardcoded limit values (ranging from 4 to 5000) scattered across `services/apis/footballApi.ts`, making query-size tuning require source edits.

1.16 WHEN the engine learning module fetches training data THEN it uses hardcoded pagination limits `50`, `10`, and `20` in `prediction/engineLearning.ts`.

**Category 4 — Missing Explicit Imports**

1.17 WHEN `routers/frontend.py` is loaded THEN names used across its 3000+ lines may resolve at runtime only because of prior `from x import *` statements elsewhere in the module or through indirect re-exports, creating fragile implicit dependencies.

1.18 WHEN any of the 15 files identified by the audit use names sourced from sibling modules without explicit imports THEN a refactoring or module reorganisation silently breaks those call sites at runtime rather than raising an `ImportError` at load time.

1.19 WHEN `ai/llm_agent.py` is loaded THEN Python raises a `SyntaxError` due to an unmatched `)` at line 230, preventing the entire `ai` package from loading in configurations that import `llm_agent`.

---

### Expected Behavior (Correct)

**Category 1 — Circular Dependencies**

2.1 WHEN Python loads any module involved in the 18 previously circular import chains THEN the runtime SHALL complete the import without `ImportError` or partially-initialised module state, because each cycle has been broken through dependency inversion, lazy imports, or extraction of shared types into a separate module.

2.2 WHEN `mongo_store` is imported by any consumer module THEN it SHALL import cleanly without creating a back-dependency on `prediction_flow`, `buffer`, `ai_brain`, `ai_router`, `self_learner`, or `ai_prediction_pipeline`.

2.3 WHEN the application performs a cold start THEN all modules SHALL load in a deterministic, cycle-free order.

**Category 2 — Duplicate and Near-Duplicate Functions**

2.4 WHEN a developer needs to change the behaviour of any of the 19 true-duplicate functions THEN the change SHALL be made in exactly one canonical location (a shared utility module), and all previous call sites SHALL import from that location.

2.5 WHEN fixing or changing any of the near-duplicate function families THEN each family SHALL be rationalised to a single implementation with any legitimately differing parameters surfaced as arguments, and all call sites SHALL import from the canonical location.

2.6 WHEN the canonical implementations replace duplicates THEN the public interface and return types of each function SHALL remain unchanged so that all existing callers continue to work without modification to call sites.

**Category 3 — Hardcoded Values**

2.7 WHEN the ensemble model selects base weights THEN it SHALL read them from configuration (environment variable or config file), with the current hardcoded values as documented defaults only.

2.8 WHEN the market regime classifier evaluates a pick THEN it SHALL read Elite/Major/Mid/Fringe confidence and edge thresholds from configuration, with the current values as documented defaults.

2.9 WHEN the pick generator produces a recommendation THEN it SHALL read baseline confidence thresholds from configuration, with `0.50` and `0.54` as documented defaults.

2.10 WHEN the prediction agent scores opponent weight or calculates confidence THEN it SHALL read thresholds and formula parameters from configuration, with current values as documented defaults.

2.11 WHEN the confidence calibrator applies fallback thresholds THEN it SHALL read them from configuration, with `10.0` and `20.0` as documented defaults.

2.12 WHEN the Poisson model computes expected goals THEN it SHALL read the league-average goals constant from configuration, with `1.4` as the documented default.

2.13 WHEN the TypeScript prediction engine initialises ELO or Poisson parameters THEN it SHALL read `DEFAULT_ELO`, `ELO_K`, `ELO_HOME_ADVANTAGE`, `maxGoals`, and `leagueAvgGoals` from a configuration source, with current values as documented defaults.

2.14 WHEN the football API service queries data THEN all query-limit constants in `services/apis/footballApi.ts` SHALL be read from configuration or a named constants file, with current values as documented defaults.

2.15 WHEN the engine learning module fetches training data THEN its pagination limits SHALL be read from configuration, with current values as documented defaults.

**Category 4 — Missing Explicit Imports**

2.16 WHEN `routers/frontend.py` is loaded THEN every name it uses SHALL be explicitly imported at the top of the file (or in the enclosing scope), with no reliance on `from x import *` to make names available.

2.17 WHEN any of the 15 affected files are loaded THEN every name used from a sibling module SHALL have an explicit import statement, so that a missing dependency raises `ImportError` at load time rather than `NameError` at call time.

2.18 WHEN `ai/llm_agent.py` is loaded THEN Python SHALL parse the file without `SyntaxError`; the unmatched `)` at line 230 SHALL be corrected.

---

### Unchanged Behavior (Regression Prevention)

**Category 1 — Circular Dependencies**

3.1 WHEN a module is refactored to break a circular dependency THEN the module's public API (exported names, function signatures, class interfaces) SHALL CONTINUE TO be unchanged from the perspective of all callers.

3.2 WHEN dependency inversion or lazy imports are introduced THEN the runtime behaviour of all prediction pipelines SHALL CONTINUE TO produce identical results to the pre-refactor state.

**Category 2 — Duplicate Functions**

3.3 WHEN duplicates are consolidated into a canonical module THEN all existing callers that used any of the removed copies SHALL CONTINUE TO receive the same return values for the same inputs.

3.4 WHEN near-duplicate functions are rationalised THEN the canonical implementation SHALL CONTINUE TO handle all input domains previously handled by each variant (e.g. all bucket boundaries, all state labels, all error-handling paths).

**Category 3 — Hardcoded Values**

3.5 WHEN configuration is absent or the relevant key is missing THEN the system SHALL CONTINUE TO apply the previously hardcoded value as a default, so that the application runs unchanged in environments that have not been updated with explicit configuration.

3.6 WHEN any configurable value is added to configuration THEN all prediction logic that uses it SHALL CONTINUE TO produce results identical to the previous hardcoded behaviour when the default value is supplied.

**Category 4 — Missing Imports**

3.7 WHEN implicit wildcard imports are replaced with explicit imports THEN all route handlers and business logic in `routers/frontend.py` SHALL CONTINUE TO operate correctly and return unchanged responses.

3.8 WHEN explicit imports are added to the 15 affected files THEN no existing functionality SHALL be removed or altered; the fixes SHALL be purely additive import declarations.

3.9 WHEN `ai/llm_agent.py` syntax is corrected THEN all LLM agent functionality that was accessible before the syntax error was introduced SHALL CONTINUE TO work as previously intended.

---

## Bug Condition Pseudocode

The following structured pseudocode captures the bug conditions and properties for property-based testing.

### Category 1 — Circular Dependencies

```pascal
FUNCTION isBugCondition_CircularImport(module_pair)
  INPUT: module_pair of type (ModuleName, ModuleName)
  OUTPUT: boolean

  RETURN module_pair IN {
    (mongo_store, prediction_flow), (mongo_store, buffer),
    (mongo_store, ai_brain),        (mongo_store, ai_router),
    (mongo_store, self_learner),    (mongo_store, ai_prediction_pipeline),
    (buffer, prediction_flow),      (buffer, ai_brain),
    (buffer, ai_router),            (buffer, self_learner),
    (buffer, ai_prediction_pipeline),
    (prediction_flow, ai_brain),    (prediction_flow, ai_router),
    (prediction_flow, self_learner),(prediction_flow, ai_prediction_pipeline),
    (ai_brain, ai_router),          (ai_brain, self_learner),
    (ai_router, self_learner)
  }
END FUNCTION

// Property: Fix Checking — no module pair in the fixed codebase forms a cycle
FOR ALL module_pair WHERE isBugCondition_CircularImport(module_pair) DO
  result ← import_graph'(module_pair)
  ASSERT NOT has_cycle(result)
END FOR

// Property: Preservation Checking — non-cycle module pairs behave identically
FOR ALL module_pair WHERE NOT isBugCondition_CircularImport(module_pair) DO
  ASSERT import_graph(module_pair) = import_graph'(module_pair)
END FOR
```

### Category 2 — Duplicate Functions

```pascal
FUNCTION isBugCondition_Duplicate(function_name, file_path)
  INPUT: function_name : string, file_path : string
  OUTPUT: boolean

  RETURN (function_name, file_path) IN known_duplicate_registry
END FUNCTION

// Property: Fix Checking — each duplicate function exists in exactly one location
FOR ALL fn WHERE isBugCondition_Duplicate(fn.name, fn.path) DO
  canonical ← lookup_canonical(fn.name)
  ASSERT count_definitions(fn.name) = 1
  ASSERT canonical.path IN shared_utility_modules
END FOR

// Property: Preservation Checking — canonical function returns identical results
FOR ALL fn WHERE isBugCondition_Duplicate(fn.name, fn.path) DO
  FOR ALL input IN sample_inputs(fn.name) DO
    ASSERT F(fn.name, input) = F'(canonical(fn.name), input)
  END FOR
END FOR
```

### Category 3 — Hardcoded Values

```pascal
FUNCTION isBugCondition_Hardcoded(file_path, symbol_name)
  INPUT: file_path : string, symbol_name : string
  OUTPUT: boolean

  RETURN (file_path, symbol_name) IN hardcoded_constants_registry
END FUNCTION

// Property: Fix Checking — value is read from config, not literal
FOR ALL (file, sym) WHERE isBugCondition_Hardcoded(file, sym) DO
  source ← value_source'(file, sym)
  ASSERT source = CONFIG_SOURCE
END FOR

// Property: Preservation Checking — default equals original hardcoded value
FOR ALL (file, sym) WHERE isBugCondition_Hardcoded(file, sym) DO
  ASSERT config_default(sym) = original_hardcoded_value(file, sym)
  ASSERT F(sym) = F'(sym)   // behaviour unchanged when default is active
END FOR
```

### Category 4 — Missing Imports

```pascal
FUNCTION isBugCondition_MissingImport(file_path, used_name)
  INPUT: file_path : string, used_name : string
  OUTPUT: boolean

  RETURN used_name IN names_used_by(file_path)
     AND used_name NOT IN explicitly_imported_names(file_path)
     AND used_name NOT IN locally_defined_names(file_path)
END FUNCTION

// Property: Fix Checking — every used name has an explicit import
FOR ALL (file, name) WHERE isBugCondition_MissingImport(file, name) DO
  ASSERT name IN explicitly_imported_names'(file)
END FOR

// Property: Preservation Checking — adding explicit imports does not change behaviour
FOR ALL (file, name) WHERE NOT isBugCondition_MissingImport(file, name) DO
  ASSERT runtime_behaviour(file) = runtime_behaviour'(file)
END FOR
```
