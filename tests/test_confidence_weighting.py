"""
Tests for confidence weighting in self_learner._tally() (R20)
-------------------------------------------------------------
Requirements: R20.2, R20.3, R20.5, R20.6

Covers all five cases specified in task 32.3:

  1. Two rows with the same outcome, confidences 90 vs 55: the 90-confidence
     row contributes more to the weighted tally.
  2. All rows at confidence = 100: weighted win_rate equals unweighted
     win_rate within ±0.001.
  3. All rows at confidence = 50: weighted win_rate equals unweighted
     win_rate within ±0.001.
  4. Row with NULL confidence: treated as confidence = 50.
  5. Combined weight = decay * (conf/100.0) verified numerically for a
     known row.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.monitoring.self_learner import (
    DECAY_FACTOR,
    _decay_weight,
    _row_weight,
    _tally,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_row(
    confidence: float | None,
    result: str = "win",
    created_at: str | None = None,
) -> sqlite3.Row:
    """Build a minimal sqlite3.Row for use in _tally() / _row_weight()."""
    now_iso = _utc_now().isoformat()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "create table r (created_at text, result text, confidence real)"
    )
    conn.execute(
        "insert into r values (?,?,?)",
        (created_at or now_iso, result, confidence),
    )
    return conn.execute("select * from r").fetchone()


# ── Case 1: higher-confidence row contributes more ────────────────────────────

def test_higher_confidence_row_contributes_more():
    """
    R20.2 — Two win rows with the same created_at but different confidence
    values (90 vs 55) must produce different weighted_total contributions,
    with the higher-confidence row contributing more.
    """
    now = _utc_now()
    now_iso = now.isoformat()

    stats_high: dict = {}
    stats_low: dict = {}

    row_high = _make_row(confidence=90.0, result="win", created_at=now_iso)
    row_low = _make_row(confidence=55.0, result="win", created_at=now_iso)

    _tally(stats_high, ("sig", "__global__"), "win", row=row_high, now=now)
    _tally(stats_low, ("sig", "__global__"), "win", row=row_low, now=now)

    wt_high = stats_high[("sig", "__global__")]["weighted_total"]
    wt_low = stats_low[("sig", "__global__")]["weighted_total"]

    assert wt_high > wt_low, (
        f"High-confidence row weight ({wt_high:.4f}) should exceed "
        f"low-confidence row weight ({wt_low:.4f})"
    )
    # Numeric check: both same-age → decay ≈ 1.0; ratio = conf_high / conf_low
    expected_ratio = 90.0 / 55.0
    actual_ratio = wt_high / wt_low
    assert abs(actual_ratio - expected_ratio) < 0.01, (
        f"Expected weight ratio ≈ {expected_ratio:.4f}, got {actual_ratio:.4f}"
    )


def test_higher_confidence_win_rate_contribution():
    """
    R20.2 — In a mixed tally, two wins with different confidence both counted
    but the higher-confidence row drives the weighted_wins higher.
    """
    now = _utc_now()
    now_iso = now.isoformat()
    stats: dict = {}

    row_90 = _make_row(confidence=90.0, result="win", created_at=now_iso)
    row_55 = _make_row(confidence=55.0, result="win", created_at=now_iso)

    _tally(stats, ("sig", "key"), "win", row=row_90, now=now)
    _tally(stats, ("sig", "key"), "win", row=row_55, now=now)

    bucket = stats[("sig", "key")]
    # Both are wins so win_rate must be 1.0 regardless of weights
    win_rate = bucket["weighted_wins"] / bucket["weighted_total"]
    assert win_rate == pytest.approx(1.0, abs=1e-9), (
        f"Both wins → weighted win_rate must be 1.0, got {win_rate:.4f}"
    )
    # But weighted_wins should equal weighted_total exactly (all wins)
    assert bucket["weighted_wins"] == pytest.approx(bucket["weighted_total"], abs=1e-9)


# ── Case 2: all rows at confidence = 100 ─────────────────────────────────────

def test_confidence_100_weighted_equals_unweighted_win_rate():
    """
    R20.3 — When all rows have confidence = 100, the confidence_weight is
    1.0 for every row. The weighted win_rate must equal the raw win/loss
    ratio within ±0.001.
    """
    now = _utc_now()
    now_iso = now.isoformat()
    # 4 wins, 2 losses → unweighted win_rate = 4/6 ≈ 0.667
    outcomes = ["win", "win", "win", "win", "loss", "loss"]
    stats: dict = {}

    for outcome in outcomes:
        row = _make_row(confidence=100.0, result=outcome, created_at=now_iso)
        _tally(stats, ("sig", "__global__"), outcome, row=row, now=now)

    bucket = stats[("sig", "__global__")]
    weighted_win_rate = bucket["weighted_wins"] / bucket["weighted_total"]
    unweighted_win_rate = bucket["wins"] / bucket["samples"]

    assert abs(weighted_win_rate - unweighted_win_rate) < 0.001, (
        f"At conf=100: weighted win_rate {weighted_win_rate:.4f} should equal "
        f"unweighted win_rate {unweighted_win_rate:.4f}"
    )
    assert abs(unweighted_win_rate - 4 / 6) < 0.001


# ── Case 3: all rows at confidence = 50 ──────────────────────────────────────

def test_confidence_50_weighted_equals_unweighted_win_rate():
    """
    R20.5 — When all rows have confidence = 50, every row gets
    confidence_weight = 0.5. Since the scale factor is uniform, the
    weighted win_rate must equal the raw win/loss ratio within ±0.001.
    """
    now = _utc_now()
    now_iso = now.isoformat()
    # 3 wins, 3 losses → unweighted win_rate = 0.50
    outcomes = ["win", "win", "win", "loss", "loss", "loss"]
    stats: dict = {}

    for outcome in outcomes:
        row = _make_row(confidence=50.0, result=outcome, created_at=now_iso)
        _tally(stats, ("sig", "__global__"), outcome, row=row, now=now)

    bucket = stats[("sig", "__global__")]
    weighted_win_rate = bucket["weighted_wins"] / bucket["weighted_total"]
    unweighted_win_rate = bucket["wins"] / bucket["samples"]

    assert abs(weighted_win_rate - unweighted_win_rate) < 0.001, (
        f"At conf=50: weighted win_rate {weighted_win_rate:.4f} should equal "
        f"unweighted win_rate {unweighted_win_rate:.4f}"
    )
    assert abs(unweighted_win_rate - 0.50) < 0.001


def test_confidence_50_all_wins_win_rate_is_one():
    """
    R20.5 — All wins at confidence=50: weighted win_rate must be 1.0.
    """
    now = _utc_now()
    now_iso = now.isoformat()
    stats: dict = {}

    for _ in range(5):
        row = _make_row(confidence=50.0, result="win", created_at=now_iso)
        _tally(stats, ("sig", "k"), "win", row=row, now=now)

    bucket = stats[("sig", "k")]
    win_rate = bucket["weighted_wins"] / bucket["weighted_total"]
    assert win_rate == pytest.approx(1.0, abs=1e-9)


# ── Case 4: NULL confidence treated as 50 ─────────────────────────────────────

def test_null_confidence_treated_as_50():
    """
    R20.6 — A row where confidence IS NULL must be treated as confidence = 50,
    yielding a confidence_weight of 0.5.
    """
    now = _utc_now()
    now_iso = now.isoformat()

    row_null = _make_row(confidence=None, result="win", created_at=now_iso)
    row_50 = _make_row(confidence=50.0, result="win", created_at=now_iso)

    w_null = _row_weight(row_null, now)
    w_50 = _row_weight(row_50, now)

    assert abs(w_null - w_50) < 1e-9, (
        f"NULL confidence should give same weight as conf=50: "
        f"null={w_null:.6f}, explicit_50={w_50:.6f}"
    )


def test_null_confidence_tally_matches_confidence_50_tally():
    """
    R20.6 — Tallying a NULL-confidence row should produce the same
    weighted_total as tallying a confidence=50 row with the same
    created_at and result.
    """
    now = _utc_now()
    now_iso = now.isoformat()

    stats_null: dict = {}
    stats_50: dict = {}

    row_null = _make_row(confidence=None, result="win", created_at=now_iso)
    row_50 = _make_row(confidence=50.0, result="win", created_at=now_iso)

    _tally(stats_null, ("sig", "k"), "win", row=row_null, now=now)
    _tally(stats_50, ("sig", "k"), "win", row=row_50, now=now)

    wt_null = stats_null[("sig", "k")]["weighted_total"]
    wt_50 = stats_50[("sig", "k")]["weighted_total"]

    assert abs(wt_null - wt_50) < 1e-9, (
        f"NULL confidence weight ({wt_null:.6f}) should equal conf=50 weight ({wt_50:.6f})"
    )


# ── Case 5: combined weight = decay * (conf / 100.0) ─────────────────────────

def test_combined_weight_equals_decay_times_confidence():
    """
    R20.2 — The combined row weight must equal decay_weight * (confidence / 100.0)
    for a known row, verified numerically.

    Row: created_at = now (decay = 1.0), confidence = 70.
    Expected combined weight = 1.0 * 0.70 = 0.70.
    """
    now = _utc_now()
    now_iso = now.isoformat()
    confidence = 70.0

    row = _make_row(confidence=confidence, result="win", created_at=now_iso)
    w = _row_weight(row, now)

    decay = _decay_weight(now_iso, now)
    expected = decay * (confidence / 100.0)

    assert abs(w - expected) < 1e-6, (
        f"Combined weight should be decay({decay:.4f}) * conf_weight({confidence/100.0:.2f}) "
        f"= {expected:.4f}, got {w:.4f}"
    )


def test_combined_weight_known_decay_and_confidence():
    """
    R20.2 — A row that is exactly 7 days old with confidence = 80.
    decay = DECAY_FACTOR^1 = 0.92
    confidence_weight = 0.80
    expected combined weight = 0.92 * 0.80 = 0.736
    """
    from datetime import timedelta

    now = _utc_now()
    one_week_ago = (now - timedelta(weeks=1)).isoformat()
    confidence = 80.0

    row = _make_row(confidence=confidence, result="win", created_at=one_week_ago)
    w = _row_weight(row, now)

    expected = DECAY_FACTOR * (confidence / 100.0)
    assert abs(w - expected) < 0.005, (
        f"Expected {expected:.4f} (decay={DECAY_FACTOR} * conf=0.80), got {w:.4f}"
    )


def test_combined_weight_confidence_zero():
    """
    R20.2 — A row with confidence = 0 should produce combined weight = 0.0
    (no contribution to the tally).
    """
    now = _utc_now()
    row = _make_row(confidence=0.0, result="win", created_at=now.isoformat())
    w = _row_weight(row, now)
    assert w == pytest.approx(0.0, abs=1e-9), (
        f"Confidence=0 should produce weight=0.0, got {w:.6f}"
    )


def test_combined_weight_confidence_exceeds_100_is_clamped():
    """
    R20.2 — If confidence exceeds 100 (malformed data), the weight should be
    clamped to at most 1.0 * decay (not amplified beyond 1.0).
    """
    now = _utc_now()
    row = _make_row(confidence=150.0, result="win", created_at=now.isoformat())
    w = _row_weight(row, now)
    # decay is ≈ 1.0 at now, confidence_weight should be clamped to 1.0
    assert w <= 1.0 + 1e-9, (
        f"Combined weight with confidence=150 should be <= 1.0, got {w:.6f}"
    )


# ── Cross-context consistency: unweighted samples counter ─────────────────────

def test_samples_counter_always_unweighted():
    """
    R20.7 — The 'samples' key in a _tally bucket must always reflect the
    raw row count, regardless of confidence or decay weighting.
    """
    now = _utc_now()
    now_iso = now.isoformat()
    stats: dict = {}

    for conf in [10.0, 50.0, 90.0, None]:
        row = _make_row(confidence=conf, result="win", created_at=now_iso)
        _tally(stats, ("s", "g"), "win", row=row, now=now)

    assert stats[("s", "g")]["samples"] == 4, (
        "samples should count 4 raw rows regardless of confidence values"
    )


def test_confidence_weighting_differentiates_win_rate():
    """
    R20.2 — If wins are all high-confidence and losses are all low-confidence,
    the weighted win_rate should be HIGHER than the unweighted win_rate.
    """
    now = _utc_now()
    now_iso = now.isoformat()
    stats: dict = {}

    # 3 wins at confidence=90, 3 losses at confidence=10
    for _ in range(3):
        row_win = _make_row(confidence=90.0, result="win", created_at=now_iso)
        row_loss = _make_row(confidence=10.0, result="loss", created_at=now_iso)
        _tally(stats, ("sig", "k"), "win", row=row_win, now=now)
        _tally(stats, ("sig", "k"), "loss", row=row_loss, now=now)

    bucket = stats[("sig", "k")]
    unweighted_win_rate = bucket["wins"] / bucket["samples"]  # = 3/6 = 0.5
    weighted_win_rate = bucket["weighted_wins"] / bucket["weighted_total"]

    assert abs(unweighted_win_rate - 0.5) < 0.001, "sanity: unweighted should be 0.5"
    assert weighted_win_rate > unweighted_win_rate, (
        f"When wins have higher confidence, weighted win_rate ({weighted_win_rate:.4f}) "
        f"should exceed unweighted ({unweighted_win_rate:.4f})"
    )
