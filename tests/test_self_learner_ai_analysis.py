"""
Tests for _incorporate_ai_analysis in app/monitoring/self_learner.py
----------------------------------------------------------------------
R1.7: If the competition_analysis table does not exist or contains no rows
matching the graded set, the function must return 0 without raising.

Additional coverage:
  - Returns 0 and is silent when rows is empty.
  - Correctly upserts to ai_analysis_feedback and returns a positive count
    when a matching competition_analysis row exists with a parseable top_table.
  - Does NOT double-count when the same (match_id, competition_key) is
    inserted twice (ON CONFLICT DO NOTHING).
  - Updates learned_model_weights for model_name='llm' once ≥ 10
    ai_analysis_feedback rows exist.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.monitoring.self_learner import _incorporate_ai_analysis, _init_learner_tables


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
    audit_json: str = '{"home_team": "Arsenal", "away_team": "Chelsea"}',
    signals_json: str = "[]",
    confidence: float = 65.0,
) -> sqlite3.Row:
    """Build a minimal sqlite3.Row-like dict for a graded prediction row."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        create table rows (
            match_id text, league_name text, pick_type text,
            selection text, result text, audit_json text,
            signals_json text, confidence real
        )
    """)
    conn.execute(
        "insert into rows values (?,?,?,?,?,?,?,?)",
        (match_id, league_name, pick_type, selection, result,
         audit_json, signals_json, confidence),
    )
    return conn.execute("select * from rows").fetchone()


def _add_competition_analysis(
    conn: sqlite3.Connection,
    competition_key: str = "premier_league",
    analysis_text: dict | None = None,
    created_at: str = "2026-08-14T10:00:00",
) -> None:
    """Insert a competition_analysis row (creates the table if needed)."""
    conn.execute("""
        create table if not exists competition_analysis (
            id integer primary key autoincrement,
            competition_key text not null,
            analysis_text text,
            generated_at text,
            created_at text not null default current_timestamp
        )
    """)
    if analysis_text is None:
        analysis_text = {
            "top_table": [
                {"team": "Arsenal", "rank": 1},
                {"team": "Chelsea", "rank": 2},
            ]
        }
    conn.execute(
        "insert into competition_analysis (competition_key, analysis_text, created_at) values (?,?,?)",
        (competition_key, json.dumps(analysis_text), created_at),
    )


# ── R1.7: missing competition_analysis table ──────────────────────────────────

def test_returns_zero_when_competition_analysis_table_missing():
    """
    R1.7 — If competition_analysis does not exist, _incorporate_ai_analysis
    must return 0 without raising any exception.
    """
    conn = _make_conn()
    # Deliberately do NOT create competition_analysis table
    rows = [_make_row(league_name="Premier League", result="win", selection="home")]

    result = _incorporate_ai_analysis(conn, rows)

    assert result == 0, (
        f"Expected 0 when competition_analysis table is absent, got {result}"
    )


def test_does_not_raise_when_competition_analysis_table_missing():
    """
    R1.7 — No exception must escape when the table is absent.
    """
    conn = _make_conn()
    rows = [_make_row()]

    try:
        _incorporate_ai_analysis(conn, rows)
    except Exception as exc:  # pragma: no cover
        pytest.fail(
            f"_incorporate_ai_analysis raised unexpectedly: {type(exc).__name__}: {exc}"
        )


# ── Empty rows ─────────────────────────────────────────────────────────────────

def test_returns_zero_for_empty_rows():
    """No rows → nothing to process, return 0 without error."""
    conn = _make_conn()
    result = _incorporate_ai_analysis(conn, [])
    assert result == 0


# ── Happy-path: matching analysis row found ────────────────────────────────────

def test_upserts_feedback_row_when_analysis_matches():
    """
    When a competition_analysis row is found for the same competition_key
    within 30 days and the AI direction can be determined, one row must be
    inserted into ai_analysis_feedback and the function must return 1.
    """
    conn = _make_conn()
    _add_competition_analysis(conn, competition_key="premier_league")

    rows = [
        _make_row(
            match_id="m1",
            league_name="Premier League",
            selection="home",
            result="win",
            audit_json=json.dumps({"home_team": "Arsenal", "away_team": "Chelsea"}),
        )
    ]

    result = _incorporate_ai_analysis(conn, rows)

    assert result == 1, f"Expected 1 upsert, got {result}"
    fb = conn.execute("select * from ai_analysis_feedback where match_id='m1'").fetchone()
    assert fb is not None, "Expected a row in ai_analysis_feedback for match m1"
    assert fb["competition_key"] == "premier_league"
    assert fb["analysis_confidence_direction"] in ("home", "away")


# ── Idempotency: ON CONFLICT DO NOTHING ───────────────────────────────────────

def test_no_double_count_on_duplicate_insert():
    """
    Calling _incorporate_ai_analysis twice with the same row must not insert
    a duplicate into ai_analysis_feedback (ON CONFLICT DO NOTHING).
    """
    conn = _make_conn()
    _add_competition_analysis(conn, competition_key="premier_league")

    rows = [
        _make_row(
            match_id="m_dup",
            league_name="Premier League",
            selection="home",
            result="win",
            audit_json=json.dumps({"home_team": "Arsenal", "away_team": "Chelsea"}),
        )
    ]

    first_call = _incorporate_ai_analysis(conn, rows)
    second_call = _incorporate_ai_analysis(conn, rows)

    count = conn.execute(
        "select count(*) from ai_analysis_feedback where match_id='m_dup'"
    ).fetchone()[0]

    assert first_call == 1, "First call should insert 1 row"
    assert second_call == 0, "Second call must be a no-op (DO NOTHING)"
    assert count == 1, "Exactly 1 row must exist after two calls"


# ── LLM weight update at ≥ 10 rows ────────────────────────────────────────────

def test_updates_llm_model_weight_after_ten_feedback_rows():
    """
    R1.5 — Once ai_analysis_feedback has ≥ 10 rows, a row for model_name='llm'
    must be written (or updated) in learned_model_weights.
    """
    conn = _make_conn()
    _add_competition_analysis(conn, competition_key="premier_league")

    # Insert 9 rows directly (pre-populate below the threshold)
    now = "2026-08-14T10:00:00"
    for i in range(9):
        conn.execute(
            """
            insert or ignore into ai_analysis_feedback
                (match_id, competition_key, analysis_correct,
                 analysis_confidence_direction, actual_result, created_at)
            values (?, 'premier_league', 1, 'home', 'win', ?)
            """,
            (f"pre_{i}", now),
        )

    # This 10th row should push us over the threshold
    rows = [
        _make_row(
            match_id="trigger_row",
            league_name="Premier League",
            selection="home",
            result="win",
            audit_json=json.dumps({"home_team": "Arsenal", "away_team": "Chelsea"}),
        )
    ]

    _incorporate_ai_analysis(conn, rows)

    llm_weight = conn.execute(
        "select learned_weight, samples from learned_model_weights where model_name='llm'"
    ).fetchone()

    assert llm_weight is not None, (
        "learned_model_weights must have a row for model_name='llm' after ≥ 10 feedback rows"
    )
    assert llm_weight["samples"] >= 10, (
        f"Expected samples >= 10, got {llm_weight['samples']}"
    )
    assert 0.0 < llm_weight["learned_weight"] <= 0.50, (
        f"learned_weight {llm_weight['learned_weight']} is out of expected range (0, 0.50]"
    )


# ── No match in competition_analysis (empty table) ────────────────────────────

def test_returns_zero_when_competition_analysis_has_no_matching_row():
    """
    R1.7 — If competition_analysis exists but has no row for this
    competition_key, the function must return 0.
    """
    conn = _make_conn()
    # Create the table but insert a row for a *different* competition
    _add_competition_analysis(conn, competition_key="la_liga")

    rows = [
        _make_row(league_name="Premier League")  # normalises to 'premier_league'
    ]

    result = _incorporate_ai_analysis(conn, rows)

    assert result == 0, (
        f"Expected 0 when no matching competition_analysis row exists, got {result}"
    )


# ── Malformed analysis_text ────────────────────────────────────────────────────

def test_skips_row_gracefully_when_analysis_text_is_invalid_json():
    """
    If analysis_text is not valid JSON, the row must be skipped silently
    and the function must return 0 without raising.
    """
    conn = _make_conn()
    conn.execute("""
        create table if not exists competition_analysis (
            id integer primary key autoincrement,
            competition_key text not null,
            analysis_text text,
            created_at text not null
        )
    """)
    conn.execute(
        "insert into competition_analysis values (1, 'premier_league', 'NOT JSON', '2026-08-14T10:00:00')"
    )

    rows = [_make_row(league_name="Premier League")]

    result = _incorporate_ai_analysis(conn, rows)

    assert result == 0
