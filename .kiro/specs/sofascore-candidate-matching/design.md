# Design Document: SofaScore Candidate Matching Bugfix

## Overview

Two defects introduced during a helper-extraction refactor silently eliminate the entire SofaScore candidate list before fuzzy matching begins. This document specifies the exact surgical changes required to both files, the test strategy, and the expected state after the fix.

---

## System Architecture

### Candidate Matching Pipeline

The candidate matching pipeline feeds every enrichment path in the application:

```
SportyBet matches (buffer)
        │
        ▼
 _candidate_sofascore_events()         ← fills candidates from sofa_event_list_cache
        │
        ▼
 is_usable_event_for_mode(event, live)  ← BUG 1 here: all candidates return False
        │
        ▼
 _with_search_fallback_candidates()    ← BUG 2 here: scorer always returns 0.0
        │   scores candidates with _event_score_for_fallback()
        ▼
 _fuzzy_match(sporty, candidates)
        │
        ▼
 sofascore_match_status = "matched" | "no_match"
```

Both bugs operate in sequence: Bug 1 empties the candidate list; Bug 2 ensures the search-fallback path also scores everything at 0.0, so even freshly-searched events never cross the 0.70 threshold.

---

## Bug 1 — Status Field Type Mismatch

### Root Cause

`_status_text()` in `app/data_clients/sofascore_client.py` always calls `.get("type")` on the `status` field:

```python
def _status_text(event: dict) -> tuple[str, str]:
    status = event.get("status") or (event.get("eventState") or {}).get("status") or {}
    status_type = str(status.get("type") or "").lower().replace(" ", "").replace("_", "")
    description = str(status.get("description") or "").lower()
    return status_type, description
```

The `sofa_event_list_cache` table stores events serialised by `_db_cache_set()`. Those events were parsed by `_parse_event()`, which **normalises** the status field into a dict. However, two code paths write events to the cache **before** passing through `_parse_event()`, so their `status` fields remain raw strings (e.g. `"notstarted"`):

1. `ingest_competition_match()` in `buffer.py` — writes raw SofaScore API events directly
2. `_sofa_event_to_buffer_doc()` in `sofa_pipeline.py` — constructs a doc from a raw event without re-parsing the status

When these events are read back from the cache and passed to `is_usable_event_for_mode()`, `_status_text()` calls `"notstarted".get("type")` → `AttributeError` silently swallowed → `status_type = ""` → not in `LIVE_STATUS_TYPES` → `is_usable_event_for_mode` returns `False` for every event.

### Fix Design

Extend `_status_text()` to handle both `str` and `dict` as the status value. When `status` is already a string, use it directly as `status_type` without `.get()`:

```python
def _status_text(event: dict) -> tuple[str, str]:
    raw = event.get("status") or (event.get("eventState") or {}).get("status") or {}
    if isinstance(raw, str):
        # Status is already a normalised string (e.g. from DB cache written pre-refactor)
        status_type = raw.lower().replace(" ", "").replace("_", "")
        description = status_type
    else:
        status_type = str(raw.get("type") or "").lower().replace(" ", "").replace("_", "")
        description = str(raw.get("description") or "").lower()
    return status_type, description
```

**Why this location:** `_status_text()` is the single point of truth for status normalisation. All callers — `is_terminal_event()`, `is_usable_event_for_mode()` — delegate to it. Fixing here fixes all callers in one place without touching call sites.

**Regression safety:** The fix is additive. When `status` is a dict (the normal path), the else branch executes identically to the current code. No existing tests or behaviours change.

**Status classification after the fix:**

| Status value (string) | `status_type` | `is_terminal` | `is_usable(live=False)` | `is_usable(live=True)` |
|---|---|---|---|---|
| `"notstarted"` | `"notstarted"` | False | **True** | False |
| `"inprogress"` | `"inprogress"` | False | False | **True** |
| `"finished"` | `"finished"` | **True** | False | False |
| `"postponed"` | `"postponed"` | **True** | False | False |
| `{"type": "notstarted"}` | `"notstarted"` | False | **True** | False |

---

## Bug 2 — Circular Import Causing Silent Score Failure

### Root Cause

`_event_score_for_fallback()` in `app/storage/buffer.py` uses a deferred import:

```python
def _event_score_for_fallback(sporty, event):
    try:
        from app.enrichment.enrichment import _event_score
        return _event_score(sporty, event)
    except Exception:
        return 0.0
```

The import chain creates a cycle:

```
app.enrichment.enrichment
    └── imports _data_sources from app.storage.buffer   (top-level import)
            └── while buffer.py is still being initialised...
                    └── _event_score_for_fallback deferred-imports enrichment
                            └── ImportError (partially-initialised module)
                                    └── swallowed by except Exception → returns 0.0
```

The cycle exists because `enrichment.py` has this top-level import:

```python
from app.storage.buffer import _data_sources
```

And `buffer.py` (via `_event_score_for_fallback`) tries to import back from `enrichment.py`. Depending on Python's import ordering, the deferred import may encounter the partially-initialised `enrichment` module, get an `ImportError` for `_event_score`, and silently return `0.0`.

### Fix Design

**Break the cycle by moving `_data_sources` out of the circular path.** The function `_data_sources` is a pure helper — it constructs a data-source summary dict from match documents. It has no dependency on anything in `buffer.py`. The refactor already extracted doc helpers into `app/utils/doc_helpers.py`; `_data_sources` should live there too.

#### Step 1 — Move `_data_sources` to `app/utils/doc_helpers.py`

Add `_data_sources` to `app/utils/doc_helpers.py`. The function signature and body are unchanged; only the module it lives in changes.

#### Step 2 — Update `enrichment.py` import

```python
# Before (creates the cycle):
from app.storage.buffer import _data_sources

# After (breaks the cycle):
from app.utils.doc_helpers import _data_sources
```

#### Step 3 — Update `buffer.py` to re-export for backward compatibility

Any other callers that import `_data_sources` from `app.storage.buffer` must continue to work. Add a re-export at the end of the relevant section in `buffer.py`:

```python
# Backward-compatible re-export — do not remove
from app.utils.doc_helpers import _data_sources  # noqa: F401
```

This means `buffer.py` no longer defines `_data_sources` itself; it just re-exports from `doc_helpers`. The cycle is broken because `enrichment.py` now imports from `doc_helpers`, which has no dependency on `buffer.py`.

**Why this approach over alternatives:**

| Alternative | Problem |
|---|---|
| Remove the `except Exception` catch in `_event_score_for_fallback` | Would surface real import errors but not fix the root cause; crashes on every call |
| Move `_event_score` to a shared utils module | Large refactor; high regression risk; `_event_score` references many enrichment-local helpers |
| Lazy-import `_data_sources` inside `enrichment.py` functions | Deferred imports fix runtime order but don't fix module-level side effects during test collection |
| Move `_data_sources` to `doc_helpers` (chosen) | Minimal change; `_data_sources` is already a pure utility; consistent with the existing refactor pattern |

---

## Files Changed

| File | Change |
|---|---|
| `app/data_clients/sofascore_client.py` | Extend `_status_text()` to handle `str` status values |
| `app/utils/doc_helpers.py` | Add `_data_sources()` function (moved from buffer) |
| `app/enrichment/enrichment.py` | Change `_data_sources` import source from `buffer` to `doc_helpers` |
| `app/storage/buffer.py` | Replace `_data_sources` definition with a re-export from `doc_helpers` |

No new tables, no schema changes, no API changes.

---

## `_data_sources` Function Specification

The function is moved as-is. Its behaviour does not change:

```python
def _data_sources(
    sofa: dict | None,
    detail: dict | None,
    sporty: dict | None,
    sportradar: dict | None = None,
) -> dict:
    """
    Build the data_sources summary dict from available match providers.
    Pure function — no I/O, no imports from buffer or enrichment.
    """
```

The existing call sites in `enrichment.py`, `sofa_pipeline.py`, and any other module that imports `_data_sources` from `buffer` continue to work via the re-export.

---

## Test Strategy

### Unit Tests — Bug 1 (`_status_text` string handling)

File: `tests/test_sofascore_candidate_matching.py`

**T1 — String status `"notstarted"` is usable for prematch:**
```python
event = {"id": 1, "status": "notstarted", "home_team": {}, "away_team": {}}
assert is_usable_event_for_mode(event, live=False) is True
```

**T2 — String status `"inprogress"` is usable for live, not prematch:**
```python
event = {"id": 2, "status": "inprogress", "home_team": {}, "away_team": {}}
assert is_usable_event_for_mode(event, live=True) is True
assert is_usable_event_for_mode(event, live=False) is False
```

**T3 — String terminal statuses are excluded from both modes:**
```python
for status in ("finished", "postponed", "cancelled", "abandoned"):
    event = {"id": 3, "status": status}
    assert is_usable_event_for_mode(event, live=False) is False
    assert is_usable_event_for_mode(event, live=True) is False
```

**T4 — Dict status still works unchanged (regression guard):**
```python
event = {"id": 4, "status": {"type": "notstarted", "description": "Not started"}}
assert is_usable_event_for_mode(event, live=False) is True
```

**T5 — Mixed event list: string status events pass the filter, terminal events do not:**
```python
events = [
    {"id": 10, "status": "notstarted"},
    {"id": 11, "status": "finished"},
    {"id": 12, "status": "inprogress"},
]
usable = [e for e in events if is_usable_event_for_mode(e, live=False)]
assert len(usable) == 1
assert usable[0]["id"] == 10
```

### Unit Tests — Bug 2 (circular import / scorer)

**T6 — `_event_score_for_fallback` returns a non-zero score for a matching event:**
```python
sporty = {"name": "Arsenal vs Chelsea", "home_team": "Arsenal", "away_team": "Chelsea",
          "tournament": "Premier League", "category": "England"}
event  = {"id": 99, "name": "Arsenal vs Chelsea",
          "home_team": {"name": "Arsenal"}, "away_team": {"name": "Chelsea"},
          "tournament": {"name": "Premier League"}}
score = _event_score_for_fallback(sporty, event)
assert score > 0.0
```

**T7 — `_data_sources` is importable from both `doc_helpers` and `buffer` (backward compatibility):**
```python
from app.utils.doc_helpers import _data_sources as ds1
from app.storage.buffer import _data_sources as ds2
assert ds1 is ds2
```

**T8 — No circular import when importing `enrichment` after `buffer`:**
```python
import importlib
import app.storage.buffer
importlib.invalidate_caches()
mod = importlib.import_module("app.enrichment.enrichment")
assert hasattr(mod, "_event_score")
```

### Property-Based Test — Bug Condition Coverage

File: `tests/test_sofascore_candidate_matching.py`

**P1 — For all non-terminal, non-live string-status events, `is_usable_event_for_mode(live=False)` returns `True`:**

```python
from hypothesis import given, settings
from hypothesis import strategies as st

TERMINAL = {"finished", "postponed", "cancelled", "abandoned", "suspended",
            "interrupted", "walkover", "awarded"}
LIVE     = {"inprogress", "live"}

@given(
    event_id=st.integers(min_value=1),
    status=st.text(min_size=1, max_size=30).filter(
        lambda s: s.lower() not in TERMINAL and s.lower() not in LIVE
    ),
)
@settings(max_examples=150)
def test_string_status_prematch_usable(event_id, status):
    event = {"id": event_id, "status": status}
    assert is_usable_event_for_mode(event, live=False) is True
```

**P2 — Terminal string statuses are always excluded:**

```python
@given(
    event_id=st.integers(min_value=1),
    status=st.sampled_from(sorted(TERMINAL)),
    live=st.booleans(),
)
@settings(max_examples=150)
def test_terminal_string_status_always_excluded(event_id, status, live):
    event = {"id": event_id, "status": status}
    assert is_usable_event_for_mode(event, live=live) is False
```

**P3 — Dict-format status results are preserved (no regression):**

```python
@given(
    event_id=st.integers(min_value=1),
    status_type=st.text(min_size=0, max_size=30),
    live=st.booleans(),
)
@settings(max_examples=150)
def test_dict_status_unchanged(event_id, status_type, live):
    event_dict = {"id": event_id, "status": {"type": status_type}}
    event_str  = {"id": event_id, "status": status_type}
    # Both representations must agree
    assert (
        is_usable_event_for_mode(event_dict, live=live)
        == is_usable_event_for_mode(event_str, live=live)
    )
```

---

## Correctness Properties

The fix is correct if and only if all three properties hold simultaneously:

1. **Fix property (status):** For every event where `isinstance(event["status"], str)` and the status string is not in `TERMINAL_STATUS_TYPES` and not in `LIVE_STATUS_TYPES`, `is_usable_event_for_mode(event, live=False)` returns `True`.

2. **Fix property (scorer):** For every `(sporty, event)` pair where the event name fuzzy-matches the sporty match name with a genuine similarity, `_event_score_for_fallback(sporty, event)` returns a value `> 0.0`.

3. **Preservation property:** For every event where `event["status"]` is already a dict, `is_usable_event_for_mode` returns the same value before and after the fix.

---

## Sequence Diagram — Post-Fix Flow

```
Scheduler
   │
   ├─► get_unenriched_batch()
   │       │
   │       ▼
   │   fetch_all_scheduled_events(date)
   │       │  (returns events from DB cache — status may be str or dict)
   │       ▼
   │   is_usable_event_for_mode(event, live=False)
   │       │  _status_text() handles both str and dict ✓
   │       │  returns True for "notstarted" events ✓
   │       ▼
   │   candidate_list  (non-empty) ✓
   │       │
   ├─► _with_search_fallback_candidates(sporty, candidates)
   │       │
   │       ▼
   │   _event_score_for_fallback(sporty, event)
   │       │  from app.utils.doc_helpers import _data_sources  ← no cycle ✓
   │       │  _event_score() executes and returns real score ✓
   │       ▼
   │   best_score >= 0.70 → skip search fallback ✓
   │       │
   └─► _fuzzy_match(sporty, candidates)
           │
           ▼
       sofascore_match_status = "matched" ✓
```

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Another caller imports `_data_sources` directly from `buffer` and breaks | Low | Medium | Re-export in `buffer.py` maintains backward compatibility |
| `_status_text` string normalisation differs from dict normalisation for edge-case status strings | Low | Low | Property test P3 asserts both representations produce identical output |
| `_parse_event` already normalises status; some events arrive double-normalised | None | None | String branch handles already-normalised strings correctly |
| Other code paths in `buffer.py` also import from `enrichment.py` at module level | Low | High | Audit of `buffer.py` imports confirms `_data_sources` is the only circular import |
