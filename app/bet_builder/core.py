"""
Bet Builder — Shared Core
=========================
Candidate fetching, pick utilities, odds arithmetic, and conviction scoring
shared by both the manual (deterministic) and LLM-powered builders.

Nothing in this module makes LLM calls.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from datetime import date, timedelta
from typing import Any

from app.storage.league_memory import list_prediction_history, save_betbuilder
from app.research.research_filter import (
    _research_filter_candidate,
    _normalise_league_key,
    _get_dynamic_rules,
)
from app.monitoring.self_learner import (
    get_learned_thresholds,
)
from app.utils.primitives import _to_float, _to_int
from app.utils.match_helpers import _normalise_selection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Suggestion tracking
# ---------------------------------------------------------------------------

def track_suggested_slip(
    selected: list[dict[str, Any]],
    *,
    mode: str,
    combined_odds_value: float | None,
    avg_confidence: int | None,
    extra_request: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Persist a just-suggested ticket to betbuilder_history the moment it is
    generated, so it is tracked and later auto-graded even if nobody books
    it. Called by every builder (manual/llm/smart) right after ranking.

    Never raises — a tracking failure must not break the suggestion
    response the caller is about to return.
    """
    if not selected:
        return None
    try:
        request = {"builder": mode, **(extra_request or {})}
        saved = save_betbuilder(
            selected,
            round(float(combined_odds_value or 0), 3),
            int(avg_confidence or 0),
            request,
        )
        return {"id": saved.get("id")}
    except Exception:
        logger.exception("Failed to auto-track suggested %s betbuilder slip", mode)
        return None


# ---------------------------------------------------------------------------
# Candidate fetching
# ---------------------------------------------------------------------------

def upcoming_prediction_candidates(limit: int = 50) -> list[dict[str, Any]]:
    """Return upcoming, unstarted, research-gated stored prediction candidates."""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    allowed_dates = {today.isoformat(), tomorrow.isoformat()}
    rows = _recent_ungraded_prediction_rows(allowed_dates, limit=max(limit, 200))
    if not rows:
        try:
            rows = list_prediction_history(limit=max(limit, 200)).get("predictions") or []
        except Exception:
            rows = []

    now_ts = time.time()
    prefiltered: list[tuple[dict[str, Any], dict[str, Any], float | None]] = []
    _filter_stats: dict[str, int] = {}

    for row in rows:
        if row.get("is_finished") or str(row.get("result") or "").lower() in {"cancelled", "finished"}:
            _filter_stats["finished"] = _filter_stats.get("finished", 0) + 1
            continue
        match_date = str(row.get("match_date") or "")[:10]
        if match_date and match_date not in allowed_dates:
            _filter_stats["wrong_date"] = _filter_stats.get("wrong_date", 0) + 1
            continue
        start_time = row.get("start_time")
        if start_time:
            try:
                kick_ts = float(start_time)
                if kick_ts > 1e12:
                    kick_ts /= 1000
                if kick_ts < now_ts:
                    _filter_stats["started"] = _filter_stats.get("started", 0) + 1
                    continue
            except (TypeError, ValueError):
                pass
        if row.get("is_live") or str(row.get("period") or "").lower() in {"h1", "h2", "et", "live", "ht"}:
            _filter_stats["live"] = _filter_stats.get("live", 0) + 1
            continue
        pick = row.get("best_pick") or _best_pick(row.get("picks") or [])
        if not pick:
            _filter_stats["no_pick"] = _filter_stats.get("no_pick", 0) + 1
            continue
        stored_odds = pick_decimal_odds(pick)
        if stored_odds is not None and stored_odds < 1.30:
            _filter_stats["low_odds"] = _filter_stats.get("low_odds", 0) + 1
            continue

        try:
            country = str(row.get("country_name") or row.get("category") or "").lower().strip()
            league_key = str(row.get("league_name") or "").lower().strip()
            if not league_key and row.get("tournament"):
                league_key = _normalise_league_key(str(row["tournament"]))
            odds_profile = extract_odds_profile(row)
            if not _research_filter_candidate(pick, odds_profile, country, league_key):
                _filter_stats["research_filter"] = _filter_stats.get("research_filter", 0) + 1
                continue
        except Exception as exc:
            logger.warning("upcoming_prediction_candidates: research filter error for %s: %s", row.get("match_id"), exc)
            _filter_stats["research_error"] = _filter_stats.get("research_error", 0) + 1
            continue

        prefiltered.append((row, pick, stored_odds))

    match_ids = [
        str(row.get("match_id") or row.get("sportybet_id") or "")
        for row, _pick, _odds in prefiltered
    ]
    match_ids = [match_id for match_id in dict.fromkeys(match_ids) if match_id]

    try:
        from app.storage.buffer import bulk_get_buffered_matches
        buffer_map = bulk_get_buffered_matches(match_ids)
    except Exception:
        buffer_map = {}

    candidates = []

    for row, pick, stored_odds in prefiltered:
        match_id = str(row.get("match_id") or row.get("sportybet_id") or "")
        if match_id:
            try:
                buf_doc = buffer_map.get(match_id)
                if not buf_doc:
                    _filter_stats["no_buffer"] = _filter_stats.get("no_buffer", 0) + 1
                    continue
                if buf_doc.get("is_finished") or buf_doc.get("is_live"):
                    _filter_stats["buffer_live"] = _filter_stats.get("buffer_live", 0) + 1
                    continue
                resolved_odds = stored_odds or _pick_buffer_decimal_odds(pick, buf_doc)
                if resolved_odds is None or resolved_odds < 1.30:
                    _filter_stats["buffer_odds"] = _filter_stats.get("buffer_odds", 0) + 1
                    continue
                pick = {**pick, "odds": resolved_odds}
            except Exception as exc:
                logger.warning("upcoming_prediction_candidates: buffer error for %s: %s", match_id, exc)
                _filter_stats["buffer_error"] = _filter_stats.get("buffer_error", 0) + 1
                continue

        candidates.append({**row, "best_pick": pick})

    if not candidates and _filter_stats:
        logger.info("upcoming_prediction_candidates: filter stats: %s", _filter_stats)

    candidates.sort(
        key=lambda r: int((r.get("best_pick") or {}).get("confidence") or 0),
        reverse=True,
    )
    return candidates[:limit]


# ---------------------------------------------------------------------------
# Live candidate fetching
# ---------------------------------------------------------------------------

# Live pick-type families this bet builder will consider. Deliberately
# excludes older heuristic-only live types with no verified booking path
# (live_team_to_score) and includes both the newer shared-grid types
# ("_grid" suffix) and their independent-heuristic counterparts, since
# _live_grid_projection_picks and _live_inplay_picks run side by side and
# either can win the confidence/clear_winner ranking for a given match.
LIVE_BETTABLE_TYPES = {
    "live_match_winner", "live_match_winner_grid",
    "live_total_goals", "live_total_goals_grid",
    "live_next_goal", "live_next_goal_grid",
    "live_no_goal_grid",
    "live_btts", "live_btts_grid",
    "live_double_chance_grid",
}

# How old a stored live prediction is allowed to be before it's treated as
# stale and regenerated. Live match state (score, minute, live stats) can
# invalidate a prediction within seconds of a goal or a red card, so this is
# deliberately much tighter than anything used for prematch predictions.
LIVE_PREDICTION_FRESHNESS_SECONDS = 150

# Cap on how many fresh predictions one live_prediction_candidates() call
# will generate synchronously, to keep endpoint latency bounded. Matches
# beyond this cap that lack a fresh stored prediction are simply skipped
# for this call -- job_unified_live will pick them up on its own cycle.
LIVE_ON_DEMAND_GENERATE_LIMIT = 6


def _best_live_pick(picks: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [p for p in picks if p.get("type") in LIVE_BETTABLE_TYPES]
    if not usable:
        return None
    return max(usable, key=lambda p: int(p.get("confidence") or 0))


def _live_match_prediction_state(limit: int) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return (freshest prediction_history row per currently-live match_id,
    list of live match_ids with no row fresh enough to trust)."""
    try:
        from app.storage.db import db_conn
        from app.storage.league_memory import _init_db

        _init_db()
        with db_conn(timeout=20) as conn:
            conn.row_factory = sqlite3.Row
            live_rows = conn.execute(
                """
                select match_id from match_buffer
                where is_live = 1 and coalesce(is_finished, 0) = 0
                order by coalesce(enriched_at, ingested_at) desc
                limit ?
                """,
                (int(max(limit, 100)),),
            ).fetchall()
            live_match_ids = [str(r["match_id"]) for r in live_rows if r["match_id"]]
            if not live_match_ids:
                return {}, []

            placeholders = ",".join("?" for _ in live_match_ids)
            pred_rows = conn.execute(
                f"""
                select match_id, created_at, picks_json, signals_json, match_name,
                       league_name, country_name, source
                from prediction_history
                where match_id in ({placeholders})
                  and created_at >= datetime('now', '-2 hours')
                  and pick_type != 'no_bet'
                order by datetime(created_at) desc, id desc
                """,
                tuple(live_match_ids),
            ).fetchall()
    except Exception:
        return {}, []

    now_ts = time.time()
    fresh_by_match: dict[str, dict[str, Any]] = {}
    for row in pred_rows:
        match_id = str(row["match_id"] or "")
        if not match_id or match_id in fresh_by_match:
            continue  # already have this match's freshest row (rows are ordered desc)
        try:
            created_ts = time.mktime(time.strptime(str(row["created_at"])[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        if now_ts - created_ts > LIVE_PREDICTION_FRESHNESS_SECONDS:
            continue
        fresh_by_match[match_id] = dict(row)

    stale_or_missing = [m for m in live_match_ids if m not in fresh_by_match]
    return fresh_by_match, stale_or_missing


def _row_age_seconds(timestamp: str | None) -> float | None:
    """Same ISO-timestamp-age parsing idiom used by _live_match_prediction_state
    above, factored out so both the freshness check and the on-demand refresh
    below agree on what "stale" means."""
    if not timestamp:
        return None
    try:
        created_ts = time.mktime(time.strptime(str(timestamp)[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None
    return time.time() - created_ts


def _refresh_live_stats_if_stale(doc: dict[str, Any], match_id: str) -> dict[str, Any]:
    """Pull one lightweight SofaScore live refresh inline if this match is
    already matched and its buffered statistics look older than the
    prediction freshness window.

    This is deliberately narrow: it never attempts matching (fuzzy-match,
    LLM fallback, etc.) -- only the 3-call live refresh
    (statistics/incidents/graph/lineups) for a match that already has a
    saved sofascore_id + sofascore_detail. A match with no SofaScore match
    yet is left to the background pipeline (job_enrich_worker /
    job_unified_live), same as before. Never raises -- prediction should
    still proceed on whatever's buffered if this fails.
    """
    sofa_id = doc.get("sofascore_id")
    existing_detail = doc.get("sofascore_detail")
    if not sofa_id or not existing_detail:
        return doc
    age = _row_age_seconds(doc.get("enriched_at"))
    if age is not None and age <= LIVE_PREDICTION_FRESHNESS_SECONDS:
        return doc
    try:
        from app.data_clients.sofascore_client import fetch_event_detail_live_refresh
        from app.storage.buffer import store_enriched

        fresh_detail = fetch_event_detail_live_refresh(int(sofa_id), existing_detail)
        store_enriched(match_id, {"sofascore_detail": fresh_detail})
        doc = {**doc, "sofascore_detail": fresh_detail}
    except Exception as exc:
        logger.debug("live_prediction_candidates: inline live-stat refresh failed for %s: %s", match_id, exc)
    return doc


def _generate_fresh_live_predictions(match_ids: list[str]) -> None:
    """Refresh live stats (if stale) and record a fresh live prediction for
    each match_id, in parallel -- this is the "if we don't have data, pull
    it and predict, right now" behavior: job_unified_live/job_enrich_worker
    cover the general live pipeline on their own cycle, but a bet-builder
    request needs the answer now, not on the next scheduled pass.

    Bounded by LIVE_ON_DEMAND_GENERATE_LIMIT concurrent workers (each match
    is an independent SofaScore fetch + prediction, so this is the same
    "parallel across matches" shape job_enrich_worker already uses -- just
    triggered synchronously from a request instead of a scheduler tick).
    A single match's refresh or prediction failing (thin live signal, no
    SofaScore match yet, etc.) must never fail the whole candidate fetch.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from app.storage.buffer import get_buffered_match
    from app.utils.prediction_flow import predict_and_record_enriched, PredictionDeferred

    targets = match_ids[:LIVE_ON_DEMAND_GENERATE_LIMIT]
    if not targets:
        return

    def _refresh_and_predict(match_id: str) -> None:
        try:
            doc = get_buffered_match(match_id)
            if not doc:
                return
            doc = _refresh_live_stats_if_stale(doc, match_id)
            predict_and_record_enriched(doc, match_id=match_id, source="live_bet_builder_on_demand")
        except PredictionDeferred:
            return
        except Exception as exc:
            logger.debug("live_prediction_candidates: on-demand prediction failed for %s: %s", match_id, exc)

    with ThreadPoolExecutor(max_workers=min(LIVE_ON_DEMAND_GENERATE_LIMIT, len(targets))) as pool:
        futures = [pool.submit(_refresh_and_predict, match_id) for match_id in targets]
        for future in as_completed(futures):
            future.result()  # exceptions are already swallowed inside; surfaces anything that isn't


def live_prediction_candidates(limit: int = 50) -> list[dict[str, Any]]:
    """Return currently in-play matches with a bettable, fresh-enough stored
    prediction -- generating one on demand for matches that are live but
    whose stored prediction is missing or stale (see
    LIVE_PREDICTION_FRESHNESS_SECONDS), up to LIVE_ON_DEMAND_GENERATE_LIMIT
    per call.

    Deliberately skips the research-filter gate `upcoming_prediction_candidates`
    applies -- that gate's dynamic per-league rules are tuned against
    prematch pick types and have no accuracy history yet for live/grid pick
    types (which only started grading correctly recently). Revisit once live
    picks have accumulated their own graded history.
    """
    from app.utils.primitives import _loads

    fresh_by_match, stale_or_missing = _live_match_prediction_state(limit)
    if stale_or_missing:
        _generate_fresh_live_predictions(stale_or_missing)
        # Re-check just the matches we attempted -- cheap relative to the
        # full query above, and picks up whatever the on-demand pass stored.
        refreshed, _still_stale = _live_match_prediction_state(limit)
        fresh_by_match.update(refreshed)

    candidates: list[dict[str, Any]] = []
    for match_id, row in fresh_by_match.items():
        picks = _loads(row.get("picks_json"), [])
        pick = _best_live_pick(picks)
        if not pick:
            continue
        odds = pick_decimal_odds(pick)
        if odds is None or odds < 1.30:
            continue
        candidates.append({
            "match_id": match_id,
            "sportybet_id": match_id,
            "match_name": row.get("match_name"),
            "league_name": row.get("league_name"),
            "country_name": row.get("country_name"),
            "signals": _loads(row.get("signals_json"), []),
            "picks": picks,
            "is_live": True,
            "best_pick": {**pick, "odds": odds},
        })

    candidates.sort(
        key=lambda r: int((r.get("best_pick") or {}).get("confidence") or 0),
        reverse=True,
    )
    return candidates[:limit]


# ---------------------------------------------------------------------------
# Conviction scoring — shared by both rank functions
# ---------------------------------------------------------------------------

def score_pick(item: dict[str, Any]) -> dict[str, Any]:
    """
    Compute all conviction components for a single analysis item.

    Returns a dict of scored fields that both rank_picks_deterministic and
    rank_picks_llm merge into their ranked entry.
    """
    engine_pick = item.get("prediction_engine_pick") or {}
    llm_conf = _to_int(item.get("llm_confidence") or item.get("confidence"), 0)
    similar_used = _to_int(item.get("similar_matches_used"), 0)
    confirmed = bool(item.get("confirmed")) or _same_outcome(
        engine_pick.get("selection"), item.get("llm_recommendation")
    )
    odds = round(
        _to_float(item.get("estimated_odds")) or pick_decimal_odds(engine_pick) or 1.0,
        3,
    )

    # Historical pick memory
    try:
        from app.storage.league_memory import betbuilder_pick_memory
        learning = betbuilder_pick_memory(
            engine_pick.get("type") or item.get("pick_type"),
            item.get("llm_recommendation") or engine_pick.get("selection"),
            item.get("league_name"),
            item.get("country_name"),
            odds,
        )
    except Exception:
        learning = {"samples": 0, "win_rate": None, "adjustment": 0}

    # Learned probability boost
    learned_prob = _learned_prob(item, engine_pick)
    learned_boost = _learned_boost(learned_prob, item, engine_pick)

    # League accuracy boost
    league_acc_boost = _league_accuracy_boost(item, engine_pick)

    # Signal combination boost
    signal_boost = _signal_combination_boost(item, engine_pick)

    base_conviction = (
        llm_conf * (1 + 0.10 * similar_used)
        + float(learning.get("adjustment") or 0)
    )
    conviction = round(base_conviction + learned_boost + league_acc_boost + signal_boost, 2)

    # Research conviction adjustment
    research_adj = _research_conviction_adj(item, engine_pick, llm_conf)

    # Optimal profile score
    optimal_score = _optimal_profile_score(item, engine_pick, llm_conf, odds)

    return {
        "confirmed": confirmed,
        "odds": odds,
        "estimated_odds": odds,
        "llm_confidence": llm_conf,
        "confidence": llm_conf,
        "conviction_score": round(conviction + research_adj, 2),
        "research_conviction_adj": research_adj,
        "optimal_profile_score": optimal_score,
        "optimal_profile": optimal_score >= 4,
        "learning": learning,
        "learned_probabilities": learned_prob,
        "league_accuracy_boost": round(league_acc_boost, 2),
        "similar_matches_used": similar_used,
    }


# ---------------------------------------------------------------------------
# Odds selection helpers
# ---------------------------------------------------------------------------

def select_by_odds(
    candidates: list[dict[str, Any]],
    target_odds: float,
    max_total_odds: float,
    min_leg_odds: float = 1.30,
) -> list[dict[str, Any]]:
    usable = []
    seen: set[str] = set()
    for item in candidates[:50]:
        match_id = str(item.get("match_id") or "")
        if match_id and match_id in seen:
            continue
        odds = float(item.get("odds") or item.get("estimated_odds") or 1)
        if odds < min_leg_odds or odds > max_total_odds:
            continue
        usable.append(item)
        if match_id:
            seen.add(match_id)

    if not usable:
        return []

    dp: dict[int, tuple[float, float, list[int]]] = {}
    scale = 1000
    for idx, item in enumerate(usable):
        odds = float(item.get("odds") or item.get("estimated_odds") or 1)
        conviction = float(item.get("conviction_score") or 0)
        additions: dict[int, tuple[float, float, list[int]]] = {
            int(round(odds * scale)): (conviction, odds, [idx])
        }
        for key, (existing_conviction, existing_odds, existing_idxs) in dp.items():
            combined = existing_odds * odds
            if combined > max_total_odds:
                continue
            combined_key = int(round(combined * scale))
            additions[combined_key] = (
                existing_conviction + conviction,
                combined,
                existing_idxs + [idx],
            )
        for key, candidate_state in additions.items():
            current = dp.get(key)
            if current is None or candidate_state[0] > current[0]:
                dp[key] = candidate_state

    best = max(
        (
            (conviction, odds, idxs)
            for conviction, odds, idxs in dp.values()
            if odds >= target_odds and odds <= max_total_odds
        ),
        key=lambda state: (state[0], -abs(state[1] - target_odds), -len(state[2])),
        default=None,
    )
    if best:
        return [usable[i] for i in best[2]]

    return _select_by_odds_greedy(usable, max_total_odds, min_leg_odds)


def _select_by_odds_greedy(
    candidates: list[dict[str, Any]],
    max_total_odds: float,
    min_leg_odds: float,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    combined = 1.0
    for item in candidates:
        odds = float(item.get("odds") or item.get("estimated_odds") or 1)
        if odds < min_leg_odds or combined * odds > max_total_odds:
            continue
        selected.append(item)
        combined *= odds
    return selected


def trim_to_ceiling(
    selected: list[dict[str, Any]], max_total_odds: float
) -> list[dict[str, Any]]:
    items = list(selected)
    while len(items) > 1 and combined_odds(items) > max_total_odds:
        items.sort(key=lambda i: float(i.get("conviction_score") or 0))
        items.pop(0)
    return items


def combined_odds(items: list[dict[str, Any]]) -> float:
    if not items:
        return 0.0
    return math.prod(float(i.get("odds") or i.get("estimated_odds") or 1) for i in items)


def select_by_conviction(
    candidates: list[dict[str, Any]],
    min_leg_odds: float = 1.30,
) -> list[dict[str, Any]]:
    """
    "Smart bet" selection: no target odds, no odds ceiling, no leg cap.

    Every candidate whose conviction_score clears ITS OWN learned bar
    (per league + pick type, via the same get_learned_thresholds() the
    research gate and _optimal_profile_score already use elsewhere) is
    included. One leg per match — if the same match produced more than one
    scored candidate, the higher-conviction one wins. Combined odds is
    whatever results from stacking every leg that earns its place; by
    design there is nothing here that trims the slip back down once it
    grows, since the whole point of this mode is "only bet what you're
    actually confident about," not "hit a number."
    """
    from app.monitoring.self_learner import get_learned_thresholds

    best_per_match: dict[str, dict[str, Any]] = {}
    for item in candidates:
        match_id = str(item.get("match_id") or item.get("sportybet_id") or "")
        if not match_id:
            continue
        odds = float(item.get("odds") or item.get("estimated_odds") or 1)
        if odds < min_leg_odds:
            continue
        league = item.get("league") or item.get("league_name") or "__global__"
        pick_type = item.get("pick_type") or item.get("type") or "match_result"
        try:
            min_conviction = float(
                get_learned_thresholds(league=league, pick_type=pick_type).get("min_confidence", 72.0)
            )
        except Exception:
            min_conviction = 72.0
        conviction = float(item.get("conviction_score") or 0)
        if conviction < min_conviction:
            continue
        current = best_per_match.get(match_id)
        if current is None or conviction > float(current.get("conviction_score") or 0):
            best_per_match[match_id] = item

    selected = list(best_per_match.values())
    selected.sort(key=lambda i: float(i.get("conviction_score") or 0), reverse=True)
    return selected


# ---------------------------------------------------------------------------
# Pick utilities
# ---------------------------------------------------------------------------

def pick_decimal_odds(pick: dict[str, Any] | None) -> float | None:
    pick = pick or {}
    stake = pick.get("stake") if isinstance(pick.get("stake"), dict) else {}
    odds = (
        _to_float(stake.get("decimal_odds"))
        or _to_float(pick.get("odds"))
        or _to_float(pick.get("decimal_odds"))
    )
    if odds and odds > 1:
        return odds
    return None


def _pick_buffer_decimal_odds(pick: dict[str, Any], buf_doc: dict[str, Any]) -> float | None:
    markets = buf_doc.get("sportybet_markets") or buf_doc.get("markets") or []
    if not markets:
        return None
    try:
        from app.data_clients.sportybet_booking import _find_market_outcome
        from app.market.market_intent import classify_market_intent

        intent = classify_market_intent(pick.get("type"), pick.get("selection"), pick)
        _market, outcome = _find_market_outcome(markets, intent, pick)
        odds = _to_float((outcome or {}).get("odds"))
        return odds if odds and odds > 1 else None
    except Exception:
        return None


def _recent_ungraded_prediction_rows(allowed_dates: set[str], limit: int) -> list[dict[str, Any]]:
    try:
        from app.storage.db import db_conn
        from app.storage.league_memory import _init_db
        from app.utils.primitives import _loads

        _init_db()
        with db_conn(timeout=20) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select ph.id, ph.source, ph.match_id, ph.match_name, ph.league_name, ph.country_name,
                       ph.pick_type, ph.selection, ph.confidence, ph.reason, ph.signals_json,
                       ph.picks_json, ph.created_at, ph.result, ph.graded_at, ph.engine, ph.is_final,
                       coalesce(mb.match_date, date(ph.created_at)) as match_date,
                       mb.start_time as start_time,
                       coalesce(mb.is_live, 0) as is_live,
                       coalesce(mb.is_finished, 0) as is_finished
                from prediction_history ph
                left join match_buffer mb on mb.match_id = ph.match_id
                where ph.created_at >= datetime('now', '-72 hours')
                  and ph.graded_at is null
                  and ph.pick_type != 'no_bet'
                  and coalesce(mb.match_date, date(ph.created_at)) in (?, ?)
                order by coalesce(ph.is_final, 1) desc, datetime(ph.created_at) desc, ph.id desc
                limit ?
                """,
                (*sorted(allowed_dates), int(limit)),
            ).fetchall()
    except Exception:
        return []

    predictions: list[dict[str, Any]] = []
    seen_matches: set[str] = set()
    for row in rows:
        match_id = str(row["match_id"] or "")
        if not match_id or match_id in seen_matches:
            continue
        seen_matches.add(match_id)
        picks = _loads(row["picks_json"], [])
        signals = _loads(row["signals_json"], [])
        stored_best = picks[0] if picks else {}
        predictions.append({
            "id": row["id"],
            "source": row["source"],
            "match_id": row["match_id"],
            "match_name": row["match_name"],
            "league_name": row["league_name"],
            "country_name": row["country_name"],
            "best_pick": {
                **stored_best,
                "type": stored_best.get("type") or row["pick_type"],
                "selection": stored_best.get("selection") or row["selection"],
                "confidence": stored_best.get("confidence") or row["confidence"],
                "reason": stored_best.get("reason") or row["reason"],
            },
            "signals": signals,
            "picks": picks,
            "result": row["result"],
            "graded_at": row["graded_at"],
            "created_at": row["created_at"],
            "match_date": row["match_date"],
            "start_time": row["start_time"],
            "is_live": bool(row["is_live"]),
            "is_finished": bool(row["is_finished"]),
        })
    return predictions


def extract_odds_profile(row: dict[str, Any]) -> dict[str, float]:
    signals_raw = row.get("signals_json") or row.get("signals") or "[]"
    if isinstance(signals_raw, str):
        try:
            signals = json.loads(signals_raw)
        except (json.JSONDecodeError, TypeError):
            signals = []
    else:
        signals = signals_raw or []
    odds: dict[str, float] = {}
    for sig in signals:
        if isinstance(sig, dict) and sig.get("name") == "odds_profile":
            profile = sig.get("value") or {}
            if isinstance(profile, dict):
                odds.update(profile)
                break
    if not odds:
        direct = row.get("odds_profile") or {}
        if isinstance(direct, dict):
            odds.update(direct)
    return {k: float(v) for k, v in odds.items() if _to_float(v) is not None}


def _rank_analyses(
    analyses: list[dict[str, Any]],
    default_source: str,
    default_type_suffix: str,
) -> list[dict[str, Any]]:
    """Score and sort analyses into a ranked list. Shared by both builder modes."""
    clean = [
        item for item in analyses
        if item.get("status") == "success" or item.get("llm_recommendation")
    ]
    if len(clean) < 1:
        raise ValueError("At least one completed analysis is required")
    ranked = []
    score_cache: dict[tuple[Any, Any, Any, Any], dict[str, Any]] = {}
    for item in clean[:100]:
        engine_pick = item.get("prediction_engine_pick") or {}
        score_key = (
            item.get("match_id") or item.get("sportybet_id"),
            engine_pick.get("type") or item.get("pick_type"),
            item.get("llm_recommendation") or engine_pick.get("selection"),
            item.get("league_name"),
        )
        if score_key not in score_cache:
            score_cache[score_key] = score_pick(item)
        scored = score_cache[score_key]
        ranked.append({
            "match_id": item.get("match_id") or item.get("sportybet_id"),
            "match": item.get("match_name") or item.get("match"),
            "league": item.get("league_name"),
            "country": item.get("country_name"),
            "type": engine_pick.get("type") or item.get("pick_type") or default_type_suffix,
            "pick_type": engine_pick.get("type") or item.get("pick_type") or default_type_suffix,
            "selection": item.get("llm_recommendation") or engine_pick.get("selection"),
            "source": default_source,
            "synthesis_reasoning": "",
            **scored,
        })
    ranked.sort(
        key=lambda i: (
            i["confirmed"],
            i["conviction_score"] + i.get("optimal_profile_score", 0) * 0.5,
            i["llm_confidence"],
        ),
        reverse=True,
    )
    return ranked


def _best_pick(picks: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [p for p in picks if p.get("type") != "no_bet"]
    if not usable:
        return None
    return max(usable, key=lambda p: int(p.get("confidence") or 0))


def _same_outcome(left: Any, right: Any) -> bool:
    return _normalise_selection(left) == _normalise_selection(right)


# Internal conviction sub-scorers
# ---------------------------------------------------------------------------

def _learned_prob(item: dict[str, Any], engine_pick: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from app.models.probability_learner import get_learned_probabilities
        from app.enrichment.signal_aggregator import normalize_signal

        signals = _build_analysis_signals(item, engine_pick)
        normalized = [normalize_signal(s.get("name", ""), s.get("value", 0)) for s in signals]
        return get_learned_probabilities(
            normalized,
            pick_type=engine_pick.get("type") or item.get("pick_type") or "match_result",
            league_key=item.get("league_name") or "__global__",
            min_samples=5,
        )
    except Exception:
        return None


def _learned_boost(
    learned_prob: dict[str, Any] | None,
    item: dict[str, Any],
    engine_pick: dict[str, Any],
) -> float:
    if not learned_prob or learned_prob.get("samples", 0) < 5:
        return 0.0
    llm_selection = (
        item.get("llm_recommendation") or engine_pick.get("selection") or ""
    ).lower()
    away = learned_prob.get("away_prob", 0.25)
    home = learned_prob.get("home_prob", 0.45)
    draw = learned_prob.get("draw_prob", 0.30)
    boost = 0.0
    if "away" in llm_selection or llm_selection == "2":
        boost = (away - 0.25) * 20
        if away >= 0.54:
            boost += 3.0
    elif "home" in llm_selection or llm_selection == "1":
        boost = (home - 0.45) * 20
    elif "draw" in llm_selection or llm_selection == "x":
        boost = (draw - 0.30) * 20
    return boost


def _league_accuracy_boost(item: dict[str, Any], engine_pick: dict[str, Any]) -> float:
    try:
        from app.monitoring.self_learner import get_league_accuracy
        lacc = get_league_accuracy(item.get("league_name") or "")
        if not lacc.get("known"):
            return 0.0
        pick_type = engine_pick.get("type") or item.get("pick_type") or "match_result"
        for lt in lacc.get("by_pick_type") or []:
            if lt.get("pick_type") in (pick_type, "__all__"):
                samples = int(lt.get("samples") or 0)
                wr = float(lt.get("win_rate") or 0)
                if samples >= 8:
                    if wr > 65.0:
                        return min(8.0, (wr - 65.0) / 5.0)
                    if wr < 50.0:
                        return max(-10.0, -((50.0 - wr) / 5.0))
                break
    except Exception:
        pass
    return 0.0


def _signal_combination_boost(item: dict[str, Any], engine_pick: dict[str, Any]) -> float:
    """Reuses the same real, league-weighted signal-combination memory the
    core prediction path already uses (weighted_signal_combination_memory in
    app.storage.league_memory), instead of a second, separate implementation.

    The previous version called build_signal_combination(signals) with a
    bare list of strings; the real function takes keyword-only arguments
    (signals as list[dict], plus pick_type/selection) -- that call always
    raised TypeError, silently swallowed by the except below, so this always
    contributed 0. It also read get_signal_combination_performance(), which
    queries signal_combination_memory -- a table confirmed to have zero rows
    in production; nothing in this app has ever written to it. Delegating to
    weighted_signal_combination_memory fixes both: it reads from the table
    that's actually populated (signal_combination_outcomes) and blends
    exact-combo / league / country scopes exactly like the main prediction
    path does.
    """
    try:
        from app.storage.league_memory import weighted_signal_combination_memory

        signals = _build_analysis_signals(item, engine_pick)
        if not signals:
            return 0.0
        pseudo_match = {
            "league_name": item.get("league_name") or item.get("league"),
            "country_name": item.get("country_name") or item.get("country"),
        }
        memory = weighted_signal_combination_memory(
            pseudo_match,
            signals,
            engine_pick.get("type") or item.get("pick_type"),
            item.get("llm_recommendation") or engine_pick.get("selection"),
        )
        return float(memory.get("adjustment") or 0)
    except Exception:
        pass
    return 0.0


def _research_conviction_adj(
    item: dict[str, Any], engine_pick: dict[str, Any], llm_conf: int
) -> float:
    selection = item.get("llm_recommendation") or engine_pick.get("selection") or ""
    sel_lower = str(selection).lower()
    country = str(item.get("country_name") or "").lower().strip()
    source = str(item.get("source") or "")
    pick_type = engine_pick.get("type") or item.get("pick_type") or "match_result"
    league = item.get("league_name") or "__global__"
    try:
        learned = get_learned_thresholds(league=league, pick_type=pick_type)
    except Exception:
        learned = {}
    adj = 0.0
    if "home or away" in sel_lower or sel_lower == "home_or_away":
        adj += learned.get("home_or_away_boost", 4)
    if ("away or draw" in sel_lower or sel_lower == "away_or_draw") and llm_conf >= 72:
        adj += learned.get("away_or_draw_penalty", -3)
    if country in _get_dynamic_rules()["trust_countries"]:
        adj += learned.get("trust_country_boost", 3)
    if source == "sportybet_market_signal":
        adj += learned.get("sportybet_signal_boost", 5)
    return adj


def _optimal_profile_score(
    item: dict[str, Any], engine_pick: dict[str, Any], llm_conf: int, odds: float
) -> int:
    sel_lower = str(
        item.get("llm_recommendation") or engine_pick.get("selection") or ""
    ).lower()
    country = str(item.get("country_name") or "").lower().strip()
    source = str(item.get("source") or "")
    pick_type = engine_pick.get("type") or item.get("pick_type") or "match_result"
    league = item.get("league_name") or "__global__"
    try:
        conf_threshold = get_learned_thresholds(league=league, pick_type=pick_type).get(
            "min_confidence", 72.0
        )
    except Exception:
        conf_threshold = 72.0
    score = 0
    if "home or away" in sel_lower or sel_lower == "home_or_away":
        score += 2  # strongest signal — counts twice
    if llm_conf >= conf_threshold:
        score += 1
    if 1.50 <= odds <= 1.99:
        score += 1
    if country in _get_dynamic_rules()["trust_countries"]:
        score += 1
    if source == "sportybet_market_signal":
        score += 1
    return min(score, 6)


def _build_analysis_signals(
    item: dict[str, Any], engine_pick: dict[str, Any]
) -> list[dict[str, Any]]:
    signals = []
    for factor in item.get("key_factors") or []:
        signals.append({"name": str(factor), "value": 0.7, "source": "llm"})
    if item.get("market_signal"):
        signals.append({"name": str(item["market_signal"]), "value": 0.6, "source": "llm"})
    if item.get("btts") is not None:
        signals.append({"name": "btts", "value": 0.8 if item["btts"] else 0.2, "source": "llm"})
    if item.get("over_2_5") is not None:
        signals.append({"name": "over_2_5", "value": 0.8 if item["over_2_5"] else 0.2, "source": "llm"})
    if engine_pick.get("selection"):
        signals.append({
            "name": str(engine_pick["selection"]),
            "value": engine_pick.get("confidence", 50) / 100,
            "source": "engine",
        })
    return signals


# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------

