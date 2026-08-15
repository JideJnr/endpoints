"""
Tests for R17 — Normalise lineup detection across all key names.

Covers:
- _LINEUP_KEYS constant contents
- _has_lineup_data() returns True for each key directly on doc
- _has_lineup_data() returns True for sofascore_detail sub-keys
- _has_lineup_data() returns False when no lineup keys are present
- _match_context() uses _has_lineup_data() to gate the lineup_window tag
"""
from __future__ import annotations

import pytest

from app.enrichment.contextual_intelligence import (
    _LINEUP_KEYS,
    _has_lineup_data,
    _match_context,
)


# ---------------------------------------------------------------------------
# _LINEUP_KEYS constant
# ---------------------------------------------------------------------------

def test_lineup_keys_contains_required_entries():
    """R17.1 — All five required key names must be present."""
    required = {"lineups", "starting_xi", "confirmed_lineups", "home_lineup", "away_lineup"}
    assert required == set(_LINEUP_KEYS)


def test_lineup_keys_is_tuple():
    assert isinstance(_LINEUP_KEYS, tuple)


# ---------------------------------------------------------------------------
# _has_lineup_data — top-level doc keys (R17.2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["lineups", "starting_xi", "confirmed_lineups", "home_lineup", "away_lineup"])
def test_has_lineup_data_top_level_key_truthy(key):
    """R17.2 — Returns True when any _LINEUP_KEYS key is present and truthy on doc."""
    doc = {key: {"home": [], "away": []}}
    assert _has_lineup_data(doc) is True


@pytest.mark.parametrize("key", ["lineups", "starting_xi", "confirmed_lineups", "home_lineup", "away_lineup"])
def test_has_lineup_data_top_level_key_falsy(key):
    """R17.2 — A key that is present but falsy (empty dict/list) does not count."""
    doc = {key: {}}
    assert _has_lineup_data(doc) is False

    doc2 = {key: []}
    assert _has_lineup_data(doc2) is False

    doc3 = {key: None}
    assert _has_lineup_data(doc3) is False


# ---------------------------------------------------------------------------
# _has_lineup_data — sofascore_detail sub-keys (R17.3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["lineups", "starting_xi", "confirmed_lineups"])
def test_has_lineup_data_sofascore_detail_key_truthy(key):
    """R17.3 — Returns True when sofascore_detail contains a recognised lineup key."""
    doc = {"sofascore_detail": {key: {"home": [1, 2, 3]}}}
    assert _has_lineup_data(doc) is True


@pytest.mark.parametrize("key", ["lineups", "starting_xi", "confirmed_lineups"])
def test_has_lineup_data_sofascore_detail_key_falsy(key):
    """R17.3 — sofascore_detail key present but empty does not count."""
    doc = {"sofascore_detail": {key: {}}}
    assert _has_lineup_data(doc) is False


def test_has_lineup_data_sofascore_detail_absent():
    """R17.3 — Missing sofascore_detail is handled gracefully."""
    doc = {}
    assert _has_lineup_data(doc) is False


def test_has_lineup_data_sofascore_detail_none():
    """R17.3 — None sofascore_detail is handled gracefully."""
    doc = {"sofascore_detail": None}
    assert _has_lineup_data(doc) is False


# ---------------------------------------------------------------------------
# _has_lineup_data — no lineup data at all (R17.4)
# ---------------------------------------------------------------------------

def test_has_lineup_data_empty_doc():
    """R17.4 — Returns False for a completely empty doc."""
    assert _has_lineup_data({}) is False


def test_has_lineup_data_unrelated_keys_only():
    """R17.4 — Unrelated keys on doc do not trigger True."""
    doc = {
        "tournament": "Premier League",
        "home_team": {"name": "Arsenal"},
        "away_team": {"name": "Chelsea"},
    }
    assert _has_lineup_data(doc) is False


def test_has_lineup_data_home_lineup_not_in_sofascore_subset():
    """home_lineup and away_lineup are only checked on doc directly, not sofascore_detail."""
    # Per spec, sofascore_detail only checks "lineups", "starting_xi", "confirmed_lineups"
    doc = {"sofascore_detail": {"home_lineup": {"players": [1, 2]}, "away_lineup": {"players": [3, 4]}}}
    assert _has_lineup_data(doc) is False


# ---------------------------------------------------------------------------
# _match_context — lineup_window tag gating (R17.4)
# ---------------------------------------------------------------------------

def _make_time_context(minutes: float) -> dict:
    return {"minutes_until_kickoff": minutes}


def test_match_context_lineup_window_tag_absent_when_lineup_present():
    """R17.4 — lineup_window tag NOT added when lineup data is present on doc."""
    doc = {"lineups": {"home": [1], "away": [2]}}
    result = _match_context(doc, _make_time_context(45))
    assert "lineup_window" not in result["tags"]


def test_match_context_lineup_window_tag_absent_when_starting_xi_present():
    """R17.2 — starting_xi on doc suppresses lineup_window tag."""
    doc = {"starting_xi": {"confirmed": True}}
    result = _match_context(doc, _make_time_context(30))
    assert "lineup_window" not in result["tags"]


def test_match_context_lineup_window_tag_absent_when_sofascore_lineups_present():
    """R17.3 — sofascore_detail.lineups suppresses lineup_window tag."""
    doc = {"sofascore_detail": {"lineups": {"home": [1, 2, 3]}}}
    result = _match_context(doc, _make_time_context(60))
    assert "lineup_window" not in result["tags"]


def test_match_context_lineup_window_tag_present_when_no_lineup_data():
    """R17.4 — lineup_window tag IS added when no lineup data and within window."""
    doc = {}
    result = _match_context(doc, _make_time_context(45))
    assert "lineup_window" in result["tags"]


def test_match_context_lineup_window_tag_absent_outside_time_window():
    """lineup_window requires 0 <= minutes_until_kickoff <= 90 regardless of lineup data."""
    doc = {}  # No lineup data
    result = _match_context(doc, _make_time_context(120))
    assert "lineup_window" not in result["tags"]


def test_match_context_lineup_window_all_five_keys_suppress_tag():
    """R17.2 — Each of the five _LINEUP_KEYS individually suppresses lineup_window."""
    for key in _LINEUP_KEYS:
        doc = {key: {"data": "present"}}
        result = _match_context(doc, _make_time_context(45))
        assert "lineup_window" not in result["tags"], f"Key '{key}' failed to suppress lineup_window tag"
