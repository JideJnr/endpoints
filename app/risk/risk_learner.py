"""
Risk Learner
============
Learns dynamic risk control thresholds from graded prediction history.

Instead of static caps (e.g. "degraded provider → cap 62"), this module
tracks the actual win rate of picks made under each risk condition and
computes optimal confidence/stake adjustments that maximize ROI.

The learner tracks outcomes by:
  - risk_condition: the specific risk factor (e.g. degraded_provider, high_volatility)
  - pick_type: match_result, total_goals, btts, etc.
  - confidence_band: 50-59, 60-69, 70-79, 80+
  - league_tier: 1-4 (from regime.py)
  - odds_range: decimal odds bucket

For each combination, it computes:
  - win_rate: actual win rate
  - avg_clv: average closing-line value
  - roi: return on investment
  - recommended_confidence_cap: the confidence level that maximizes risk-adjusted return
  - recommended_stake_cap: the stake percentage that maximizes risk-adjusted return

Usage:
    from app.risk.risk_learner import get_learned_risk_controls, record_risk_outcome

    # At prediction time — get dynamic caps
    controls = get_learned_risk_controls(
        risk_conditions=["degraded_provider", "high_volatility"],
        pick_type="match_result",
        confidence=72,
        league_tier=2,
        decimal_odds=2.5,
    )

    # After grading — record the outcome for learning
    record_risk_outcome(
        risk_conditions=["degraded_provider"],
        pick_type="match_result",
        confidence=62,
        league_tier=2,
        decimal_odds=2.5,
        result="win",
        stake_per_100=1.0,
        clv_percent=2.5,
    )
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH, _conn
from app.storage.league_memory import _init_db

# Learned risk-outcome rows are trusted only while reasonably fresh.
#
# rebuild_risk_controls() (below) is currently the ONLY writer of the
# risk_outcomes table and it is NOT called from anywhere in the app (no
# scheduler job, no grading hook) — see the warning on rebuild_risk_controls()
# for why it has been left disconnected. That means, today, risk_outcomes is
# always empty and every call to get_learned_risk_controls() falls through to
# the "bootstrap" default at the bottom of that function, which is the
# existing fail-safe for "no data yet".
#
# RISK_CONTROLS_MAX_AGE_DAYS exists for the future: if rebuild_risk_controls()
# (or a proper incremental writer) is ever wired in and then stops running
# (a broken cron entry, an exception that gets swallowed upstream, etc.), a
# learned row should not go on being trusted forever just because it once
# existed. Once a row is older than this, it is treated the same as
# "insufficient samples" and the read path falls back the same way it does
# for a genuinely empty table.
RISK_CONTROLS_MAX_AGE_DAYS = 45


def _risk_row_is_stale(last_updated: Any, max_age_days: int = RISK_CONTROLS_MAX_AGE_DAYS) -> bool:
    """True when a risk_outcomes row's last_updated is missing, unparseable,
    or older than max_age_days. Fails safe: anything we can't confidently
    call "fresh" is treated as stale so callers fall back to conservative
    defaults instead of trusting it."""
    if not last_updated:
        return True
    try:
        row_dt = datetime.fromisoformat(str(last_updated).replace("Z", "+00:00"))
        if row_dt.tzinfo is None:
            row_dt = row_dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - row_dt.astimezone(timezone.utc)).days
        return age_days > max_age_days
    except Exception:
        return True


# ── Data Structures ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RiskOutcome:
    risk_conditions: str       # JSON array of condition names
    pick_type: str
    confidence_band: str       # "50-59", "60-69", "70-79", "80+"
    league_tier: int           # 1-4
    odds_range: str            # "1.0-1.5", "1.5-2.0", "2.0-3.0", "3.0-5.0", "5.0+"
    samples: int
    wins: int
    losses: int
    win_rate: float | None
    avg_clv_percent: float | None
    avg_roi: float | None
    recommended_confidence_cap: int
    recommended_stake_cap: float
    last_updated: str


@dataclass(frozen=True)
class LearnedRiskControls:
    """Dynamic risk controls learned from history."""
    confidence_cap: int
    stake_cap_per_100: float
    hard_block: bool
    block_reason: str | None
    source: str              # "learned", "fallback", "bootstrap"
    samples: int
    win_rate: float | None
    avg_clv: float | None


# ── Database Schema ────────────────────────────────────────────────────────────

def _init_risk_learner_tables(conn: sqlite3.Connection) -> None:
    """Create tables for tracking risk condition outcomes."""
    conn.execute("""
        create table if not exists risk_outcomes (
            id integer primary key autoincrement,
            risk_conditions text not null,       -- JSON array
            pick_type text not null,
            confidence_band text not null,
            league_tier integer not null,
            odds_range text not null,
            samples integer not null default 0,
            wins integer not null default 0,
            losses integer not null default 0,
            win_rate real,
            avg_clv_percent real,
            avg_roi real,
            recommended_confidence_cap integer not null default 72,
            recommended_stake_cap real not null default 5.0,
            last_updated text not null default current_timestamp,
            unique(risk_conditions, pick_type, confidence_band, league_tier, odds_range)
        )
    """)
    conn.execute("""
        create index if not exists idx_risk_outcomes_conditions
        on risk_outcomes(risk_conditions)
    """)
    conn.execute("""
        create index if not exists idx_risk_outcomes_pick_type
        on risk_outcomes(pick_type)
    """)
    conn.execute("""
        create table if not exists risk_control_history (
            id integer primary key autoincrement,
            match_id text not null,
            risk_conditions text not null,
            pick_type text not null,
            raw_confidence integer not null,
            applied_confidence_cap integer not null,
            applied_stake_cap real not null,
            hard_blocked integer not null default 0,
            result text,
            clv_percent real,
            created_at text not null default current_timestamp
        )
    """)
    conn.execute("""
        create index if not exists idx_risk_control_history_match
        on risk_control_history(match_id)
    """)


# ── Core Learning Functions ────────────────────────────────────────────────────

def record_risk_outcome(
    risk_conditions: list[str],
    pick_type: str,
    confidence: int,
    league_tier: int,
    decimal_odds: float,
    result: str,
    stake_per_100: float = 0.0,
    clv_percent: float | None = None,
) -> None:
    """Record the outcome of a pick made under specific risk conditions.

    Call this after a prediction is graded to feed the learner.
    """
    _init_db()
    conditions_key = _conditions_key(risk_conditions)
    band = _confidence_band(confidence)
    odds_range = _odds_range(decimal_odds)

    with db_conn(timeout=20) as conn:
        _init_risk_learner_tables(conn)
        conn.execute("""
            insert into risk_outcomes
                (risk_conditions, pick_type, confidence_band, league_tier, odds_range,
                 samples, wins, losses, win_rate, avg_clv_percent, avg_roi,
                 recommended_confidence_cap, recommended_stake_cap, last_updated)
            values (?, ?, ?, ?, ?, 1,
                    case when ? = 'win' then 1 else 0 end,
                    case when ? = 'loss' then 1 else 0 end,
                    case when ? = 'win' then 1.0 else 0.0 end,
                    ?, ?, ?, ?, current_timestamp)
            on conflict(risk_conditions, pick_type, confidence_band, league_tier, odds_range)
            do update set
                samples = risk_outcomes.samples + 1,
                wins = risk_outcomes.wins + case when ? = 'win' then 1 else 0 end,
                losses = risk_outcomes.losses + case when ? = 'loss' then 1 else 0 end,
                win_rate = round(1.0 * (risk_outcomes.wins + case when ? = 'win' then 1 else 0 end) /
                                 (risk_outcomes.samples + 1), 4),
                avg_clv_percent = case
                    when ? is not null and risk_outcomes.avg_clv_percent is not null
                    then round((risk_outcomes.avg_clv_percent * risk_outcomes.samples + ?) /
                               (risk_outcomes.samples + 1), 2)
                    when ? is not null then round(?, 2)
                    else risk_outcomes.avg_clv_percent
                end,
                avg_roi = case
                    when ? is not null and risk_outcomes.avg_roi is not null
                    then round((risk_outcomes.avg_roi * risk_outcomes.samples + ?) /
                               (risk_outcomes.samples + 1), 4)
                    else risk_outcomes.avg_roi
                end,
                recommended_confidence_cap = _compute_confidence_cap(
                    risk_outcomes.wins + case when ? = 'win' then 1 else 0 end,
                    risk_outcomes.samples + 1,
                    risk_outcomes.recommended_confidence_cap
                ),
                recommended_stake_cap = _compute_stake_cap(
                    risk_outcomes.wins + case when ? = 'win' then 1 else 0 end,
                    risk_outcomes.samples + 1,
                    risk_outcomes.recommended_stake_cap,
                    ?
                ),
                last_updated = current_timestamp
        """, (
            conditions_key, pick_type, band, league_tier, odds_range,
            result, result, result,
            clv_percent, clv_percent,  # avg_clv update
            clv_percent, clv_percent,  # avg_roi placeholder
            result, result,  # confidence cap update
            result, result, clv_percent,  # stake cap update
        ))
        conn.commit()


def _compute_confidence_cap(wins: int, samples: int, current_cap: int) -> int:
    """Compute the confidence cap that maximizes risk-adjusted return.

    Strategy:
    - If win_rate < 40% with >= 10 samples: reduce cap aggressively
    - If win_rate 40-55%: moderate reduction
    - If win_rate 55-70%: slight reduction or maintain
    - If win_rate > 70% with >= 20 samples: allow higher cap (trust the condition)
    - Always maintain a floor of 45 and ceiling of 85
    """
    if samples < 5:
        return current_cap  # Not enough data to adjust

    win_rate = wins / samples if samples > 0 else 0.5

    if win_rate < 0.35 and samples >= 15:
        return max(45, int(current_cap * 0.75))  # Poor performance → significant reduction
    elif win_rate < 0.45 and samples >= 10:
        return max(50, int(current_cap * 0.85))  # Below average → moderate reduction
    elif win_rate < 0.55:
        return max(52, int(current_cap * 0.95))  # Slightly below average → slight reduction
    elif win_rate > 0.70 and samples >= 20:
        return min(85, int(current_cap * 1.05))  # Strong performance → allow higher cap
    elif win_rate > 0.65 and samples >= 15:
        return min(80, int(current_cap * 1.02))  # Good performance → slight increase

    return current_cap  # Maintain current cap for 55-65% range


def _compute_stake_cap(wins: int, samples: int, current_cap: float, avg_clv: float | None) -> float:
    """Compute the stake cap that maximizes risk-adjusted return.

    Strategy:
    - If win_rate < 40%: reduce stake significantly
    - If win_rate 40-55%: moderate reduction
    - If CLV is positive and win_rate > 55%: maintain or slightly increase
    - If CLV is negative: reduce stake regardless of win rate
    - Always maintain floor of 0.25 and ceiling of 8.0
    """
    if samples < 5:
        return current_cap

    win_rate = wins / samples if samples > 0 else 0.5

    # CLV-based adjustment
    clv_factor = 1.0
    if avg_clv is not None:
        if avg_clv < -5.0:
            clv_factor = 0.6  # Negative CLV → reduce stake
        elif avg_clv < -2.0:
            clv_factor = 0.8
        elif avg_clv > 5.0:
            clv_factor = 1.2  # Positive CLV → can increase stake
        elif avg_clv > 2.0:
            clv_factor = 1.1

    # Win rate-based adjustment
    if win_rate < 0.35 and samples >= 15:
        wr_factor = 0.5
    elif win_rate < 0.45 and samples >= 10:
        wr_factor = 0.7
    elif win_rate < 0.55:
        wr_factor = 0.9
    elif win_rate > 0.70 and samples >= 20:
        wr_factor = 1.15
    elif win_rate > 0.65 and samples >= 15:
        wr_factor = 1.05
    else:
        wr_factor = 1.0

    new_cap = current_cap * clv_factor * wr_factor
    return max(0.25, min(8.0, round(new_cap, 2)))


def get_learned_risk_controls(
    risk_conditions: list[str],
    pick_type: str,
    confidence: int,
    league_tier: int,
    decimal_odds: float,
    min_samples: int = 8,
) -> LearnedRiskControls:
    """Get dynamically learned risk controls for a specific scenario.

    Returns confidence cap, stake cap, and hard-block decision based on
    historical outcomes of picks made under the same risk conditions.
    """
    _init_db()
    conditions_key = _conditions_key(risk_conditions)
    band = _confidence_band(confidence)
    odds_range = _odds_range(decimal_odds)

    with db_conn(timeout=15) as conn:
        _init_risk_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            select * from risk_outcomes
            where risk_conditions = ? and pick_type = ? and confidence_band = ?
              and league_tier = ? and odds_range = ?
        """, (conditions_key, pick_type, band, league_tier, odds_range)).fetchone()

    # Only trust this row when it has enough samples AND has been refreshed
    # recently (see _risk_row_is_stale / RISK_CONTROLS_MAX_AGE_DAYS above) —
    # a stale row is treated the same as "not enough data".
    if row and row["samples"] >= min_samples and not _risk_row_is_stale(row["last_updated"]):
        win_rate = row["win_rate"]
        hard_block = win_rate is not None and win_rate < 0.30 and row["samples"] >= 20
        block_reason = "learned_poor_performance" if hard_block else None

        return LearnedRiskControls(
            confidence_cap=row["recommended_confidence_cap"],
            stake_cap_per_100=row["recommended_stake_cap"],
            hard_block=hard_block,
            block_reason=block_reason,
            source="learned",
            samples=row["samples"],
            win_rate=win_rate,
            avg_clv=row["avg_clv_percent"],
        )

    # Fallback: try broader match (any confidence band, any odds range)
    with db_conn(timeout=15) as conn:
        _init_risk_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            select * from risk_outcomes
            where risk_conditions = ? and pick_type = ? and league_tier = ?
            order by samples desc
            limit 1
        """, (conditions_key, pick_type, league_tier)).fetchone()

    if row and row["samples"] >= min_samples and not _risk_row_is_stale(row["last_updated"]):
        win_rate = row["win_rate"]
        hard_block = win_rate is not None and win_rate < 0.30 and row["samples"] >= 20
        return LearnedRiskControls(
            confidence_cap=row["recommended_confidence_cap"],
            stake_cap_per_100=row["recommended_stake_cap"],
            hard_block=hard_block,
            block_reason="learned_poor_performance" if hard_block else None,
            source="learned_broad",
            samples=row["samples"],
            win_rate=win_rate,
            avg_clv=row["avg_clv_percent"],
        )

    # Bootstrap: no data yet (or the only matching rows are stale), use conservative defaults
    return LearnedRiskControls(
        confidence_cap=72,
        stake_cap_per_100=2.0,
        hard_block=False,
        block_reason=None,
        source="bootstrap",
        samples=0,
        win_rate=None,
        avg_clv=None,
    )


def get_risk_control_summary() -> dict[str, Any]:
    """Return a summary of all learned risk controls for the dashboard."""
    _init_db()
    with db_conn(timeout=20) as conn:
        _init_risk_learner_tables(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            select risk_conditions, pick_type, confidence_band, league_tier, odds_range,
                   samples, wins, losses, win_rate, avg_clv_percent,
                   recommended_confidence_cap, recommended_stake_cap, last_updated
            from risk_outcomes
            order by samples desc, last_updated desc
        """).fetchall()

    return {
        "total_conditions": len(rows),
        "conditions": [
            {
                "risk_conditions": row["risk_conditions"],
                "pick_type": row["pick_type"],
                "confidence_band": row["confidence_band"],
                "league_tier": row["league_tier"],
                "odds_range": row["odds_range"],
                "samples": row["samples"],
                "wins": row["wins"],
                "losses": row["losses"],
                "win_rate": row["win_rate"],
                "avg_clv_percent": row["avg_clv_percent"],
                "confidence_cap": row["recommended_confidence_cap"],
                "stake_cap": row["recommended_stake_cap"],
                "last_updated": row["last_updated"],
            }
            for row in rows
        ],
    }


def rebuild_risk_controls() -> dict[str, Any]:
    """Rebuild all risk control recommendations from graded prediction history.

    Scans prediction_history for all graded predictions, extracts their
    risk conditions at prediction time, and recomputes optimal caps.

    NOT CURRENTLY CALLED ANYWHERE IN THE APP. This is intentional, not an
    oversight — do not wire it into a scheduled job (e.g. self_learner's
    run_learning_cycle, which two scheduler.py jobs already invoke on every
    grading pass) without first fixing the issue below:

    This function is NOT idempotent / safe to call repeatedly. It does a
    full, unfiltered rescan of every graded prediction in prediction_history
    every time it runs (no "since last run" cursor, no delete-and-rebuild of
    risk_outcomes first — contrast with self_learner.run_learning_cycle,
    which does `delete from signal_weights` etc. before recomputing from
    scratch). Each row it processes goes through record_risk_outcome(),
    whose SQL does `samples = risk_outcomes.samples + 1` — an incremental
    counter increment, designed for "record one freshly-graded outcome"
    calls, not for repeatedly replaying the entire history. Calling this on
    a schedule would re-add every already-counted historical outcome on
    every run, inflating `samples`/`wins`/`losses` without bound and
    corrupting the derived recommended_confidence_cap / recommended_stake_cap
    (both of which gate real stake sizing in risk_manager.py).

    Before wiring this in, either (a) rewrite it to aggregate stats in
    Python and delete-and-rebuild risk_outcomes from scratch each run (the
    pattern self_learner.run_learning_cycle uses), or (b) hook
    record_risk_outcome() directly into wherever predictions get graded, so
    each outcome is recorded exactly once, and drop the periodic full
    rescan entirely.

    Until one of those is done, the read path (get_learned_risk_controls /
    get_learned_risk_controls_for_pick) is written to be safe regardless:
    with risk_outcomes always empty, every read falls through to the
    documented "bootstrap" conservative default, and risk_manager.py's
    apply_risk_controls() additionally never trusts a learned row with
    fewer than LEARNED_RISK_MIN_SAMPLES samples. See RISK_CONTROLS_MAX_AGE_DAYS
    above for the staleness guard that also protects against a
    once-populated table silently going stale if this is later wired in and
    then breaks.
    """
    _init_db()
    with db_conn(timeout=30) as conn:
        _init_risk_learner_tables(conn)
        conn.row_factory = sqlite3.Row

        # Get all graded predictions with their risk management data
        rows = conn.execute("""
            select ph.id, ph.match_id, ph.pick_type, ph.selection, ph.confidence,
                   ph.result, ph.league_name, ph.country_name, ph.graded_at,
                   ph.audit_json, ph.signals_json,
                   rm.risk_level, rm.violations, rm.assurance, rm.learned_classification
            from prediction_history ph
            left join (
                select match_id, json_extract(risk_management, '$.risk_level') as risk_level,
                       json_extract(risk_management, '$.violations') as violations,
                       json_extract(risk_management, '$.assurance') as assurance,
                       json_extract(risk_management, '$.learned_classification') as learned_classification
                from prediction_history
                where risk_management is not null
            ) rm on ph.match_id = rm.match_id
            where ph.graded_at is not null
              and ph.result in ('win', 'loss')
              and ph.pick_type != 'no_bet'
              and ph.audit_json is not null
        """).fetchall()

    updated = 0
    for row in rows:
        try:
            audit = _parse_json(row["audit_json"]) or {}
            risk_mgmt = audit.get("risk_management") or {}
            contextual = audit.get("contextual_intelligence") or {}
            market = contextual.get("market_behavior") or {}
            readiness = audit.get("enrichment", {}).get("assurance", "full")

            # Extract risk conditions from the prediction
            conditions = _extract_risk_conditions(risk_mgmt, contextual, market, readiness)
            if not conditions:
                continue

            # Get odds from the prediction
            odds = _extract_odds_from_audit(audit)
            league_tier = _estimate_league_tier(row["league_name"], row["country_name"])
            pick_type = row["pick_type"]
            confidence = int(row["confidence"] or 50)
            result = row["result"]

            # Get CLV if available
            clv_row = conn.execute("""
                select clv_percent from clv_entries
                where match_id = ? and pick_type = ?
                limit 1
            """, (row["match_id"], pick_type)).fetchone()
            clv = clv_row["clv_percent"] if clv_row else None

            record_risk_outcome(
                risk_conditions=conditions,
                pick_type=pick_type,
                confidence=confidence,
                league_tier=league_tier,
                decimal_odds=odds,
                result=result,
                clv_percent=clv,
            )
            updated += 1
        except Exception:
            continue

    return {"status": "ok", "records_processed": updated}


# ── Helper Functions ───────────────────────────────────────────────────────────

def _conditions_key(conditions: list[str]) -> str:
    """Create a stable key for a set of risk conditions."""
    return "|".join(sorted(set(conditions)))


def _confidence_band(confidence: int) -> str:
    """Map confidence to band."""
    if confidence >= 80:
        return "80+"
    elif confidence >= 70:
        return "70-79"
    elif confidence >= 60:
        return "60-69"
    else:
        return "50-59"


def _odds_range(decimal_odds: float) -> str:
    """Map decimal odds to range bucket."""
    if decimal_odds < 1.5:
        return "1.0-1.5"
    elif decimal_odds < 2.0:
        return "1.5-2.0"
    elif decimal_odds < 3.0:
        return "2.0-3.0"
    elif decimal_odds < 5.0:
        return "3.0-5.0"
    else:
        return "5.0+"


def _estimate_league_tier(league_name: str | None, country_name: str | None) -> int:
    """Estimate league tier from name. Falls back to tier 3."""
    from app.market.regime import get_regime
    regime = get_regime(league_name, country_name)
    return regime.tier


def _extract_risk_conditions(
    risk_mgmt: dict[str, Any],
    contextual: dict[str, Any],
    market: dict[str, Any],
    readiness: str,
) -> list[str]:
    """Extract risk condition tags from prediction metadata."""
    conditions = []

    # Provider/data quality conditions
    if readiness in ("sportybet_prematch_minimum", "sportybet_market_signal"):
        conditions.append("degraded_provider")

    # Market behavior conditions
    volatility = market.get("volatility_percent", 0)
    if volatility >= 30:
        conditions.append("extreme_volatility")
    elif volatility >= 18:
        conditions.append("high_volatility")
    elif volatility >= 12:
        conditions.append("moderate_volatility")

    if "thin_market_history" in (market.get("flags") or []):
        conditions.append("thin_market")

    if market.get("sharp_signal"):
        conditions.append("sharp_money_signal")

    # Contextual conditions
    risk = contextual.get("risk", {})
    if risk.get("level") == "high":
        conditions.append("contextual_high_risk")
    elif risk.get("level") == "medium":
        conditions.append("contextual_medium_risk")

    # Learned classification
    learned = risk_mgmt.get("learned_classification")
    if learned == "learned_high_risk":
        conditions.append("learned_high_risk")
    elif learned == "smart_bet":
        conditions.append("smart_bet")
    elif learned == "history_thin":
        conditions.append("thin_history")

    # Violations
    violations = risk_mgmt.get("violations", [])
    for v in violations:
        if v not in conditions:
            conditions.append(v)

    return conditions if conditions else ["standard"]


def _extract_odds_from_audit(audit: dict[str, Any]) -> float:
    """Extract decimal odds from audit data."""
    market = audit.get("market", {})
    odds_1x2 = market.get("odds_at_prediction", {})
    one_x_two = odds_1x2.get("one_x_two", {})

    # Try to get odds for the primary pick
    primary = audit.get("no_prediction", {})
    if primary.get("status") == "published":
        picks = audit.get("picks", [])
        if picks:
            pick = picks[0]
            stake = pick.get("stake", {})
            if isinstance(stake, dict):
                odds = stake.get("decimal_odds")
                if odds:
                    return float(odds)

    # Fallback: use average of 1x2 odds
    odds_values = [
        v for v in [one_x_two.get("home"), one_x_two.get("draw"), one_x_two.get("away")]
        if v and v > 0
    ]
    if odds_values:
        return sum(odds_values) / len(odds_values)

    return 2.0  # Default even odds


def _parse_json(value: Any) -> dict[str, Any] | None:
    """Safely parse JSON string."""
    if not value or not isinstance(value, str):
        return None
    import json
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


# ── Integration Helper ─────────────────────────────────────────────────────────

def get_learned_risk_controls_for_pick(
    doc: dict[str, Any],
    pick: dict[str, Any],
    contextual_intelligence: dict[str, Any] | None = None,
    risk_report: dict[str, Any] | None = None,
) -> LearnedRiskControls:
    """Convenience function to get learned risk controls for a specific pick.

    Extracts risk conditions from the prediction context and returns
    dynamically learned confidence/stake caps.
    """
    contextual = contextual_intelligence or {}
    risk_mgmt = risk_report or {}
    market = contextual.get("market_behavior") or {}
    readiness = (doc.get("prediction_readiness") or {}).get("assurance", "full")

    # Extract risk conditions
    conditions = _extract_risk_conditions(
        risk_mgmt, contextual, market, readiness
    )

    # Get pick metadata
    pick_type = pick.get("type", "match_result")
    confidence = int(pick.get("confidence") or 50)
    league_tier = _estimate_league_tier(
        doc.get("league_name") or doc.get("tournament"),
        doc.get("country_name") or doc.get("category"),
    )
    decimal_odds = float(pick.get("decimal_odds") or pick.get("odds") or 2.0)

    return get_learned_risk_controls(
        risk_conditions=conditions,
        pick_type=pick_type,
        confidence=confidence,
        league_tier=league_tier,
        decimal_odds=decimal_odds,
    )

