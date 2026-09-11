"""
Tests for the league-accuracy shrinkage + confidence-gated ranking fix
(2026-09-04 / 2026-09-06).

Background: the user asked for bet builder to weight league accuracy so a
well-established league's genuine 58% win rate outranks a barely-sampled
league's misleading 80%. Two places computed/consumed league accuracy using
RAW win_rate with only a flat sample-count cliff (self_learner.py's
league_accuracy write loop and update_tournament_preferences' priority
cascade; bet_builder/core.py's _league_accuracy_boost) -- meaning a league
with exactly enough samples to clear the cliff got its raw rate trusted at
full strength regardless of whether that was 8 samples or 800.

Fix, round 1 (point estimate): reuse the existing _shrink_win_rate
(Empirical-Bayes shrinkage) already used for per-signal weights, now also
applied to league accuracy via a new `shrunk_win_rate` column. The user
caught a real bug in this round: the prior was a flat 0.5, so an 80%-off-5
league collapsed almost all the way to a coin flip ("that took much drop").
Fixed by shrinking toward `_league_shrinkage_prior` -- this system's own
LEAVE-ONE-OUT baseline win rate for that pick_type -- instead of 50%.

Fix, round 2 (confidence gate): even with the corrected prior, a pure point
estimate doesn't GUARANTEE a deep/reliable league always outranks a thin/
lucky one -- a thin sample's honest best-guess can still land a hair above a
deep sample's. The user explicitly asked for a guarantee, not just a better
guess. So a second, deliberately pessimistic score was added:
`rank_score` (Wilson score interval lower bound, `_wilson_lower_bound`) is
stored alongside `shrunk_win_rate` and `baseline_win_rate` (the leave-one-out
prior each row was computed against). `rank_score` gates the POSITIVE side
only: a league only gets ranking credit for looking better than its baseline
once we're ~90% confident it actually is, not just that it got lucky on a
handful of games. It never adds an extra penalty beyond what shrinkage
already applies on the negative side, and it's never shown as "the"
accuracy number -- `shrunk_win_rate` remains the best-guess display value.

Coverage:
  - _shrink_win_rate: the core arithmetic.
  - _wilson_lower_bound: confidence-adjusted score is far more conservative
    than the point estimate for thin samples, and close to it for deep ones.
  - league_accuracy write path: shrunk_win_rate/rank_score/baseline_win_rate
    are computed and persisted correctly by a self-contained equivalent of
    the write loop, using a leave-one-out baseline across several leagues.
  - get_league_accuracy: returns all three fields, falling back gracefully
    for rows written before the columns existed (schema migration safety).
  - update_tournament_preferences: an unproven league that merely LOOKS
    better than baseline is demoted out of the top priority tiers until its
    rank_score actually clears that baseline -- the guarantee the user asked
    for, exercised end to end via run_learning_cycle.
  - bet_builder._league_accuracy_boost: boost is computed from
    shrunk_win_rate, and is withheld entirely (0.0) when rank_score hasn't
    caught up to baseline, even though shrunk_win_rate alone would have
    earned a positive boost.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# Point the app at an isolated, throwaway DB file before any app module is
# imported -- app.config.config reads PREDICTX_DB_PATH once at import time.
_TMP_DB = tempfile.NamedTemporaryFile(prefix="predictx_test_", suffix=".sqlite3", delete=False)
_TMP_DB.close()
os.environ["PREDICTX_DB_PATH"] = _TMP_DB.name

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.monitoring import self_learner as sl  # noqa: E402
from app.storage.db import db_conn  # noqa: E402


def _fresh_db() -> None:
    """Wipe and re-init the learner tables so each test starts clean."""
    with db_conn(timeout=30) as conn:
        conn.execute("drop table if exists league_accuracy")
        conn.execute("drop table if exists tournament_preferences")
        sl._init_learner_tables(conn)
        conn.commit()


# ── _shrink_win_rate ─────────────────────────────────────────────────────

def test_shrink_win_rate_barely_moves_a_deep_sample():
    # 58% off 150 graded picks is a real track record -- shrinkage should
    # leave it close to where it started, when the prior IS 58%.
    shrunk = sl._shrink_win_rate(0.58, 150, strength=sl.LEAGUE_ACCURACY_SHRINKAGE_STRENGTH, prior=0.58)
    assert 0.55 <= shrunk <= 0.58


def test_shrink_win_rate_pulls_a_thin_sample_hard_toward_the_prior():
    # 80% off 5 graded picks is mostly noise -- shrinkage should pull it
    # most of the way back toward whatever the prior is, here a 50% prior.
    shrunk = sl._shrink_win_rate(0.80, 5, strength=sl.LEAGUE_ACCURACY_SHRINKAGE_STRENGTH, prior=0.5)
    assert shrunk < 0.60


def test_shrink_win_rate_uses_the_real_baseline_not_a_flat_50_percent():
    # This is the user's exact bug report: shrinking an 80%-off-5 league
    # toward a flat 50% prior takes far too big a bite. Shrinking toward a
    # realistic ~65% system baseline instead should land noticeably higher.
    toward_50 = sl._shrink_win_rate(0.80, 5, strength=sl.LEAGUE_ACCURACY_SHRINKAGE_STRENGTH, prior=0.5)
    toward_65 = sl._shrink_win_rate(0.80, 5, strength=sl.LEAGUE_ACCURACY_SHRINKAGE_STRENGTH, prior=0.65)
    assert toward_65 > toward_50
    assert toward_65 > 0.60


def test_shrink_win_rate_zero_samples_returns_prior():
    assert sl._shrink_win_rate(0.99, 0, strength=sl.LEAGUE_ACCURACY_SHRINKAGE_STRENGTH, prior=0.5) == 0.5
    assert sl._shrink_win_rate(0.99, 0, strength=sl.LEAGUE_ACCURACY_SHRINKAGE_STRENGTH, prior=0.65) == 0.65


# ── _wilson_lower_bound ──────────────────────────────────────────────────

def test_wilson_lower_bound_is_far_more_conservative_for_thin_samples():
    # 4/5 = 80% raw. The Wilson lower bound should sit well below both the
    # raw rate and a typical shrunk point estimate -- 5 games just isn't
    # enough to be ~90% confident of anything close to 80%.
    lb = sl._wilson_lower_bound(4, 5)
    assert lb < 0.55


def test_wilson_lower_bound_stays_close_to_point_estimate_for_deep_samples():
    # 87/150 = 58%. With a large sample, the Wilson lower bound should sit
    # close to the raw rate -- there's little uncertainty left to punish.
    lb = sl._wilson_lower_bound(87, 150)
    assert 0.50 <= lb <= 0.58


def test_wilson_lower_bound_zero_samples_is_zero():
    assert sl._wilson_lower_bound(0, 0) == 0.0


# ── league_accuracy write path ──────────────────────────────────────────

def _write_league_accuracy_row(conn: sqlite3.Connection, *, league_key: str, league_name: str,
                                pick_type: str, samples: int, wins: int, prior: float = 0.5) -> None:
    """Minimal equivalent of the write loop inside run_learning_cycle (no
    recency weighting, since these tests don't need it) -- exercises the
    exact SQL/columns the real loop writes to catch schema mistakes."""
    win_rate = wins / samples if samples > 0 else 0.0
    shrunk_win_rate = sl._shrink_win_rate(win_rate, samples, strength=sl.LEAGUE_ACCURACY_SHRINKAGE_STRENGTH, prior=prior)
    rank_score = sl._wilson_lower_bound(wins, samples)
    conn.execute(
        """
        insert into league_accuracy
            (league_key, league_name, pick_type, samples, wins,
             win_rate, shrunk_win_rate, rank_score, baseline_win_rate,
             avg_confidence, calibration_gap, last_updated)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
        on conflict(league_key, pick_type) do update set
            league_name       = excluded.league_name,
            samples           = excluded.samples,
            wins              = excluded.wins,
            win_rate          = excluded.win_rate,
            shrunk_win_rate   = excluded.shrunk_win_rate,
            rank_score        = excluded.rank_score,
            baseline_win_rate = excluded.baseline_win_rate,
            avg_confidence    = excluded.avg_confidence,
            calibration_gap   = excluded.calibration_gap,
            last_updated      = excluded.last_updated
        """,
        (league_key, league_name, pick_type, samples, wins, round(win_rate, 4),
         round(shrunk_win_rate, 4), round(rank_score, 4), round(prior, 4),
         70.0, round(win_rate - 0.70, 4)),
    )


def test_get_league_accuracy_returns_shrunk_win_rate():
    _fresh_db()
    with db_conn(timeout=30) as conn:
        _write_league_accuracy_row(
            conn, league_key="la_liga", league_name="La Liga",
            pick_type="match_result", samples=150, wins=87, prior=0.58,  # 58%
        )
        conn.commit()

    result = sl.get_league_accuracy("La Liga")
    assert result["known"] is True
    row = result["by_pick_type"][0]
    assert row["win_rate"] == 58.0
    # Shrunk value should be close to raw (deep sample), but not identical.
    assert 55.0 <= row["shrunk_win_rate"] <= 58.0
    # rank_score should also be close to raw for a deep sample.
    assert 50.0 <= row["rank_score"] <= 58.0


def test_get_league_accuracy_falls_back_to_raw_win_rate_for_pre_fix_rows():
    # Simulate a row written before shrunk_win_rate/rank_score existed
    # (NULL columns) -- schema migration safety.
    _fresh_db()
    with db_conn(timeout=30) as conn:
        conn.execute(
            """
            insert into league_accuracy
                (league_key, league_name, pick_type, samples, wins, win_rate,
                 shrunk_win_rate, rank_score, baseline_win_rate,
                 avg_confidence, calibration_gap, last_updated)
            values ('obscure_league', 'Obscure League', 'match_result', 5, 4, 0.8,
                    null, null, null, 70.0, 0.1, current_timestamp)
            """
        )
        conn.commit()

    result = sl.get_league_accuracy("Obscure League")
    row = result["by_pick_type"][0]
    assert row["shrunk_win_rate"] == 80.0  # falls back to raw, doesn't crash
    assert row["rank_score"] == 80.0  # falls back through shrunk_win_rate to raw
    assert row["baseline_win_rate"] == 50.0  # falls back to a neutral 50%


# ── update_tournament_preferences ───────────────────────────────────────

def test_tournament_priority_gates_thin_lucky_league_out_of_top_tier():
    _fresh_db()
    with db_conn(timeout=30) as conn:
        # A realistic multi-league system, all clustered around a genuine
        # ~58% baseline, so the leave-one-out prior each league is shrunk/
        # ranked against stays stable near 58% regardless of which single
        # league is excluded.
        _write_league_accuracy_row(
            conn, league_key="la_liga", league_name="La Liga",
            pick_type="match_result", samples=150, wins=87, prior=0.58,  # 58%, deep
        )
        for i in range(3):
            _write_league_accuracy_row(
                conn, league_key=f"mid_league_{i}", league_name=f"Mid League {i}",
                pick_type="match_result", samples=60, wins=35, prior=0.58,  # ~58%, deep-ish
            )
        _write_league_accuracy_row(
            conn, league_key="obscure_cup", league_name="Obscure Cup",
            pick_type="match_result", samples=5, wins=4, prior=0.58,  # 80% off 5 -- noise
        )
        conn.commit()

    sl.update_tournament_preferences()

    la_liga = sl.get_tournament_priority("La Liga")
    obscure = sl.get_tournament_priority("Obscure Cup")
    # Lower priority number = liked/processed first. The deep, genuinely
    # accurate league must never rank behind the thin, noisy one -- this is
    # the guarantee: it's enforced via the confidence gate, not by hoping
    # the point estimates happen to land in the right order.
    assert la_liga["priority"] <= obscure["priority"]
    # Obscure Cup's raw/shrunk rate looks great, but 5 games isn't enough to
    # be confident it's actually better than baseline -- it must not have
    # been granted a top-tier (0 or 1) priority on the strength of that alone.
    assert obscure["priority"] >= 2


def test_tournament_priority_grants_top_tier_once_a_league_has_earned_it():
    # A league that GENUINELY clears the baseline with a deep sample should
    # still reach the top tier -- the gate withholds unearned credit, it
    # doesn't block earned credit.
    _fresh_db()
    with db_conn(timeout=30) as conn:
        for i in range(3):
            _write_league_accuracy_row(
                conn, league_key=f"mid_league_{i}", league_name=f"Mid League {i}",
                pick_type="match_result", samples=60, wins=35, prior=0.58,  # ~58%
            )
        _write_league_accuracy_row(
            conn, league_key="premier_league", league_name="Premier League",
            pick_type="match_result", samples=400, wins=320, prior=0.58,  # 80% off 400 -- earned
        )
        conn.commit()

    sl.update_tournament_preferences()
    premier = sl.get_tournament_priority("Premier League")
    assert premier["priority"] <= 1


# ── bet_builder._league_accuracy_boost ──────────────────────────────────

def test_league_accuracy_boost_uses_shrunk_rate_not_raw(monkeypatch):
    import app.bet_builder.core as core

    def fake_get_league_accuracy(league):
        return {
            "known": True,
            "by_pick_type": [
                {"pick_type": "match_result", "samples": 5, "win_rate": 80.0,
                 "shrunk_win_rate": 57.5, "rank_score": 50.0, "baseline_win_rate": 58.0},
            ],
        }

    monkeypatch.setattr("app.monitoring.self_learner.get_league_accuracy", fake_get_league_accuracy)
    item = {"league_name": "Obscure Cup", "pick_type": "match_result"}
    engine_pick = {"type": "match_result"}
    boost = core._league_accuracy_boost(item, engine_pick)
    # shrunk_win_rate is below the 65 threshold, so no boost is granted at all.
    assert boost < 1.0


def test_league_accuracy_boost_still_rewards_a_deep_genuine_track_record(monkeypatch):
    import app.bet_builder.core as core

    def fake_get_league_accuracy(league):
        return {
            "known": True,
            "by_pick_type": [
                {"pick_type": "match_result", "samples": 300, "win_rate": 72.0,
                 "shrunk_win_rate": 70.0, "rank_score": 68.0, "baseline_win_rate": 58.0},
            ],
        }

    monkeypatch.setattr("app.monitoring.self_learner.get_league_accuracy", fake_get_league_accuracy)
    item = {"league_name": "Premier League", "pick_type": "match_result"}
    engine_pick = {"type": "match_result"}
    boost = core._league_accuracy_boost(item, engine_pick)
    # rank_score (68) clears baseline (58) -- credit is earned, not just claimed.
    assert boost > 0.5


def test_league_accuracy_boost_withheld_when_rank_score_has_not_caught_up(monkeypatch):
    # The core guarantee test for bet_builder: a league whose shrunk point
    # estimate clears the 65 threshold, but whose confidence-adjusted
    # rank_score has NOT yet caught up to its own baseline, must get
    # exactly zero boost -- not a smaller boost, none at all.
    import app.bet_builder.core as core

    def fake_get_league_accuracy(league):
        return {
            "known": True,
            "by_pick_type": [
                {"pick_type": "match_result", "samples": 5, "win_rate": 80.0,
                 "shrunk_win_rate": 68.0,  # clears the > 65 gate on point estimate alone
                 "rank_score": 44.0,       # but confidence-adjusted score is still low
                 "baseline_win_rate": 58.0},
            ],
        }

    monkeypatch.setattr("app.monitoring.self_learner.get_league_accuracy", fake_get_league_accuracy)
    item = {"league_name": "Thin League", "pick_type": "match_result"}
    engine_pick = {"type": "match_result"}
    boost = core._league_accuracy_boost(item, engine_pick)
    assert boost == 0.0


def test_league_accuracy_boost_falls_back_to_raw_win_rate_when_shrunk_missing(monkeypatch):
    import app.bet_builder.core as core

    def fake_get_league_accuracy(league):
        return {
            "known": True,
            "by_pick_type": [
                {"pick_type": "match_result", "samples": 300, "win_rate": 72.0},  # legacy row: no new keys
            ],
        }

    monkeypatch.setattr("app.monitoring.self_learner.get_league_accuracy", fake_get_league_accuracy)
    item = {"league_name": "Premier League", "pick_type": "match_result"}
    engine_pick = {"type": "match_result"}
    boost = core._league_accuracy_boost(item, engine_pick)
    # No rank_score/baseline_win_rate present -- gate can't be evaluated, so
    # it must not block the boost (falls back to pre-gate behaviour).
    assert boost > 0.5


# ── run_learning_cycle: shrinks toward the system's own baseline, and gates
#    ranking credit until it's earned ──────────────────────────────────────

def _insert_graded_prediction(conn: sqlite3.Connection, *, match_id: str, league_name: str,
                               pick_type: str, result: str, confidence: int = 70) -> None:
    conn.execute(
        """
        insert into prediction_history
            (source, match_id, match_name, league_name, pick_type, selection,
             confidence, signals_json, picks_json, result, graded_at, created_at)
        values (?, ?, ?, ?, ?, ?, ?, '[]', '[]', ?, current_timestamp, current_timestamp)
        """,
        ("test", match_id, f"{league_name} match {match_id}", league_name, pick_type,
         "Home", confidence, result),
    )


def test_run_learning_cycle_shrinks_toward_system_baseline_not_flat_50():
    # A system whose overall accuracy genuinely runs well above 50% (65%
    # here, from several deep, dominant leagues) should shrink a thin,
    # unfamiliar league's rate toward THAT baseline, not toward a naive
    # coin-flip 50%.
    _fresh_db()
    with db_conn(timeout=30) as conn:
        conn.execute("delete from prediction_history where source = 'test'")
        # Deep, dominant league: 100 graded picks, 65 wins (65%).
        for i in range(100):
            result = "win" if i < 65 else "loss"
            _insert_graded_prediction(
                conn, match_id=f"big-{i}", league_name="Big League",
                pick_type="match_result", result=result,
            )
        # Three more deep leagues at the same ~65% rate, so Big League's own
        # leave-one-out baseline (computed from every OTHER league) stays
        # close to 65% too, rather than being unstably anchored on Thin
        # League alone.
        for m in range(3):
            for i in range(60):
                result = "win" if i < 39 else "loss"  # 39/60 = 65%
                _insert_graded_prediction(
                    conn, match_id=f"mid-{m}-{i}", league_name=f"Mid League {m}",
                    pick_type="match_result", result=result,
                )
        # Thin, unfamiliar league: 5 graded picks, 4 wins (80%).
        for i in range(5):
            result = "win" if i < 4 else "loss"
            _insert_graded_prediction(
                conn, match_id=f"thin-{i}", league_name="Thin League",
                pick_type="match_result", result=result,
            )
        conn.commit()

    result = sl.run_learning_cycle()
    assert result.get("league_updates", 0) >= 2

    thin = sl.get_league_accuracy("Thin League")
    thin_row = thin["by_pick_type"][0]
    assert thin_row["win_rate"] == 80.0
    # Old behaviour (flat 0.5 prior, strength=20) would have landed at 56%.
    # With the system's real ~65% baseline as the prior instead, it should
    # land noticeably higher than that, and below its own raw 80%.
    assert thin_row["shrunk_win_rate"] > 60.0
    assert thin_row["shrunk_win_rate"] < 80.0
    # Thin League's baseline_win_rate (leave-one-out prior) should reflect
    # the ~65% system, not a flat 50%.
    assert thin_row["baseline_win_rate"] > 60.0
    # And its rank_score (confidence-adjusted) should sit well below its own
    # shrunk point estimate -- 5 games is not enough to be confident.
    assert thin_row["rank_score"] < thin_row["shrunk_win_rate"]

    big = sl.get_league_accuracy("Big League")
    big_row = big["by_pick_type"][0]
    # The deep league's own rate IS close to the system baseline already,
    # so shrinkage should barely move it. Its rank_score is still
    # deliberately pessimistic (Wilson lower bound, not a point estimate),
    # but with 100 samples the gap to its own point estimate should be
    # modest -- nothing like the ~30-point gap Thin League's 5 samples get.
    assert abs(big_row["shrunk_win_rate"] - big_row["win_rate"]) < 3.0
    assert big_row["rank_score"] < big_row["shrunk_win_rate"]
    assert (big_row["shrunk_win_rate"] - big_row["rank_score"]) < (thin_row["shrunk_win_rate"] - thin_row["rank_score"])
