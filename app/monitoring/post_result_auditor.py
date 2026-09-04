"""
Post-Result Auditor
-------------------
After every grading cycle, for every newly-graded LOSS, this module:

  1. Fetches the match's live statistics from the ``matches`` table.
  2. Calls ``compute_statistical_dominance`` to determine which side dominated.
  3. Classifies the loss into one of six loss-type buckets:
       - 'dominant_team_lost'      – we called the stat-dominant side correctly; they lost anyway
       - 'called_wrong_side'       – we called the weaker side; clear stat read was opposite
       - 'stat_blind'              – no live stats available; can't assess stat quality
       - 'low_confidence_miss'     – confidence < 60; marginal pick, expected noise
       - 'overconfident_miss'      – confidence >= 75 and we were wrong
       - 'correct_stat_wrong_result' – stats were essentially even; outcome was a coin-flip loss
  4. Writes one row per loss into ``prediction_loss_analysis``.
  5. Returns a summary dict for the monitor log.

Usage (called automatically from prediction_monitor.py after grading):

    from app.monitoring.post_result_auditor import run_post_result_audit

    summary = run_post_result_audit()

Direct queries for inspection:

    SELECT loss_type, count(*), avg(confidence)
    FROM prediction_loss_analysis
    WHERE stat_contradiction = 1
    GROUP BY loss_type
    ORDER BY count(*) DESC;
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from app.storage.db import db_conn, _init_db
from app.match_facts import compute_statistical_dominance
from app.utils.primitives import _loads, _to_int, _safe_float

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Confidence thresholds for loss classification
_LOW_CONFIDENCE_THRESHOLD = 60
_HIGH_CONFIDENCE_THRESHOLD = 75

# Minimum dominance gap for "called_wrong_side" — below this the stats were
# too close to call either way, so it's a coin-flip rather than a clear error.
_WRONG_SIDE_MIN_GAP = 0.25


# ── Public entry point ────────────────────────────────────────────────────────

def run_post_result_audit(limit: int = 500) -> dict[str, Any]:
    """Process up to *limit* newly-graded losses and write audit rows.

    Safe to call repeatedly — uses INSERT OR IGNORE on the unique key
    (match_id, pick_type, selection) so re-runs are idempotent.

    Returns
    -------
    dict with counts: audited, skipped, contradiction_count, by_loss_type
    """
    _init_db()

    # ── 1. Fetch unaudited losses ─────────────────────────────────────────────
    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        losses = conn.execute(
            """
            select ph.id, ph.match_id, ph.match_name, ph.league_name,
                   ph.country_name, ph.pick_type, ph.selection, ph.confidence,
                   ph.final_home, ph.final_away, ph.signals_json, ph.audit_json,
                   ph.models_json, ph.graded_at
            from prediction_history ph
            where ph.result = 'loss'
              and ph.graded_at is not null
              and ph.pick_type != 'no_bet'
              -- Only rows not yet audited
              and not exists (
                  select 1 from prediction_loss_analysis pla
                  where pla.match_id  = ph.match_id
                    and pla.pick_type = ph.pick_type
                    and pla.selection = ph.selection
              )
            order by datetime(ph.graded_at) desc
            limit ?
            """,
            (max(1, min(int(limit), 2000)),),
        ).fetchall()

    if not losses:
        return {"status": "ok", "audited": 0, "skipped": 0,
                "contradiction_count": 0, "by_loss_type": {}}

    # ── 2. Batch-load match stats (one query, not N) ──────────────────────────
    match_ids = list({row["match_id"] for row in losses})
    match_stats: dict[str, dict[str, Any]] = {}
    _BATCH = 500  # SQLite IN() limit safety
    with db_conn(timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        for i in range(0, len(match_ids), _BATCH):
            chunk = match_ids[i : i + _BATCH]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""
                select match_id, live_statistics_json, final_home_goals,
                       final_away_goals, half_time_home_goals, half_time_away_goals
                from matches
                where match_id in ({placeholders})
                """,
                chunk,
            ).fetchall()
            for r in rows:
                match_stats[r["match_id"]] = dict(r)

    # ── 3. Audit each loss and write rows ─────────────────────────────────────
    audited = 0
    skipped = 0
    contradiction_count = 0
    by_loss_type: dict[str, int] = {}

    with db_conn(timeout=30) as conn:
        for loss in losses:
            try:
                row_dict = _audit_one_loss(loss, match_stats)
            except Exception as exc:
                logger.debug("post_result_auditor: skipped %s – %s", loss["match_id"], exc)
                skipped += 1
                continue

            try:
                conn.execute(
                    """
                    insert or ignore into prediction_loss_analysis (
                        prediction_id, match_id, league_name, country_name,
                        pick_type, selection, confidence, final_home, final_away,
                        dominant_side, dominance_gap, dominance_confidence,
                        dominance_basis, stats_used_json,
                        home_stat_score, away_stat_score,
                        home_xg, away_xg,
                        home_shots_on_target, away_shots_on_target,
                        home_possession, away_possession,
                        home_big_chances, away_big_chances,
                        actual_winner, better_side_won, stat_contradiction,
                        loss_type, model_disagreement, diversity_score,
                        signals_json, audit_json
                    ) values (
                        :prediction_id, :match_id, :league_name, :country_name,
                        :pick_type, :selection, :confidence, :final_home, :final_away,
                        :dominant_side, :dominance_gap, :dominance_confidence,
                        :dominance_basis, :stats_used_json,
                        :home_stat_score, :away_stat_score,
                        :home_xg, :away_xg,
                        :home_shots_on_target, :away_shots_on_target,
                        :home_possession, :away_possession,
                        :home_big_chances, :away_big_chances,
                        :actual_winner, :better_side_won, :stat_contradiction,
                        :loss_type, :model_disagreement, :diversity_score,
                        :signals_json, :audit_json
                    )
                    """,
                    row_dict,
                )
                audited += 1
                if row_dict["stat_contradiction"]:
                    contradiction_count += 1
                lt = row_dict["loss_type"] or "unknown"
                by_loss_type[lt] = by_loss_type.get(lt, 0) + 1
            except Exception as exc:
                logger.debug("post_result_auditor: insert failed %s – %s", loss["match_id"], exc)
                skipped += 1

        conn.commit()

    logger.info(
        "post_result_auditor: audited=%d skipped=%d contradictions=%d",
        audited, skipped, contradiction_count,
    )
    return {
        "status": "ok",
        "audited": audited,
        "skipped": skipped,
        "contradiction_count": contradiction_count,
        "by_loss_type": by_loss_type,
    }


# ── Read-back helpers ─────────────────────────────────────────────────────────

def get_loss_audit_summary(
    league_name: str | None = None,
    pick_type: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Return aggregated loss-analysis stats, optionally filtered by league / pick type."""
    _init_db()
    filters = ["datetime(recorded_at) >= datetime('now', ?)"]
    params: list[Any] = [f"-{days} days"]
    if league_name:
        filters.append("league_name = ?")
        params.append(league_name)
    if pick_type:
        filters.append("pick_type = ?")
        params.append(pick_type)
    where = " and ".join(filters)

    with db_conn(timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select loss_type,
                   count(*)                                    as total,
                   sum(stat_contradiction)                     as contradictions,
                   avg(confidence)                             as avg_confidence,
                   sum(case when dominance_confidence = 'high'     then 1 else 0 end) as high_dom,
                   sum(case when dominance_confidence = 'moderate' then 1 else 0 end) as mod_dom
            from prediction_loss_analysis
            where {where}
            group by loss_type
            order by total desc
            """,
            params,
        ).fetchall()

        totals = conn.execute(
            f"""
            select count(*)              as total,
                   sum(stat_contradiction) as contradictions,
                   avg(dominance_gap)    as avg_dom_gap,
                   avg(confidence)       as avg_confidence
            from prediction_loss_analysis
            where {where}
            """,
            params,
        ).fetchone()

    breakdown = [
        {
            "loss_type": r["loss_type"],
            "total": r["total"],
            "contradictions": r["contradictions"] or 0,
            "avg_confidence": round(float(r["avg_confidence"] or 0), 1),
            "high_dominance_losses": r["high_dom"] or 0,
            "moderate_dominance_losses": r["mod_dom"] or 0,
        }
        for r in rows
    ]
    return {
        "total_losses": int(totals["total"] or 0),
        "total_contradictions": int(totals["contradictions"] or 0),
        "avg_dominance_gap": round(float(totals["avg_dom_gap"] or 0), 4),
        "avg_confidence": round(float(totals["avg_confidence"] or 0), 1),
        "breakdown_by_loss_type": breakdown,
        "days": days,
    }


def get_stat_contradiction_losses(
    limit: int = 50,
    min_dominance_confidence: str = "moderate",
) -> list[dict[str, Any]]:
    """Return the worst statistical contradictions — matches where the dominant
    side lost and our model agreed with that dominant side but still lost.

    These are the most actionable rows: the stats said one thing, the result
    said another. Ordered by dominance gap descending (biggest upsets first).
    """
    _init_db()
    conf_rank = {"high": 2, "moderate": 1, "marginal": 0}
    min_rank = conf_rank.get(min_dominance_confidence, 1)

    confidence_filter = (
        "dominance_confidence in ('high', 'moderate')"
        if min_rank >= 1
        else "dominance_confidence in ('high', 'moderate', 'marginal')"
    )

    with db_conn(timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            select *
            from prediction_loss_analysis
            where stat_contradiction = 1
              and {confidence_filter}
            order by abs(dominance_gap) desc, recorded_at desc
            limit ?
            """,
            (max(1, min(int(limit), 500)),),
        ).fetchall()

    return [dict(r) for r in rows]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _audit_one_loss(
    loss: sqlite3.Row,
    match_stats: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build the full audit dict for one graded loss row."""
    match_id  = loss["match_id"]
    pick_type = loss["pick_type"] or ""
    selection = loss["selection"] or ""
    confidence = _to_int(loss["confidence"], 0)
    final_home = _to_int(loss["final_home"], 0) if loss["final_home"] is not None else None
    final_away = _to_int(loss["final_away"], 0) if loss["final_away"] is not None else None

    # Live statistics
    match_row = match_stats.get(match_id) or {}
    live_stats_raw = match_row.get("live_statistics_json") or "{}"
    live_stats: dict[str, Any] = _loads(live_stats_raw, {})

    dominance = compute_statistical_dominance(live_stats, final_home, final_away)

    # Per-stat raw values for the inspector columns
    summary = live_stats.get("summary", {})
    home_xg          = _safe_float((summary.get("xg") or {}).get("home"))
    away_xg          = _safe_float((summary.get("xg") or {}).get("away"))
    home_sot         = _to_int((summary.get("shots_on_target") or {}).get("home"))
    away_sot         = _to_int((summary.get("shots_on_target") or {}).get("away"))
    home_poss        = _safe_float((summary.get("ball_possession") or {}).get("home"))
    away_poss        = _safe_float((summary.get("ball_possession") or {}).get("away"))
    home_bc          = _to_int((summary.get("big_chances") or {}).get("home"))
    away_bc          = _to_int((summary.get("big_chances") or {}).get("away"))

    # Determine which side we predicted
    predicted_side = _infer_predicted_side(pick_type, selection)

    # Model disagreement — was the ensemble diverse on this pick?
    audit_data  = _loads(loss["audit_json"] or "{}", {})
    signals     = _loads(loss["signals_json"] or "[]", [])
    diversity_score = _extract_diversity_score(audit_data)
    model_disagreement = 1 if (diversity_score is not None and diversity_score >= 35) else 0

    # Loss-type classification
    loss_type = _classify_loss_type(
        confidence=confidence,
        dominance=dominance,
        predicted_side=predicted_side,
    )

    return {
        "prediction_id":       loss["id"],
        "match_id":            match_id,
        "league_name":         loss["league_name"],
        "country_name":        loss["country_name"],
        "pick_type":           pick_type,
        "selection":           selection,
        "confidence":          confidence,
        "final_home":          final_home,
        "final_away":          final_away,
        # Dominance
        "dominant_side":       dominance.get("dominant_side"),
        "dominance_gap":       dominance.get("dominance_gap"),
        "dominance_confidence":dominance.get("dominance_confidence"),
        "dominance_basis":     dominance.get("dominance_basis"),
        "stats_used_json":     json.dumps(dominance.get("stats_used", []), default=str),
        "home_stat_score":     (dominance.get("stat_scores") or {}).get("home"),
        "away_stat_score":     (dominance.get("stat_scores") or {}).get("away"),
        # Per-stat columns
        "home_xg":             home_xg,
        "away_xg":             away_xg,
        "home_shots_on_target":home_sot,
        "away_shots_on_target":away_sot,
        "home_possession":     home_poss,
        "away_possession":     away_poss,
        "home_big_chances":    home_bc,
        "away_big_chances":    away_bc,
        # Outcome
        "actual_winner":       dominance.get("actual_winner"),
        "better_side_won":     1 if dominance.get("better_side_won") else (
                                   0 if dominance.get("better_side_won") is False else None),
        "stat_contradiction":  1 if dominance.get("stat_contradiction") else 0,
        # Classification
        "loss_type":           loss_type,
        "model_disagreement":  model_disagreement,
        "diversity_score":     diversity_score,
        # Full blobs for deep drill-down
        "signals_json":        loss["signals_json"] or "[]",
        "audit_json":          loss["audit_json"] or "{}",
    }


def _classify_loss_type(
    confidence: int,
    dominance: dict[str, Any],
    predicted_side: str | None,
) -> str:
    """Map a loss onto one of the six taxonomy buckets.

    Decision order matters — check the most decisive conditions first.
    """
    dom_side   = dominance.get("dominant_side")
    dom_conf   = dominance.get("dominance_confidence", "none")
    dom_gap    = abs(dominance.get("dominance_gap") or 0.0)
    basis      = dominance.get("dominance_basis", "no_stats")
    stat_contradiction = dominance.get("stat_contradiction", False)

    # No stats — can't judge stat quality
    if basis == "no_stats" or dom_conf == "none":
        return "stat_blind"

    # We called the right side statistically but they lost (true upset)
    if stat_contradiction and dom_conf in ("high", "moderate"):
        return "dominant_team_lost"

    # We called the wrong side when stats clearly favoured the other
    if (
        predicted_side
        and dom_side
        and dom_side not in ("even", None)
        and predicted_side != dom_side
        and dom_gap >= _WRONG_SIDE_MIN_GAP
        and dom_conf in ("high", "moderate")
    ):
        return "called_wrong_side"

    # Stats were essentially even — coin-flip territory
    if dom_conf in ("marginal", "none") or dom_side == "even":
        if confidence >= _HIGH_CONFIDENCE_THRESHOLD:
            return "overconfident_miss"
        return "correct_stat_wrong_result"

    # Overconfident on a tight call
    if confidence >= _HIGH_CONFIDENCE_THRESHOLD:
        return "overconfident_miss"

    # Low confidence pick — expected noise
    if confidence < _LOW_CONFIDENCE_THRESHOLD:
        return "low_confidence_miss"

    # Catch-all
    return "correct_stat_wrong_result"


def _infer_predicted_side(pick_type: str, selection: str) -> str | None:
    """Return 'home', 'away', or None from pick_type + selection strings."""
    combined = f"{pick_type} {selection}".lower()
    if any(w in combined for w in ("home", "1x", "home_win", "home win")):
        return "home"
    if any(w in combined for w in ("away", "x2", "away_win", "away win")):
        return "away"
    # Goals / BTTS picks don't have a side
    return None


def _extract_diversity_score(audit: dict[str, Any]) -> int | None:
    """Pull the ensemble diversity_score from the audit blob if present."""
    # enriched_prediction stores it under audit → ensemble or audit → diversity
    try:
        ensemble = audit.get("ensemble") or {}
        ds = ensemble.get("diversity_score")
        if ds is not None:
            return int(ds)
        # Some paths store it at the top-level audit
        ds = audit.get("diversity_score")
        if ds is not None:
            return int(ds)
    except (TypeError, ValueError):
        pass
    return None
