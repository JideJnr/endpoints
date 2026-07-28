"""Evidence-first Ollama prediction pipeline with a rules-engine fallback.

The module deliberately keeps each evidence step independent: a missing source or
model failure produces a useful sentence and never prevents the final decision.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import sqlite3
import time
from typing import Any

from app.league_memory import DB_PATH, _init_db

logger = logging.getLogger(__name__)

H2H_FALLBACK = "No H2H history available for this fixture."
COMMON_FALLBACK = "No common opponents found in recent history."
FORM_FALLBACK = "Standings data unavailable; form data not present."
ODDS_FALLBACK = "Odds movement data unavailable."
SIMILAR_FALLBACK = "No tier-comparable similar matches found."


@dataclass
class TeamBehaviourProfile:
    team_name: str
    btts_rate: float = 0.0
    over_2_5_rate: float = 0.0
    clean_sheet_rate: float = 0.0
    comeback_rate: float = 0.0
    high_scorer_flag: int = 0
    loss_to_nil_rate: float = 0.0
    sample_size: int = 0
    computed_at: str = ""


@dataclass
class ReasoningContext:
    match_name: str
    h2h_statement: str
    common_opponent_statement: str
    form_statement: str
    odds_statement: str
    similar_matches_statement: str


@dataclass
class MarketCandidate:
    market_key: str
    label: str
    score: float


def _name(doc: dict[str, Any]) -> str:
    return str(doc.get("name") or doc.get("sportybet_name") or doc.get("match_name") or "Unknown fixture")


def _teams(doc: dict[str, Any]) -> tuple[str, str]:
    detail = doc.get("sofascore_detail") or {}
    home = detail.get("home_team") or detail.get("homeTeam") or doc.get("home_team") or doc.get("home") or ""
    away = detail.get("away_team") or detail.get("awayTeam") or doc.get("away_team") or doc.get("away") or ""
    home = home.get("name", "") if isinstance(home, dict) else home
    away = away.get("name", "") if isinstance(away, dict) else away
    if not home and " vs " in _name(doc):
        home, away = _name(doc).split(" vs ", 1)
    return str(home), str(away)


def _tournament(doc: dict[str, Any]) -> str:
    return str(doc.get("tournament") or doc.get("league_name") or ((doc.get("sofascore_detail") or {}).get("tournament") or {}).get("name") or "")


def _best_odds(doc: dict[str, Any]) -> float:
    odds = doc.get("odds") or doc.get("markets") or ((doc.get("sportybet") or {}).get("odds")) or {}
    values: list[float] = []
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values(): walk(item)
        elif isinstance(value, list):
            for item in value: walk(item)
        else:
            try:
                n = float(value)
                if n > 1: values.append(n)
            except (TypeError, ValueError): pass
    walk(odds)
    return max(values) if values else 0.0


def classify_tournament_tier(tournament_name: str) -> int:
    name = (tournament_name or "").lower()
    if any(x in name for x in ("champions league", "premier league", "laliga", "serie a", "bundesliga", "ligue 1")):
        return 1
    if any(x in name for x in ("europa league", "championship", "eredivisie", "primeira liga", "super lig")):
        return 2
    return 3


def sort_gate(matches: list[dict]) -> list[dict]:
    for doc in matches:
        best = _best_odds(doc)
        doc["low_value_odds"] = best < 1.3
    def key(doc: dict) -> tuple:
        best = _best_odds(doc)
        start = doc.get("start_time") or doc.get("match_date") or ""
        return (str(start), classify_tournament_tier(_tournament(doc)), 0 if 1.3 <= best <= 3.5 else 1, -best)
    return sorted(matches, key=key)


def _apply_recency_decay(h2h_matches: list[dict]) -> list[dict]:
    for index, match in enumerate(h2h_matches):
        match["weight"] = 1.0 if index < 2 else 0.6 if index < 5 else 0.3
    return h2h_matches


def _score(match: dict, side: str) -> float | None:
    keys = (f"{side}_goals", f"score_{side}", side)
    score = match.get("score") or {}
    for key in keys:
        value = match.get(key, score.get(side) if isinstance(score, dict) else None)
        try: return float(value)
        except (TypeError, ValueError): continue
    return None


def _build_h2h_statement(h2h_matches: list[dict]) -> str:
    if not h2h_matches: return H2H_FALLBACK
    rows = _apply_recency_decay(h2h_matches)
    total = home = draw = away = 0.0
    for row in rows:
        hg, ag, weight = _score(row, "home"), _score(row, "away"), float(row.get("weight", 1))
        if hg is None or ag is None: continue
        total += weight
        if hg > ag: home += weight
        elif ag > hg: away += weight
        else: draw += weight
    if not total: return H2H_FALLBACK
    return f"Decay-weighted H2H: home wins {home/total:.0%}, draws {draw/total:.0%}, away wins {away/total:.0%} across {len(rows)} meetings."


def _ollama_model() -> str | None:
    """Return the first available local model via the router."""
    from app.ai_router import get_router
    return get_router().best_available()


def _call_provider(model: str, prompt: str, timeout: int) -> str:
    """Route through AIRouter — Ollama primary, Groq final fallback."""
    from app.ai_router import get_router
    task = "reasoning" if model not in ("groq",) else "analysis"
    return get_router().call_auto(prompt, task=task)


# Kept as an internal compatibility alias for existing step tests/call sites.
def _ollama(model: str, prompt: str, timeout: int) -> str:
    from app.ai_router import get_router
    # Step functions pass the model name as a hint for task routing
    task = "reasoning"
    return get_router().call_auto(prompt, task=task)


def _step_h2h(doc: dict, model: str, timeout: int = 20) -> str:
    try:
        matches = list(doc.get("h2h") or doc.get("h2h_matches") or [])
        event_id = doc.get("sofascore_id") or ((doc.get("sofascore_detail") or {}).get("id"))
        if not matches and event_id:
            from app.sofascore_client import fetch_h2h
            raw = fetch_h2h(int(event_id)) or {}
            matches = raw.get("events", raw) if isinstance(raw, dict) else raw
        evidence = _build_h2h_statement(matches if isinstance(matches, list) else [])
        if evidence == H2H_FALLBACK: return evidence
        return _ollama(model, f"Summarise this H2H evidence for a football prediction in one factual sentence: {evidence}", timeout) or evidence
    except Exception as exc:
        logger.warning("AI step h2h failed: %s", exc)
        return H2H_FALLBACK


def _step_common_opponent(doc: dict, model: str, timeout: int = 20) -> str:
    try:
        home, away = _teams(doc)
        if not home or not away: return COMMON_FALLBACK
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            rows = conn.execute("""select home_team, away_team, final_home_goals, final_away_goals from matches
                where is_finished=1 and (lower(home_team)=lower(?) or lower(away_team)=lower(?) or lower(home_team)=lower(?) or lower(away_team)=lower(?))
                order by last_seen_at desc limit 40""", (home, home, away, away)).fetchall()
        opponents = set()
        for h, a, *_ in rows:
            if str(h).lower() == home.lower(): opponents.add(str(a).lower())
            if str(a).lower() == home.lower(): opponents.add(str(h).lower())
        away_opponents = {str(a).lower() for h, a, *_ in rows if str(h).lower() == away.lower()} | {str(h).lower() for h, a, *_ in rows if str(a).lower() == away.lower()}
        common = sorted(opponents & away_opponents)
        if not common: return COMMON_FALLBACK
        evidence = f"Both teams have faced common recent opponents: {', '.join(common[:5])}."
        return _ollama(model, f"Give one cautious football insight from: {evidence}", timeout) or evidence
    except Exception as exc:
        logger.warning("AI step common_opponent failed: %s", exc)
        return COMMON_FALLBACK


def _step_form(doc: dict, model: str, timeout: int = 20) -> str:
    try:
        standings = doc.get("standings") or ((doc.get("sofascore_detail") or {}).get("standings"))
        if not standings: return FORM_FALLBACK
        home, away = _teams(doc)
        evidence = f"Form/standings for {home} vs {away}: {str(standings)[:380]}"
        return _ollama(model, f"Summarise in one factual sentence: {evidence}", timeout) or evidence
    except Exception as exc:
        logger.warning("AI step form failed: %s", exc)
        return FORM_FALLBACK


def _classify_odds_movement(opening: float, current: float) -> str:
    return "shortened" if current < opening else "drifted" if current > opening else "stable"


def _step_odds(doc: dict, model: str, timeout: int = 20) -> str:
    try:
        match_id = str(doc.get("sportybet_id") or doc.get("id") or "")
        if not match_id: return ODDS_FALLBACK
        from app.agent_tools import get_odds_movement
        data = get_odds_movement(match_id) or {}
        if not data: return ODDS_FALLBACK
        evidence = f"Odds movement: {json.dumps(data, default=str)[:350]}"
        return _ollama(model, f"State the market signal in one cautious sentence: {evidence}", timeout) or evidence
    except Exception as exc:
        logger.warning("AI step odds failed: %s", exc)
        return ODDS_FALLBACK


def apply_tier_filter(candidates: list[dict], target_tier: int) -> list[dict]:
    return [item for item in candidates if target_tier != 1 or classify_tournament_tier(str(item.get("league_name") or item.get("tournament") or "")) != 3]


def _step_similar_matches(doc: dict, model: str, timeout: int = 20) -> str:
    try:
        from app.similar_matches import find_similar_matches
        raw = find_similar_matches(doc) or {}
        candidates = raw.get("matches", raw.get("similar_matches", [])) if isinstance(raw, dict) else raw
        candidates = apply_tier_filter(list(candidates or []), classify_tournament_tier(_tournament(doc)))
        if not candidates: return SIMILAR_FALLBACK
        evidence = f"Comparable historical matches: {json.dumps(candidates[:3], default=str)[:420]}"
        return _ollama(model, f"Give one evidence-only insight: {evidence}", timeout) or evidence
    except Exception as exc:
        logger.warning("AI step similar_matches failed: %s", exc)
        return SIMILAR_FALLBACK


def _history_for_team(team_name: str, conn: sqlite3.Connection) -> list[tuple[int, int, bool]]:
    if not team_name: return []
    rows = conn.execute("""select home_team, away_team, final_home_goals, final_away_goals from matches
        where is_finished=1 and (lower(home_team)=lower(?) or lower(away_team)=lower(?))
        order by last_seen_at desc limit 10""", (team_name, team_name)).fetchall()
    result = []
    for home, away, hg, ag in rows:
        if hg is None or ag is None: continue
        is_home = str(home).lower() == team_name.lower()
        result.append((int(hg if is_home else ag), int(ag if is_home else hg), is_home))
    return result


def derive_team_profile(team_name: str, conn: sqlite3.Connection) -> TeamBehaviourProfile:
    history = _history_for_team(team_name, conn)
    n = len(history); now = datetime.now(timezone.utc).isoformat()
    if n < 3: return TeamBehaviourProfile(team_name=team_name, sample_size=n, computed_at=now)
    scored = [x[0] for x in history]; conceded = [x[1] for x in history]
    return TeamBehaviourProfile(team_name, sum(a > 0 and b > 0 for a,b,_ in history)/n, sum(a+b > 2 for a,b,_ in history)/n,
        sum(b == 0 for b in conceded)/n, 0.0, int(sum(a >= 2 for a in scored)/n >= .6), sum(a == 0 and b > 0 for a,b,_ in history)/n, n, now)


def persist_team_profile(profile: TeamBehaviourProfile, conn: sqlite3.Connection) -> None:
    conn.execute("""insert or replace into team_behaviour_profiles values (?, ?, ?, ?, ?, ?, ?, ?, ?)""", tuple(asdict(profile).values()))


def shortlist_markets(home: TeamBehaviourProfile, away: TeamBehaviourProfile) -> list[MarketCandidate]:
    one_x_two = [MarketCandidate("home_win", "Home win", .5 + (home.clean_sheet_rate-away.loss_to_nil_rate)/2), MarketCandidate("draw", "Draw", .55), MarketCandidate("away_win", "Away win", .5 + (away.clean_sheet_rate-home.loss_to_nil_rate)/2)]
    if min(home.sample_size, away.sample_size) < 3: return sorted(one_x_two, key=lambda x: x.score, reverse=True)[:3]
    both = (home.btts_rate + away.btts_rate)/2; over = (home.over_2_5_rate + away.over_2_5_rate)/2
    candidates = one_x_two + [MarketCandidate("btts_yes", "Both teams to score", both), MarketCandidate("over_2_5", "Over 2.5 goals", over), MarketCandidate("under_2_5", "Under 2.5 goals", 1-over), MarketCandidate("btts_no", "BTTS No", 1-both)]
    selected = [item for item in candidates if item.score >= .55]
    if not any(item.market_key in {"home_win", "draw", "away_win"} for item in selected): selected.append(max(one_x_two, key=lambda x: x.score))
    return sorted(selected, key=lambda x: x.score, reverse=True)[:5]


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
        from app.competition_analyser import get_latest_analysis, init_competition_analysis_table
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


def _call_decider(response_chain: list[str], home_profile: TeamBehaviourProfile, away_profile: TeamBehaviourProfile, shortlisted_markets: list[MarketCandidate], similar_match_history: Any, match_name: str, model: str, timeout: int = 45, competition_context: str | None = None) -> dict:
    from app.ai_router import get_router, parse_json_response
    context_block = f"Competition context: {competition_context} | " if competition_context else ""
    prompt = f"Decide football prediction for {match_name}. {context_block}Evidence: {' | '.join(response_chain)}. Markets: {[asdict(x) for x in shortlisted_markets]}. Return JSON only with market,outcome,confidence,value_bet,btts,over_2_5,key_factors,reasoning."
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
    from app.prediction_flow import apply_prediction_state
    logger.warning("Rules-engine fallback invoked: %s", reason)
    result = apply_prediction_state(doc, **kwargs)
    result["prediction_source"] = "rules_engine_fallback"
    if isinstance(result.get("prediction"), dict): result["prediction"]["prediction_source"] = "rules_engine_fallback"
    return result


def run_ai_prediction_with_fallback(doc: dict[str, Any], *, match_id: str | None = None, match_date: str | None = None, source: str = "enriched_ensemble", attach_brain: bool = False, allow_repeat: bool = False, use_ollama_pipeline: bool | None = None) -> dict[str, Any]:
    kwargs = dict(match_id=match_id, match_date=match_date, source=source, attach_brain=attach_brain, allow_repeat=allow_repeat, use_ollama_pipeline=use_ollama_pipeline)
    try:
        from app.ai_router import get_router
        router = get_router()
        model = router.best_available() or ("groq" if router.is_groq_available() else None)
        if not model:
            result = _rules_fallback(doc, "unavailable", **kwargs)
            result["competition_analysis_used"] = False
            result["competition_analysis_key"] = None
            return result
        doc["low_value_odds"] = _best_odds(doc) < 1.3
        logger.info("AI pipeline match=%s sportybet_id=%s tier=%s odds=%s low_value=%s", _name(doc), doc.get("sportybet_id"), classify_tournament_tier(_tournament(doc)), _best_odds(doc), doc["low_value_odds"])
        _init_db()
        competition_context: str | None = None
        competition_analysis_key: str | None = None
        with sqlite3.connect(DB_PATH, timeout=20) as conn:
            home, away = _teams(doc); hp, ap = derive_team_profile(home, conn), derive_team_profile(away, conn)
            persist_team_profile(hp, conn); persist_team_profile(ap, conn); conn.commit()
            competition_context = _get_competition_context(doc, conn)
            if competition_context:
                competition_analysis_key = (
                    (doc.get("competition_special") or {}).get("key")
                    or (doc.get("known_competition") or {}).get("key")
                )
        # Run all 5 evidence steps in parallel — they are fully independent.
        # Sequential execution was the single biggest AI pipeline bottleneck
        # (5 × 20s timeout = up to 100s; parallel = ~20s worst case).
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        _steps = [
            (_step_h2h,             H2H_FALLBACK,     0),
            (_step_common_opponent, COMMON_FALLBACK,  1),
            (_step_form,            FORM_FALLBACK,    2),
            (_step_odds,            ODDS_FALLBACK,    3),
            (_step_similar_matches, SIMILAR_FALLBACK, 4),
        ]
        chain = [fallback for _, fallback, _ in _steps]  # pre-fill with fallbacks
        with ThreadPoolExecutor(max_workers=5) as _pool:
            _futures = {
                _pool.submit(fn, doc, model): (fallback, idx)
                for fn, fallback, idx in _steps
            }
            for _future in _as_completed(_futures):
                _fallback, _idx = _futures[_future]
                try:
                    sentence = _future.result()
                    chain[_idx] = sentence or _fallback
                    logger.debug("AI step idx=%d: %s", _idx, chain[_idx])
                except Exception as exc:
                    logger.warning("AI step idx=%d failed: %s", _idx, exc)
        markets = shortlist_markets(hp, ap); logger.debug("Response chain: %s", chain)
        decision = _call_decider(chain, hp, ap, markets, [], _name(doc), model, competition_context=competition_context)
        from app.prediction_flow import apply_prediction_state
        result = apply_prediction_state(doc, **kwargs)
        if result.get("status") != "predicted": return result
        prediction = result["prediction"]
        reasoning_context = {
            **asdict(ReasoningContext(_name(doc), *chain)),
            # Preserve the ordered evidence chain for the API/UI and audits.
            "response_chain": chain,
        }
        prediction.update({"prediction_source":"ollama_pipeline", "ai_provider":model, "reasoning_context":reasoning_context, "market":decision["market"], "outcome":decision["outcome"], "key_factors":decision["key_factors"], "reasoning":decision["reasoning"], "confidence":_convert_confidence(decision["confidence"]), "value_bet":decision["value_bet"], "btts":decision["btts"], "over_2_5":decision["over_2_5"]})
        result["prediction_source"] = "ollama_pipeline"; result["reasoning_context"] = prediction["reasoning_context"]
        result["competition_analysis_used"] = competition_context is not None
        result["competition_analysis_key"] = competition_analysis_key
        logger.info("AI pipeline completed match=%s outcome=%s", _name(doc), decision["outcome"])
        return result
    except Exception as exc:
        logger.exception("AI pipeline failed: %s", exc)
        result = _rules_fallback(doc, "exception", **kwargs)
        result["competition_analysis_used"] = False
        result["competition_analysis_key"] = None
        return result


def job_ai_prediction_queue(batch_size: int = 10) -> dict:
    from app.activity_log import record_activity
    from app.pipeline_registry import is_pipeline_enabled
    if not is_pipeline_enabled("ai_prediction_queue"): return {"status":"skipped", "reason":"pipeline_disabled"}
    record_activity("AI prediction queue started", job="ai_prediction_queue", status="running")
    _init_db(); summary = {"status":"ok", "job":"job_ai_prediction_queue", "batch_size":batch_size, "processed":0, "ollama_used":0, "fallback_used":0, "errors":0}
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        rows = conn.execute("""select match_id, match_date, raw_enriched from match_buffer
            where raw_enriched is not null and is_finished=0 and is_live=0
              and json_extract(raw_enriched, '$.prediction') is null""").fetchall()
    docs = []
    for match_id, date, raw in rows:
        try: docs.append({**json.loads(raw), "match_id":match_id, "match_date":date})
        except Exception: summary["errors"] += 1
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
    from app.buffer import store_enriched as _store

    def _process_one(doc: dict) -> dict:
        outcome = run_ai_prediction_with_fallback(
            doc,
            match_id=str(doc.get("match_id") or ""),
            match_date=doc.get("match_date"),
        )
        doc["ai_prediction_queue_pending"] = False
        _store(str(doc.get("match_id") or ""), doc)
        return outcome

    # Run up to 3 matches concurrently — each prediction is I/O-bound
    # (SportyBet HTTP + Ollama HTTP) so threads don't contend on the GIL.
    with _TPE(max_workers=3) as pool:
        futures = {pool.submit(_process_one, doc): doc for doc in sort_gate(docs)[:batch_size]}
        for future in _ac(futures):
            try:
                outcome = future.result()
                summary["processed"] += 1
                summary["ollama_used" if outcome.get("prediction_source") == "ollama_pipeline" else "fallback_used"] += 1
            except Exception as exc:
                logger.exception("AI queue match failed: %s", exc)
                summary["errors"] += 1
    record_activity("AI prediction queue completed", job="ai_prediction_queue", status="ok" if not summary["errors"] else "error", details=summary)
    return summary
