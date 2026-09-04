"""Ties the pipeline together: the decider call, the rules-engine fallback,
the top-level entry point (``run_ai_prediction_with_fallback``), and the
scheduled job queue that drives it (``job_ai_prediction_queue``).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import time
from typing import Any

from app.storage.db import db_conn, _init_db

from app.ai.prediction_pipeline.weights import get_specialist_weights
from app.ai.prediction_pipeline.evidence import (
    H2H_FALLBACK,
    COMMON_FALLBACK,
    FORM_FALLBACK,
    ODDS_FALLBACK,
    SIMILAR_FALLBACK,
    _evidence_status,
    _name,
    _teams,
    _tournament,
    _best_odds,
    classify_tournament_tier,
    sort_gate,
    _step_h2h,
    _step_common_opponent,
    _step_form,
    _step_odds,
    _step_similar_matches,
    _step_team_history,
)
from app.ai.prediction_pipeline.teams import TeamBehaviourProfile, derive_team_profile, persist_team_profile
from app.ai.prediction_pipeline.markets import MarketCandidate, shortlist_markets

logger = logging.getLogger(__name__)

# ── Per-match decision cache ───────────────────────────────────────────────────
# The ai_prediction_queue runs every 5 minutes. Without a cache the same match
# re-runs all 6 evidence steps + the decider on every cycle until the cooldown
# in prediction_flow._recent_ungraded_prediction() (3 h for prematch, 2 min for
# live) finally blocks it. That's up to 36 unnecessary LLM calls per prematch
# match per hour.
#
# Cache key: SHA-256 of match_id + match_date + tournament. Stable across
# queue cycles for the same fixture; changes naturally if the match is
# rescheduled or re-ingested with a different date.
#
# TTL: 20 minutes — long enough to span several queue cycles, short enough
# that a match whose enrichment data materially changes (live score update,
# fresh odds snapshot) will get a new pipeline run.
#
# Max size: 500 entries. Each entry holds the full decision dict (~1 KB),
# so worst case is ~500 KB of process heap. Eviction is LRU-style (pop the
# oldest key when full) identical to team_watcher_engine._AI_MODEL_CACHE.
_DECISION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DECISION_CACHE_TTL = 1200   # 20 minutes
_DECISION_CACHE_MAX_SIZE = 500


def _decision_cache_key(doc: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "match_id": doc.get("match_id") or doc.get("sportybet_id") or doc.get("id") or "",
            "match_date": doc.get("match_date") or "",
            "tournament": doc.get("tournament") or doc.get("league_name") or "",
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _decision_cache_get(doc: dict[str, Any]) -> dict[str, Any] | None:
    key = _decision_cache_key(doc)
    entry = _DECISION_CACHE.get(key)
    if entry is None:
        return None
    ts, decision = entry
    if time.monotonic() - ts >= _DECISION_CACHE_TTL:
        _DECISION_CACHE.pop(key, None)
        return None
    return decision


def _decision_cache_set(doc: dict[str, Any], decision: dict[str, Any]) -> None:
    key = _decision_cache_key(doc)
    now = time.monotonic()
    if len(_DECISION_CACHE) >= _DECISION_CACHE_MAX_SIZE:
        # Evict all expired entries first; if still full, drop the oldest.
        expired = [k for k, (ts, _) in _DECISION_CACHE.items() if now - ts >= _DECISION_CACHE_TTL]
        for k in expired:
            _DECISION_CACHE.pop(k, None)
        while len(_DECISION_CACHE) >= _DECISION_CACHE_MAX_SIZE:
            _DECISION_CACHE.pop(next(iter(_DECISION_CACHE)), None)
    _DECISION_CACHE[key] = (now, decision)



@dataclass
class ReasoningContext:
    match_name: str
    h2h_statement: str
    common_opponent_statement: str
    form_statement: str
    odds_statement: str
    similar_matches_statement: str
    team_history_statement: str


def _truncate_competition_context(text: str) -> str:
    """Truncate at nearest sentence boundary at or before 100-token limit."""
    if len(text) // 4 <= 100:
        return text
    target = 400  # 100 tokens * 4 chars
    truncated = text[:target]
    boundary = truncated.rfind(". ")
    return (truncated[:boundary + 1] if boundary >= 0 else truncated).strip()


def _get_competition_context(doc: dict, conn) -> str | None:
    """Return truncated competition analysis text, or None on any failure."""
    try:
        key = (
            (doc.get("competition_special") or {}).get("key")
            or (doc.get("known_competition") or {}).get("key")
        )
        if not key:
            return None
        from app.competition.competition_analyser import get_latest_analysis, init_competition_analysis_table
        init_competition_analysis_table(conn)
        row = get_latest_analysis(key, conn)
        if not row:
            return None
        text = row.get("analysis_text") or ""
        if not text:
            return None
        truncated = _truncate_competition_context(text)
        logger.debug(
            "_get_competition_context: key=%s generated_at=%s",
            key, row.get("generated_at"),
        )
        return truncated
    except Exception as exc:
        logger.debug("_get_competition_context failed (non-critical): %s", exc)
        return None


def _get_structured_competition_intelligence(doc: dict) -> dict | None:
    """Same de-noised competition/team-watcher edge that now feeds the
    statistical ensemble (app/models/ensemble.py's "competition_intelligence"
    weight) and the deterministic signals list
    (app/enrichment/enriched_prediction.py::_competition_intelligence_signal),
    reused here instead of reimplemented a third time. Previously this AI
    pipeline -- the one that actually runs live predictions -- only ever
    saw a truncated free-text analysis blob (_get_competition_context
    below), never the structured table/form/team-watcher numbers; the
    structured version was only ever wired into the separate advisory
    app/ai/llm_analysis.py path. Returns None (silently, non-critical) on
    any error or when the signal itself decides the data is too thin/
    unavailable -- same gating as everywhere else it's used.
    """
    try:
        from app.enrichment.enriched_prediction import _competition_intelligence_signal
        return _competition_intelligence_signal(doc)
    except Exception as exc:
        logger.debug("_get_structured_competition_intelligence failed (non-critical): %s", exc)
        return None


def _call_decider(response_chain: list[str], home_profile: TeamBehaviourProfile, away_profile: TeamBehaviourProfile, shortlisted_markets: list[MarketCandidate], similar_match_history: Any, match_name: str, model: str, timeout: int = 45, competition_context: str | None = None, competition_intelligence: dict | None = None, specialist_weights: dict[str, float] | None = None) -> dict:
    from app.ai.ai_router import get_router, parse_json_response
    context_block = f"Competition context: {competition_context} | " if competition_context else ""
    if competition_intelligence and competition_intelligence.get("value"):
        # Compact numeric form (not prose) -- table/form/team-watcher edges
        # on the same -8..+8-ish home/away scale the decider already sees
        # from the ensemble's own signals, plus which components actually
        # had enough data to be used.
        ci_value = competition_intelligence["value"]
        context_block += (
            f"Competition intelligence "
            f"(classification={ci_value.get('classification')}, "
            f"home-advantage direction, "
            f"components_used={ci_value.get('components_used')}): "
            f"table_edge_ppg={ci_value.get('table_edge_ppg')}, "
            f"form_edge={ci_value.get('form_edge')}, "
            f"watcher_edge={ci_value.get('watcher_edge')}, "
            f"net_direction={ci_value.get('direction')} | "
        )
    unavailable = [
        label for label, statement in zip(
            ("H2H", "common opponents", "form", "odds", "similar matches", "team previous matches"),
            response_chain,
        ) if _evidence_status(statement) == "unavailable"
    ]
    # Build weighted analyst block so the AI agent knows which specialists to trust more
    weights = specialist_weights or {}
    analyst_labels = [
        "H2H Analyst", "Common Opponent Analyst", "Form Analyst",
        "Market Odds Analyst", "Similar Match Analyst", "Team Previous Matches Analyst",
    ]
    weighted_evidence = [
        {
            "analyst": analyst_labels[i],
            "finding": response_chain[i],
            "weight": round(weights.get(analyst_labels[i], 1.0), 3),
            "available": _evidence_status(response_chain[i]) == "available",
        }
        for i in range(len(response_chain))
    ]
    prompt = (
        f"Decide football prediction for {match_name}. {context_block}"
        f"Weighted analyst findings (weight reflects historical accuracy — higher = more reliable): "
        f"{json.dumps(weighted_evidence, default=str)[:600]}. "
        f"Unavailable evidence: {unavailable or 'none'}. Do not present unavailable evidence as a positive factor. "
        f"Markets: {[asdict(x) for x in shortlisted_markets]}. "
        "Return JSON only with market,outcome,confidence,value_bet,btts,over_2_5,key_factors,reasoning."
    )
    started = time.monotonic()
    raw = get_router().call_analysis(prompt)
    logger.debug("AI decider elapsed_ms=%d", (time.monotonic()-started)*1000)
    parsed = parse_json_response(raw)
    required = {"market", "outcome", "confidence", "value_bet", "btts", "over_2_5", "key_factors", "reasoning"}
    if not required <= parsed.keys(): raise ValueError("Decider response missing required keys")
    return parsed


def _convert_confidence(raw: float) -> int:
    return max(0, min(100, round(float(raw) * 100)))


def _rules_fallback(doc: dict, reason: str, **kwargs: Any) -> dict:
    from app.utils.prediction_flow import apply_prediction_state
    logger.warning("Rules-engine fallback invoked: %s", reason)
    # Never trigger the LLM pipeline from the fallback path — it already failed
    # or was unavailable, so forcing use_llm_pipeline=False here prevents a
    # second independent pipeline run that would overwrite the DB row and return
    # the wrong pipeline's output via the API.
    kwargs["use_llm_pipeline"] = False
    result = apply_prediction_state(doc, **kwargs)
    result["prediction_source"] = "rules_engine_fallback"
    if isinstance(result.get("prediction"), dict): result["prediction"]["prediction_source"] = "rules_engine_fallback"
    return result


def run_ai_prediction_with_fallback(doc: dict[str, Any], *, match_id: str | None = None, match_date: str | None = None, source: str = "enriched_ensemble", attach_brain: bool = False, allow_repeat: bool = False, use_llm_pipeline: bool | None = None) -> dict[str, Any]:
    kwargs = dict(
        match_id=match_id,
        match_date=match_date,
        source=source,
        attach_brain=attach_brain,
        allow_repeat=allow_repeat,
        # Fallback path must never trigger the LLM pipeline — use_llm_pipeline
        # is overridden to False in _rules_fallback above, but set it False here
        # too so the kwargs dict is never accidentally used with True elsewhere.
        use_llm_pipeline=False,
    )
    try:
        from app.ai.ai_router import get_router
        router = get_router()
        model = router.best_available()
        if not model:
            result = _rules_fallback(doc, "unavailable", **kwargs)
            result["competition_analysis_used"] = False
            result["competition_analysis_key"] = None
            return result
        doc["low_value_odds"] = _best_odds(doc) < 1.3
        logger.info("AI pipeline match=%s sportybet_id=%s tier=%s odds=%s low_value=%s", _name(doc), doc.get("sportybet_id"), classify_tournament_tier(_tournament(doc)), _best_odds(doc), doc["low_value_odds"])

        # ── Decision cache check ───────────────────────────────────────────
        # If an identical match already went through the full 7-call pipeline
        # within the last 20 minutes, reuse the cached decision instead of
        # re-firing all 6 evidence steps + decider. The prediction_flow cooldown
        # gate (3 h prematch / 2 min live) prevents double-recording regardless,
        # but by the time it fires the LLM calls have already happened and been
        # billed. Checking the cache here short-circuits before any network call.
        cached_decision = _decision_cache_get(doc)
        if cached_decision is not None:
            logger.debug("AI pipeline decision cache hit match=%s", _name(doc))
            # Re-enter from the build step — chain and profiles aren't needed
            # because we're replaying a previously validated decision.
            decision = cached_decision["decision"]
            chain = cached_decision["chain"]
            hp = cached_decision["hp"]
            ap = cached_decision["ap"]
            competition_context = cached_decision.get("competition_context")
            competition_analysis_key = cached_decision.get("competition_analysis_key")
            competition_intelligence = cached_decision.get("competition_intelligence")
            specialist_weights = cached_decision["specialist_weights"]
            # Jump straight to the build step below — skip all LLM calls.
            _use_cached = True
        else:
            _use_cached = False

        if not _use_cached:
            _init_db()
            competition_context = None
            competition_analysis_key = None
            with db_conn(timeout=20) as conn:
                home, away = _teams(doc); hp, ap = derive_team_profile(home, conn), derive_team_profile(away, conn)
                persist_team_profile(hp, conn); persist_team_profile(ap, conn); conn.commit()
                competition_context = _get_competition_context(doc, conn)
                if competition_context:
                    competition_analysis_key = (
                        (doc.get("competition_special") or {}).get("key")
                        or (doc.get("known_competition") or {}).get("key")
                    )
            # Structured table/form/team-watcher edge (same source now feeding
            # the ensemble) -- independent of and in addition to the free-text
            # competition_context above, so this pipeline sees the real numbers
            # even on a match where no free-text analysis has been generated.
            competition_intelligence = _get_structured_competition_intelligence(doc)
            # Run all 5 evidence steps in parallel — they are fully independent.
            # Sequential execution was the single biggest AI pipeline bottleneck
            # (5 x 20s timeout = up to 100s; parallel = ~20-25s worst case).
            #
            # IMPORTANT: each step's own `timeout=` only bounds urllib's socket
            # connect/read calls. DNS resolution (getaddrinfo) happens BEFORE the
            # socket exists and is NOT covered by that timeout at all in the
            # stdlib — on a flaky resolver (seen in practice on Windows) a step
            # can hang far past its stated timeout with no way to interrupt it.
            # Two defenses against that here:
            #   1. `as_completed(..., timeout=EVIDENCE_STEPS_DEADLINE)` bounds how
            #      long we personally wait, instead of waiting for every future.
            #   2. `_pool.shutdown(wait=False, cancel_futures=True)` instead of a
            #      `with`-block (which calls shutdown(wait=True) and would just
            #      re-introduce the same unbounded hang on exit).
            # A step that's still running when we give up keeps running in its
            # thread in the background (Python can't force-kill a thread) but no
            # longer blocks this request or the app.
            from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed, TimeoutError as _FutureTimeoutError
            EVIDENCE_STEPS_DEADLINE = 35  # slightly above the slowest single step's own 25s timeout
            DECIDER_DEADLINE = 40  # slightly above call_analysis's own 30s timeout
            _steps = [
                (_step_h2h,             H2H_FALLBACK,     0),
                (_step_common_opponent, COMMON_FALLBACK,  1),
                (_step_form,            FORM_FALLBACK,    2),
                (_step_odds,            ODDS_FALLBACK,    3),
                (_step_similar_matches, SIMILAR_FALLBACK, 4),
                (_step_team_history,     "Team previous-match history unavailable.", 5),
            ]
            chain = [fallback for _, fallback, _ in _steps]  # pre-fill with fallbacks
            _pool = ThreadPoolExecutor(max_workers=len(_steps))
            try:
                _futures = {
                    _pool.submit(fn, doc, model): (fallback, idx)
                    for fn, fallback, idx in _steps
                }
                try:
                    for _future in _as_completed(_futures, timeout=EVIDENCE_STEPS_DEADLINE):
                        _fallback, _idx = _futures[_future]
                        try:
                            sentence = _future.result()
                            chain[_idx] = sentence or _fallback
                            logger.debug("AI step idx=%d: %s", _idx, chain[_idx])
                        except Exception as exc:
                            logger.warning("AI step idx=%d failed: %s", _idx, exc)
                except _FutureTimeoutError:
                    logger.warning(
                        "AI evidence steps exceeded %ss deadline; proceeding with fallback text for whichever steps didn't finish",
                        EVIDENCE_STEPS_DEADLINE,
                    )
            finally:
                _pool.shutdown(wait=False, cancel_futures=True)
            markets = shortlist_markets(hp, ap); logger.debug("Response chain: %s", chain)
            specialist_weights = get_specialist_weights(league=_tournament(doc))
            # Same DNS-hang risk applies to the single decider call — bound it
            # with its own throwaway single-worker pool so a stuck call can be
            # abandoned (raising, which the outer except already falls back on)
            # instead of hanging the request forever.
            _decider_pool = ThreadPoolExecutor(max_workers=1)
            try:
                _decider_future = _decider_pool.submit(
                    _call_decider, chain, hp, ap, markets, [], _name(doc), model,
                    competition_context=competition_context,
                    competition_intelligence=competition_intelligence,
                    specialist_weights=specialist_weights,
                )
                try:
                    decision = _decider_future.result(timeout=DECIDER_DEADLINE)
                except _FutureTimeoutError as exc:
                    raise RuntimeError(f"AI decider exceeded {DECIDER_DEADLINE}s deadline") from exc
            finally:
                _decider_pool.shutdown(wait=False, cancel_futures=True)

            # Store the fresh result in the decision cache so subsequent queue
            # cycles for the same match skip all LLM calls for the next 20 min.
            _decision_cache_set(doc, {
                "decision": decision,
                "chain": chain,
                "hp": hp,
                "ap": ap,
                "competition_context": competition_context,
                "competition_analysis_key": competition_analysis_key,
                "competition_intelligence": competition_intelligence,
                "specialist_weights": specialist_weights,
            })

        reasoning_context = {
            **asdict(ReasoningContext(_name(doc), *chain)),
            "response_chain": chain,
            "evidence_availability": {
                label: _evidence_status(statement)
                for label, statement in zip(
                    ("h2h", "common_opponents", "form", "odds", "similar_matches", "team_previous_matches"),
                    chain,
                )
            },
            "analysts": [
                {"name": "H2H Analyst",                   "trained_knowledge": "Historical meetings and rivalry pattern reading",          "finding": chain[0], "evidence_status": _evidence_status(chain[0]), "weight": specialist_weights.get("H2H Analyst", 1.0)},
                {"name": "Common Opponent Analyst",        "trained_knowledge": "Shared-opponent performance comparison",                   "finding": chain[1], "evidence_status": _evidence_status(chain[1]), "weight": specialist_weights.get("Common Opponent Analyst", 1.0)},
                {"name": "Form Analyst",                   "trained_knowledge": "Standings, recent form, ratings, and table pressure",      "finding": chain[2], "evidence_status": _evidence_status(chain[2]), "weight": specialist_weights.get("Form Analyst", 1.0)},
                {"name": "Market Odds Analyst",            "trained_knowledge": "Odds movement, pricing pressure, and market signal quality","finding": chain[3], "evidence_status": _evidence_status(chain[3]), "weight": specialist_weights.get("Market Odds Analyst", 1.0)},
                {"name": "Similar Match Analyst",          "trained_knowledge": "Tier-comparable historical match outcomes",                 "finding": chain[4], "evidence_status": _evidence_status(chain[4]), "weight": specialist_weights.get("Similar Match Analyst", 1.0)},
                {"name": "Team Previous Matches Analyst",  "trained_knowledge": "Both teams' full recent finished-match profiles",          "finding": chain[5], "evidence_status": _evidence_status(chain[5]), "weight": specialist_weights.get("Team Previous Matches Analyst", 1.0)},
            ],
        }

        # Build the one canonical prediction dict (with a real "picks" list) here,
        # instead of letting apply_prediction_state's use_llm_pipeline=True branch
        # trigger a second, independent LLM pipeline run (app.ai.llm_pipeline.run_llm_pipeline).
        # That second pipeline's output has no "picks" list, so record_prediction's
        # confidence/no_bet gate silently dropped it before it ever reached
        # prediction_history — this decider's decision is now the only thing recorded.
        from app.storage.league_memory._helpers import build_pick
        confidence = _convert_confidence(decision["confidence"])
        reason_text = decision.get("reasoning")
        if isinstance(reason_text, dict):
            reason_text = " ".join(str(v) for v in reason_text.values() if v)
        reason_text = str(reason_text or "AI evidence-pipeline decision")[:500]
        pick = build_pick(
            str(decision.get("market") or "match_result"),
            str(decision.get("outcome") or ""),
            confidence,
            reason_text,
            source="ai_prediction_pipeline",
            include_market_intent=True,
        )
        signals = [
            {
                "name": f"specialist_{str(a['name']).lower().replace(' ', '_')}",
                "value": a["finding"],
                "impact": round((float(a.get("weight") or 1.0) - 1.0) * 10, 1),
            }
            for a in reasoning_context["analysts"]
        ]
        resolved_match_id = str(match_id or doc.get("sportybet_id") or doc.get("id") or "")
        prebuilt_prediction: dict[str, Any] = {
            "name": _name(doc),
            "tournament": _tournament(doc),
            "country": doc.get("country") or doc.get("category"),
            "match_id": resolved_match_id,
            "sportybet_id": doc.get("sportybet_id") or resolved_match_id,
            "sofascore_id": doc.get("sofascore_id") or ((doc.get("sofascore_detail") or {}).get("id")),
            "match_date": match_date or doc.get("match_date"),
            "prediction_source": "llm_pipeline",
            "ai_provider": model,
            "reasoning_context": reasoning_context,
            "market": decision["market"],
            "outcome": decision["outcome"],
            "key_factors": decision.get("key_factors") or [],
            "reasoning": decision.get("reasoning"),
            "confidence": confidence,
            "value_bet": bool(decision.get("value_bet")),
            "btts": decision.get("btts"),
            "over_2_5": decision.get("over_2_5"),
            "picks": [pick],
            "signals": signals,
        }

        from app.utils.prediction_flow import apply_prediction_state
        result = apply_prediction_state(
            doc,
            match_id=match_id,
            match_date=match_date,
            source=source,
            attach_brain=attach_brain,
            allow_repeat=allow_repeat,
            prebuilt_prediction=prebuilt_prediction,
        )
        if result.get("status") != "predicted": return result
        result["prediction_source"] = "llm_pipeline"
        result["reasoning_context"] = reasoning_context
        result["competition_analysis_used"] = competition_context is not None
        result["competition_analysis_key"] = competition_analysis_key
        result["competition_intelligence_used"] = bool(competition_intelligence)
        logger.info("AI pipeline completed match=%s outcome=%s", _name(doc), decision["outcome"])
        return result
    except Exception as exc:
        logger.exception("AI pipeline failed: %s", exc)
        result = _rules_fallback(doc, "exception", **kwargs)
        result["competition_analysis_used"] = False
        result["competition_analysis_key"] = None
        result["competition_intelligence_used"] = False
        return result


def job_ai_prediction_queue(batch_size: int = 10) -> dict:
    from app.utils.activity_log import record_activity
    from app.scheduling.pipeline_registry import is_pipeline_enabled
    from app.storage.buffer import get_buffered_match as _get_buffered_match, store_enriched as _store
    if not is_pipeline_enabled("ai_prediction_queue"):
        return {"status": "skipped", "reason": "pipeline_disabled"}
    record_activity("AI prediction queue started", job="ai_prediction_queue", status="running")
    _init_db()
    summary = {"status": "ok", "job": "job_ai_prediction_queue", "batch_size": batch_size, "processed": 0, "llm_used": 0, "fallback_used": 0, "deferred_not_ready": 0, "errors": 0}
    now_ts = datetime.now(timezone.utc).timestamp()
    with db_conn(timeout=30) as conn:
        rows = conn.execute(
            """
            select match_id, match_date, raw_enriched
            from match_buffer
            where raw_enriched is not null
              and is_finished = 0
              and json_extract(raw_enriched, '$.sofascore_match_status') != 'srl_skip'
              and (
                    json_extract(raw_enriched, '$.manual_prediction_state') is not null
                 or json_extract(raw_enriched, '$.prediction') is not null
                 or json_extract(raw_enriched, '$.ai_prediction_queue_pending') = 1
                 or (
                      -- Pick up any enriched match that has no prediction yet
                      json_extract(raw_enriched, '$.enriched_at') is not null
                      and json_extract(raw_enriched, '$.prediction') is null
                      and json_extract(raw_enriched, '$.prediction_error') is null
                    )
              )
              and json_extract(raw_enriched, '$.ai_prediction_state') is null
              -- A match with no sofascore_detail can never pass prediction_readiness()
              -- (sofascore_detail is a hard requirement -- see enriched_prediction.py).
              -- The enrichment worker already won't re-attempt a SofaScore match for
              -- an unmatched fixture until its own sofascore_retry_after_ts cooldown
              -- lapses (get_unenriched_batch, ~3h for prematch), so re-selecting such
              -- a row here every 5-10 minutes in between can't find anything new --
              -- skip it until it's actually due for reconsideration. Bind the
              -- current time as a parameter rather than strftime('%s','now') --
              -- that combined with coalesce() silently mis-compares (confirmed
              -- empirically); a bound REAL parameter, the pattern already used
              -- safely elsewhere in this codebase (buffer.py's get_unenriched_batch),
              -- does not have that problem.
              and (
                    json_extract(raw_enriched, '$.sofascore_detail') is not null
                 or coalesce(cast(json_extract(raw_enriched, '$.sofascore_retry_after_ts') as real), 0) <= ?
              )
            """,
            (now_ts,),
        ).fetchall()
    docs = []
    for match_id, date, raw in rows:
        try:
            docs.append({**json.loads(raw), "match_id": match_id, "match_date": date})
        except Exception:
            summary["errors"] += 1
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac

    def _process_one(doc: dict) -> dict:
        if not doc.get("prediction_readiness") or not doc.get("sofascore_detail"):
            try:
                from app.enrichment.match_enrichment import enrich_buffered_match
                enrich_buffered_match(str(doc.get("match_id") or ""), auto_predict=False)
                refreshed = _get_buffered_match(str(doc.get("match_id") or ""))
                if refreshed:
                    doc.update(refreshed)
            except Exception:
                pass

        # Cheap, local, no-network gate BEFORE the expensive work below (team
        # profile derivation, 5 parallel AI evidence steps, the LLM call
        # itself). predict_and_record_enriched already refuses to record a
        # real prediction for a match that isn't SofaScore-enriched yet --
        # that check was correct, it just ran at the very end, after all of
        # that work had already happened, on every not-yet-enriched match,
        # every single queue cycle. Checking readiness here instead means an
        # unenriched match is skipped immediately instead of wastefully
        # attempted. It isn't lost: enrich_worker re-flags
        # ai_prediction_queue_pending=True on every pass until a match is
        # actually ready, so it keeps getting reconsidered -- just cheaply
        # in the meantime, not with a full AI pipeline run each time.
        from app.enrichment.enriched_prediction import prediction_readiness

        readiness = prediction_readiness(doc)
        doc["prediction_readiness"] = readiness
        if not readiness.get("ready"):
            doc["ai_prediction_queue_pending"] = True
            _store(str(doc.get("match_id") or ""), doc)
            return {"status": "deferred", "prediction_source": "not_ready", "readiness": readiness}

        outcome = run_ai_prediction_with_fallback(
            doc,
            match_id=str(doc.get("match_id") or ""),
            match_date=doc.get("match_date"),
            use_llm_pipeline=True,
            attach_brain=True,
        )
        doc["ai_prediction_queue_pending"] = False
        _store(str(doc.get("match_id") or ""), doc)
        return outcome

    with _TPE(max_workers=3) as pool:
        futures = {pool.submit(_process_one, doc): doc for doc in sort_gate(docs)[:batch_size]}
        for future in _ac(futures):
            try:
                outcome = future.result()
                if outcome.get("prediction_source") == "not_ready":
                    summary["deferred_not_ready"] += 1
                else:
                    summary["processed"] += 1
                    summary["llm_used" if outcome.get("prediction_source") == "llm_pipeline" else "fallback_used"] += 1
            except Exception as exc:
                logger.exception("AI queue match failed: %s", exc)
                summary["errors"] += 1
    record_activity("AI prediction queue completed", job="ai_prediction_queue", status="ok" if not summary["errors"] else "error", details=summary)
    return summary
