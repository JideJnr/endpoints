# Implementation Plan

- [x] 1. Write bug condition exploration tests (BEFORE implementing any fix)
  - **Property 1: Bug Condition** - Status Type Mismatch and Circular Import Scorer Failure
  - **CRITICAL**: These tests MUST FAIL on unfixed code — failure confirms the bugs exist
  - **DO NOT attempt to fix the test or the code when they fail**
  - **NOTE**: These tests encode the expected behavior — they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples that demonstrate both bugs exist
  - Create `tests/test_sofascore_candidate_matching.py` with the following test cases:
  - **T1** — String status `"notstarted"`: `event = {"id": 1, "status": "notstarted"}` → assert `is_usable_event_for_mode(event, live=False) is True`
    - **EXPECTED OUTCOME on unfixed code**: FAILS — `_status_text()` calls `"notstarted".get("type")` → `AttributeError` swallowed → `status_type = ""` → returns `False`
    - Document counterexample: `is_usable_event_for_mode({"id": 1, "status": "notstarted"}, live=False)` returns `False` instead of `True`
  - **T2** — String status `"inprogress"`: assert `is_usable_event_for_mode(event, live=True) is True` AND `is_usable_event_for_mode(event, live=False) is False`
    - **EXPECTED OUTCOME on unfixed code**: FAILS — same `.get("type")` failure makes both return `False`
    - Document counterexample: `is_usable_event_for_mode({"id": 2, "status": "inprogress"}, live=True)` returns `False` instead of `True`
  - **T3** — Terminal string statuses (`"finished"`, `"postponed"`, `"cancelled"`, `"abandoned"`): assert `is_usable_event_for_mode(event, live=False) is False` AND `is_usable_event_for_mode(event, live=True) is False`
    - **EXPECTED OUTCOME on unfixed code**: PASSES (all return `False`, albeit for the wrong reason — empty `status_type`)
    - If all four statuses pass, mark T3 as observed-passing; note it must continue to pass after the fix
  - **T4** — Regression guard with dict status `{"type": "notstarted"}`: assert `is_usable_event_for_mode(event, live=False) is True`
    - **EXPECTED OUTCOME on unfixed code**: PASSES (dict path is the current working path)
    - Mark as regression guard; must still pass after fix
  - **T5** — Mixed event list (string statuses): only the `"notstarted"` event (id=10) passes `live=False` filter; `"finished"` (id=11) and `"inprogress"` (id=12) do not
    - **EXPECTED OUTCOME on unfixed code**: FAILS — all three return `False`, so `usable` is empty (length 0, not 1)
    - Document counterexample: no events pass the filter; list is empty
  - **T6** — Circular import scorer: `_event_score_for_fallback(sporty_arsenal_chelsea, event_arsenal_chelsea)` returns `> 0.0`
    - `sporty = {"name": "Arsenal vs Chelsea", "home_team": "Arsenal", "away_team": "Chelsea", "tournament": "Premier League", "category": "England"}`
    - `event  = {"id": 99, "name": "Arsenal vs Chelsea", "home_team": {"name": "Arsenal"}, "away_team": {"name": "Chelsea"}, "tournament": {"name": "Premier League"}}`
    - **Scoped PBT Approach**: Test is deterministic — scope to this exact plausible-match pair
    - **EXPECTED OUTCOME on unfixed code**: FAILS — circular import `ImportError` swallowed → returns `0.0`
    - Document counterexample: `_event_score_for_fallback(sporty, event)` returns `0.0` instead of a positive score
  - Run all six tests on UNFIXED code; T1, T2, T5, T6 must FAIL; T3 and T4 may PASS
  - Record all counterexamples in comments or test output to understand root causes
  - Mark task complete when tests are written, run, and failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Dict Status Unchanged and No Circular Import Regression
  - **IMPORTANT**: Follow observation-first methodology — observe UNFIXED code behavior for non-buggy inputs first
  - **Observe on unfixed code**:
    - `is_usable_event_for_mode({"id": 4, "status": {"type": "notstarted"}}, live=False)` → `True` (dict path works)
    - `is_usable_event_for_mode({"id": 5, "status": {"type": "finished"}}, live=False)` → `False` (terminal excluded)
    - `is_usable_event_for_mode({"id": 6, "status": {"type": "inprogress"}}, live=True)` → `True` (live works)
  - **T7** — `_data_sources` importable from both locations (backward compatibility):
    - `from app.utils.doc_helpers import _data_sources as ds1`
    - `from app.storage.buffer import _data_sources as ds2`
    - assert `ds1 is ds2`
    - **NOTE**: This will FAIL on unfixed code if `_data_sources` does not yet exist in `doc_helpers`; write the test now and verify after fix
  - **T8** — No circular import when importing `enrichment` after `buffer`:
    - `import app.storage.buffer`; `importlib.invalidate_caches()`; `mod = importlib.import_module("app.enrichment.enrichment")`
    - assert `hasattr(mod, "_event_score")`
    - **EXPECTED OUTCOME on unfixed code**: may raise `ImportError` or return partial module; document observed behavior
  - **P2** (Hypothesis) — Terminal string statuses always excluded for any event_id and live bool:
    - `@given(event_id=st.integers(min_value=1), status=st.sampled_from(sorted(TERMINAL)), live=st.booleans())`
    - `@settings(max_examples=150)`
    - assert `is_usable_event_for_mode({"id": event_id, "status": status}, live=live) is False`
    - **EXPECTED OUTCOME on unfixed code**: PASSES (all string statuses return `False` — accidentally correct)
    - Record that this must continue to pass after fix
  - **P3** (Hypothesis) — Dict-format and string-format status produce identical `is_usable_event_for_mode` results:
    - `@given(event_id=st.integers(min_value=1), status_type=st.text(min_size=0, max_size=30), live=st.booleans())`
    - `@settings(max_examples=150)`
    - `event_dict = {"id": event_id, "status": {"type": status_type}}`; `event_str = {"id": event_id, "status": status_type}`
    - assert `is_usable_event_for_mode(event_dict, live=live) == is_usable_event_for_mode(event_str, live=live)`
    - **EXPECTED OUTCOME on unfixed code**: FAILS — string path always returns `False`; dict path may return `True`
    - Write the test now; it will PASS after fix; documents the preservation requirement
  - Run P2 on unfixed code — must PASS (baseline)
  - Run T7, T8, P3 on unfixed code — observe and document behavior
  - Mark task complete when tests are written, run, and baseline behavior is documented
  - _Requirements: 3.1, 3.2, 3.3_

- [x] 3. Fix Bug 2 — Circular import (move `_data_sources` to `doc_helpers`)

  - [x] 3.1 Add `_data_sources` to `app/utils/doc_helpers.py`
    - Copy the full `_data_sources` function body from `app/storage/buffer.py` into `app/utils/doc_helpers.py`
    - Function signature and body must be identical — this is a pure move, not a rewrite
    - Ensure all imports the function needs (if any) are present in `doc_helpers.py`
    - _Bug_Condition: isBugCondition_CircularImport — `enrichment.py` imports `_data_sources` from `buffer.py`; `buffer.py` deferred-imports back from `enrichment.py` → `ImportError` swallowed → `_event_score_for_fallback` returns `0.0`_
    - _Expected_Behavior: `_event_score_for_fallback(sporty, event)` returns a value `> 0.0` for plausible-match pairs once the cycle is broken_
    - _Preservation: All existing callers of `_data_sources` continue to receive the same function object via the re-export in `buffer.py`_
    - _Requirements: 1.3, 1.4, 2.3, 2.4, 3.4, 3.5, 3.6_

  - [x] 3.2 Update `app/enrichment/enrichment.py` import
    - Change `from app.storage.buffer import _data_sources` → `from app.utils.doc_helpers import _data_sources`
    - Verify no other imports in `enrichment.py` create a new cycle with `buffer.py`
    - _Requirements: 2.3, 2.4_

  - [x] 3.3 Replace `_data_sources` definition in `app/storage/buffer.py` with a re-export
    - Remove (or comment out) the `_data_sources` function definition from `buffer.py`
    - Add re-export: `from app.utils.doc_helpers import _data_sources  # noqa: F401`
    - Place the re-export in the same logical section as the original definition to minimise diff scope
    - _Requirements: 3.4, 3.5_

- [-] 4. Fix Bug 1 — Status field type mismatch in `app/data_clients/sofascore_client.py`

  - [ ] 4.1 Extend `_status_text()` to handle both `str` and `dict` status values
    - Replace the single-branch `status.get("type")` logic with an `isinstance(raw, str)` branch:
      ```python
      raw = event.get("status") or (event.get("eventState") or {}).get("status") or {}
      if isinstance(raw, str):
          status_type = raw.lower().replace(" ", "").replace("_", "")
          description = status_type
      else:
          status_type = str(raw.get("type") or "").lower().replace(" ", "").replace("_", "")
          description = str(raw.get("description") or "").lower()
      ```
    - The `else` branch must be byte-for-byte identical to the current implementation so the dict path is unchanged
    - _Bug_Condition: isBugCondition_StatusMismatch — `isinstance(event["status"], str)` is `True`; `_status_text` calls `.get("type")` on a string → `AttributeError` swallowed → `status_type = ""` → `is_usable_event_for_mode` returns `False`_
    - _Expected_Behavior: For all string-status events where status not in `TERMINAL_STATUS_TYPES` and not in `LIVE_STATUS_TYPES`, `is_usable_event_for_mode(event, live=False)` returns `True`_
    - _Preservation: For all dict-status events, `is_usable_event_for_mode` returns the same value as before the fix (else branch is unchanged)_
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 3.1, 3.2, 3.3_

  - [ ] 4.2 Verify `is_terminal_event()` also benefits from the fix
    - `is_terminal_event()` also calls `_status_text()` — confirm that string terminal statuses (e.g. `"finished"`) now correctly return `True` from `is_terminal_event()`
    - No code change needed; this is a verification step only
    - _Requirements: 3.1_

  - [ ] 4.3 Verify exploration test T1 now passes
    - **Property 1: Expected Behavior** - String Status `"notstarted"` Usable for Prematch
    - **IMPORTANT**: Re-run the SAME test T1 from task 1 — do NOT write a new test
    - The T1 test encodes the expected behavior; when it passes, it confirms the fix is correct
    - Run T1: `is_usable_event_for_mode({"id": 1, "status": "notstarted"}, live=False) is True`
    - **EXPECTED OUTCOME**: PASSES (confirms Bug 1 is fixed)
    - _Requirements: 2.1, 2.2_

- [ ] 5. Verify all bug condition exploration tests now pass
  - **Property 1: Expected Behavior** - All Exploration Tests Pass After Fix
  - **IMPORTANT**: Re-run ALL tests T1–T6 from task 1 — do NOT write new tests
  - T1: `is_usable_event_for_mode({"id": 1, "status": "notstarted"}, live=False) is True` — **EXPECTED: PASSES**
  - T2: `is_usable_event_for_mode({"id": 2, "status": "inprogress"}, live=True) is True` AND `live=False is False` — **EXPECTED: PASSES**
  - T3: all terminal string statuses return `False` for both live modes — **EXPECTED: PASSES** (was already passing; must continue)
  - T4: dict status `{"type": "notstarted"}` returns `True` for `live=False` — **EXPECTED: PASSES** (regression guard)
  - T5: mixed list — only `"notstarted"` event (id=10) passes `live=False` — **EXPECTED: PASSES**
  - T6: `_event_score_for_fallback(sporty_arsenal_chelsea, event_arsenal_chelsea) > 0.0` — **EXPECTED: PASSES**
  - If any test still fails, diagnose and fix before proceeding
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [ ] 6. Verify all preservation tests still pass after fix
  - **Property 2: Preservation** - No Regressions After Fix
  - **IMPORTANT**: Re-run ALL tests T7, T8, P2, P3 from task 2 — do NOT write new tests
  - T7: `_data_sources` importable from both `doc_helpers` and `buffer`; `ds1 is ds2` — **EXPECTED: PASSES**
  - T8: no `ImportError` when importing `enrichment` after `buffer`; `hasattr(mod, "_event_score")` — **EXPECTED: PASSES**
  - P2 (Hypothesis, 150 examples): terminal string statuses always excluded — **EXPECTED: PASSES** (was passing; must continue)
  - P3 (Hypothesis, 150 examples): dict-format and string-format status produce identical results — **EXPECTED: PASSES**
  - Confirm all four tests pass; no regressions introduced
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [ ] 7. Write and run P1 hypothesis test — non-terminal non-live string statuses are always usable for prematch
  - **Property 1: Bug Condition** - All Non-Terminal Non-Live String Statuses Pass Prematch Filter
  - Write P1 in `tests/test_sofascore_candidate_matching.py`:
    ```python
    TERMINAL = {"finished", "postponed", "cancelled", "abandoned", "suspended",
                "interrupted", "walkover", "awarded"}
    LIVE = {"inprogress", "live"}

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
  - Run with 150 examples — **EXPECTED OUTCOME**: PASSES (confirms Fix property 1 from design holds universally)
  - If Hypothesis finds a counterexample, investigate and fix `_status_text()` normalisation edge case
  - _Requirements: 2.1, 2.2_

- [ ] 8. Final checkpoint — full test suite passes
  - Run the complete `tests/test_sofascore_candidate_matching.py` file
  - All tests must pass: T1, T2, T3, T4, T5, T6, T7, T8, P1, P2, P3
  - Confirm the three correctness properties from design hold:
    1. Fix property (status): every string-status non-terminal non-live event returns `True` from `is_usable_event_for_mode(live=False)`
    2. Fix property (scorer): `_event_score_for_fallback` returns `> 0.0` for plausible-match pairs
    3. Preservation property: dict-status events produce identical results before and after fix
  - If any test fails, resolve before marking complete
  - Ensure all tests pass; ask the user if questions arise
