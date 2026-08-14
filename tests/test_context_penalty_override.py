"""
Tests for learned context penalty override in contextual_intelligence.py
------------------------------------------------------------------------
R8.5 — Tag with a league-specific row in context_penalty_adjustments → the
        penalty_override value is used instead of the hardcoded fallback.
R8.6 — Tag with only a global ('__global__') row → the global override is used.
R8.7 — Tag with no matching rows → falls back to the hardcoded penalty value.

Additional coverage:
  - DB error (exception) inside _learned_penalty_for_tag → returns None without
    raising, so the hardcoded fallback is used.
  - _match_context() correctly reflects the learned penalty in its 'adjustment'
    field for a known tag (e.g. 'friendly').
  - samples < 10 threshold: a row with samples = 9 is ignored and falls back to
    the hardcoded value.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

import pytest

from app.enrichment.contextual_intelligence import (
    _learned_penalty_for_tag,
    _match_context,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    """In-memory SQLite connection with the context_penalty_adjustments table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        create table if not exists context_penalty_adjustments (
            context_tag TEXT not null,
            league_key TEXT not null default '__global__',
            penalty_override REAL,
            samples INTEGER not null default 0,
            win_rate REAL,
            last_updated TEXT not null default current_timestamp,
            primary key (context_tag, league_key)
        )
    """)
    conn.commit()
    return conn


@contextmanager
def _patched_db(conn: sqlite3.Connection) -> Iterator[None]:
    """
    Patch db_conn and _init_db in contextual_intelligence so that all DB calls
    hit the given in-memory connection instead of the real on-disk DB.
    """
    @contextmanager
    def _mock_db_conn(timeout: int = 5) -> Iterator[sqlite3.Connection]:
        yield conn

    with (
        patch("app.enrichment.contextual_intelligence.db_conn", _mock_db_conn),
        patch("app.enrichment.contextual_intelligence._init_db", lambda: None),
    ):
        yield


def _insert_penalty(
    conn: sqlite3.Connection,
    context_tag: str,
    league_key: str,
    penalty_override: float,
    samples: int = 15,
) -> None:
    conn.execute(
        """
        insert or replace into context_penalty_adjustments
            (context_tag, league_key, penalty_override, samples, last_updated)
        values (?, ?, ?, ?, '2026-08-14T10:00:00')
        """,
        (context_tag, league_key, penalty_override, samples),
    )
    conn.commit()


# ── R8.5: league-specific row is returned ─────────────────────────────────────

def test_returns_league_specific_penalty_when_row_exists():
    """
    R8.5 — When context_penalty_adjustments contains a row for (tag, league_key)
    with samples >= 10, _learned_penalty_for_tag must return that penalty_override.
    """
    conn = _make_conn()
    _insert_penalty(conn, "friendly", "premier_league", -3.5)

    with _patched_db(conn):
        result = _learned_penalty_for_tag("friendly", "premier_league")

    assert result == pytest.approx(-3.5), (
        f"Expected -3.5 for league-specific row, got {result}"
    )


# ── R8.6: global fallback when no league-specific row ─────────────────────────

def test_returns_global_penalty_when_only_global_row_exists():
    """
    R8.6 — When no league-specific row exists but a '__global__' row exists with
    samples >= 10, _learned_penalty_for_tag must return the global penalty_override.
    """
    conn = _make_conn()
    # Only a __global__ row — no league-specific row
    _insert_penalty(conn, "friendly", "__global__", -2.0)

    with _patched_db(conn):
        result = _learned_penalty_for_tag("friendly", "la_liga")

    assert result == pytest.approx(-2.0), (
        f"Expected global override -2.0, got {result}"
    )


# ── R8.7: no rows → returns None (use hardcoded fallback) ─────────────────────

def test_returns_none_when_no_rows_exist():
    """
    R8.7 — When no rows exist for the tag in context_penalty_adjustments,
    _learned_penalty_for_tag must return None so the caller uses the hardcoded
    fallback.
    """
    conn = _make_conn()
    # Table is empty

    with _patched_db(conn):
        result = _learned_penalty_for_tag("friendly", "bundesliga")

    assert result is None, (
        f"Expected None when no rows exist, got {result}"
    )


# ── samples < 10 threshold ────────────────────────────────────────────────────

def test_ignores_row_with_insufficient_samples():
    """
    A row with samples = 9 (below the minimum of 10) must be ignored; the
    function should return None and let the caller fall back to the hardcoded value.
    """
    conn = _make_conn()
    _insert_penalty(conn, "friendly", "ligue_1", -4.0, samples=9)

    with _patched_db(conn):
        result = _learned_penalty_for_tag("friendly", "ligue_1")

    assert result is None, (
        f"Expected None for samples=9 (below threshold), got {result}"
    )


# ── league-specific takes precedence over global ──────────────────────────────

def test_league_specific_takes_precedence_over_global():
    """
    When both a league-specific and a global row exist, the league-specific
    penalty_override must be returned (not the global one).
    """
    conn = _make_conn()
    _insert_penalty(conn, "derby_or_rivalry", "serie_a", -5.0)
    _insert_penalty(conn, "derby_or_rivalry", "__global__", -1.5)

    with _patched_db(conn):
        result = _learned_penalty_for_tag("derby_or_rivalry", "serie_a")

    assert result == pytest.approx(-5.0), (
        f"Expected league-specific -5.0 to take precedence, got {result}"
    )


# ── DB error → None, no exception raised ─────────────────────────────────────

def test_returns_none_on_db_error_without_raising():
    """
    If a DB exception occurs inside _learned_penalty_for_tag, it must return
    None silently without propagating the exception.
    """
    @contextmanager
    def _broken_db_conn(timeout: int = 5) -> Iterator[None]:
        raise sqlite3.OperationalError("simulated DB failure")
        yield  # pragma: no cover

    with (
        patch("app.enrichment.contextual_intelligence.db_conn", _broken_db_conn),
        patch("app.enrichment.contextual_intelligence._init_db", lambda: None),
    ):
        try:
            result = _learned_penalty_for_tag("friendly", "premier_league")
        except Exception as exc:  # pragma: no cover
            pytest.fail(
                f"_learned_penalty_for_tag raised unexpectedly: {type(exc).__name__}: {exc}"
            )

    assert result is None, f"Expected None on DB error, got {result}"


# ── _match_context() integration: learned penalty replaces hardcoded value ────

def test_match_context_uses_learned_penalty_for_friendly():
    """
    R8.5 — _match_context() must apply the learned penalty_override for 'friendly'
    instead of the hardcoded -6 when a qualifying row is present.

    The 'friendly' tag has a hardcoded penalty of -6.  We insert a learned
    override of -1.  The resulting adjustment must reflect -1 (not -6).
    """
    conn = _make_conn()
    # Override 'friendly' penalty from -6 to -1 for 'premier_league'
    _insert_penalty(conn, "friendly", "premier_league", -1.0)

    doc = {
        "tournament": "Premier League",
        "name": "International Friendly",
        "category": "England",
    }
    time_context: dict = {}

    with _patched_db(conn):
        result = _match_context(doc, time_context)

    assert "friendly" in result["tags"], "Expected 'friendly' tag to be present"
    # Hardcoded fallback is -6; learned override is -1.
    # The adjustment must be > -6 (closer to -1), indicating the override was applied.
    assert result["adjustment"] > -6, (
        f"Expected adjustment > -6 (learned override = -1), got {result['adjustment']}"
    )


def test_match_context_uses_hardcoded_penalty_when_no_learned_row():
    """
    R8.7 — When no learned row exists for 'friendly', _match_context() must
    apply the hardcoded -6 penalty.
    """
    conn = _make_conn()
    # No rows inserted

    doc = {
        "tournament": "La Liga",
        "name": "Club Friendly",
        "category": "Spain",
    }
    time_context: dict = {}

    with _patched_db(conn):
        result = _match_context(doc, time_context)

    assert "friendly" in result["tags"], "Expected 'friendly' tag to be present"
    # With only the hardcoded -6 and the clamp at max(-10, min(4, ...)),
    # adjustment must be -6 (the sole penalty in this doc).
    assert result["adjustment"] == -6, (
        f"Expected adjustment = -6 (hardcoded fallback), got {result['adjustment']}"
    )


def test_match_context_uses_global_penalty_for_unknown_league():
    """
    R8.6 — When a global override exists for 'friendly' but no league-specific
    row, _match_context() must use the global override.
    """
    conn = _make_conn()
    # Global override: -3
    _insert_penalty(conn, "friendly", "__global__", -3.0)

    doc = {
        "tournament": "Bundesliga",
        "name": "Friendly Match",
        "category": "Germany",
    }
    time_context: dict = {}

    with _patched_db(conn):
        result = _match_context(doc, time_context)

    assert "friendly" in result["tags"], "Expected 'friendly' tag to be present"
    assert result["adjustment"] == -3, (
        f"Expected adjustment = -3 (global override), got {result['adjustment']}"
    )
