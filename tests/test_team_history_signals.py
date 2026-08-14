"""
Unit tests for _team_history_signals() in enriched_prediction.py
-----------------------------------------------------------------
R11.2 — Team with prediction_total >= 10 and accuracy < 0.40
         → risk signal with impact = -3.
R11.3 — Team with prediction_total >= 10 and accuracy >= 0.60
         → boost signal with impact = +2.
R11.5 — Team with prediction_total < 10 → no signal emitted.
R11.6 — DB error (including missing table) → returns [] without raising.

Additional coverage:
  - Both home and away teams qualify independently (R11.4).
  - Neutral accuracy (40–60 %) produces no signal.
  - Empty team name skips the query gracefully.
  - Missing competition key skips the query gracefully.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

import pytest

from app.enrichment.enriched_prediction import _team_history_signals


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    """In-memory SQLite connection with the team_competitions table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_competitions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            team_key           TEXT    NOT NULL,
            competition_key    TEXT    NOT NULL,
            team_name          TEXT    NOT NULL DEFAULT '',
            competition_name   TEXT    NOT NULL DEFAULT '',
            prediction_correct INTEGER NOT NULL DEFAULT 0,
            prediction_total   INTEGER NOT NULL DEFAULT 0,
            UNIQUE (team_key, competition_key)
        )
    """)
    conn.commit()
    return conn


@contextmanager
def _patched_db(conn: sqlite3.Connection) -> Iterator[None]:
    """
    Patch db_conn and _init_db in enriched_prediction so all DB calls use
    the given in-memory connection rather than the real on-disk database.
    """
    @contextmanager
    def _mock_db_conn(timeout: int = 5) -> Iterator[sqlite3.Connection]:
        yield conn

    with (
        patch("app.enrichment.enriched_prediction.db_conn", _mock_db_conn),
        patch("app.enrichment.enriched_prediction._init_db", lambda: None),
    ):
        yield


def _insert_team(
    conn: sqlite3.Connection,
    team_key: str,
    competition_key: str,
    prediction_total: int,
    prediction_correct: int,
    team_name: str = "",
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO team_competitions
            (team_key, competition_key, team_name, competition_name,
             prediction_total, prediction_correct)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (team_key, competition_key, team_name, "", prediction_total, prediction_correct),
    )
    conn.commit()


def _make_detail(home_name: str = "Home FC", away_name: str = "Away FC") -> dict:
    return {
        "home_team": {"name": home_name, "id": 1},
        "away_team": {"name": away_name, "id": 2},
    }


def _make_doc(tournament: str = "Premier League") -> dict:
    return {"tournament": tournament}


# ── R11.2: risk signal when accuracy < 0.40 ───────────────────────────────────

def test_risk_signal_when_accuracy_below_40_percent():
    """
    R11.2 — When prediction_total >= 10 and accuracy < 0.40, the returned list
    must contain a 'team_prediction_history_risk' signal with impact = -3.
    """
    conn = _make_conn()
    # home team: 3 correct out of 10 = 30% accuracy (below 40%)
    # competition_key must match what normalize_league("Premier League") returns: "premier league"
    _insert_team(conn, "home-fc", "premier league", 10, 3, team_name="Home FC")

    doc = _make_doc("Premier League")
    detail = _make_detail("Home FC", "Away FC")

    with _patched_db(conn):
        signals = _team_history_signals(doc, detail)

    risk = [s for s in signals if s["name"] == "team_prediction_history_risk"]
    assert len(risk) == 1, f"Expected 1 risk signal, got {len(risk)}"
    assert risk[0]["impact"] == -3, f"Expected impact=-3, got {risk[0]['impact']}"
    assert risk[0]["value"]["side"] == "home"
    assert risk[0]["value"]["prediction_total"] == 10
    assert risk[0]["value"]["accuracy"] == pytest.approx(0.3)


# ── R11.3: boost signal when accuracy >= 0.60 ─────────────────────────────────

def test_boost_signal_when_accuracy_at_or_above_60_percent():
    """
    R11.3 — When prediction_total >= 10 and accuracy >= 0.60, the returned list
    must contain a 'team_prediction_history_boost' signal with impact = +2.
    """
    conn = _make_conn()
    # away team: 8 correct out of 10 = 80% accuracy (above 60%)
    _insert_team(conn, "away-fc", "premier league", 10, 8, team_name="Away FC")

    doc = _make_doc("Premier League")
    detail = _make_detail("Home FC", "Away FC")

    with _patched_db(conn):
        signals = _team_history_signals(doc, detail)

    boost = [s for s in signals if s["name"] == "team_prediction_history_boost"]
    assert len(boost) == 1, f"Expected 1 boost signal, got {len(boost)}"
    assert boost[0]["impact"] == 2, f"Expected impact=+2, got {boost[0]['impact']}"
    assert boost[0]["value"]["side"] == "away"
    assert boost[0]["value"]["accuracy"] == pytest.approx(0.8)


# ── R11.3: boost at exactly 60% accuracy ──────────────────────────────────────

def test_boost_signal_at_exactly_60_percent():
    """Accuracy of exactly 0.60 (boundary) must produce a boost signal."""
    conn = _make_conn()
    _insert_team(conn, "home-fc", "premier league", 10, 6, team_name="Home FC")

    doc = _make_doc("Premier League")
    detail = _make_detail("Home FC", "Away FC")

    with _patched_db(conn):
        signals = _team_history_signals(doc, detail)

    boost = [s for s in signals if s["name"] == "team_prediction_history_boost"]
    assert len(boost) == 1, f"Expected boost at exactly 60%, got {signals}"


# ── R11.5: no signal when prediction_total < 10 ───────────────────────────────

def test_no_signal_when_prediction_total_below_10():
    """
    R11.5 — When prediction_total < 10, no signal must be emitted regardless
    of the accuracy value.
    """
    conn = _make_conn()
    # 9 predictions, only 1 correct = 11% accuracy — but total < 10
    _insert_team(conn, "home-fc", "premier league", 9, 1, team_name="Home FC")

    doc = _make_doc("Premier League")
    detail = _make_detail("Home FC", "Away FC")

    with _patched_db(conn):
        signals = _team_history_signals(doc, detail)

    history_signals = [
        s for s in signals
        if s["name"] in ("team_prediction_history_risk", "team_prediction_history_boost")
    ]
    assert history_signals == [], (
        f"Expected no history signals when total < 10, got {history_signals}"
    )


# ── R11.5: no signal when prediction_total is 0 ──────────────────────────────

def test_no_signal_when_prediction_total_is_zero():
    """Edge case: total = 0 must not emit a signal and must not divide by zero."""
    conn = _make_conn()
    _insert_team(conn, "home-fc", "premier league", 0, 0, team_name="Home FC")

    doc = _make_doc("Premier League")
    detail = _make_detail("Home FC", "Away FC")

    with _patched_db(conn):
        signals = _team_history_signals(doc, detail)

    history_signals = [
        s for s in signals
        if s["name"] in ("team_prediction_history_risk", "team_prediction_history_boost")
    ]
    assert history_signals == [], f"Expected no signal for total=0, got {history_signals}"


# ── Neutral accuracy: no signal ───────────────────────────────────────────────

def test_no_signal_for_neutral_accuracy():
    """Accuracy in the 40–60 % band must not emit any history signal."""
    conn = _make_conn()
    # 5 correct out of 10 = 50% — neutral band
    _insert_team(conn, "home-fc", "premier league", 10, 5, team_name="Home FC")

    doc = _make_doc("Premier League")
    detail = _make_detail("Home FC", "Away FC")

    with _patched_db(conn):
        signals = _team_history_signals(doc, detail)

    history_signals = [
        s for s in signals
        if s["name"] in ("team_prediction_history_risk", "team_prediction_history_boost")
    ]
    assert history_signals == [], f"Expected no signal in neutral band, got {history_signals}"


# ── R11.4: both teams can qualify independently ───────────────────────────────

def test_both_teams_qualify_independently():
    """
    R11.4 — When both home and away teams qualify (one risk, one boost),
    both signals are returned independently.
    """
    conn = _make_conn()
    # home: 30% accuracy → risk
    _insert_team(conn, "home-fc", "premier league", 10, 3, team_name="Home FC")
    # away: 70% accuracy → boost
    _insert_team(conn, "away-fc", "premier league", 10, 7, team_name="Away FC")

    doc = _make_doc("Premier League")
    detail = _make_detail("Home FC", "Away FC")

    with _patched_db(conn):
        signals = _team_history_signals(doc, detail)

    risk = [s for s in signals if s["name"] == "team_prediction_history_risk"]
    boost = [s for s in signals if s["name"] == "team_prediction_history_boost"]

    assert len(risk) == 1, f"Expected 1 risk signal, got {risk}"
    assert len(boost) == 1, f"Expected 1 boost signal, got {boost}"
    assert risk[0]["value"]["side"] == "home"
    assert boost[0]["value"]["side"] == "away"


# ── R11.6: DB error → returns [] without raising ──────────────────────────────

def test_returns_empty_list_on_db_error():
    """
    R11.6 — If the DB raises any exception (e.g. missing table, connection
    error), _team_history_signals must return [] without propagating the error.
    """
    @contextmanager
    def _broken_db_conn(timeout: int = 5) -> Iterator[None]:
        raise sqlite3.OperationalError("no such table: team_competitions")
        yield  # pragma: no cover

    with (
        patch("app.enrichment.enriched_prediction.db_conn", _broken_db_conn),
        patch("app.enrichment.enriched_prediction._init_db", lambda: None),
    ):
        try:
            result = _team_history_signals(
                _make_doc("Premier League"),
                _make_detail("Home FC", "Away FC"),
            )
        except Exception as exc:  # pragma: no cover
            pytest.fail(
                f"_team_history_signals raised unexpectedly: {type(exc).__name__}: {exc}"
            )

    assert result == [], f"Expected [] on DB error, got {result}"


# ── R11.6: no row found → no signal, no error ────────────────────────────────

def test_no_signal_when_no_row_in_table():
    """When team_competitions has no row for the team, no signal is returned."""
    conn = _make_conn()
    # Table is empty — no row for any team

    doc = _make_doc("Premier League")
    detail = _make_detail("Home FC", "Away FC")

    with _patched_db(conn):
        signals = _team_history_signals(doc, detail)

    assert signals == [], f"Expected empty list when no DB row exists, got {signals}"


# ── Missing team name → skipped gracefully ────────────────────────────────────

def test_skips_gracefully_when_team_name_is_empty():
    """Empty team name normalises to an empty team_key, which is skipped."""
    conn = _make_conn()

    doc = _make_doc("Premier League")
    # home_team with no name
    detail = {"home_team": {"name": "", "id": 1}, "away_team": {"name": "Away FC", "id": 2}}

    with _patched_db(conn):
        signals = _team_history_signals(doc, detail)

    # Must not raise; may return [] or only signals for teams with valid keys
    history_signals = [
        s for s in signals
        if s["name"] in ("team_prediction_history_risk", "team_prediction_history_boost")
    ]
    assert history_signals == [], f"Expected no signal for empty team name, got {history_signals}"


# ── Missing tournament → comp_key is empty, query is skipped ─────────────────

def test_skips_gracefully_when_tournament_missing():
    """When tournament is absent, comp_key is empty and queries are skipped."""
    conn = _make_conn()
    _insert_team(conn, "home-fc", "", 10, 1, team_name="Home FC")

    doc = {}  # no tournament key
    detail = _make_detail("Home FC", "Away FC")

    with _patched_db(conn):
        signals = _team_history_signals(doc, detail)

    history_signals = [
        s for s in signals
        if s["name"] in ("team_prediction_history_risk", "team_prediction_history_boost")
    ]
    assert history_signals == [], f"Expected no signal when tournament is absent, got {history_signals}"
