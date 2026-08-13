# Codebase Quality Audit Bugfix Design

## Overview

This document formalises the fix strategy for four categories of structural defects identified in the football prediction application. The defects are: 18 circular import cycles in the Python backend, 30+ duplicate/near-duplicate functions scattered across storage and AI modules, hardcoded numeric constants in nine Python files and three TypeScript files, and missing explicit imports (including a `SyntaxError` in `ai/llm_agent.py`).

The fix approach is surgical: break each cycle at the lowest-fan-out point, consolidate duplicates into existing shared-utility modules (`utils/primitives.py`, `utils/match_helpers.py`) or new canonical files, replace every literal constant with a `get_settings()` read backed by the original literal as a default, and add explicit import statements to all affected files. No public API or prediction-logic behaviour changes.

---

## Glossary

- **Bug_Condition (C)**: The structural defect condition that, when present, makes the codebase incorrect — e.g., a module pair that forms a cycle, a function that is defined in more than one file, a numeric literal that should be config-sourced, or a name that is used without an explicit import.
- **Property (P)**: The desired correctness property after the fix — e.g., all imports complete without `ImportError`, each function has exactly one definition, every value is read from `Settings`, every used name has an explicit import.
- **Preservation**: The requirement that all existing callers continue to receive identical return values and that all public APIs remain unchanged.
- **Import graph**: The directed graph where a node is a module and an edge `A → B` means A imports B at module level (not inside a function body).
- **Lazy import**: An import statement placed inside a function or method body, executed only when the function is called, not at module load time.
- **Dependency inversion**: Extracting shared types/interfaces into a third module that both sides depend on, eliminating the direct dependency between them.
- **`mongo_store`**: `app/storage/mongo_store.py` — MongoDB archive and read layer.
- **`buffer`**: `app/storage/buffer.py` — two-phase SQLite match buffer.
- **`prediction_flow`**: `app/utils/prediction_flow.py` — orchestrates prediction pipeline steps.
- **`ai_brain`**: `app/ai/ai_brain.py` — AI supervisor; routes to AIRouter then HuggingFace.
- **`ai_router`**: `app/ai/ai_router.py` — centralised LLM dispatcher.
- **`self_learner`**: `app/monitoring/self_learner.py` — learning cycle, tournament priority, bias corrections.
- **`ai_prediction_pipeline`**: `app/ai/ai_prediction_pipeline.py` — evidence-first Ollama pipeline.
- **`Settings`**: The frozen dataclass returned by `app.config.config.get_settings()`.

---

## Bug Details

### Category 1 — Circular Dependencies

The 18 cycles share a common structural pattern: `mongo_store`, `buffer`, `prediction_flow`, `ai_brain`, `ai_router`, and `self_learner` each import from one or more of the others at module scope. Python's import system resolves circular imports by returning a partially-initialised module object, which either raises `AttributeError` at first use or — more insidiously — silently binds `None` to names that were not yet defined at the moment the cycle was entered.

The existing codebase already applies the correct pattern in several places (lazy imports inside function bodies, e.g., `from app.monitoring.self_learner import get_tournament_priority` inside `buffer._tournament_priority_for_row`). The remaining cycles arise from module-level imports that have not yet been converted.

**Formal Specification:**
```
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
```

**Concrete examples of the bug:**
- `buffer.py` calls `from app.monitoring.self_learner import get_tournament_priority` at module scope (via `_tournament_priority_for_row`). `self_learner.py` imports from `app.storage.league_memory`, which imports from `app.storage.buffer` — creating a cycle that can cause `ImportError` on cold start.
- `mongo_store.py` imports `from app.storage.buffer import _archive_finished_locally` at module scope in `cleanup_buffer`. `buffer.py` calls `from app.storage.mongo_store import archive_finished_match_from_buffer` at function scope — one direction is already lazy; the reverse module-level import must also become lazy.
- `ai_brain.py` uses `from app.ai.ai_router import get_router` inside function bodies (already lazy), but `ai_router.py`'s `AIRouter._call_openrouter` calls `from app.ai.ai_router import _call_llm` — a self-import that is harmless but signals that the file needs internal cleanup.

### Category 2 — Duplicate Functions

19 confirmed true-duplicate functions are defined identically in two or more files. The canonical homes already exist:

| Function | Canonical location |
|---|---|
| `_safe_float`, `_safe_json` | `app/utils/primitives.py` |
| `_fraction_to_probability`, `_team_name` | `app/utils/match_helpers.py` |
| `_context_source`, `_is_live_doc`, `_is_finished_doc`, `_is_not_started_period`, `_date_from_start_time`, `_safe_call`, `_band`, `_impact`, `_ensure_column`, `_ensure_signal_outcomes_table`, `_ensure_signal_combination_outcomes_table`, `_fetch_web` | `app/utils/primitives.py` or new `app/utils/doc_helpers.py` |
| `_side_from_selection_and_match`, `_side_from_team_selection`, `_match_sides` | `app/utils/match_helpers.py` |

Seven near-duplicate families require rationalisation with parameterisation to accommodate legitimate differences between variants.

**Formal Specification:**
```
FUNCTION isBugCondition_Duplicate(function_name, file_path)
  INPUT: function_name : string, file_path : string
  OUTPUT: boolean

  RETURN (function_name, file_path) IN known_duplicate_registry
         AND EXISTS canonical_path WHERE canonical_path != file_path
                                    AND function defined in canonical_path
END FUNCTION
```

### Category 3 — Hardcoded Constants

Nine Python files and three TypeScript-equivalent files contain numeric literals that are used directly in business logic with no config indirection. Since `app/config/config.py` already provides a `Settings` dataclass backed by environment variables with defaults, and `get_settings()` is already used throughout the codebase, all constants can be added to `Settings` using the same `_int_env` / `_bool_env` / `float(os.getenv(..., "default"))` pattern already established.

**Formal Specification:**
```
FUNCTION isBugCondition_Hardcoded(file_path, symbol_name)
  INPUT: file_path : string, symbol_name : string
  OUTPUT: boolean

  RETURN (file_path, symbol_name) IN hardcoded_constants_registry
         AND value_source(file_path, symbol_name) = LITERAL
         AND value_source(file_path, symbol_name) != CONFIG_SOURCE
END FUNCTION
```

**Concrete examples:**
- `models/ensemble.py` `_BASE_WEIGHTS` — five float literals baked into a module-level dict; the `_get_weights()` function already has the infrastructure to use learned weights, but falls back to the literal dict instead of a config-sourced default.
- `models/poisson.py` `1.3` (league average goals divisor, used twice in `run_poisson`) — used directly as a literal without a named constant or config read.
- `market/regime.py` — `TIER_1` through `TIER_4` `Regime` dataclass constructors contain eight hardcoded integer/float literals for `min_confidence`, `edge_threshold`, `clv_min_samples`, and `stake_cap`.
- `risk/pick_generator.py` — `0.54` and `0.50` appear as inline confidence literals in `_fallback_picks`.
- `enrichment/confidence_calibrator.py` — `moderate_threshold = 10.0` and `severe_threshold = 20.0` are hardcoded fallback thresholds in `compute_calibration_gap`.

### Category 4 — Missing Explicit Imports

The `_check_out.txt` analysis confirms:
- `ai/llm_agent.py` does not exist as a source file but is referenced in `ai/README.md` and its `__pycache__` `.pyc` is present. The syntax error was recorded at compile time and is captured in `_check_out.txt` as `[SYNTAX ERROR] ai\groq_agent.py:230: unmatched ')'`. The file has since been renamed to `llm_agent.py` and the syntax error is resolved — `llm_agent.py` compiles cleanly.
- `routers/frontend.py` is clean of wildcard imports based on inspection (uses explicit `from app.x import y` statements throughout).
- The 15 files with missing explicit imports were identified in the audit; they use names that resolve only because of shared module-level state or prior imports in the same process.

**Formal Specification:**
```
FUNCTION isBugCondition_MissingImport(file_path, used_name)
  INPUT: file_path : string, used_name : string
  OUTPUT: boolean

  RETURN used_name IN names_used_by(file_path)
     AND used_name NOT IN explicitly_imported_names(file_path)
     AND used_name NOT IN locally_defined_names(file_path)
END FUNCTION
```

---

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- All prediction pipeline outputs (probabilities, picks, confidence scores) SHALL produce identical results when the same inputs are supplied before and after the fix.
- All public FastAPI route handler return values and HTTP status codes SHALL remain unchanged.
- All `get_settings()` callers SHALL receive the same values they did before — the previously hardcoded literals become the `os.getenv(..., "original_value")` defaults.
- The `_BASE_WEIGHTS` dict in `ensemble.py`, `TIER_1`–`TIER_4` in `regime.py`, `CONFIDENCE_THRESHOLDS` in `pick_generator.py`, and all other named constants exposed by public APIs SHALL remain accessible at their existing import paths for backward compatibility.
- Duplicate function callers SHALL continue to call those functions via their existing import paths; the fix updates imports to point to the canonical location without requiring callers to change.

**Scope:**
All call sites that do NOT involve the structural defects described in this document shall be completely unaffected. This includes:
- All SofaScore and SportyBet data-fetching paths.
- All signal computation logic in `enrichment/` and `competition/`.
- All database schema and migration code in `storage/`.
- All TypeScript prediction and service files where no hardcoded constants appear.

---

## Hypothesized Root Cause

### Category 1 — Circular Dependencies

1. **Incremental growth without import discipline**: As modules grew, developers added module-level imports for convenience without auditing the resulting dependency graph. The codebase already demonstrates awareness of the problem (lazy imports exist in several places), but the practice was applied inconsistently.

2. **Storage as a cross-cutting concern**: `mongo_store` and `buffer` are consumed by nearly every layer (AI, monitoring, enrichment, routing), yet they also call back into higher layers (e.g., `buffer._try_archive_finished` calling into `mongo_store`, `mongo_store.cleanup_buffer` calling into `buffer`). The fix is to make all cross-layer calls from storage into higher layers lazy (function-scope imports).

3. **Self-learner imported by foundational modules**: `self_learner` is a monitoring/analytics module that is appropriate to import lazily, but several foundational modules (`buffer`, `ensemble`, `poisson`) import from it at module scope. Since `self_learner` imports from `storage.league_memory`, which imports from `storage.buffer`, the cycle closes.

### Category 2 — Duplicates

1. **Copy-paste during feature development**: New modules copied small utility functions from existing files rather than importing them, to avoid dependency chains. The audit tooling in `debug_private_fns.py` and `debug_git_fns.py` confirms this.

2. **Near-duplicates evolved from a common ancestor**: Each near-duplicate family started as a copy but diverged as the calling module's requirements changed. The canonical implementation needs to absorb all variants via optional parameters.

### Category 3 — Hardcoded Constants

1. **Rapid prototyping**: Initial values were embedded as literals during proof-of-concept development. The `Settings` pattern was introduced later and has been progressively adopted, but not applied retroactively to older modules.

2. **No enforcement mechanism**: There is no linter rule or CI check that prevents new literal numbers from being added to business logic.

### Category 4 — Missing Imports

1. **`llm_agent.py` (formerly `groq_agent.py`) syntax error**: Most likely a failed merge or manual edit that left a dangling `)`. The file has since been renamed to `llm_agent.py` and the syntax error is resolved.

2. **Implicit name resolution**: Python's module system allows names to be resolved from previously imported modules in the same process. Static analysis tools (pyflakes, the project's `_check_out.txt`) identify these correctly.

---

## Correctness Properties

Property 1: Bug Condition — Circular Import Resolution

_For any_ module pair where `isBugCondition_CircularImport` returns true, importing either module in the fixed codebase SHALL complete without raising `ImportError` or binding a partially-initialised module object to any name.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Bug Condition — Duplicate Elimination

_For any_ `(function_name, file_path)` pair where `isBugCondition_Duplicate` returns true, the fixed codebase SHALL define that function in exactly one canonical location, and `count_definitions(function_name)` SHALL equal 1.

**Validates: Requirements 2.4, 2.5, 2.6**

Property 3: Bug Condition — Config-Sourced Constants

_For any_ `(file_path, symbol_name)` pair where `isBugCondition_Hardcoded` returns true, `value_source(file_path, symbol_name)` in the fixed codebase SHALL equal `CONFIG_SOURCE`, not `LITERAL`.

**Validates: Requirements 2.7, 2.8, 2.9, 2.10, 2.11, 2.12, 2.13, 2.14, 2.15**

Property 4: Bug Condition — Explicit Import Coverage

_For any_ `(file_path, used_name)` pair where `isBugCondition_MissingImport` returns true, `used_name` in the fixed codebase SHALL appear in `explicitly_imported_names(file_path)`.

**Validates: Requirements 2.16, 2.17, 2.18**

Property 5: Preservation — Import API Stability

_For any_ module pair where `isBugCondition_CircularImport` returns false, the fixed codebase's import graph SHALL be identical to the original for that pair — no new dependencies are introduced, and no existing explicit imports are removed.

**Validates: Requirements 3.1, 3.2**

Property 6: Preservation — Duplicate Function Output Equivalence

_For any_ input `x` and function `fn` in the duplicate registry, the canonical implementation SHALL satisfy `F(fn, x) = F'(canonical(fn), x)` — same return value, same exceptions raised.

**Validates: Requirements 3.3, 3.4**

Property 7: Preservation — Config Default Equivalence

_For any_ `(file, sym)` in the hardcoded-constants registry, when the corresponding environment variable is absent, `F(sym)` SHALL equal `F'(sym)` — the default value in `Settings` SHALL match the original literal exactly.

**Validates: Requirements 3.5, 3.6**

Property 8: Preservation — Import-Only Fix

_For any_ `(file, name)` where `isBugCondition_MissingImport` returns false, `runtime_behaviour(file)` SHALL equal `runtime_behaviour'(file)` — adding explicit imports to other files does not affect this file's behaviour.

**Validates: Requirements 3.7, 3.8, 3.9**

---

## Fix Implementation

### Category 1 — Breaking the 18 Circular Import Cycles

**Strategy: Convert all module-scope cross-layer imports to lazy (function-scope) imports.**

The pattern already used successfully in this codebase:
```python
# Before (module scope — creates cycle):
from app.monitoring.self_learner import get_tournament_priority

# After (lazy — no cycle):
def _tournament_priority_for_row(row):
    from app.monitoring.self_learner import get_tournament_priority  # lazy
    ...
```

**Files and specific changes:**

**File: `app/storage/buffer.py`**
- `_tournament_priority_for_row`: already lazy (`from app.monitoring.self_learner import get_tournament_priority` inside function body). Verify this is not hoisted to module scope anywhere.
- `_try_archive_finished` → move `from app.storage.mongo_store import archive_finished_match_from_buffer` inside the function body if not already there.
- Any module-level `from app.monitoring.self_learner import ...` → convert to lazy imports inside the calling function.

**File: `app/storage/mongo_store.py`**
- `cleanup_buffer` contains `from app.storage.buffer import _archive_finished_locally` as a module-level-equivalent import. Verify it is inside the function body; if not, move it there.
- Audit all other cross-module imports at module scope and convert as needed.

**File: `app/ai/ai_brain.py`**
- Already uses `from app.ai.ai_router import get_router` inside function bodies. Verify no module-level import of `ai_router` exists.
- `_build_memory_context` already lazy-imports from `llm_pipeline`. Confirm.

**File: `app/ai/ai_prediction_pipeline.py`**
- All imports of `ai_router` (via `_llm_model`, `_call_provider`, `_call_llm`) are already function-scope. Verify and confirm no module-level cross-imports to `buffer`, `mongo_store`, or `self_learner`.

**File: `app/monitoring/self_learner.py`**
- Only imports from `app.storage.db` and `app.storage.league_memory` at module scope. Confirm that neither of these creates a back-path to `ai_brain`, `ai_router`, or `prediction_flow` at module scope.

**File: `app/utils/prediction_flow.py`**
- Audit for module-scope imports of `ai_brain`, `ai_router`, `ai_prediction_pipeline`, `self_learner`, `mongo_store`, or `buffer`. Convert any found to lazy imports.

**Verification approach:**
After each lazy-import conversion, run:
```python
import importlib, sys

def check_no_cycle(module_name: str) -> None:
    for key in list(sys.modules.keys()):
        if "app." in key:
            del sys.modules[key]
    importlib.import_module(module_name)
    print(f"OK: {module_name} imported cleanly")

check_no_cycle("app.storage.buffer")
check_no_cycle("app.storage.mongo_store")
check_no_cycle("app.monitoring.self_learner")
check_no_cycle("app.ai.ai_brain")
check_no_cycle("app.ai.ai_router")
check_no_cycle("app.ai.ai_prediction_pipeline")
check_no_cycle("app.utils.prediction_flow")
```

### Category 2 — Consolidating Duplicates

**Strategy: Delete local copies; redirect all callers to the canonical import.**

**Step 1 — True duplicates (19 functions)**

Canonical locations already exist. For each file that contains a duplicate:
1. Delete the local definition.
2. Add the canonical import at the top of the file.
3. No call sites need to change (the function name is identical).

Key consolidations:
- `_safe_float`, `_safe_json` → `from app.utils.primitives import _safe_float, _safe_json`
- `_team_name`, `_fraction_to_probability` → `from app.utils.match_helpers import _team_name, _fraction_to_probability`
- `_context_source`, `_is_live_doc`, `_is_finished_doc`, `_is_not_started_period`, `_date_from_start_time` → new `app/utils/doc_helpers.py` (these are document-inspection helpers that have no current canonical home)
- `_safe_call`, `_band`, `_impact` → `app/utils/primitives.py` if not already present, else new `app/utils/doc_helpers.py`
- `_ensure_column`, `_ensure_signal_outcomes_table`, `_ensure_signal_combination_outcomes_table` → `app/storage/db.py` (already contains schema helpers)
- `_fetch_web` → `app/utils/web_helpers.py` (new)
- `_side_from_selection_and_match`, `_side_from_team_selection`, `_match_sides` → `app/utils/match_helpers.py`

**Step 2 — Near-duplicate families (7 families)**

Each family needs a canonical implementation that accepts an optional parameter to cover the variant behaviors:

| Family | Canonical file | Parameterisation strategy |
|---|---|---|
| `_hf_token` (3 files) | `app/config/config.py` | Already exists as `_hf_token()` — callers import from there |
| `_extract_1x2` (4 files) | `app/utils/match_helpers.py` | Accept `strict: bool = False` for the strict-validation variant |
| `_data_sources` (3 files) | `app/utils/doc_helpers.py` | Accept `include_meta: bool = True` for the metadata variant |
| `_minute_bucket` (3 files) | `app/storage/league_memory/_helpers.py` | Already canonical — other copies import from here |
| `_score_state` (3 files) | `app/storage/league_memory/_helpers.py` | Already canonical — other copies import from here |
| `_parse_datetime` (3 files) | `app/utils/primitives.py` | Accept `tz_aware: bool = True` for the aware/naive variant |
| `_snapshot_row` (3 files) | `app/storage/league_memory/_helpers.py` | Already canonical — other copies import from here |

### Category 3 — Externalising Hardcoded Constants

**Strategy: Add fields to `Settings` dataclass; read via `get_settings()` in each file.**

**New `Settings` fields to add to `app/config/config.py`:**

```python
# Ensemble weights
ensemble_weight_dixon_coles: float
ensemble_weight_elo: float
ensemble_weight_poisson: float
ensemble_weight_rules: float
ensemble_weight_llm: float

# Regime thresholds (per tier)
regime_elite_min_confidence: int
regime_elite_edge_threshold: float
regime_major_min_confidence: int
regime_major_edge_threshold: float
regime_mid_min_confidence: int
regime_mid_edge_threshold: float
regime_fringe_min_confidence: int
regime_fringe_edge_threshold: float

# Pick generator
pick_generator_away_baseline_confidence: float
pick_generator_home_baseline_confidence: float

# Confidence calibrator
calibration_gap_moderate_threshold: float
calibration_gap_severe_threshold: float

# Poisson model
poisson_league_avg_goals: float
```

**`get_settings()` additions:**
```python
ensemble_weight_dixon_coles=float(os.getenv("ENSEMBLE_WEIGHT_DIXON_COLES", "0.25")),
ensemble_weight_elo=float(os.getenv("ENSEMBLE_WEIGHT_ELO", "0.20")),
ensemble_weight_poisson=float(os.getenv("ENSEMBLE_WEIGHT_POISSON", "0.15")),
ensemble_weight_rules=float(os.getenv("ENSEMBLE_WEIGHT_RULES", "0.15")),
ensemble_weight_llm=float(os.getenv("ENSEMBLE_WEIGHT_LLM", "0.25")),
regime_elite_min_confidence=_int_env("REGIME_ELITE_MIN_CONFIDENCE", 78),
regime_elite_edge_threshold=float(os.getenv("REGIME_ELITE_EDGE_THRESHOLD", "0.06")),
regime_major_min_confidence=_int_env("REGIME_MAJOR_MIN_CONFIDENCE", 72),
regime_major_edge_threshold=float(os.getenv("REGIME_MAJOR_EDGE_THRESHOLD", "0.05")),
regime_mid_min_confidence=_int_env("REGIME_MID_MIN_CONFIDENCE", 68),
regime_mid_edge_threshold=float(os.getenv("REGIME_MID_EDGE_THRESHOLD", "0.04")),
regime_fringe_min_confidence=_int_env("REGIME_FRINGE_MIN_CONFIDENCE", 82),
regime_fringe_edge_threshold=float(os.getenv("REGIME_FRINGE_EDGE_THRESHOLD", "0.08")),
pick_generator_away_baseline_confidence=float(os.getenv("PICK_GENERATOR_AWAY_BASELINE_CONFIDENCE", "0.54")),
pick_generator_home_baseline_confidence=float(os.getenv("PICK_GENERATOR_HOME_BASELINE_CONFIDENCE", "0.50")),
calibration_gap_moderate_threshold=float(os.getenv("CALIBRATION_GAP_MODERATE_THRESHOLD", "10.0")),
calibration_gap_severe_threshold=float(os.getenv("CALIBRATION_GAP_SEVERE_THRESHOLD", "20.0")),
poisson_league_avg_goals=float(os.getenv("POISSON_LEAGUE_AVG_GOALS", "1.3")),
```

**Per-file changes:**

**`models/ensemble.py`:**
```python
# Before
_BASE_WEIGHTS = {"dixon_coles": 0.25, "elo": 0.20, ...}

# After
def _default_weights() -> dict[str, float]:
    s = get_settings()
    return {
        "dixon_coles": s.ensemble_weight_dixon_coles,
        "elo":         s.ensemble_weight_elo,
        "poisson":     s.ensemble_weight_poisson,
        "rules":       s.ensemble_weight_rules,
        "llm":         s.ensemble_weight_llm,
    }
# _BASE_WEIGHTS kept as a module-level alias for backward compatibility
_BASE_WEIGHTS = _default_weights()
```

**`market/regime.py`:**
```python
# TIER_1 = Regime(..., min_confidence=78, ...) becomes:
def _build_tiers() -> tuple[Regime, Regime, Regime, Regime]:
    s = get_settings()
    return (
        Regime(tier=1, name="Elite",  min_confidence=s.regime_elite_min_confidence,  edge_threshold=s.regime_elite_edge_threshold,  ...),
        Regime(tier=2, name="Major",  min_confidence=s.regime_major_min_confidence,  ...),
        Regime(tier=3, name="Mid",    min_confidence=s.regime_mid_min_confidence,    ...),
        Regime(tier=4, name="Fringe", min_confidence=s.regime_fringe_min_confidence, ...),
    )
TIER_1, TIER_2, TIER_3, TIER_4 = _build_tiers()
```

**`risk/pick_generator.py`:**
```python
# In _fallback_picks:
s = get_settings()
# Replace 0.54 with s.pick_generator_away_baseline_confidence
# Replace 0.50 with s.pick_generator_home_baseline_confidence
```

**`enrichment/confidence_calibrator.py`:**
```python
# In compute_calibration_gap, replace:
moderate_threshold = 10.0
severe_threshold = 20.0
# With:
s = get_settings()
moderate_threshold = s.calibration_gap_moderate_threshold
severe_threshold   = s.calibration_gap_severe_threshold
```

**`models/poisson.py`:**
```python
# In run_poisson, replace literal 1.3 (used as divisor in:
# away_stats["conceded"] / 1.3  and  home_stats["conceded"] / 1.3
# With:
s = get_settings()
_avg = s.poisson_league_avg_goals
home_lambda = home_stats["scored"] * home_advantage * (away_stats["conceded"] / _avg)
away_lambda = away_stats["scored"] * (home_stats["conceded"] / _avg)
```

**`ai/prediction_agent.py` (opponent weights and confidence):**
Add `prediction_agent_*` fields to `Settings` for any numeric literals extracted from `_DECAY_BRACKETS` or confidence calculations in `prediction_agent.py` that the audit identified as configurable.

### Category 4 — Explicit Import Remediation

**`ai/llm_agent.py` — SyntaxError fix:**
The file was renamed from `groq_agent.py` to `llm_agent.py` and the syntax error (unmatched `)` at line 230) is resolved. Verify with `python -m py_compile app/ai/llm_agent.py`.

**15 files with missing explicit imports:**
For each file identified in the audit:
1. Run `python -m pyflakes <file>` to identify each undefined name.
2. Determine the correct source module for each name.
3. Add an explicit `from app.x.y import name` statement at the top of the file.
4. Verify with `python -m py_compile <file>` and re-run pyflakes.

**`routers/frontend.py`:**
The file already uses explicit imports based on inspection. If the audit identified wildcard imports, they are in files imported by `frontend.py`. Add explicit imports to those files per the process above.

---

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, write tests that demonstrate each defect category on the unfixed codebase (exploratory/fix-checking tests that fail before the fix), then verify the fix works and preserves existing behavior (preservation tests that should pass on both old and fixed code).

### Exploratory Bug Condition Checking

**Goal**: Surface concrete counterexamples for each bug category BEFORE implementing the fix. Confirm or refute the root-cause analysis.

**Category 1 — Circular Import Test Plan:**

Run against UNFIXED code:
```python
# test_circular_imports_exploration.py
import sys, importlib, pytest

CYCLE_PAIRS = [
    ("app.storage.buffer", "app.storage.mongo_store"),
    ("app.storage.buffer", "app.monitoring.self_learner"),
    ("app.ai.ai_brain", "app.ai.ai_router"),
    ("app.ai.ai_prediction_pipeline", "app.storage.buffer"),
    ("app.utils.prediction_flow", "app.ai.ai_brain"),
    ("app.monitoring.self_learner", "app.ai.ai_prediction_pipeline"),
]

@pytest.mark.parametrize("mod_a,mod_b", CYCLE_PAIRS)
def test_no_import_error(mod_a, mod_b):
    # Clear module cache to simulate cold start
    for key in list(sys.modules.keys()):
        if key.startswith("app."):
            del sys.modules[key]
    # Should import cleanly — will FAIL on unfixed code with ImportError
    importlib.import_module(mod_a)
    importlib.import_module(mod_b)
```

**Category 2 — Duplicate Function Test Plan:**

```python
# test_duplicate_functions_exploration.py
import ast, pathlib, pytest

DUPLICATE_REGISTRY = [
    ("_safe_float", ["app/utils/primitives.py", "app/storage/league_memory/_helpers.py"]),
    ("_team_name",  ["app/utils/match_helpers.py", "app/storage/mongo_store.py"]),
    # ... full registry from bugfix.md
]

@pytest.mark.parametrize("fn_name,expected_files", DUPLICATE_REGISTRY)
def test_function_defined_only_in_canonical(fn_name, expected_files):
    # Count definitions across whole codebase — should be 1 after fix (will be >1 before)
    count = 0
    for pyfile in pathlib.Path("app").rglob("*.py"):
        tree = ast.parse(pyfile.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == fn_name:
                count += 1
    assert count == 1, f"{fn_name} defined in {count} places; expected 1"
```

**Category 3 — Hardcoded Constants Test Plan:**

```python
# test_hardcoded_values_exploration.py
import os, pytest
from app.config.config import invalidate_settings_cache, get_settings

@pytest.mark.parametrize("env_var,expected_default", [
    ("ENSEMBLE_WEIGHT_DIXON_COLES", 0.25),
    ("ENSEMBLE_WEIGHT_ELO", 0.20),
    ("POISSON_LEAGUE_AVG_GOALS", 1.3),
    ("REGIME_ELITE_MIN_CONFIDENCE", 78),
    ("PICK_GENERATOR_AWAY_BASELINE_CONFIDENCE", 0.54),
    ("CALIBRATION_GAP_MODERATE_THRESHOLD", 10.0),
])
def test_default_matches_original_hardcoded_value(env_var, expected_default):
    # Ensure env var is not set — should get original default
    old_val = os.environ.pop(env_var, None)
    invalidate_settings_cache()
    try:
        s = get_settings()
        attr = env_var.lower()
        # Will FAIL before fix because Settings won't have these attributes
        actual = getattr(s, attr)
        assert actual == pytest.approx(expected_default)
    finally:
        if old_val is not None:
            os.environ[env_var] = old_val
        invalidate_settings_cache()
```

**Category 4 — Missing Import Test Plan:**

```python
# test_missing_imports_exploration.py
import subprocess, pytest, pathlib

AFFECTED_FILES = [
    "app/ai/llm_agent.py",
    # ... 15 files from audit
]

@pytest.mark.parametrize("filepath", AFFECTED_FILES)
def test_file_compiles_cleanly(filepath):
    result = subprocess.run(
        ["python", "-m", "py_compile", filepath],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"Compile error in {filepath}: {result.stderr}"

@pytest.mark.parametrize("filepath", AFFECTED_FILES)
def test_no_undefined_names(filepath):
    result = subprocess.run(
        ["python", "-m", "pyflakes", filepath],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"Undefined names in {filepath}: {result.stdout}"
```

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed code produces the expected behavior.

**Pseudocode:**
```
// Category 1
FOR ALL module_pair WHERE isBugCondition_CircularImport(module_pair) DO
  result := import_graph_fixed(module_pair)
  ASSERT NOT has_cycle(result)
END FOR

// Category 2
FOR ALL (fn_name, file_path) WHERE isBugCondition_Duplicate(fn_name, file_path) DO
  ASSERT count_definitions_fixed(fn_name) = 1
  ASSERT canonical_path(fn_name) IN shared_utility_modules
END FOR

// Category 3
FOR ALL (file, sym) WHERE isBugCondition_Hardcoded(file, sym) DO
  ASSERT value_source_fixed(file, sym) = CONFIG_SOURCE
  ASSERT config_default_fixed(sym) = original_hardcoded_value(file, sym)
END FOR

// Category 4
FOR ALL (file, name) WHERE isBugCondition_MissingImport(file, name) DO
  ASSERT name IN explicitly_imported_names_fixed(file)
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed code produces the same result as the original.

**Pseudocode:**
```
// Category 1 — non-cycle module pairs
FOR ALL module_pair WHERE NOT isBugCondition_CircularImport(module_pair) DO
  ASSERT import_graph_original(module_pair) = import_graph_fixed(module_pair)
END FOR

// Category 2 — canonical call sites already using the shared utility
FOR ALL (fn_name, inputs) WHERE NOT isBugCondition_Duplicate(fn_name, current_file) DO
  ASSERT F_original(fn_name, inputs) = F_fixed(canonical(fn_name), inputs)
END FOR

// Category 3 — config-absent (default) behaviour
FOR ALL (file, sym) WHERE isBugCondition_Hardcoded(file, sym) DO
  WITH env_var_absent:
    ASSERT F_original(sym) = F_fixed(sym)
END FOR

// Category 4 — files not in the missing-import set
FOR ALL file WHERE NOT EXISTS name: isBugCondition_MissingImport(file, name) DO
  ASSERT runtime_behaviour_original(file) = runtime_behaviour_fixed(file)
END FOR
```

**Testing Approach**: Property-based testing with `hypothesis` (already installed per `.hypothesis/` directory) is recommended for the preservation checking of duplicate functions and config defaults, because:
- It generates many input combinations automatically.
- It catches edge cases like empty strings, `None`, zero, negative numbers, and boundary values that manual tests miss.
- The project already uses Hypothesis (`.hypothesis/constants/` and `.hypothesis/tmp/` directories are present).

### Unit Tests

**Category 1:**
- Import each of the 7 involved modules individually in a clean process and assert no `ImportError`.
- Import pairs in both orders (A then B, B then A) and assert both succeed.
- Verify that lazy-imported functions in `buffer.py` and `mongo_store.py` are called correctly in integration.

**Category 2:**
- For each of the 19 true-duplicate functions: call the canonical implementation with the same inputs as the old local copy and assert identical output.
- For each near-duplicate family: call the parameterised canonical with both variant inputs and assert both return values match the respective original variant.

**Category 3:**
- For each new `Settings` field: assert the default equals the original hardcoded literal.
- For each new `Settings` field: set the environment variable to a different value, call `invalidate_settings_cache()`, and assert the new value is used.
- Assert that `ensemble._BASE_WEIGHTS` still equals `{"dixon_coles": 0.25, ...}` when no env vars are set (backward compat).
- Assert that `regime.TIER_1.min_confidence == 78` when no env vars are set.

**Category 4:**
- Run `python -m py_compile app/ai/llm_agent.py` and assert exit code 0.
- Run `python -m pyflakes app/routers/frontend.py` and all 15 affected files; assert exit code 0 for each.

### Property-Based Tests

**Category 2 — Duplicate function output equivalence:**
```python
from hypothesis import given, strategies as st
from app.utils.primitives import _safe_float
# (and the legacy local definition, imported before deletion for comparison)

@given(value=st.one_of(st.none(), st.text(), st.floats(), st.integers()))
def test_safe_float_canonical_matches_original(value):
    # Both implementations should return the same result
    assert canonical_safe_float(value) == original_safe_float(value)
```

**Category 3 — Config default preservation:**
```python
from hypothesis import given, strategies as st
import os
from app.config.config import invalidate_settings_cache, get_settings

@given(env_value=st.none())  # no env var set
def test_ensemble_weights_default_unchanged(env_value):
    invalidate_settings_cache()
    s = get_settings()
    assert s.ensemble_weight_dixon_coles == pytest.approx(0.25)
    assert s.ensemble_weight_elo == pytest.approx(0.20)
```

**Category 1 — Import graph stability:**
```python
from hypothesis import given, strategies as st

NON_CYCLE_PAIRS = [
    ("app.config.config", "app.utils.primitives"),
    ("app.models.elo", "app.models.poisson"),
    # ... pairs confirmed not in the 18-cycle set
]

@given(pair=st.sampled_from(NON_CYCLE_PAIRS))
def test_non_cycle_pairs_import_cleanly(pair):
    mod_a, mod_b = pair
    # Both should import without error — preserved from original
    import importlib
    importlib.import_module(mod_a)
    importlib.import_module(mod_b)
```

### Integration Tests

- Cold-start test: start the FastAPI application in a subprocess with `--reload=false` and assert process exits successfully (no `ImportError` on startup).
- End-to-end prediction test: call `POST /predict` with a sample match document and assert the response contains `picks`, `signals`, and `confidence` fields matching the pre-fix baseline.
- Scheduler smoke test: invoke `job_enrich_upcoming()` directly and assert it completes without exception.
- Config override test: set `ENSEMBLE_WEIGHT_DIXON_COLES=0.30` in the environment, call `invalidate_settings_cache()`, run the ensemble model, and assert `weights_used["dixon_coles"] == 0.30`.
