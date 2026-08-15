"""
Tests for temporal decay weighting in self_learner.py
------------------------------------------------------
Requirements: R19.2, R19.3, R19.4, R19.6

Covers the four cases specified in task 31.3:

  1. A row with age_in_weeks = 0  → decay_weight = 1.0 exactly.
  2. A row with age_in_weeks = 52 → decay_weight ≈ 0.014 (within ±0.001).
  3. Two rows with identical outcomes but ages 1 week apart → older row
     contributes less to the weighted tally.
  4. All rows with the same created_at → weighted win_rate equals
     unweighted win_rate within ±0.001.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.monitoring.self_learner import (
    DECAY_FACTOR,
    _decay_weight,
    _tally,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_row(created_at: str, result: str = "win", confidence: float = 100.0) -> sqlite3.Row:
    """Build a minimal sqlite3.Row for use in _tally()."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "create table r (created_at text, result text, confidence real)"
    )
    conn.execute(
        "insert into r values (?,?,?)", (created_at, result, confidence)
    )
    return conn.execute("select * from r").fetchone()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── Case 1: zero-age row returns decay = 1.0 ──────────────────────────────────

def test_decay_weight_zero_age_returns_one():
    """
    R19.3 — A row whose created_at equals now must return a decay weight of
    exactly 1.0 (age_weeks = 0, DECAY_FACTOR**0 = 1.0).
    """
    now = _utc_now()
    result = _decay_weight(now.isoformat(), now)
    assert result == pytest.approx(1.0, abs=1e-9), (
        f"Expected 1.0 for zero-age row, got {result}"
    )


def test_decay_weight_zero_age_naive_iso():
    """
    R19.3 — Naive ISO timestamp (no timezone) at now should also give 1.0
    because the helper normalises missing tzinfo to UTC.
    """
    now = _utc_now()
    naive_iso = now.replace(tzinfo=None).isoformat()
    result = _decay_weight(naive_iso, now)
    # Allow a tiny rounding tolerance from the .days rounding
    assert result == pytest.approx(1.0, abs=0.01), (
        f"Expected ≈1.0 for naive zero-age ISO, got {result}"
    )


# ── Case 2: 52-week-old row returns ≈ 0.014 ───────────────────────────────────

def test_decay_weight_52_weeks():
    """
    R19.3, R19.4 — A row 52 weeks old must return DECAY_FACTOR**52 ≈ 0.014
    (within ±0.001).

    Mathematical check: 0.92**52 ≈ 0.01397
    """
    now = _utc_now()
    old_dt = now - timedelta(weeks=52)
    result = _decay_weight(old_dt.isoformat(), now)
    expected = DECAY_FACTOR ** 52
    assert abs(result - expected) < 0.001, (
        f"Expected DECAY_FACTOR**52 ≈ {expected:.4f}, got {result:.4f}"
    )
    assert 0.010 <= result <= 0.020, (
        f"decay_weight for 52-week-old row should be ~0.014, got {result:.4f}"
    )


def test_decay_weight_one_week():
    """
    R19.2 — A one-week-old row decays by exactly DECAY_FACTOR**1.
    """
    now = _utc_now()
    one_week_ago = now - timedelta(weeks=1)
    result = _decay_weight(one_week_ago.isoformat(), now)
    expected = DECAY_FACTOR ** 1
    assert abs(result - expected) < 0.001, (
        f"Expected {expected:.4f} for 1-week-old row, got {result:.4f}"
    )


# ── Case 3: older row contributes less when ages differ by 1 week ─────────────

def test_older_row_contributes_less_to_tally():
    """
    R19.2 — Two win rows with identical confidence (100), one 0 weeks old
    and one 1 week old, should produce different weighted contributions.
    The 1-week-old row should contribute less (weight ≈ DECAY_FACTOR).
    """
    now = _utc_now()

    stats_new: dict = {}
    stats_old: dict = {}

    row_new = _make_row(_iso(now), result="win", confidence=100.0)
    row_old = _make_row(_iso(now - timedelta(weeks=1)), result="win", confidence=100.0)

    _tally(stats_new, ("sig", "__global__"), "win", row=row_new, now=now)
    _tally(stats_old, ("sig", "__global__"), "win", row=row_old, now=now)

    wt_new = stats_new[("sig", "__global__")]["weighted_total"]
    wt_old = stats_old[("sig", "__global__")]["weighted_total"]

    assert wt_new > wt_old, (
        f"Newer row weight ({wt_new:.4f}) should exceed older row weight ({wt_old:.4f})"
    )
    assert abs(wt_old - wt_new * DECAY_FACTOR) < 0.01, (
        f"Expected older weight ≈ newer_weight * DECAY_FACTOR ({wt_new * DECAY_FACTOR:.4f}), "
        f"got {wt_old:.4f}"
    )


def test_two_rows_one_week_apart_older_weighs_less_in_combined_tally():
    """
    R19.2, R19.6 — When two wins are added to the same key with different
    ages, the combined weighted total should reflect that the newer row
    contributes more.
    """
    now = _utc_now()
    stats: dict = {}

    row_new = _make_row(_iso(now), result="win", confidence=100.0)
    row_old = _make_row(_iso(now - timedelta(weeks=1)), result="win", confidence=100.0)

    _tally(stats, ("sig", "league"), "win", row=row_new, now=now)
    _tally(stats, ("sig", "league"), "win", row=row_old, now=now)

    bucket = stats[("sig", "league")]
    # newer contributes ~1.0 * (100/100), older contributes ~0.92 * (100/100)
    expected_total = pytest.approx(1.0 + DECAY_FACTOR, abs=0.01)
    assert bucket["weighted_total"] == expected_total, (
        f"Expected weighted_total ≈ {1.0 + DECAY_FACTOR:.4f}, got {bucket['weighted_total']:.4f}"
    )
    assert bucket["samples"] == 2


# ── Case 4: same created_at → weighted win_rate == unweighted win_rate ─────────

def test_same_created_at_weighted_equals_unweighted_win_rate():
    """
    R19.6 — When all rows share the same created_at, every row has the
    same decay weight.  The weighted win rate must equal the raw win/loss
    ratio within ±0.001 regardless of what that weight is.
    """
    now = _utc_now()
    fixed_ts = _iso(now - timedelta(days=30))  # 30 days old, constant

    # 3 wins, 2 losses (win_rate = 0.60 unweighted)
    outcomes = ["win", "win", "win", "loss", "loss"]
    stats: dict = {}

    for outcome in outcomes:
        row = _make_row(fixed_ts, result=outcome, confidence=100.0)
        _tally(stats, ("sig", "__global__"), outcome, row=row, now=now)

    bucket = stats[("sig", "__global__")]
    weighted_win_rate = bucket["weighted_wins"] / bucket["weighted_total"]
    unweighted_win_rate = bucket["wins"] / bucket["samples"]

    assert abs(weighted_win_rate - unweighted_win_rate) < 0.001, (
        f"Same-age rows: weighted win_rate {weighted_win_rate:.4f} should equal "
        f"unweighted win_rate {unweighted_win_rate:.4f}"
    )
    assert abs(unweighted_win_rate - 0.60) < 0.001


def test_same_created_at_all_losses_weighted_rate():
    """
    R19.6 — All losses with same created_at: weighted win rate = 0.0.
    """
    now = _utc_now()
    fixed_ts = _iso(now - timedelta(days=14))
    stats: dict = {}

    for _ in range(5):
        row = _make_row(fixed_ts, result="loss", confidence=100.0)
        _tally(stats, ("sig", "key"), "loss", row=row, now=now)

    bucket = stats[("sig", "key")]
    weighted_win_rate = bucket["weighted_wins"] / bucket["weighted_total"]
    assert weighted_win_rate == pytest.approx(0.0, abs=1e-9)


# ── Guard: invalid / missing created_at ───────────────────────────────────────

def test_decay_weight_invalid_created_at_returns_one():
    """
    R19.7 — If created_at is unparseable, _decay_weight must return 1.0
    without raising.
    """
    now = _utc_now()
    result = _decay_weight("not-a-date", now)
    assert result == 1.0, f"Expected 1.0 for invalid date, got {result}"


def test_decay_weight_empty_string_returns_one():
    """
    R19.7 — Empty string created_at must return 1.0.
    """
    now = _utc_now()
    result = _decay_weight("", now)
    assert result == 1.0


# ── Decay is monotonically decreasing with age ────────────────────────────────

def test_decay_is_monotonically_decreasing():
    """
    R19.2, R19.4 — Older rows must always have a lower or equal decay weight
    than newer rows.
    """
    now = _utc_now()
    ages_weeks = [0, 1, 4, 12, 26, 52]
    weights = [
        _decay_weight(_iso(now - timedelta(weeks=w)), now)
        for w in ages_weeks
    ]
    for i in range(len(weights) - 1):
        assert weights[i] >= weights[i + 1], (
            f"Decay not monotone: weight at week {ages_weeks[i]} ({weights[i]:.4f}) "
            f"should be >= week {ages_weeks[i+1]} ({weights[i+1]:.4f})"
        )


# ── _tally samples counter tracks unweighted count ───────────────────────────

def test_tally_samples_count_is_unweighted():
    """
    R19.6 — _tally["samples"] must always equal the raw number of rows added,
    independent of any weighting.
    """
    now = _utc_now()
    stats: dict = {}

    for week_offset in range(5):
        row = _make_row(
            _iso(now - timedelta(weeks=week_offset)),
            result="win",
            confidence=100.0,
        )
        _tally(stats, ("s", "g"), "win", row=row, now=now)

    assert stats[("s", "g")]["samples"] == 5
