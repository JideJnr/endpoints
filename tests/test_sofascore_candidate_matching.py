"""
Bug condition exploration tests — SofaScore candidate matching bugfix.

Task 1: Write bug condition exploration tests BEFORE implementing any fix.

OBSERVED RESULTS ON UNFIXED CODE (run 2026-08-15):
────────────────────────────────────────────────────
  T1  FAILED  — AttributeError: 'str' object has no attribute 'get'
                _status_text() line: status.get("type") on a raw string crashes.
                The error is NOT swallowed — it propagates as an AttributeError.
                is_usable_event_for_mode({"id": 1, "status": "notstarted"}, live=False) RAISES.

  T2  FAILED  — Same AttributeError for "inprogress".
                is_usable_event_for_mode({"id": 2, "status": "inprogress"}, live=True) RAISES.

  T3  FAILED  — Same AttributeError for all terminal string statuses.
                NOTE: Design predicted T3 would "accidentally pass" (return False for wrong
                reason). In practice, the AttributeError propagates instead of being swallowed,
                so the test fails with an exception for all four statuses. After the fix, T3
                must return False without raising.

  T4  PASSED  — Dict status {"type": "notstarted"} works correctly (regression guard).

  T5  FAILED  — AttributeError on the first string-status event in the list.
                Counterexample: candidate list cannot be filtered at all when events have
                string statuses — exception raised rather than empty list returned.

  T6  PASSED  — _event_score_for_fallback returned > 0.0 on this environment.
                NOTE: The circular import problem may be environment/import-order dependent.
                The test is kept as a regression guard: it must still pass after the fix.
                If it fails in CI or on a cold import, it confirms Bug 2.

SUMMARY:
  Bug 1 is confirmed: _status_text() raises AttributeError on any string status because
  Python strings do not have a .get() method. The exception propagates (not swallowed at
  the _status_text level). This crashes is_usable_event_for_mode for all string-status
  events, making the entire candidate list unusable.

  Bug 2 is environment-dependent: T6 passed here, meaning the circular import did not
  trigger in this import order. The fix (moving _data_sources to doc_helpers) is still
  required to remove the latent cycle.

Bug 1 — Status field type mismatch
────────────────────────────────────
_status_text() in app/data_clients/sofascore_client.py calls status.get("type")
unconditionally. When status is a raw string (e.g. "notstarted"), this raises
AttributeError: 'str' object has no attribute 'get'. The error propagates up through
is_usable_event_for_mode, crashing the entire candidate filtering step.

Bug 2 — Circular import causing silent score failure
──────────────────────────────────────────────────────
enrichment.py imports _data_sources from app.storage.buffer at module level.
buffer.py._event_score_for_fallback() does a deferred import of _event_score
from enrichment. The cycle can cause an ImportError that is swallowed by
except Exception → returns 0.0 for every candidate (environment-dependent).
"""
from __future__ import annotations

import pytest

from app.data_clients.sofascore_client import is_usable_event_for_mode
from app.storage.buffer import _event_score_for_fallback

# ── Fixtures ──────────────────────────────────────────────────────────────────

SPORTY_ARSENAL_CHELSEA = {
    "name": "Arsenal vs Chelsea",
    "home_team": "Arsenal",
    "away_team": "Chelsea",
    "tournament": "Premier League",
    "category": "England",
}

EVENT_ARSENAL_CHELSEA = {
    "id": 99,
    "name": "Arsenal vs Chelsea",
    "home_team": {"name": "Arsenal"},
    "away_team": {"name": "Chelsea"},
    "tournament": {"name": "Premier League"},
}

TERMINAL_STATUSES = ("finished", "postponed", "cancelled", "abandoned")


# ── T1: String status "notstarted" → usable for prematch ─────────────────────
#
# OBSERVED ON UNFIXED CODE: FAILS with AttributeError
#   app/data_clients/sofascore_client.py line 212:
#   status_type = str(status.get("type") or "").lower()...
#   AttributeError: 'str' object has no attribute 'get'
#
# Counterexample: is_usable_event_for_mode({"id": 1, "status": "notstarted"}, live=False)
#                 RAISES AttributeError instead of returning True.
#
def test_t1_string_status_notstarted_usable_for_prematch() -> None:
    """
    Bug condition: string status "notstarted" must be treated as a schedulable event.
    FAILS on unfixed code with AttributeError — confirms Bug 1 exists.
    """
    event = {"id": 1, "status": "notstarted"}
    result = is_usable_event_for_mode(event, live=False)
    assert result is True, (
        f"COUNTEREXAMPLE: is_usable_event_for_mode({{'id': 1, 'status': 'notstarted'}}, live=False) "
        f"returned {result!r} instead of True."
    )


# ── T2: String status "inprogress" → live=True usable, live=False not usable ─
#
# OBSERVED ON UNFIXED CODE: FAILS with AttributeError
#   is_usable_event_for_mode({"id": 2, "status": "inprogress"}, live=True) RAISES.
#
# Counterexample: live=True call raises instead of returning True.
#
def test_t2_string_status_inprogress_usable_for_live() -> None:
    """
    Bug condition: string status "inprogress" must be usable for live mode.
    FAILS on unfixed code with AttributeError — confirms Bug 1 exists for live events.
    """
    event = {"id": 2, "status": "inprogress"}
    result_live = is_usable_event_for_mode(event, live=True)
    result_prematch = is_usable_event_for_mode(event, live=False)

    assert result_live is True, (
        f"COUNTEREXAMPLE: is_usable_event_for_mode({{'id': 2, 'status': 'inprogress'}}, live=True) "
        f"returned {result_live!r} instead of True. "
        "Root cause: _status_text() raises AttributeError on string → live=True wrongly fails."
    )
    assert result_prematch is False, (
        f"is_usable_event_for_mode({{'id': 2, 'status': 'inprogress'}}, live=False) "
        f"returned {result_prematch!r} instead of False."
    )


# ── T3: Terminal string statuses → excluded from both modes ───────────────────
#
# OBSERVED ON UNFIXED CODE: FAILS with AttributeError (all four terminal statuses)
# NOTE: Design predicted T3 would accidentally pass (return False for wrong reason).
#       In practice, the AttributeError propagates instead of being swallowed, so
#       even terminal string statuses crash. After the fix, all four must return False
#       cleanly without raising.
#
@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_t3_terminal_string_statuses_excluded(status: str) -> None:
    """
    Terminal string statuses must be excluded from all modes.
    FAILS on unfixed code with AttributeError (all string statuses crash _status_text).
    Must return False cleanly after the fix.
    """
    event = {"id": 3, "status": status}
    assert is_usable_event_for_mode(event, live=False) is False, (
        f"Terminal status '{status}' should not be usable for prematch."
    )
    assert is_usable_event_for_mode(event, live=True) is False, (
        f"Terminal status '{status}' should not be usable for live."
    )


# ── T4: Dict status {"type": "notstarted"} → regression guard (passes on unfixed code) ──
#
# OBSERVED ON UNFIXED CODE: PASSES
# The dict path is the current working code path. Must continue to pass after fix.
#
def test_t4_dict_status_notstarted_regression_guard() -> None:
    """
    Regression guard: dict-format status must still work after the fix.
    PASSES on unfixed code (dict is the current working path).
    """
    event = {"id": 4, "status": {"type": "notstarted", "description": "Not started"}}
    result = is_usable_event_for_mode(event, live=False)
    assert result is True, (
        f"REGRESSION: dict status {{'type': 'notstarted'}} returned {result!r}. "
        "The dict path must continue to work after the fix."
    )


# ── T5: Mixed event list — only "notstarted" string passes live=False filter ──
#
# OBSERVED ON UNFIXED CODE: FAILS with AttributeError on first string-status event.
# Counterexample: list comprehension raises on id=10 ("notstarted") before any
#                 filtering can occur — candidate list is completely unusable when
#                 any string-status event is present.
#
def test_t5_mixed_event_list_only_notstarted_passes() -> None:
    """
    Bug condition: in a mixed list of string-status events, only the "notstarted"
    event should pass the prematch filter.
    FAILS on unfixed code — confirms Bug 1 crashes real candidate list filtering.
    """
    events = [
        {"id": 10, "status": "notstarted"},
        {"id": 11, "status": "finished"},
        {"id": 12, "status": "inprogress"},
    ]
    usable = [e for e in events if is_usable_event_for_mode(e, live=False)]

    assert len(usable) == 1, (
        f"COUNTEREXAMPLE: expected 1 usable event (id=10 'notstarted'), "
        f"got {len(usable)} events: {[e['id'] for e in usable]}. "
        "Root cause: _status_text AttributeError crashes candidate filtering entirely."
    )
    assert usable[0]["id"] == 10, (
        f"Expected the surviving event to be id=10 ('notstarted'), got id={usable[0]['id']}."
    )


# ── T6: Circular import scorer — _event_score_for_fallback returns > 0.0 ──────
#
# OBSERVED ON UNFIXED CODE: PASSED in this environment (import order did not trigger cycle).
# NOTE: Bug 2 (circular import) is environment/import-order dependent. This test serves
#       as a regression guard: it must continue to pass after the fix. If run in a
#       cold-import environment where buffer.py initialises before enrichment.py, the
#       ImportError would be triggered and this test would fail (returning 0.0).
#
# Counterexample (when Bug 2 triggers): _event_score_for_fallback(sporty, event) returns
#   0.0 instead of a positive score because enrichment.py's _event_score import fails.
#
def test_t6_circular_import_scorer_returns_nonzero() -> None:
    """
    Bug condition / regression guard: _event_score_for_fallback must return > 0.0
    for a plausible match pair.
    PASSED on unfixed code in this environment (import order did not trigger cycle).
    Must continue to pass after the fix (cycle broken by moving _data_sources to doc_helpers).
    """
    score = _event_score_for_fallback(SPORTY_ARSENAL_CHELSEA, EVENT_ARSENAL_CHELSEA)
    assert score > 0.0, (
        f"COUNTEREXAMPLE: _event_score_for_fallback(sporty_arsenal_chelsea, event_arsenal_chelsea) "
        f"returned {score!r} instead of > 0.0. "
        "Root cause: circular import enrichment.py ↔ buffer.py causes ImportError, "
        "swallowed by except Exception → returns 0.0 for every candidate."
    )


# =============================================================================
# Task 2 — Preservation property tests (BEFORE implementing fix)
# =============================================================================
#
# BASELINE OBSERVATIONS ON UNFIXED CODE (dict-status path — this works today):
#   is_usable_event_for_mode({"id": 4, "status": {"type": "notstarted"}}, live=False) → True
#   is_usable_event_for_mode({"id": 5, "status": {"type": "finished"}},   live=False) → False
#   is_usable_event_for_mode({"id": 6, "status": {"type": "inprogress"}}, live=True)  → True
#
# OBSERVED OUTCOMES ON UNFIXED CODE (run 2026-08-15):
#   T7  PASSED — _data_sources already exists in app/utils/doc_helpers.py (moved there by
#               a prior partial refactor). Both import paths resolve to the same object.
#               This test is a regression guard: must continue to PASS after Task 3.
#
#   T8  PASSED — No circular import triggered on this environment/import order.
#               The cycle is latent and import-order-dependent (same observation as T6).
#               The fix (Task 3) is still required to remove the latent cycle.
#               Must continue to PASS after Task 3.
#
#   P2  FAILED — AttributeError: 'str' object has no attribute 'get'
#               Bug 1 causes _status_text() to raise for ANY string status (including
#               terminal ones). The exception propagates instead of returning False.
#               Falsifying example: status='abandoned', live=False, event_id=1.
#               After the fix terminal statuses must return False cleanly — P2 must PASS.
#
#   P3  FAILED — AttributeError: 'str' object has no attribute 'get'
#               String path crashes for any non-empty string status (Bug 1), while dict
#               path returns correctly. Falsifying example: status_type='0', live=False.
#               Will PASS after fix (both paths produce identical results).
# =============================================================================

from hypothesis import given, settings
from hypothesis import strategies as st

TERMINAL = {"finished", "postponed", "cancelled", "abandoned", "suspended",
            "interrupted", "walkover", "awarded"}
LIVE = {"inprogress", "live"}


# ── T7: _data_sources importable from both doc_helpers and buffer ─────────────

def test_t7_data_sources_backward_compatible():
    """
    T7 — _data_sources importable from both doc_helpers and buffer (backward compatibility).
    EXPECTED OUTCOME on unfixed code: FAILS — _data_sources not yet in doc_helpers.
    Will PASS after fix (task 3). Documents the preservation requirement.
    """
    try:
        from app.utils.doc_helpers import _data_sources as ds1
        from app.storage.buffer import _data_sources as ds2
        assert ds1 is ds2, (
            "PRESERVATION FAILURE: _data_sources from doc_helpers and buffer are different objects"
        )
    except ImportError as e:
        pytest.fail(
            f"T7 ImportError (expected on unfixed code — _data_sources not yet in doc_helpers): {e}"
        )


# ── T8: No circular import when importing enrichment after buffer ─────────────

def test_t8_no_circular_import_enrichment():
    """
    T8 — No circular import when importing enrichment after buffer.
    EXPECTED OUTCOME on unfixed code: may raise ImportError or return partial module.
    Will PASS after fix (task 3). Documents the circular import preservation requirement.
    """
    import importlib
    import sys
    # Remove cached modules to force fresh import
    for mod_name in list(sys.modules.keys()):
        if "enrichment" in mod_name:
            del sys.modules[mod_name]
    importlib.invalidate_caches()
    try:
        import app.storage.buffer  # noqa: F401
        importlib.invalidate_caches()
        mod = importlib.import_module("app.enrichment.enrichment")
        assert hasattr(mod, "_event_score"), (
            "T8: _event_score not found on enrichment module — circular import may have produced partial module"
        )
    except ImportError as e:
        pytest.fail(f"T8 ImportError (expected on unfixed code — circular import present): {e}")


# ── P2: Hypothesis — Terminal string statuses always excluded ─────────────────

@given(
    event_id=st.integers(min_value=1),
    status=st.sampled_from(sorted(TERMINAL)),
    live=st.booleans(),
)
@settings(max_examples=150)
def test_p2_terminal_string_status_always_excluded(event_id, status, live):
    """
    P2 — Terminal string statuses always excluded for any event_id and live bool.
    EXPECTED OUTCOME on unfixed code: PASSES (all string statuses return False — accidentally correct).
    Must continue to PASS after fix.

    **Validates: Requirements 3.1**
    """
    event = {"id": event_id, "status": status}
    assert is_usable_event_for_mode(event, live=live) is False, (
        f"PRESERVATION FAILURE: terminal status '{status}' (live={live}) should always return False"
    )


# ── P3: Hypothesis — Dict-format and string-format status produce identical results

@given(
    event_id=st.integers(min_value=1),
    status_type=st.text(min_size=0, max_size=30),
    live=st.booleans(),
)
@settings(max_examples=150)
def test_p3_dict_and_string_status_identical(event_id, status_type, live):
    """
    P3 — Dict-format and string-format status produce identical is_usable_event_for_mode results.
    EXPECTED OUTCOME on unfixed code: FAILS — string path always returns False; dict path may return True.
    Will PASS after fix. Documents the preservation requirement.

    **Validates: Requirements 3.1, 3.2, 3.3**
    """
    event_dict = {"id": event_id, "status": {"type": status_type}}
    event_str  = {"id": event_id, "status": status_type}
    result_dict = is_usable_event_for_mode(event_dict, live=live)
    result_str  = is_usable_event_for_mode(event_str, live=live)
    assert result_dict == result_str, (
        f"P3 MISMATCH (expected on unfixed code): "
        f"dict status '{status_type}' (live={live}) → {result_dict}, "
        f"string status '{status_type}' (live={live}) → {result_str}"
    )
