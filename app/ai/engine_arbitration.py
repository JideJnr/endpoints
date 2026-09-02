"""
Dual-engine prediction arbitration
===================================
Both the deterministic (stats/Poisson) engine and the AI/LLM pipeline can now
independently predict the same match -- previously a shared 180-minute
cooldown meant whichever engine reached apply_prediction_state() first for a
match silently blocked the other one from ever getting recorded (see the
per-engine cooldown fix in app/utils/prediction_flow.py). Now both get
recorded as separate rows in prediction_history, each tagged with its own
`engine` column.

This module's job is narrow: once BOTH engines have an ungraded row for the
same match, decide which one is treated as THE prediction (is_final=1) --
for the dashboard, bet builder, and any user-facing win-rate stat. It never
deletes or blocks either row: both stay in prediction_history, both get
graded independently by the normal grading job, so each engine keeps
building its own real, honest accuracy track record regardless of which one
"won" arbitration on any given match. Downstream analytics/consumers that
should only see one call per match filter on is_final=1; anything computing
per-engine accuracy (get_engine_accuracy below) deliberately does NOT filter
on is_final, since a demoted row is still real graded evidence about that
engine.

Called from record_prediction() right after a row is inserted. Must never
raise -- a failure here must not undo or break the prediction that was just
successfully recorded.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from app.storage.db import db_conn

logger = logging.getLogger(__name__)

_OTHER_ENGINE = {"deterministic": "ai_llm", "ai_llm": "deterministic"}

# Below this many graded samples in a scope, that scope's win rate isn't
# trusted enough to decide anything on its own -- fall back to a broader
# scope, and ultimately to the safe default (deterministic, the incumbent
# system) when neither engine has enough history yet.
_MIN_TRUSTED_SAMPLES = 5
# How many graded samples until a scope's evidence is weighted at full
# strength (mirrors the damping used by weighted_signal_combination_memory).
_FULL_CONFIDENCE_SAMPLES = 15.0


def get_engine_accuracy(engine: str, league_name: str | None, pick_type: str | None) -> dict[str, Any]:
    """
    Real graded win rate for one engine, scoped as tightly as the data allows.

    Tries league+pick_type first (the most relevant comparison -- "who's
    better at match-result picks in this league"), falls back to
    pick_type-only, then engine-wide, stopping at the first scope with at
    least _MIN_TRUSTED_SAMPLES graded rows. Never raises; returns
    samples=0 if the engine has no graded history anywhere yet.
    """
    try:
        with db_conn(timeout=15) as conn:
            conn.row_factory = sqlite3.Row

            def _query(where_extra: str, params_extra: tuple) -> tuple[int, int]:
                row = conn.execute(
                    f"""
                    select count(*) as samples,
                           sum(case when result = 'win' then 1 else 0 end) as wins
                    from prediction_history
                    where coalesce(engine, 'deterministic') = ?
                      and result in ('win', 'loss')
                      {where_extra}
                    """,
                    (engine, *params_extra),
                ).fetchone()
                return int(row["samples"] or 0), int(row["wins"] or 0)

            scopes: list[tuple[str, str, tuple]] = []
            if league_name and pick_type:
                scopes.append(("league_and_pick_type", "and league_name = ? and pick_type = ?", (league_name, pick_type)))
            if pick_type:
                scopes.append(("pick_type_only", "and pick_type = ?", (pick_type,)))
            scopes.append(("engine_wide", "", ()))

            last_samples, last_wins, last_label = 0, 0, "engine_wide"
            for label, where_extra, params_extra in scopes:
                samples, wins = _query(where_extra, params_extra)
                last_samples, last_wins, last_label = samples, wins, label
                if samples >= _MIN_TRUSTED_SAMPLES:
                    return {
                        "samples": samples,
                        "wins": wins,
                        "win_rate": wins / samples,
                        "scope": label,
                    }
            return {
                "samples": last_samples,
                "wins": last_wins,
                "win_rate": (last_wins / last_samples) if last_samples else 0.0,
                "scope": last_label,
            }
    except Exception:
        logger.exception("get_engine_accuracy failed for engine=%s", engine)
        return {"samples": 0, "wins": 0, "win_rate": 0.0, "scope": "error"}


def _weighted_score(accuracy: dict[str, Any]) -> float:
    samples = int(accuracy.get("samples") or 0)
    if samples <= 0:
        return 0.0
    weight = min(1.0, samples / _FULL_CONFIDENCE_SAMPLES)
    return float(accuracy.get("win_rate") or 0.0) * weight


def arbitrate_dual_engine_prediction(*, match_id: str, league_name: str | None, just_recorded_engine: str) -> dict[str, Any] | None:
    """
    If the other engine also has a live (ungraded) prediction for this exact
    match, decide which of the two counts as the final pick. No-ops (returns
    None) if only one engine has weighed in so far -- there's nothing to
    arbitrate between yet, and the freshly inserted row's default is_final=1
    already stands.
    """
    other_engine = _OTHER_ENGINE.get(just_recorded_engine)
    if not other_engine or not match_id:
        return None
    try:
        with db_conn(timeout=15) as conn:
            conn.row_factory = sqlite3.Row

            def _latest_ungraded(engine: str) -> sqlite3.Row | None:
                return conn.execute(
                    """
                    select id, pick_type, selection, confidence
                    from prediction_history
                    where match_id = ?
                      and coalesce(engine, 'deterministic') = ?
                      and graded_at is null
                    order by datetime(created_at) desc, id desc
                    limit 1
                    """,
                    (match_id, engine),
                ).fetchone()

            mine = _latest_ungraded(just_recorded_engine)
            theirs = _latest_ungraded(other_engine)
            if not mine or not theirs:
                return None  # only one engine has a live prediction here so far

            agree = mine["pick_type"] == theirs["pick_type"] and mine["selection"] == theirs["selection"]
            if agree:
                # Same call, no real conflict -- keep the AI's row as final
                # when both weighed in (richer reasoning/audit trail attached),
                # deterministic's row otherwise. Either way this only affects
                # what's SHOWN as the pick; both rows still grade and both
                # still count toward each engine's own accuracy track record.
                ai_row = mine if just_recorded_engine == "ai_llm" else theirs
                det_row = theirs if just_recorded_engine == "ai_llm" else mine
                winner_id, loser_id = ai_row["id"], det_row["id"]
                reason = "agreement"
            else:
                pick_type = mine["pick_type"]
                mine_acc = get_engine_accuracy(just_recorded_engine, league_name, pick_type)
                theirs_acc = get_engine_accuracy(other_engine, league_name, pick_type)
                mine_score = _weighted_score(mine_acc)
                theirs_score = _weighted_score(theirs_acc)
                if mine_score == theirs_score == 0.0:
                    # Neither engine has enough graded history in this scope
                    # yet to trust either one over the other -- default to
                    # the deterministic engine, the proven incumbent.
                    winner_id = mine["id"] if just_recorded_engine == "deterministic" else theirs["id"]
                    loser_id = theirs["id"] if just_recorded_engine == "deterministic" else mine["id"]
                    reason = "no_track_record_yet_default_deterministic"
                elif mine_score >= theirs_score:
                    winner_id, loser_id = mine["id"], theirs["id"]
                    reason = "track_record"
                else:
                    winner_id, loser_id = theirs["id"], mine["id"]
                    reason = "track_record"

            conn.execute("update prediction_history set is_final = 1 where id = ?", (winner_id,))
            conn.execute("update prediction_history set is_final = 0 where id = ?", (loser_id,))
            conn.commit()
            return {"match_id": match_id, "winner_id": winner_id, "loser_id": loser_id, "agree": agree, "reason": reason}
    except Exception:
        logger.exception("arbitrate_dual_engine_prediction failed for match_id=%s", match_id)
        return None
