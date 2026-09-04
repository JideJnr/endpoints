"""
league_memory.queries
~~~~~~~~~~~~~~~~~~~~~
Domain-level analytical and grading queries for the league memory store.
Direct CRUD operations live in crud.py.
"""
from __future__ import annotations

import re
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.storage.db import (
    DB_PATH, _conn, close_db, connect_readonly_db, db_conn, get_db,
    _init_db, _init_db_unlocked, _ensure_column, _is_sqlite_lock,
    _DB_SCHEMA_READY, _DB_SCHEMA_LOCK, _run_schema_migrations,
    _run_legacy_backfills, _existing_schema_can_be_trusted,
    _ensure_prediction_history_columns,
)
from app.market.market_intent import classify_market_intent, grade_market_intent
from app.signal_combinations import build_signal_combination, live_context_from_prediction
from app.monitoring.prediction_audit import build_pick_audit, build_prediction_audit, grading_reason
from ._helpers import (
    normalize_league, _league_from_match, _country_from_match, _country_from_league,
    _is_country_like, _match_fingerprint, _match_1x2_odds_profile,
    _to_int, _to_float, _safe_json, _safe_float, _rate,
    _memory_row, _snapshot_memory_row, _match_row, _snapshot_row,
    _prediction_row, _decision_row, _get_passed_models, _standings_from_matches,
    _normalise_start_seconds, _datetime_to_seconds, _date_from_start,
    _sofa_ids_from_raw,
    _same_team, _close_match_row, _team_form_from_rows, _team_name,
    _grade_pick_for_match, _side_from_selection_and_match,
    _betbuilder_leg_key, _odds_band, _infer_betbuilder_pick_type,
    _decorate_betbuilder_selections,
    grade_prediction_row, update_prediction_result,
)
from .crud import _sofascore_ids_for_predictions, store_local_signal_outcomes, _aggregate_resolved_snapshots, _safe_mark_buffer_finished, _backfill_local_signal_outcomes_from_history
from .schema import _ensure_signal_combination_outcomes_table, _ensure_signal_outcomes_table

def get_league_memory(league: str | None = None) -> dict[str, Any]:
    _init_db()
    with _conn(timeout=15) as conn:
        if league:
            key = normalize_league(league)
            row = conn.execute(
                """
                select
                    league_key,
                    max(league_name) as league_name,
                    count(*) as samples,
                    sum(case when had_late_goal = 1 then 1 else 0 end) as late_goals,
                    avg(had_late_goal) as late_goal_rate
                from late_goal_snapshots
                where league_key = ? and had_late_goal is not null
                group by league_key
                """,
                (key,),
            ).fetchone()
            return _memory_row(row, key, league)

        rows = conn.execute(
            """
            select
                league_key,
                max(league_name) as league_name,
                count(*) as samples,
                sum(case when had_late_goal = 1 then 1 else 0 end) as late_goals,
                avg(had_late_goal) as late_goal_rate
            from late_goal_snapshots
            where had_late_goal is not null
            group by league_key
            order by samples desc, late_goal_rate desc
            """
        ).fetchall()
        return {"leagues": [_memory_row(row) for row in rows]}


def league_memory_for_match(match: dict[str, Any]) -> dict[str, Any]:
    return get_league_memory(_league_from_match(match))


def get_snapshot_memory(
    league: str | None = None,
    minute_bucket: str | None = None,
    score_state: str | None = None,
    min_samples: int = 1,
) -> dict[str, Any]:
    _init_db()
    with _conn() as conn:
        _aggregate_resolved_snapshots(conn)
        conn.commit()

    clauses = ["1 = 1"]
    params: list[Any] = []
    if league:
        clauses.append("league_key = ?")
        params.append(normalize_league(league))
    if minute_bucket:
        clauses.append("minute_bucket = ?")
        params.append(minute_bucket)
    if score_state:
        clauses.append("score_state = ?")
        params.append(score_state)
    where = " and ".join(clauses)

    with _conn() as conn:
        rows = conn.execute(
            f"""
            select
                league_key,
                max(league_name) as league_name,
                minute_bucket,
                score_state,
                sum(samples) as samples,
                sum(next_goal_hits) as next_goal_hits,
                sum(over_1_5_hits) as over_1_5_hits,
                sum(over_2_5_hits) as over_2_5_hits,
                sum(favorite_recovered_hits) as favorite_recovered_hits,
                sum(red_card_team_conceded_hits) as red_card_team_conceded_hits
            from snapshot_aggregates
            where {where}
            group by league_key, minute_bucket, score_state
            having sum(samples) >= ?
            order by samples desc, (sum(next_goal_hits) * 1.0 / sum(samples)) desc
            limit 200
            """,
            (*params, min_samples),
        ).fetchall()
    return {"snapshots": [_snapshot_memory_row(row) for row in rows]}


def late_goal_memory_signal(match: dict[str, Any]) -> dict[str, Any] | None:
    memory = league_memory_for_match(match)
    samples = memory.get("samples", 0)
    if samples < 2:
        return None
    rate = memory.get("late_goal_rate", 0)
    # Smoothed around 50%, so tiny samples help but do not dominate.
    adjusted_rate = ((rate * samples) + 1.0) / (samples + 2)
    impact = round((adjusted_rate - 0.5) * 24, 2)
    if abs(impact) < 1.5:
        return None
    return {
        "name": "league_memory_late_goal",
        "value": {
            "league": memory.get("league_name"),
            "samples": samples,
            "late_goal_rate": round(rate, 3),
            "smoothed_rate": round(adjusted_rate, 3),
        },
        "impact": impact,
    }


def weighted_prediction_memory(
    match: dict[str, Any],
    pick_type: str | None,
    selection: str | None,
) -> dict[str, Any]:
    """Blend graded prediction performance: tournament first, then country, then global."""
    _init_db()
    league = _league_from_match(match)
    country = _country_from_match(match, league)
    pick_type = pick_type or ""
    selection = selection or ""

    with _conn() as conn:
        scopes = {
            "tournament": _prediction_scope_stats(
                conn,
                "league_name = ? and pick_type = ? and selection = ?",
                (league, pick_type, selection),
            ),
            "country": _prediction_scope_stats(
                conn,
                "country_name = ? and pick_type = ? and selection = ?",
                (country, pick_type, selection),
            ),
            "global": _prediction_scope_stats(
                conn,
                "pick_type = ? and selection = ?",
                (pick_type, selection),
            ),
        }

    base_weights = {"tournament": 0.55, "country": 0.30, "global": 0.15}
    weighted = 0.0
    total_weight = 0.0
    for scope, stats in scopes.items():
        samples = stats["samples"]
        if samples <= 0:
            continue
        sample_factor = min(1.0, samples / 30)
        weight = base_weights[scope] * sample_factor
        weighted += stats["win_rate"] * weight
        total_weight += weight

    blended = round(weighted / total_weight * 100, 1) if total_weight else None
    impact = 0
    if blended is not None:
        impact = max(-8, min(8, round((blended - 52) / 4)))

    return {
        "league": league,
        "country": country,
        "pick_type": pick_type,
        "selection": selection,
        "weights": base_weights,
        "scopes": scopes,
        "blended_win_rate": blended,
        "confidence_adjustment": impact,
    }


def weighted_candidate_memory(
    match: dict[str, Any],
    pick_type: str | None,
    selection: str | None = None,
) -> dict[str, Any]:
    """Learning memory for secondary/overlay candidates by tournament, country, global."""
    _init_db()
    league = _league_from_match(match)
    country = _country_from_match(match, league)
    pick_type = pick_type or ""
    selection = selection or ""
    with _conn() as conn:
        scopes = {
            "tournament": _candidate_scope_stats(conn, "league_name = ? and pick_type = ?", (league, pick_type)),
            "country": _candidate_scope_stats(conn, "country_name = ? and pick_type = ?", (country, pick_type)),
            "global": _candidate_scope_stats(conn, "pick_type = ?", (pick_type,)),
        }
        if selection:
            scopes["selection_global"] = _candidate_scope_stats(
                conn,
                "pick_type = ? and selection = ?",
                (pick_type, selection),
            )

    base_weights = {"tournament": 0.50, "country": 0.30, "global": 0.20, "selection_global": 0.20}
    weighted = 0.0
    total_weight = 0.0
    for scope, stats in scopes.items():
        samples = stats["samples"]
        if samples <= 0:
            continue
        sample_factor = min(1.0, samples / 20)
        weight = base_weights[scope] * sample_factor
        weighted += stats["win_rate"] * weight
        total_weight += weight
    blended = round(weighted / total_weight * 100, 1) if total_weight else None
    return {
        "league": league,
        "country": country,
        "pick_type": pick_type,
        "selection": selection,
        "scopes": scopes,
        "blended_win_rate": blended,
        "allow": blended is None or blended >= 50,
        "confidence_adjustment": 0 if blended is None else max(-10, min(8, round((blended - 52) / 4))),
    }


def _candidate_scope_stats(conn: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> dict[str, Any]:
    row = conn.execute(
        f"""
        select
            count(*) as samples,
            sum(case when result = 'win' then 1 else 0 end) as wins,
            sum(case when result = 'loss' then 1 else 0 end) as losses
        from (
            select *
            from (
                select
                    pch.*,
                    row_number() over (
                        partition by match_id, pick_type, selection, role
                        order by datetime(coalesce(graded_at, created_at)) desc, id desc
                    ) as rn
                from prediction_candidate_history pch
                where graded_at is not null and result in ('win', 'loss')
            )
            where rn = 1
        )
        where {where}
        """,
        params,
    ).fetchone()
    samples = int(row["samples"] or 0) if row else 0
    wins = int(row["wins"] or 0) if row else 0
    losses = int(row["losses"] or 0) if row else 0
    graded = wins + losses
    return {
        "samples": graded,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / graded, 3) if graded else 0.0,
    }


def weighted_finished_match_memory(match: dict[str, Any]) -> dict[str, Any]:
    """Blend finished outcomes by tournament, country, global, and similar 1X2 odds."""
    _init_db()
    league = _league_from_match(match)
    country = _country_from_match(match, league)
    odds_profile = _match_1x2_odds_profile(match)
    with _conn() as conn:
        scopes = _finished_memory_scopes(conn, league, country, odds_profile)

    base_weights = {
        "tournament_odds": 0.50,
        "country_odds": 0.30,
        "global_odds": 0.20,
        "tournament": 0.25,
        "country": 0.15,
        "global": 0.10,
    }
    totals = {
        "home_win_rate": 0.0,
        "draw_rate": 0.0,
        "away_win_rate": 0.0,
        "over_1_5_rate": 0.0,
        "over_2_5_rate": 0.0,
        "over_3_5_rate": 0.0,
        "btts_rate": 0.0,
        "avg_goals": 0.0,
    }
    total_weight = 0.0
    effective_weights: dict[str, float] = {}
    for scope, stats in scopes.items():
        samples = stats["samples"]
        if samples <= 0:
            continue
        sample_factor = min(1.0, samples / 50)
        weight = base_weights[scope] * sample_factor
        effective_weights[scope] = round(weight, 3)
        total_weight += weight
        for key in totals:
            totals[key] += float(stats[key] or 0) * weight

    blended = {
        key: round(value / total_weight, 3)
        for key, value in totals.items()
    } if total_weight else {}

    return {
        "league": league,
        "country": country,
        "odds_profile": odds_profile,
        "weights": base_weights,
        "effective_weights": effective_weights,
        "scopes": scopes,
        "blended": blended,
        "samples": sum(stats["samples"] for stats in scopes.values()),
    }


def close_match_strength_context(match: dict[str, Any], limit: int = 8) -> dict[str, Any]:
    """
    Compact historical context for matches close to this one.
    Uses our own DB first: same teams, same league/country, and similar 1X2 odds.
    """
    _init_db()
    league = _league_from_match(match)
    country = _country_from_match(match, league)
    home = _team_name(match, "home") or ""
    away = _team_name(match, "away") or ""
    odds_profile = _match_1x2_odds_profile(match)
    with _conn() as conn:
        team_rows = conn.execute(
            """
            select home_team, away_team, final_home_goals, final_away_goals, league_name, country_name, start_time
            from matches
            where is_finished = 1
              and final_home_goals is not null
              and final_away_goals is not null
              and (
                    lower(home_team) in (?, ?)
                 or lower(away_team) in (?, ?)
              )
            order by last_seen_at desc
            limit ?
            """,
            (str(home or "").lower(), str(away or "").lower(), str(home or "").lower(), str(away or "").lower(), max(limit * 2, 12)),
        ).fetchall()
        league_stats = _finished_scope_stats(conn, "m.league_name = ?", (league,))
        country_stats = _finished_scope_stats(conn, "m.country_name = ?", (country,))
        odds_stats = _finished_scope_stats(conn, "{odds_filter}", (), odds_profile) if odds_profile else {
            "samples": 0,
            "home_win_rate": 0.0,
            "draw_rate": 0.0,
            "away_win_rate": 0.0,
            "over_1_5_rate": 0.0,
            "over_2_5_rate": 0.0,
            "over_3_5_rate": 0.0,
            "btts_rate": 0.0,
            "avg_goals": 0.0,
            "odds_filtered": False,
        }

    team_matches = [_close_match_row(row, home, away) for row in team_rows[:limit]]
    home_form = _team_form_from_rows(team_rows, home)
    away_form = _team_form_from_rows(team_rows, away)
    strength_delta = round((home_form.get("points_per_game", 0.0) - away_form.get("points_per_game", 0.0)), 3)
    return {
        "league": league,
        "country": country,
        "home_team": home,
        "away_team": away,
        "odds_profile": odds_profile,
        "team_matches": team_matches,
        "team_samples": len(team_matches),
        "home_recent_record": home_form,
        "away_recent_record": away_form,
        "strength_delta_ppg": strength_delta,
        "league_outcomes": league_stats,
        "country_outcomes": country_stats,
        "similar_odds_outcomes": odds_stats,
        "samples": int(league_stats.get("samples") or 0) + int(country_stats.get("samples") or 0) + int(odds_stats.get("samples") or 0) + len(team_matches),
    }


def _finished_memory_scopes(
    conn: sqlite3.Connection,
    league: str,
    country: str,
    odds_profile: dict[str, float | str] | None,
) -> dict[str, dict[str, Any]]:
    scopes: dict[str, dict[str, Any]] = {}
    if odds_profile:
        scopes["tournament_odds"] = _finished_scope_stats(
            conn,
            "m.league_name = ? and {odds_filter}",
            (league,),
            odds_profile,
        )
        scopes["country_odds"] = _finished_scope_stats(
            conn,
            "m.country_name = ? and {odds_filter}",
            (country,),
            odds_profile,
        )
        scopes["global_odds"] = _finished_scope_stats(conn, "{odds_filter}", (), odds_profile)

    plain = {
        "tournament": _finished_scope_stats(conn, "m.league_name = ?", (league,)),
        "country": _finished_scope_stats(conn, "m.country_name = ?", (country,)),
        "global": _finished_scope_stats(conn, "1 = 1", ()),
    }
    # Only let broad memory help when the odds-similar scope is thin.
    fallback_pairs = (
        ("tournament_odds", "tournament"),
        ("country_odds", "country"),
        ("global_odds", "global"),
    )
    for odds_key, plain_key in fallback_pairs:
        if (scopes.get(odds_key) or {}).get("samples", 0) < 12:
            scopes[plain_key] = plain[plain_key]
    if not odds_profile:
        scopes.update(plain)
    return scopes


def grade_prediction(prediction_id: int, final_home: int, final_away: int) -> dict[str, Any]:
    _init_db()
    with _conn() as conn:
        row = conn.execute("select * from prediction_history where id = ?", (prediction_id,)).fetchone()
        if not row:
            return {"graded": False, "reason": "not found"}
        result, grade_info = grade_prediction_row(row, final_home, final_away)
        models = _safe_json(row["models_json"] if "models_json" in row.keys() else "{}", {})
        grade_info["passed_models"] = _get_passed_models(models, result)
        update_prediction_result(conn, prediction_id, result, final_home, final_away, grade_info)
        conn.commit()
    _grade_decision_logs_by_ids([str(row["match_id"])], final_home, final_away)
    _store_signal_outcome_for_row(row, result)
    _grade_ai_specialist_contributions(row, result)
    return {"graded": True, "id": prediction_id, "result": result, "final_home": final_home, "final_away": final_away}


def _grade_ai_specialist_contributions(row: sqlite3.Row, result: str) -> None:
    """Feed graded outcomes back into ai_prediction_pipeline's specialist weights.

    Only ai_prediction_pipeline.run_ai_prediction_with_fallback's picks carry a
    reasoning_context.analysts list (stashed inside audit_json since
    prediction_history has no dedicated column for it) — the deterministic
    ensemble path has none, so this is a no-op for those rows.
    """
    if result not in {"win", "loss"}:
        return
    try:
        audit = _safe_json(row["audit_json"] if "audit_json" in row.keys() else "{}", {})
        analysts = (audit or {}).get("reasoning_context", {}).get("analysts") if isinstance(audit, dict) else None
        if not analysts:
            return
        from app.ai.ai_prediction_pipeline import grade_specialist_contributions
        grade_specialist_contributions(
            {"analysts": analysts},
            result,
            league=row["league_name"] if "league_name" in row.keys() else None,
            pick_type=row["pick_type"] if "pick_type" in row.keys() else None,
        )
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("league_memory", "grade_ai_specialist_contributions_error", exc)


def _grade_candidate_row(row: sqlite3.Row, final_home: int, final_away: int) -> str:
    pick_type = row["pick_type"]
    selection = row["selection"]
    pt = str(pick_type or "").lower()
    sel = str(selection or "").lower()
    # The shared-grid live picks in _live_grid_projection_picks() are tagged
    # with a "_grid" suffix (live_next_goal_grid, live_no_goal_grid,
    # live_total_goals_grid, live_match_winner_grid, live_btts_grid) so they
    # can be told apart from the older independent-heuristic picks below.
    # None of the pt == checks below matched that suffix, so every grid pick
    # silently fell through to the generic final-score-only grader at the
    # bottom -- which either voids them or (for market families that need to
    # know the score AT PICK TIME, not just the final score) grades them
    # wrong. Stripping the suffix here routes grid picks through the exact
    # same live-context-aware logic as their heuristic counterparts.
    pt_base = pt[:-5] if pt.endswith("_grid") else pt
    if pt_base == "live_team_to_score":
        try:
            context = json.loads(row["context_json"] or "{}")
        except Exception:
            context = {}
        start_home = _to_int(context.get("score_home"), 0)
        start_away = _to_int(context.get("score_away"), 0)
        home_delta = final_home - start_home
        away_delta = final_away - start_away
        side = _side_from_selection_and_match(sel, row["match_name"])
        # side can also come back "ambiguous" (both team names plausibly
        # matched the selection text) — that must be refused, not treated
        # as a guessable side.
        if side not in ("home", "away"):
            return "void"
        picked_delta = home_delta if side == "home" else away_delta
        other_delta = away_delta if side == "home" else home_delta
        if picked_delta > 0 and other_delta == 0:
            return "win"
        if picked_delta == 0 and other_delta > 0:
            return "loss"
        return "void"
    if pt_base in {"live_next_goal", "live_no_goal"}:
        try:
            context = json.loads(row["context_json"] or "{}")
        except Exception:
            context = {}
        start_home = _to_int(context.get("score_home"), 0)
        start_away = _to_int(context.get("score_away"), 0)
        start_total = start_home + start_away
        final_total = final_home + final_away
        # "No More Goals" is the OPPOSITE bet from "a team scores next" --
        # this used to have no branch for it at all (there was no way to
        # even publish that pick before this fix) and would have graded it
        # backwards had it existed, since the old logic only ever asked "did
        # any goal happen". Win only if the score never moved again.
        if pt_base == "live_no_goal" or "no more goal" in sel or "no goal" in sel or sel.strip() in {"none", "no more goals"}:
            return "win" if final_total == start_total else "loss"
        # Selection names a specific team ("Arsenal to score next") -- grade
        # on THAT team's delta, same as live_team_to_score, not "any goal by
        # either side" like the old fallback below did.
        side = _side_from_selection_and_match(sel, row["match_name"])
        if side in ("home", "away"):
            home_delta = final_home - start_home
            away_delta = final_away - start_away
            picked_delta = home_delta if side == "home" else away_delta
            other_delta = away_delta if side == "home" else home_delta
            if picked_delta > 0 and other_delta == 0:
                return "win"
            if picked_delta == 0 and other_delta > 0:
                return "loss"
            return "void"
        if "next goal" in sel:
            # Legacy generic phrasing with no identifiable team in the
            # selection text -- keep the old "did any goal happen" behaviour
            # as a last resort rather than voiding rows we used to grade.
            return "win" if final_total > start_total else "loss"
        return "void"
    if pt_base == "live_total_goals":
        try:
            context = json.loads(row["context_json"] or "{}")
        except Exception:
            context = {}
        start_total = _to_int(context.get("score_home"), 0) + _to_int(context.get("score_away"), 0)
        final_total = final_home + final_away
        match = re.search(r"(over|under)\s+(\d+(?:\.\d+)?)", sel)
        if match:
            line = float(match.group(2))
            if match.group(1) == "over":
                return "win" if final_total > line else "loss"
            return "win" if final_total < line else "loss"
        return "void"
    if pt_base == "live_match_winner":
        return _grade_pick_for_match("match_result", selection, final_home, final_away, row["match_name"])
    if pt_base == "live_double_chance":
        # Double chance is fully determined by the final score alone (no
        # pick-time context needed, unlike next-goal/no-goal/totals above),
        # so this can go straight through the same team-name-aware grader
        # the plain "double_chance" pick type already uses.
        return _grade_pick_for_match("double_chance", selection, final_home, final_away, row["match_name"])
    if pt_base == "live_double_chance":
        return _grade_pick_for_match("double_chance", selection, final_home, final_away, row["match_name"])
    if pt_base in {"live_btts", "btts"}:
        # BTTS doesn't need the score-at-pick-time context that the other
        # live markets do -- "did both teams score at all" is answerable
        # from the final score alone.
        both_scored = final_home > 0 and final_away > 0
        no_dir = bool(re.search(r"\bno\b", sel))
        yes_dir = bool(re.search(r"\byes\b", sel))
        if no_dir and not yes_dir:
            return "win" if not both_scored else "loss"
        if yes_dir:
            return "win" if both_scored else "loss"
        return "void"
    if pt_base == "consensus_longshot_value":
        try:
            context = json.loads(row["context_json"] or "{}")
        except Exception:
            context = {}
        intent = context.get("market_intent") if isinstance(context.get("market_intent"), dict) else {}
        graded = grade_market_intent(intent, selection, final_home, final_away, row["match_name"])
        if graded != "void":
            return graded
        return _grade_pick_for_match("match_result", selection, final_home, final_away, row["match_name"])
    if pt_base == "value_overlay":
        pick_type = "value_bet"
    return _grade_pick_for_match(pick_type, selection, final_home, final_away, row["match_name"])


def _grading_reason_for_candidate_row(row: sqlite3.Row, final_home: int, final_away: int, result: str | None = None) -> dict[str, Any]:
    context = _safe_json(row["context_json"] if "context_json" in row.keys() else "{}", {})
    intent = context.get("market_intent") if isinstance(context.get("market_intent"), dict) else None
    info = grading_reason(row["pick_type"], row["selection"], final_home, final_away, row["match_name"], intent)
    if result and info.get("result") != result:
        info["result"] = result
        info["reason"] = f"{row['selection']} graded {result} with candidate-specific live/context rule on final score {final_home}-{final_away}."
    return info


def _store_signal_outcome_for_row(row: sqlite3.Row, result: str) -> None:
    """Store graded decision-signal outcomes in local memory, with Mongo as optional mirror."""
    try:
        payload = _signal_outcome_payload_for_row(row, result)
        if not payload:
            return
        _store_signal_outcome_payload(payload)
    except Exception:
        pass


def _signal_outcome_payload_for_row(row: sqlite3.Row, result: str) -> dict[str, Any] | None:
    """Build signal-outcome payload without opening another SQLite writer."""
    if result not in {"win", "loss"}:
        return None
    try:
        try:
            from app.monitoring.self_learner import _decision_signals_for_row

            signals = _decision_signals_for_row(row)
        except Exception:
            signals = _safe_json(row["signals_json"] if "signals_json" in row.keys() else "[]", [])
        if not signals:
            return None
        return {
            "match_id": str(row["match_id"]),
            "match_name": row["match_name"] if "match_name" in row.keys() else None,
            "tournament": row["league_name"] if "league_name" in row.keys() else None,
            "country": row["country_name"] if "country_name" in row.keys() else None,
            "match_date": str(row["created_at"] or "")[:10] if "created_at" in row.keys() else None,
            "signals": signals,
            "result": result,
            "pick_type": row["pick_type"] if "pick_type" in row.keys() else None,
            "selection": row["selection"] if "selection" in row.keys() else None,
            "confidence": int(row["confidence"] or 0) if "confidence" in row.keys() else None,
            "prediction_mode": row["prediction_mode"] if "prediction_mode" in row.keys() else None,
            "live_context": _safe_json(row["live_context_json"] if "live_context_json" in row.keys() else "{}", {}),
        }
    except Exception:
        return None


def _store_signal_outcome_payload(payload: dict[str, Any]) -> None:
    """Store signal outcome payload after the grading write transaction commits."""
    base_payload = {
        key: payload.get(key)
        for key in (
            "match_id",
            "match_name",
            "tournament",
            "country",
            "match_date",
            "signals",
            "result",
            "pick_type",
            "selection",
            "confidence",
        )
    }
    store_local_signal_outcomes(**base_payload)
    store_local_signal_combination_outcome(
        **base_payload,
        prediction_mode=payload.get("prediction_mode"),
        live_context=payload.get("live_context"),
    )
    try:
        from app.storage.mongo_store import is_configured, store_signal_outcomes

        if is_configured():
            store_signal_outcomes(**base_payload)
    except Exception:
        pass


def store_local_signal_combination_outcome(
    match_id: str,
    match_name: str | None,
    tournament: str | None,
    country: str | None,
    match_date: str | None,
    signals: list[dict[str, Any]],
    result: str,
    pick_type: str | None,
    selection: str | None,
    confidence: int | None,
    prediction_mode: str | None = None,
    live_context: dict[str, Any] | None = None,
) -> int:
    """Persist the whole signal set as one learnable combination outcome."""
    if result not in {"win", "loss"} or not signals:
        return 0
    resolved_mode = prediction_mode or ("live" if any(str((s or {}).get("name") or "").startswith("live_") for s in signals if isinstance(s, dict)) else "prematch")
    live_context = live_context or live_context_from_prediction({"prediction_mode": resolved_mode, "signals": signals, "score": {}})
    combo = build_signal_combination(
        signals=signals,
        pick_type=pick_type,
        selection=selection,
        prediction_mode=resolved_mode,
        live_context=live_context,
    )
    if not combo.get("key"):
        return 0
    with _conn() as conn:
        _ensure_signal_combination_outcomes_table(conn)
        conn.execute(
            """
            insert into signal_combination_outcomes (
                combination_key, combination_json, signal_names_json,
                match_id, match_name, tournament, country, match_date,
                result, pick_type, selection, confidence, prediction_mode,
                live_context_json
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(match_id, pick_type, selection, combination_key) do update set
                combination_json = excluded.combination_json,
                signal_names_json = excluded.signal_names_json,
                match_name = excluded.match_name,
                tournament = excluded.tournament,
                country = excluded.country,
                match_date = excluded.match_date,
                result = excluded.result,
                confidence = excluded.confidence,
                prediction_mode = excluded.prediction_mode,
                live_context_json = excluded.live_context_json,
                recorded_at = current_timestamp
            """,
            (
                combo["key"],
                json.dumps(combo.get("payload") or {}),
                json.dumps(combo.get("signal_names") or []),
                match_id,
                match_name,
                tournament,
                country,
                match_date,
                result,
                pick_type,
                selection,
                confidence,
                resolved_mode,
                json.dumps(live_context),
            ),
        )
        conn.commit()
    return 1


def _grade_candidate_predictions_for_match(
    match_id: str,
    sofa_ids: list[str],
    final_home: int,
    final_away: int,
) -> int:
    keys = [match_id, *sofa_ids]
    placeholders = ",".join("?" for _ in keys)
    signal_payloads: list[dict[str, Any]] = []
    with _conn() as conn:
        rows = conn.execute(
            f"""
            select *
            from prediction_candidate_history
            where graded_at is null and match_id in ({placeholders})
            """,
            tuple(keys),
        ).fetchall()
        for row in rows:
            result = _grade_candidate_row(row, final_home, final_away)
            grade_info = _grading_reason_for_candidate_row(row, final_home, final_away, result)
            conn.execute(
                """
                update prediction_candidate_history
                set result = ?, final_home = ?, final_away = ?, grading_reason_json = ?, graded_at = current_timestamp
                where id = ?
                """,
                (result, final_home, final_away, json.dumps(grade_info), row["id"]),
            )
            payload = _signal_outcome_payload_for_row(row, result)
            if payload:
                signal_payloads.append(payload)
        conn.commit()
    for payload in signal_payloads:
        _store_signal_outcome_payload(payload)
    return len(rows)


def grade_predictions_for_date(match_date: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    _init_db()
    finished = {_numeric_id(e["id"]): e for e in events if (e.get("status") or {}).get("type") == "finished"}
    if not finished:
        return {"graded": 0, "skipped": 0, "no_finished_events": True}

    with _conn() as conn:
        _ensure_buffer_tables(conn)
        # Match by the actual match date from the buffer first, fall back to
        # date(created_at) so predictions made on a different calendar day than
        # the match (e.g. late-night predictions for next-day fixtures) are
        # still picked up.
        rows = conn.execute(
            """
            select ph.*
            from prediction_history ph
            left join match_buffer mb on mb.match_id = ph.match_id
            left join future_match_buffer fb on fb.match_id = ph.match_id
            where ph.graded_at is null
              and (
                coalesce(mb.match_date, fb.match_date, date(ph.created_at)) = ?
              )
            """,
            (match_date,),
        ).fetchall()
        candidate_rows = conn.execute(
            """
            select distinct pch.match_id
            from prediction_candidate_history pch
            left join match_buffer mb on mb.match_id = pch.match_id
            left join future_match_buffer fb on fb.match_id = pch.match_id
            where pch.graded_at is null
              and (
                coalesce(mb.match_date, fb.match_date, date(pch.created_at)) = ?
              )
            """,
            (match_date,),
        ).fetchall()
        all_match_ids = list({str(row["match_id"]) for row in rows} | {str(row["match_id"]) for row in candidate_rows})
        sofa_ids_by_match = _sofascore_ids_for_predictions(conn, all_match_ids)

    graded = skipped = candidate_graded = 0
    for row in rows:
        match_id = str(row["match_id"])
        event = finished.get(_numeric_id(match_id))
        sofa_ids = sofa_ids_by_match.get(match_id, [])
        if not event:
            event = next((finished.get(sofa_id) for sofa_id in sofa_ids if finished.get(sofa_id)), None)
        if not event:
            skipped += 1
            continue
        score = event.get("score") or {}
        final_home = _to_int(score.get("home"), 0)
        final_away = _to_int(score.get("away"), 0)
        candidate_graded += _grade_candidate_predictions_for_match(match_id, sofa_ids, final_home, final_away)
        _grade_decision_logs_by_ids([match_id, *sofa_ids], final_home, final_away)
        result, grade_info = grade_prediction_row(row, final_home, final_away)
        models = _safe_json(row["models_json"] if "models_json" in row.keys() else "{}", {})
        grade_info["passed_models"] = _get_passed_models(models, result)
        with _conn() as conn:
            update_prediction_result(conn, row["id"], result, final_home, final_away, grade_info)
            conn.commit()
        _store_signal_outcome_for_row(row, result)

        # Record outcome for the probability learner
        try:
            from app.models.probability_learner import learn_probabilities
            from app.enrichment.signal_aggregator import normalize_signal

            signals_json = row.get("signals_json") or "[]"
            signals = json.loads(signals_json) if signals_json else []
            normalized_signals = []
            for sig in signals:
                name = sig.get("name") or sig.get("signal_name") or ""
                value = sig.get("value") or sig.get("signal_value") or 0
                normalized = normalize_signal(name, value)
                normalized_signals.append(normalized)

            learn_probabilities(
                signals=normalized_signals,
                result=result,
                pick_type=row.get("pick_type") or "match_result",
                league_key="__global__",
                confidence=float(row.get("confidence") or 0.5),
            )
        except Exception:
            pass

        graded += 1

    primary_ids = {str(row["match_id"]) for row in rows}
    for match_id in all_match_ids:
        if match_id in primary_ids:
            continue
        event = finished.get(_numeric_id(match_id))
        sofa_ids = sofa_ids_by_match.get(match_id, [])
        if not event:
            event = next((finished.get(sofa_id) for sofa_id in sofa_ids if finished.get(sofa_id)), None)
        if not event:
            continue
        score = event.get("score") or {}
        final_home = _to_int(score.get("home"), 0)
        final_away = _to_int(score.get("away"), 0)
        candidate_graded += _grade_candidate_predictions_for_match(match_id, sofa_ids, final_home, final_away)
        _grade_decision_logs_by_ids([match_id, *sofa_ids], final_home, final_away)

    return {"graded": graded, "candidate_graded": candidate_graded, "skipped": skipped, "date": match_date}


def _numeric_id(raw: Any) -> str:
    """Extract the numeric event id from any match_id form.

    Handles:
      sr:match:72180092                 -> 72180092
      competition:eliteserien:15260867 -> 15260867
      72180092                         -> 72180092
    """
    s = str(raw or "").strip()
    if s.startswith("sr:match:"):
        return s[len("sr:match:"):]
    parts = s.split(":")
    if len(parts) >= 2 and parts[-1].isdigit():
        return parts[-1]
    return s


def grade_overdue_predictions(hours_after_kickoff: float = 2.0, limit: int = 300) -> dict[str, Any]:
    """Grade every ungraded match that has finished.

    A match becomes a grading candidate as soon as it has **kicked off** — we no
    longer wait an arbitrary number of hours after kickoff.  The actual gate is
    the finished-status check from the result providers (SportyBet tried first,
    then SofaScore): only matches reported as ``finished`` are graded, so live
    or not-yet-played matches are safely skipped.  This guarantees that *all*
    finished-but-ungraded matches get graded on the next cycle.

    ``hours_after_kickoff`` is retained only for API compatibility and no longer
    delays grading.
    """
    _init_db()
    import time as _time

    now_seconds = _time.time()
    # Candidate = any match that has kicked off.  The finished-status check
    # inside the Sporty/Sofa lookups is what actually decides gradability.
    cutoff_seconds = now_seconds
    rows = _pending_matches_for_grading(cutoff_seconds, limit)
    if not rows:
        return {
            "status": "ok",
            "checked": 0,
            "eligible": 0,
            "graded": 0,
            "candidate_graded": 0,
            "decision_graded": 0,
            "still_live": 0,
            "not_found": 0,
            "skipped": 0,
            "errors": [],
        }

    graded = candidate_graded = decision_graded = still_live = not_found = skipped = 0
    sporty_graded = sofa_graded = 0
    errors: list[str] = []
    already_graded: set[str] = set()

    # 1) Sporty result checker. It can grade even when buffer/Mongo state was
    # lost, because the result id matches our Sporty match id.
    try:
        from app.data_clients.sportybet_client import fetch_results

        known_starts = [
            _normalise_start_seconds(row["start_time"]) or _datetime_to_seconds(row["first_seen"])
            for row in rows
        ]
        known_starts = [ts for ts in known_starts if ts]
        start_ms = int((min(known_starts) - 3 * 3600) * 1000) if known_starts else int((now_seconds - 72 * 3600) * 1000)
        end_ms = int((now_seconds + 30 * 60) * 1000)
        sporty_results = fetch_results(start_ms, end_ms, count=max(500, limit))
        sporty_by_id = {
            _numeric_id(result.get("id")): result
            for result in sporty_results
            if (result.get("score") or {}).get("home") is not None
            and (result.get("score") or {}).get("away") is not None
        }
        for row in rows:
            match_id = str(row["match_id"])
            result = sporty_by_id.get(_numeric_id(match_id))
            if not result:
                continue
            score = result.get("score") or {}
            counts = _grade_match_predictions_by_ids(
                match_id=match_id,
                linked_ids=_sofa_ids_from_raw(row["raw_enriched"]),
                final_home=_to_int(score.get("home"), 0),
                final_away=_to_int(score.get("away"), 0),
            )
            if counts["primary"] or counts["candidate"] or counts.get("decisions"):
                graded += counts["primary"]
                candidate_graded += counts["candidate"]
                decision_graded += counts.get("decisions", 0)
                sporty_graded += counts["primary"]
                already_graded.add(match_id)
                _safe_mark_buffer_finished(match_id, score.get("home"), score.get("away"))
    except Exception as exc:
        errors.append(f"sporty_results: {exc}")

    # 2) SofaScore fallback and live guard. If SofaScore says the match is live,
    # do not grade it even if kickoff+2h has passed.
    try:
        from app.data_clients.sofascore_client import fetch_all_scheduled_events, fetch_event, fetch_live_events

        try:
            live_events = fetch_live_events()
        except Exception:
            live_events = []
        live_ids = {str(event.get("id")) for event in live_events}
        by_date: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            match_id = str(row["match_id"])
            if match_id in already_graded:
                continue
            match_date = row["match_date"] or _date_from_start(row["start_time"]) or str(row["first_seen"] or "")[:10]
            if match_date:
                by_date[str(match_date)].append(row)

        for match_date, date_rows in by_date.items():
            try:
                events = fetch_all_scheduled_events(match_date)
            except Exception as exc:
                errors.append(f"sofascore {match_date}: {exc}")
                skipped += len(date_rows)
                continue
            event_by_id = {_numeric_id(event.get("id")): event for event in events}
            for row in date_rows:
                match_id = str(row["match_id"])
                sofa_ids = _sofa_ids_from_raw(row["raw_enriched"])
                _row_sofa = str(row["sofascore_id"]) if row["sofascore_id"] else None
                if _row_sofa and _row_sofa not in sofa_ids:
                    sofa_ids.append(_row_sofa)
                event = event_by_id.get(_numeric_id(match_id))
                if not event:
                    event = next((event_by_id.get(sofa_id) for sofa_id in sofa_ids if event_by_id.get(sofa_id)), None)
                if not event and sofa_ids:
                    for sofa_id in sofa_ids:
                        try:
                            direct_event = fetch_event(sofa_id)
                        except Exception:
                            direct_event = {}
                        if direct_event:
                            event = direct_event
                            break
                if not event:
                    not_found += 1
                    continue
                status_type = str(((event.get("status") or {}).get("type")) or "").lower()
                event_id = str(event.get("id"))
                if status_type in {"inprogress", "live"} or event_id in live_ids:
                    still_live += 1
                    continue
                if status_type != "finished":
                    skipped += 1
                    continue
                score = event.get("score") or {}
                counts = _grade_match_predictions_by_ids(
                    match_id=match_id,
                    linked_ids=sofa_ids,
                    final_home=_to_int(score.get("home"), 0),
                    final_away=_to_int(score.get("away"), 0),
                )
                graded += counts["primary"]
                candidate_graded += counts["candidate"]
                decision_graded += counts.get("decisions", 0)
                sofa_graded += counts["primary"]
                _safe_mark_buffer_finished(match_id, score.get("home"), score.get("away"))
    except Exception as exc:
        errors.append(f"sofascore_results: {exc}")

    return {
        "status": "ok",
        "checked": len(rows),
        "eligible": len(rows),
        "graded": graded,
        "candidate_graded": candidate_graded,
        "decision_graded": decision_graded,
        "sporty_graded": sporty_graded,
        "sofascore_graded": sofa_graded,
        "still_live": still_live,
        "not_found": not_found,
        "skipped": skipped,
        "errors": errors[:5],
    }


def check_and_grade_match_result(match_id: str, hours_back: int = 72) -> dict[str, Any]:
    """Check SportyBet/SofaScore for one match result and grade all related rows."""
    _init_db()
    import time as _time

    match_id = str(match_id)
    with _conn() as conn:
        _ensure_buffer_tables(conn)
        row = conn.execute(
            """
            select ? as match_id,
                   coalesce(mb.match_date, fb.match_date) as match_date,
                   coalesce(mb.start_time, fb.start_time) as start_time,
                   coalesce(mb.raw_enriched, fb.raw_enriched) as raw_enriched
            from (select 1) seed
            left join match_buffer mb on mb.match_id = ?
            left join future_match_buffer fb on fb.match_id = ?
            """,
            (match_id, match_id, match_id),
        ).fetchone()

    sofa_ids = _sofa_ids_from_raw(row["raw_enriched"] if row else None)
    now_ms = int(_time.time() * 1000)
    start_ms = now_ms - max(1, hours_back) * 3_600_000
    try:
        from app.data_clients.sportybet_client import fetch_results

        for result in fetch_results(start_ms, now_ms, count=500):
            if _numeric_id(result.get("id")) != _numeric_id(match_id):
                continue
            score = result.get("score") or {}
            if score.get("home") is None or score.get("away") is None:
                break
            counts = _grade_match_predictions_by_ids(match_id, sofa_ids, _to_int(score.get("home"), 0), _to_int(score.get("away"), 0))
            _safe_mark_buffer_finished(match_id, score.get("home"), score.get("away"))
            return {"status": "graded", "source": "sportybet", "match_id": match_id, "score": score, **counts}
    except Exception as exc:
        sporty_error = str(exc)
    else:
        sporty_error = None

    match_date = (row["match_date"] if row else None) or _date_from_start(row["start_time"] if row else None)
    if not match_date:
        return {"status": "not_found", "match_id": match_id, "sporty_error": sporty_error, "reason": "no match date for SofaScore fallback"}
    try:
        from app.data_clients.sofascore_client import fetch_all_scheduled_events, fetch_event

        events = fetch_all_scheduled_events(str(match_date))
        event_by_id = {str(event.get("id")): event for event in events}
        event = next((event_by_id.get(sofa_id) for sofa_id in sofa_ids if event_by_id.get(sofa_id)), None)
        if not event and sofa_ids:
            for sofa_id in sofa_ids:
                direct_event = fetch_event(sofa_id)
                if direct_event:
                    event = direct_event
                    break
        if not event:
            return {"status": "not_found", "match_id": match_id, "sporty_error": sporty_error, "source": "sofascore"}
        status_type = str(((event.get("status") or {}).get("type")) or "").lower()
        if status_type in {"inprogress", "live"}:
            return {"status": "still_live", "match_id": match_id, "source": "sofascore", "sofascore_id": event.get("id")}
        if status_type != "finished":
            return {"status": "not_finished", "match_id": match_id, "source": "sofascore", "status_type": status_type}
        score = event.get("score") or {}
        counts = _grade_match_predictions_by_ids(match_id, sofa_ids, _to_int(score.get("home"), 0), _to_int(score.get("away"), 0))
        _safe_mark_buffer_finished(match_id, score.get("home"), score.get("away"))
        return {"status": "graded", "source": "sofascore", "match_id": match_id, "score": score, **counts}
    except Exception as exc:
        return {"status": "error", "match_id": match_id, "sporty_error": sporty_error, "error": str(exc)}


def grade_orphaned_predictions(limit: int = 1000) -> dict[str, Any]:
    """Grade ``prediction_history`` rows that have no match_buffer entry.

    These are predictions whose match was removed from the buffer after being
    archived (finished).  The standard ``grade_overdue_predictions`` grader
    skips them because it relies on the buffer for ``start_time``.

    This function:
    1. Finds all ungraded rows that are absent from both ``match_buffer`` and
       ``future_match_buffer``.
    2. Groups them by the date derived from ``created_at`` (best proxy available
       when no buffer row exists).
    3. Fetches SofaScore finished events for each distinct date.
    4. Grades each row via the stored ``sofascore_id`` or SportyBet match id.

    Returns::

        {
            "status": "ok",
            "inspected": <total orphaned rows>,
            "graded": <rows successfully graded>,
            "candidate_graded": <candidate rows graded>,
            "not_found": <rows where no SofaScore result was found>,
            "errors": [...]
        }
    """
    _init_db()

    # ── 1. Fetch all orphaned ungraded rows ──────────────────────────────────
    with _conn() as conn:
        _ensure_buffer_tables(conn)
        rows = conn.execute(
            """
            SELECT ph.*
            FROM prediction_history ph
            WHERE ph.graded_at IS NULL
              AND ph.pick_type != 'no_bet'
              AND ph.match_id NOT IN (SELECT match_id FROM match_buffer)
              AND ph.match_id NOT IN (SELECT match_id FROM future_match_buffer)
            ORDER BY ph.created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    if not rows:
        return {
            "status": "ok",
            "inspected": 0,
            "graded": 0,
            "candidate_graded": 0,
            "not_found": 0,
            "errors": [],
        }

    # ── 2. Group by date (use created_at date as proxy for match date) ───────
    by_date: dict[str, list] = defaultdict(list)
    for row in rows:
        date_key = str(row["created_at"] or "")[:10]
        if date_key:
            by_date[date_key].append(row)

    graded = candidate_graded = not_found = 0
    errors: list[str] = []

    # ── 3. For each date, fetch SofaScore events and grade matching rows ──────
    try:
        from app.data_clients.sofascore_client import fetch_all_scheduled_events, fetch_event
    except Exception as exc:
        return {"status": "error", "reason": f"sofascore import failed: {exc}"}

    for match_date, date_rows in by_date.items():
        try:
            events = fetch_all_scheduled_events(match_date)
        except Exception as exc:
            errors.append(f"sofascore {match_date}: {exc}")
            not_found += len(date_rows)
            continue

        event_by_id = {_numeric_id(e.get("id")): e for e in events}
        finished = {eid: e for eid, e in event_by_id.items()
                    if str(((e.get("status") or {}).get("type")) or "").lower() == "finished"}

        for row in date_rows:
            match_id = str(row["match_id"])
            # Collect all known SofaScore ids for this row
            sofa_ids: list[str] = []
            if row["sofascore_id"]:
                sofa_ids.append(str(row["sofascore_id"]))

            # Try to find a matching finished event
            event = finished.get(_numeric_id(match_id))
            if not event:
                event = next((finished.get(sid) for sid in sofa_ids if finished.get(sid)), None)

            # Direct fetch fallback for each sofa_id
            if not event and sofa_ids:
                for sid in sofa_ids:
                    try:
                        direct = fetch_event(sid)
                        if direct and str(((direct.get("status") or {}).get("type")) or "").lower() == "finished":
                            event = direct
                            break
                    except Exception:
                        pass

            if not event:
                not_found += 1
                continue

            score = event.get("score") or {}
            final_home = _to_int(score.get("home"), 0)
            final_away = _to_int(score.get("away"), 0)

            counts = _grade_match_predictions_by_ids(
                match_id=match_id,
                linked_ids=sofa_ids,
                final_home=final_home,
                final_away=final_away,
            )
            graded += counts["primary"]
            candidate_graded += counts["candidate"]

    return {
        "status": "ok",
        "inspected": len(rows),
        "graded": graded,
        "candidate_graded": candidate_graded,
        "not_found": not_found,
        "errors": errors[:10],
    }


def _ensure_buffer_tables(conn: sqlite3.Connection) -> None:
    """Ensure match_buffer and future_match_buffer exist so grading never fails with 'no such table'."""
    for table in ("match_buffer", "future_match_buffer"):
        try:
            conn.execute(
                f"""
                create table if not exists {table} (
                    match_id     text primary key,
                    match_date   text,
                    tournament   text,
                    category     text,
                    name         text,
                    start_time   integer,
                    period       text,
                    score_home   text,
                    score_away   text,
                    is_live      integer not null default 0,
                    is_finished  integer not null default 0,
                    ingested_at  text not null default current_timestamp,
                    enriched_at  text,
                    data_source  text not null default 'sportybet',
                    sportybet_id text,
                    sofascore_id text,
                    raw_sporty   text not null default '{{}}',
                    raw_enriched text
                )
                """
            )
        except Exception:
            pass


def _pending_matches_for_grading(cutoff_seconds: float, limit: int) -> list[sqlite3.Row]:
    with _conn() as conn:
        _ensure_buffer_tables(conn)
        rows = conn.execute(
            """
            with pending as (
                select match_id, min(created_at) as first_seen
                from prediction_history
                where graded_at is null and pick_type != 'no_bet'
                group by match_id
                union all
                select match_id, min(created_at) as first_seen
                from prediction_candidate_history
                where graded_at is null
                group by match_id
                union all
                select match_id, min(created_at) as first_seen
                from prediction_decision_log
                where graded_at is null
                group by match_id
            ),
            grouped as (
                select match_id, min(first_seen) as first_seen
                from pending
                group by match_id
            )
            select g.match_id, g.first_seen,
                   coalesce(mb.match_date, fb.match_date) as match_date,
                   coalesce(mb.start_time, fb.start_time) as start_time,
                   coalesce(mb.is_live, fb.is_live, 0) as is_live,
                   coalesce(mb.raw_enriched, fb.raw_enriched) as raw_enriched,
                   coalesce(mb.raw_sporty, fb.raw_sporty) as raw_sporty,
                   coalesce(mb.sofascore_id, fb.sofascore_id, ph_ids.sofascore_id) as sofascore_id,
                   coalesce(mb.sportybet_id, fb.sportybet_id) as sportybet_id
            from grouped g
            left join (
                select match_id, max(sofascore_id) as sofascore_id
                from prediction_history
                where sofascore_id is not null
                group by match_id
            ) ph_ids on ph_ids.match_id = g.match_id
            left join match_buffer mb on mb.match_id = g.match_id
            left join future_match_buffer fb on fb.match_id = g.match_id
            order by coalesce(mb.start_time, fb.start_time, strftime('%s', g.first_seen) * 1000) asc
            limit ?
            """,
            (limit,),
        ).fetchall()
    eligible: list[sqlite3.Row] = []
    for row in rows:
        kickoff = _normalise_start_seconds(row["start_time"])
        if kickoff is None:
            # Without kickoff we cannot assert overdue, but Sporty can still
            # confirm by id. Use first_seen only to allow result lookup, never
            # to mark not-found as a final state.
            kickoff = _datetime_to_seconds(row["first_seen"])
        if kickoff is not None and kickoff <= cutoff_seconds:
            eligible.append(row)
    return eligible


def _grade_match_predictions_by_ids(match_id: str, linked_ids: list[str], final_home: int, final_away: int) -> dict[str, int]:
    keys = []
    for value in [match_id, *linked_ids]:
        if value is not None and str(value) not in keys:
            keys.append(str(value))
    placeholders = ",".join("?" for _ in keys)
    primary = candidate = 0
    signal_payloads: list[dict[str, Any]] = []
    with _conn(timeout=15) as conn:
        rows = conn.execute(
            f"""
            select *
            from prediction_history
            where graded_at is null and match_id in ({placeholders})
            """,
            tuple(keys),
        ).fetchall()
        for row in rows:
            result, grade_info = grade_prediction_row(row, final_home, final_away)
            models = _safe_json(row["models_json"] if "models_json" in row.keys() else "{}", {})
            grade_info["passed_models"] = _get_passed_models(models, result)
            update_prediction_result(conn, row["id"], result, final_home, final_away, grade_info)
            payload = _signal_outcome_payload_for_row(row, result)
            if payload:
                signal_payloads.append(payload)
            primary += 1

        candidate_rows = conn.execute(
            f"""
            select *
            from prediction_candidate_history
            where graded_at is null and match_id in ({placeholders})
            """,
            tuple(keys),
        ).fetchall()
        for row in candidate_rows:
            result = _grade_candidate_row(row, final_home, final_away)
            grade_info = _grading_reason_for_candidate_row(row, final_home, final_away, result)
            conn.execute(
                """
                update prediction_candidate_history
                set result = ?, final_home = ?, final_away = ?, grading_reason_json = ?, graded_at = current_timestamp
                where id = ?
                """,
                (result, final_home, final_away, json.dumps(grade_info), row["id"]),
            )
            payload = _signal_outcome_payload_for_row(row, result)
            if payload:
                signal_payloads.append(payload)
            candidate += 1
        conn.commit()
    for payload in signal_payloads:
        _store_signal_outcome_payload(payload)
    decisions = _grade_decision_logs_by_ids(keys, final_home, final_away)
    return {"primary": primary, "candidate": candidate, "decisions": decisions}


def _grade_decision_logs_by_ids(keys: list[str], final_home: int, final_away: int) -> int:
    clean_keys = []
    for value in keys:
        if value is not None and str(value) not in clean_keys:
            clean_keys.append(str(value))
    if not clean_keys:
        return 0
    placeholders = ",".join("?" for _ in clean_keys)
    graded = 0
    with _conn(timeout=15) as conn:
        rows = conn.execute(
            f"""
            select *
            from prediction_decision_log
            where graded_at is null and match_id in ({placeholders})
            """,
            tuple(clean_keys),
        ).fetchall()
        for row in rows:
            grade_info = _grade_decision_row(row, final_home, final_away)
            conn.execute(
                """
                update prediction_decision_log
                set result = ?, final_home = ?, final_away = ?, grading_reason_json = ?, graded_at = current_timestamp
                where id = ?
                """,
                (grade_info.get("result"), final_home, final_away, json.dumps(grade_info), row["id"]),
            )
            graded += 1
        conn.commit()
    return graded


def _grade_decision_row(row: sqlite3.Row, final_home: int, final_away: int) -> dict[str, Any]:
    pick_type = row["pick_type"]
    selection = row["selection"]
    if row["decision_type"] == "published" and pick_type != "no_bet":
        info = grading_reason(pick_type, selection, final_home, final_away, row["match_name"])
        # grading_reason already runs _fallback_grade internally when
        # grade_market_intent returns "void", so there is no separate
        # _grade_pick_for_match call here.
        info["decision_type"] = row["decision_type"]
        return info

    audit = _safe_json(row["audit_json"], {})
    rejected = (((audit.get("signals") or {}).get("rejected")) or []) if isinstance(audit, dict) else []
    rejected_outcome = None
    rejected_pick = None
    for item in rejected:
        rejected_type = item.get("type") or item.get("pick_type")
        rejected_selection = item.get("selection")
        if not rejected_type or not rejected_selection:
            continue
        result = grading_reason(rejected_type, rejected_selection, final_home, final_away, row["match_name"]).get("result")
        if result in {"win", "loss"}:
            rejected_outcome = result
            rejected_pick = item
            break

    if row["decision_type"] in {"no_bet", "deferred"}:
        if rejected_outcome == "loss":
            result = "correct_avoid"
            reason = "Avoid/deferred decision protected against a rejected pick that would have lost."
        elif rejected_outcome == "win":
            result = "missed_opportunity"
            reason = "Avoid/deferred decision skipped a rejected pick that would have won."
        else:
            result = "avoided"
            reason = "No graded rejected pick was available, so the no-prediction decision is recorded as avoided."
    else:
        result = "observed"
        reason = "Decision was observed after match completion."

    return {
        "version": "decision_grading_v1",
        "result": result,
        "decision_type": row["decision_type"],
        "final_score": {"home": final_home, "away": final_away, "total": final_home + final_away},
        "rejected_pick_checked": rejected_pick,
        "reason": reason,
    }


def get_grading_metrics() -> dict[str, Any]:
    _init_db()
    with _conn() as conn:
        totals = conn.execute(
            """
            select
                count(*) as total,
                sum(case when graded_at is not null then 1 else 0 end) as graded,
                sum(case when result = 'win' then 1 else 0 end) as wins,
                sum(case when result = 'loss' then 1 else 0 end) as losses,
                sum(case when result = 'void' then 1 else 0 end) as voids
            from (
                select *
                from (
                    select
                        ph.*,
                        row_number() over (
                            partition by match_id, pick_type, selection
                            order by
                                case when graded_at is not null then 0 else 1 end,
                                datetime(coalesce(graded_at, created_at)) desc,
                                id desc
                        ) as rn
                    from prediction_history ph
                    where pick_type != 'no_bet' and coalesce(is_final, 1) = 1
                )
                where rn = 1
            )
            """
        ).fetchone()
        by_type = conn.execute(
            """
            select pick_type,
                   count(*) as total,
                   sum(case when result = 'win' then 1 else 0 end) as wins,
                   sum(case when result = 'loss' then 1 else 0 end) as losses
            from (
                select *
                from (
                    select
                        ph.*,
                        row_number() over (
                            partition by match_id, pick_type, selection
                            order by
                                case when graded_at is not null then 0 else 1 end,
                                datetime(coalesce(graded_at, created_at)) desc,
                                id desc
                        ) as rn
                    from prediction_history ph
                    where graded_at is not null and pick_type != 'no_bet' and coalesce(is_final, 1) = 1
                )
                where rn = 1
            )
            group by pick_type
            order by total desc
            """
        ).fetchall()
        recent = conn.execute(
            """
            select result, confidence, match_name, selection, created_at
            from (
                select *
                from (
                    select
                        ph.*,
                        row_number() over (
                            partition by match_id, pick_type, selection
                            order by datetime(coalesce(graded_at, created_at)) desc, id desc
                        ) as rn
                    from prediction_history ph
                    where graded_at is not null and pick_type != 'no_bet' and coalesce(is_final, 1) = 1
                )
                where rn = 1
            )
            order by graded_at desc
            limit 20
            """
        ).fetchall()

    graded = totals["graded"] or 0
    wins = totals["wins"] or 0
    losses = totals["losses"] or 0
    win_rate = round(wins / graded, 3) if graded else None
    return {
        "total_predictions": totals["total"] or 0,
        "graded": graded,
        "pending": (totals["total"] or 0) - graded,
        "wins": wins,
        "losses": losses,
        "voids": totals["voids"] or 0,
        "win_rate": win_rate,
        "win_percent": round(win_rate * 100, 1) if win_rate is not None else None,
        "by_type": [
            {
                "pick_type": r["pick_type"],
                "total": r["total"],
                "wins": r["wins"] or 0,
                "losses": r["losses"] or 0,
                "win_rate": round((r["wins"] or 0) / r["total"], 3) if r["total"] else None,
            }
            for r in by_type
        ],
        "recent": [
            {
                "result": r["result"],
                "confidence": r["confidence"],
                "match": r["match_name"],
                "selection": r["selection"],
                "created_at": r["created_at"],
            }
            for r in recent
        ],
    }


def get_local_signal_stats(
    country: str | None = None,
    tournament: str | None = None,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Aggregate signal win rates from local SQLite device memory."""
    _init_db()
    clauses = ["result in ('win', 'loss')"]
    params: list[Any] = []
    scope = "device"
    if tournament:
        clauses.append("lower(coalesce(tournament, '')) like ?")
        params.append(f"%{str(tournament or '').lower()}%")
        scope = f"device:tournament:{tournament}"
    elif country:
        clauses.append("lower(coalesce(country, '')) like ?")
        params.append(f"%{str(country or '').lower()}%")
        scope = f"device:country:{country}"
    params.append(max(1, int(min_samples or 5)))
    with _conn() as conn:
        _ensure_signal_outcomes_table(conn)
        existing = conn.execute("select count(*) from signal_outcomes").fetchone()[0]
    if not existing:
        _backfill_local_signal_outcomes_from_history()
    with _conn() as conn:
        _ensure_signal_outcomes_table(conn)
        rows = conn.execute(
            f"""
            select signal_name,
                   count(*) as total,
                   sum(case when result = 'win' then 1 else 0 end) as wins,
                   sum(case when result = 'loss' then 1 else 0 end) as losses,
                   avg(signal_impact) as avg_impact,
                   avg(confidence) as avg_confidence
            from signal_outcomes
            where {' and '.join(clauses)}
            group by signal_name
            having count(*) >= ?
            order by (1.0 * wins / nullif(total, 0)) desc, total desc
            """,
            params,
        ).fetchall()
    return {
        "configured": True,
        "storage": "device",
        "scope": scope,
        "min_samples": min_samples,
        "signals": [
            {
                "signal": row["signal_name"],
                "total": row["total"],
                "wins": row["wins"],
                "losses": row["losses"],
                "win_rate": round((float(row["wins"] or 0) / max(1, int(row["total"] or 0))) * 100, 1),
                "avg_impact": round(float(row["avg_impact"] or 0), 2),
                "avg_confidence": round(float(row["avg_confidence"] or 0), 1),
            }
            for row in rows
        ],
    }


def weighted_signal_combination_memory(
    match: dict[str, Any],
    signals: list[dict[str, Any]],
    pick_type: str | None,
    selection: str | None,
    *,
    prediction_mode: str | None = None,
    live_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return learned performance for this signal combination and pick."""
    _init_db()
    combo = build_signal_combination(
        signals=signals,
        pick_type=pick_type,
        selection=selection,
        prediction_mode=prediction_mode,
        live_context=live_context,
    )
    if not combo.get("key"):
        return {"samples": 0, "win_rate": None, "adjustment": 0, "combination": combo}
    league = _league_from_match(match)
    country = _country_from_match(match, league)
    with _conn() as conn:
        _ensure_signal_combination_outcomes_table(conn)
        scopes = [
            ("exact", "combination_key = ? and pick_type = ?", (combo["key"], pick_type), 0.70),
            ("tournament", "combination_key = ? and pick_type = ? and tournament = ?", (combo["key"], pick_type, league), 0.20),
            ("country", "combination_key = ? and pick_type = ? and country = ?", (combo["key"], pick_type, country), 0.10),
        ]
        rows = [_combination_scope_stats(conn, where, params, label, weight) for label, where, params, weight in scopes]
    usable = [row for row in rows if row["samples"] > 0]
    if not usable:
        return {"samples": 0, "win_rate": None, "adjustment": 0, "combination": combo, "scope": "none"}
    weighted = 0.0
    weight_total = 0.0
    samples = 0
    for row in usable:
        sample_factor = min(1.0, row["samples"] / 20)
        weight = row["weight"] * sample_factor
        weighted += row["win_rate"] * weight
        weight_total += weight
        samples += row["samples"]
    win_rate = weighted / weight_total if weight_total else None
    adjustment = 0
    if win_rate is not None:
        # weight_total is already a graceful confidence measure: each scope's
        # sample_factor (samples/20, capped at 1.0) damps a thin scope down
        # before it's even blended, so a single exact-combo sample naturally
        # contributes ~0.035 of full weight (0.70 * 1/20), not zero. The old
        # hard `samples >= 3` gate threw that damped signal away entirely --
        # in practice, real data (checked in production) shows ~250 distinct
        # combination keys from 257 graded rows, i.e. the exact scope is a
        # singleton for the vast majority of combinations, so almost nothing
        # ever cleared that gate. Scaling by weight_total instead means a
        # single sample nudges by a fraction of a point (rounds to 0 when
        # truly negligible) and a well-populated exact/league match can still
        # reach the full +-10 range -- graceful, not all-or-nothing, and it
        # gets stronger automatically as more graded outcomes accumulate.
        raw_adjustment = (win_rate - 0.52) * 20
        confidence_factor = min(1.0, weight_total)
        adjustment = round(raw_adjustment * confidence_factor)
        adjustment = max(-10, min(10, adjustment))
    return {
        "samples": samples,
        "win_rate": round(win_rate * 100, 1) if win_rate is not None else None,
        "adjustment": adjustment,
        "combination": combo,
        "scopes": usable,
    }


def get_local_signal_combination_stats(min_samples: int = 3, limit: int = 50) -> dict[str, Any]:
    _init_db()
    with _conn() as conn:
        _ensure_signal_combination_outcomes_table(conn)
        rows = conn.execute(
            """
            select combination_key,
                   max(combination_json) as combination_json,
                   max(signal_names_json) as signal_names_json,
                   pick_type,
                   count(*) as total,
                   sum(case when result = 'win' then 1 else 0 end) as wins,
                   sum(case when result = 'loss' then 1 else 0 end) as losses,
                   avg(confidence) as avg_confidence
            from signal_combination_outcomes
            where result in ('win', 'loss')
            group by combination_key, pick_type
            having count(*) >= ?
            order by (1.0 * wins / nullif(total, 0)) desc, total desc
            limit ?
            """,
            (max(1, int(min_samples or 3)), max(1, int(limit or 50))),
        ).fetchall()
    return {
        "configured": True,
        "storage": "device",
        "min_samples": min_samples,
        "combinations": [
            {
                "combination_key": row["combination_key"],
                "combination": _safe_json(row["combination_json"], {}),
                "signals": _safe_json(row["signal_names_json"], []),
                "pick_type": row["pick_type"],
                "total": row["total"],
                "wins": row["wins"] or 0,
                "losses": row["losses"] or 0,
                "win_rate": round((float(row["wins"] or 0) / max(1, int(row["total"] or 0))) * 100, 1),
                "avg_confidence": round(float(row["avg_confidence"] or 0), 1),
            }
            for row in rows
        ],
    }


def _combination_scope_stats(
    conn: sqlite3.Connection,
    where: str,
    params: tuple[Any, ...],
    label: str,
    weight: float,
) -> dict[str, Any]:
    row = conn.execute(
        f"""
        select count(*) as samples,
               sum(case when result = 'win' then 1 else 0 end) as wins,
               sum(case when result = 'loss' then 1 else 0 end) as losses
        from signal_combination_outcomes
        where result in ('win', 'loss') and {where}
        """,
        params,
    ).fetchone()
    samples = int(row["samples"] or 0) if row else 0
    wins = int(row["wins"] or 0) if row else 0
    losses = int(row["losses"] or 0) if row else 0
    return {
        "scope": label,
        "samples": samples,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / samples if samples else 0.0,
        "weight": weight,
    }


def betbuilder_pick_memory(
    pick_type: str | None,
    selection: str | None,
    league: str | None = None,
    country: str | None = None,
    odds: float | None = None,
) -> dict[str, Any]:
    """Read betbuilder-specific leg memory for future combination selection."""
    _init_db()
    pick_type = str(pick_type or "")
    selection = str(selection or "")
    if not pick_type or not selection:
        return {"samples": 0, "win_rate": None, "adjustment": 0}
    odds_band = _odds_band(odds)
    scopes = []
    with _conn() as conn:
        for label, where, params, weight in [
            ("league_odds", "pick_type=? and selection=? and league_name=? and odds_band=?", (pick_type, selection, league or "", odds_band), 0.45),
            ("country_odds", "pick_type=? and selection=? and country_name=? and odds_band=?", (pick_type, selection, country or "", odds_band), 0.30),
            ("global", "pick_type=? and selection=?", (pick_type, selection), 0.25),
        ]:
            row = conn.execute(
                f"""
                select count(*) samples,
                       sum(case when result='win' then 1 else 0 end) wins,
                       sum(case when result='loss' then 1 else 0 end) losses
                from betbuilder_leg_history
                where graded_at is not null and result in ('win','loss') and {where}
                """,
                params,
            ).fetchone()
            samples = int(row["samples"] or 0)
            wins = int(row["wins"] or 0)
            losses = int(row["losses"] or 0)
            scopes.append({
                "scope": label,
                "weight": weight,
                "samples": samples,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / samples, 3) if samples else None,
            })
    usable = [scope for scope in scopes if scope["samples"]]
    if not usable:
        return {"samples": 0, "win_rate": None, "adjustment": 0, "odds_band": odds_band, "scopes": scopes}
    weight_sum = sum(scope["weight"] for scope in usable)
    blended = sum((scope["win_rate"] or 0) * scope["weight"] for scope in usable) / max(weight_sum, 0.01)
    samples = sum(scope["samples"] for scope in usable)
    adjustment = round(max(-8, min(8, (blended - 0.55) * 28)))
    return {
        "samples": samples,
        "win_rate": round(blended, 3),
        "adjustment": adjustment,
        "odds_band": odds_band,
        "scopes": scopes,
    }


def _prediction_scope_stats(conn: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> dict[str, Any]:
    row = conn.execute(
        f"""
        select
            count(*) as samples,
            sum(case when result = 'win' then 1 else 0 end) as wins,
            sum(case when result = 'loss' then 1 else 0 end) as losses
        from (
            select *
            from (
                select
                    ph.*,
                    row_number() over (
                        partition by match_id, pick_type, selection
                        order by datetime(coalesce(graded_at, created_at)) desc, id desc
                    ) as rn
                from prediction_history ph
                where graded_at is not null
                  and result in ('win', 'loss')
            )
            where rn = 1
        )
        where {where}
        """,
        params,
    ).fetchone()
    samples = row["samples"] or 0
    wins = row["wins"] or 0
    return {
        "samples": samples,
        "wins": wins,
        "losses": row["losses"] or 0,
        "win_rate": round(wins / samples, 3) if samples else 0.0,
    }


def _finished_scope_stats(
    conn: sqlite3.Connection,
    where: str,
    params: tuple[Any, ...],
    odds_profile: dict[str, float | str] | None = None,
) -> dict[str, Any]:
    odds_params: tuple[Any, ...] = ()
    if "{odds_filter}" in where:
        if not odds_profile:
            where = where.replace("{odds_filter}", "0 = 1")
        else:
            fav = str(odds_profile.get("favorite_side") or "")
            fav_odds = float(odds_profile.get("favorite_odds") or 0)
            home = float(odds_profile.get("home_odds") or 0)
            draw = float(odds_profile.get("draw_odds") or 0)
            away = float(odds_profile.get("away_odds") or 0)
            where = where.replace(
                "{odds_filter}",
                """
                lo.home_odds is not null and lo.draw_odds is not null and lo.away_odds is not null
                and case
                    when lo.home_odds <= lo.draw_odds and lo.home_odds <= lo.away_odds then 'home'
                    when lo.away_odds <= lo.home_odds and lo.away_odds <= lo.draw_odds then 'away'
                    else 'draw'
                end = ?
                and abs(
                    case
                        when lo.home_odds <= lo.draw_odds and lo.home_odds <= lo.away_odds then lo.home_odds
                        when lo.away_odds <= lo.home_odds and lo.away_odds <= lo.draw_odds then lo.away_odds
                        else lo.draw_odds
                    end - ?
                ) <= 0.45
                and ((abs(lo.home_odds - ?) + abs(lo.draw_odds - ?) + abs(lo.away_odds - ?)) / 3.0) <= 0.75
                """,
            )
            odds_params = (fav, fav_odds, home, draw, away)
    row = conn.execute(
        f"""
        with latest_odds as (
            select os.*
            from odds_snapshots os
            join (
                select match_id, max(snapshot_time) as snapshot_time
                from odds_snapshots
                group by match_id
            ) latest
              on latest.match_id = os.match_id
             and latest.snapshot_time = os.snapshot_time
        )
        select
            count(*) as samples,
            sum(case when m.final_home_goals > m.final_away_goals then 1 else 0 end) as home_wins,
            sum(case when m.final_home_goals = m.final_away_goals then 1 else 0 end) as draws,
            sum(case when m.final_away_goals > m.final_home_goals then 1 else 0 end) as away_wins,
            sum(case when m.final_home_goals + m.final_away_goals > 1 then 1 else 0 end) as over_1_5,
            sum(case when m.final_home_goals + m.final_away_goals > 2 then 1 else 0 end) as over_2_5,
            sum(case when m.final_home_goals + m.final_away_goals > 3 then 1 else 0 end) as over_3_5,
            sum(case when m.final_home_goals > 0 and m.final_away_goals > 0 then 1 else 0 end) as btts,
            avg(m.final_home_goals + m.final_away_goals) as avg_goals
        from matches m
        left join latest_odds lo on lo.match_id = m.match_id
        where m.is_finished = 1
          and m.final_home_goals is not null
          and m.final_away_goals is not null
          and {where}
        """,
        params + odds_params,
    ).fetchone()
    samples = row["samples"] or 0

    def rate(key: str) -> float:
        return round((row[key] or 0) / samples, 3) if samples else 0.0

    return {
        "samples": samples,
        "home_win_rate": rate("home_wins"),
        "draw_rate": rate("draws"),
        "away_win_rate": rate("away_wins"),
        "over_1_5_rate": rate("over_1_5"),
        "over_2_5_rate": rate("over_2_5"),
        "over_3_5_rate": rate("over_3_5"),
        "btts_rate": rate("btts"),
        "avg_goals": round(float(row["avg_goals"] or 0), 3),
        "odds_filtered": bool(odds_profile),
    }


