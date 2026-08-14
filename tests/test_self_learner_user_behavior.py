"""
Tests for _incorporate_user_behavior in app/monitoring/self_learner.py
-----------------------------------------------------------------------
R2.8: If no rows with user_pick_signal exist in the graded set, the function
must return 0 without raising an exception.

Additional coverage:
  - Returns 0 and is silent when rows is empty.
  - Correctly upserts to user_behavior_outcomes and returns a positive count
    when user_pick_signal signals are present.
  - Does NOT double-count when the same (match_id, pick_type) is inserted twice
    (ON CONFLICT DO NOTHING).
  - Updates learned_model_weights for user_behavior_calibration once >= 15
    agreed rows exist.
  - Updates learned_model_weights for user_behavior_disagree_calibration once
    >= 15 disagreed rows exist.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.monitoring.self_learner import _incorporate_user_behavior, _init_learner_tables


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    """In-memory SQLite connection with all learner tables created."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_learner_tables(conn)
    return conn


def _make_row(
    match_id: str = "match_001",
    league_name: str = "Premier League",
    pick_type: str = "match_result",
    selection: str = "home",
    result: str = "win",
    signals_json: str = "[]",
    confidence: float = 65.0,
    audit_json: str = "{}",
) -> sqlite3.Row:
    """Build a minimal sqlite3.Row-like object for a graded prediction row."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        create table rows (
            match_id text, league_name text, pick_type text,
            selection text, result text, signals_json text,
            confidence real, audit_json text
        )
    """)
    conn.execute(
        "insert into rows values (?,?,?,?,?,?,?,?)",
        (match_id, league_name, pick_type, selection, result,
         signals_json, confidence, audit_json),
    )
    return conn.execute("select * from rows").fetchone()


def _signals_json_with_user_pick(impact: float) -> str:
    """Build a signals_json string containing a single user_pick_signal."""
    return json.dumps([{"name": "user_pick_signal", "impact": impact, "value": impact}])


# ── R2.8: no user_pick_signal rows ────────────────────────────────────────────

def test_returns_zero_when_no_user_pick_signal():
    """
    R2.8 — If no rows with user_pick_signal exist in the graded set,
    _incorporate_user_behavior must return 0 without raising any exception.
    """
    conn = _make_conn()
    # signals_json with other signals but NOT user_pick_signal
    signals = json.dumps([
        {"name": "goal_pressure", "impact": 3},
        {"name": "elo_model", "impact": 2},
    ])
    rows = [_make_row(signals_json=signals, result="win")]

    result = _incorporate_user_behavior(conn, rows)

    assert result == 0, (
        f"Expected 0 when no user_pick_signal exists, got {result}"
    )


def test_does_not_raise_when_no_user_pick_signal():
    """
    R2.8 — No exception must escape when no user_pick_signal is present.
    """
    conn = _make_conn()
    rows = [_make_row(signals_json="[]", result="win")]

    try:
        _incorporate_user_behavior(conn, rows)
    except Exception as exc:  # pragma: no cover
        pytest.fail(
            f"_incorporate_user_behavior raised unexpectedly: {type(exc).__name__}: {exc}"
        )


def test_returns_zero_for_empty_rows():
    """No rows → nothing to process, return 0 without error."""
    conn = _make_conn()
    result = _incorporate_user_behavior(conn, [])
    assert result == 0


# ── Happy-path: user_pick_signal present ──────────────────────────────────────

def test_upserts_user_behavior_outcome_when_signal_present():
    """
    R2.1, R2.4 — When a row has a user_pick_signal, one row must be inserted
    into user_behavior_outcomes and the function must return 1.
    """
    conn = _make_conn()
    rows = [
        _make_row(
            match_id="m1",
            pick_type="match_result",
            result="win",
            signals_json=_signals_json_with_user_pick(impact=4.0),
        )
    ]

    result = _incorporate_user_behavior(conn, rows)

    assert result == 1, f"Expected 1 upsert, got {result}"
    row = conn.execute(
        "select * from user_behavior_outcomes where match_id='m1'"
    ).fetchone()
    assert row is not None, "Expected a row in user_behavior_outcomes for match m1"
    assert row["pick_type"] == "match_result"
    assert row["user_agreed"] == 1  # positive impact → agreed
    assert row["result"] == "win"


def test_user_agreed_zero_for_negative_impact():
    """
    R2.2 — Negative impact means user_agreed = 0 (user disagreed).
    """
    conn = _make_conn()
    rows = [
        _make_row(
            match_id="m2",
            result="loss",
            signals_json=_signals_json_with_user_pick(impact=-2.0),
        )
    ]

    _incorporate_user_behavior(conn, rows)

    row = conn.execute(
        "select user_agreed from user_behavior_outcomes where match_id='m2'"
    ).fetchone()
    assert row is not None
    assert row["user_agreed"] == 0


# ── Idempotency: ON CONFLICT DO NOTHING ───────────────────────────────────────

def test_no_double_count_on_duplicate_insert():
    """
    R2.4 — Calling _incorporate_user_behavior twice with the same row must not
    insert a duplicate (ON CONFLICT DO NOTHING).
    """
    conn = _make_conn()
    rows = [
        _make_row(
            match_id="m_dup",
            pick_type="match_result",
            result="win",
            signals_json=_signals_json_with_user_pick(impact=4.0),
        )
    ]

    first_call = _incorporate_user_behavior(conn, rows)
    second_call = _incorporate_user_behavior(conn, rows)

    count = conn.execute(
        "select count(*) from user_behavior_outcomes where match_id='m_dup'"
    ).fetchone()[0]

    assert first_call == 1, "First call should insert 1 row"
    assert second_call == 0, "Second call must be a no-op (DO NOTHING)"
    assert count == 1, "Exactly 1 row must exist after two calls"


# ── Model weight update at >= 15 agreed rows ──────────────────────────────────

def test_updates_agree_calibration_weight_after_15_agreed_rows():
    """
    R2.5 — Once user_behavior_outcomes has >= 15 rows where user_agreed=1,
    a row for model_name='user_behavior_calibration' must be written (or
    updated) in learned_model_weights with a value in [0, 6].
    """
    conn = _make_conn()
    now = "2026-08-14T10:00:00"
    # Pre-populate 14 agreed+win rows
    for i in range(14):
        conn.execute(
            """
            insert or ignore into user_behavior_outcomes
                (match_id, pick_type, user_agreed, result, created_at)
            values (?, 'match_result', 1, 'win', ?)
            """,
            (f"pre_{i}", now),
        )

    # 15th row pushes over threshold
    rows = [
        _make_row(
            match_id="trigger",
            pick_type="match_result",
            result="win",
            signals_json=_signals_json_with_user_pick(impact=4.0),
        )
    ]

    _incorporate_user_behavior(conn, rows)

    weight_row = conn.execute(
        "select learned_weight, samples from learned_model_weights "
        "where model_name='user_behavior_calibration'"
    ).fetchone()

    assert weight_row is not None, (
        "learned_model_weights must have a row for model_name='user_behavior_calibration'"
    )
    assert weight_row["samples"] >= 15
    assert 0.0 <= weight_row["learned_weight"] <= 6.0, (
        f"learned_weight {weight_row['learned_weight']} must be in [0, 6]"
    )


def test_updates_disagree_calibration_weight_after_15_disagreed_rows():
    """
    R2.6 — Once user_behavior_outcomes has >= 15 rows where user_agreed=0,
    a row for model_name='user_behavior_disagree_calibration' must be written
    in learned_model_weights with a value in [-4, 0].
    """
    conn = _make_conn()
    now = "2026-08-14T10:00:00"
    # Pre-populate 14 disagreed+loss rows
    for i in range(14):
        conn.execute(
            """
            insert or ignore into user_behavior_outcomes
                (match_id, pick_type, user_agreed, result, created_at)
            values (?, 'match_result', 0, 'loss', ?)
            """,
            (f"dis_pre_{i}", now),
        )

    # 15th row pushes over threshold
    rows = [
        _make_row(
            match_id="dis_trigger",
            pick_type="match_result",
            result="loss",
            signals_json=_signals_json_with_user_pick(impact=-2.0),
        )
    ]

    _incorporate_user_behavior(conn, rows)

    weight_row = conn.execute(
        "select learned_weight, samples from learned_model_weights "
        "where model_name='user_behavior_disagree_calibration'"
    ).fetchone()

    assert weight_row is not None, (
        "learned_model_weights must have a row for model_name='user_behavior_disagree_calibration'"
    )
    assert weight_row["samples"] >= 15
    assert -4.0 <= weight_row["learned_weight"] <= 0.0, (
        f"learned_weight {weight_row['learned_weight']} must be in [-4, 0]"
    )
