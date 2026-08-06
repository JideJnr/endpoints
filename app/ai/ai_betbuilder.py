from __future__ import annotations

import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.storage.buffer import get_buffered_match
from app.storage.league_memory import list_prediction_history
from app.research.research_filter import _research_filter_candidate, _normalise_league_key, _get_dynamic_rules


_ANALYSIS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 10 * 60


def enriched_match_analysis(sportybet_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    cache_key = str(sportybet_id)
    now = time.time()
    if not force_refresh:
        cached = _ANALYSIS_CACHE.get(cache_key)
        if cached and now - cached[0] <= _CACHE_TTL_SECONDS:
            return {**cached[1], "cached": True}

    doc = get_buffered_match(cache_key)
    if not doc:
        raise ValueError("Match not found in the active buffer")

    engine_pick = _best_engine_pick(cache_key)
    if not engine_pick:
        engine_pick = _best_pick_from_doc(doc)
    if not engine_pick:
        raise ValueError("No prediction-engine pick found for this match")

    gated = similarity_gate(doc, _similar_matches(doc, limit=10))[:5]
    groq_input = _analysis_doc(doc, engine_pick, gated)
    analysis = _run_openrouter_enriched(groq_input)
    if analysis.get("status") in {"openrouter_unavailable", "agent_build_failed", "error"}:
        return analysis

    groq_selection = analysis.get("recommendation") or analysis.get("groq_recommendation")
    confirmed = _same_outcome(engine_pick.get("selection"), groq_selection)
    result = {
        "status": "success",
        "sportybet_id": cache_key,
        "match_id": cache_key,
        "match_name": doc.get("sportybet_name") or doc.get("match_name") or doc.get("name") or cache_key,
        "league_name": doc.get("tournament") or doc.get("league_name"),
        "country_name": doc.get("category") or doc.get("country_name"),
        "groq_recommendation": groq_selection,
        "groq_confidence": _to_int(analysis.get("confidence"), 0),
        "value_bet": bool(analysis.get("value_bet")),
        "market_signal": analysis.get("market_signal"),
        "btts": analysis.get("btts"),
        "over_2_5": analysis.get("over_2_5"),
        "reasoning": analysis.get("reasoning"),
        "key_factors": analysis.get("key_factors") or [],
        "prediction_engine_pick": engine_pick,
        "similar_matches": gated,
        "similar_matches_used": len(gated),
        "estimated_odds": _pick_decimal_odds(engine_pick),
        "confirmed": confirmed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }
    _ANALYSIS_CACHE[cache_key] = (now, result)
    return result


def build_ai_betbuilder(payload: dict[str, Any]) -> dict[str, Any]:
    target_odds = max(1.01, _to_float(payload.get("target_odds")) or 5.0)
    # Allow a generous ceiling so the selector has room to reach high target odds.
    # Default to 3x the target so there's enough headroom without being unbounded.
    max_total_odds = max(target_odds, _to_float(payload.get("max_total_odds")) or target_odds * 3.0)
    # Scale the candidate pool with the target odds.
    # For 300 odds (≈ 9 legs at 1.9 each) we need a much larger pool to find
    # enough qualifying matches.  Formula: need ≈ log(target) / log(avg_leg_odds)
    # where avg_leg_odds ≈ 1.65.  We fetch 3× that estimate, capped at 200.
    import math as _math
    estimated_legs = max(6, int(_math.log(max(target_odds, 2)) / _math.log(1.65)) + 2)
    default_limit = min(200, estimated_legs * 3)
    candidate_limit = max(10, min(_to_int(payload.get("candidate_limit"), default_limit), 200))
    candidates = upcoming_prediction_candidates(limit=candidate_limit)
    analyses: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if not candidates:
        return {
            "status": "error",
            "message": "No current prediction-engine candidates are available",
            "groq_powered": True,
            "candidate_count": 0,
            "candidate_limit": candidate_limit,
        }

    def _analyse_candidate(candidate: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        match_id = str(candidate.get("match_id") or "")
        if not match_id:
            return candidate, {"status": "skipped", "message": "Candidate has no match id"}
        return candidate, enriched_match_analysis(match_id)

    # Run analyses concurrently. Scale workers with candidate count but stay
    # reasonable to avoid saturating the Groq API rate limit.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=min(10, len(candidates))) as pool:
        futures = [pool.submit(_analyse_candidate, candidate) for candidate in candidates]
        for future in as_completed(futures):
            candidate, analysis = future.result()
            if analysis.get("status") != "success":
                failures.append({
                    "match_id": candidate.get("match_id"),
                    "match": candidate.get("match_name"),
                    "status": analysis.get("status"),
                    "message": analysis.get("message"),
                })
                continue
            analyses.append(analysis)

    if len(analyses) < 2:
        return {
            "status": "error",
            "message": "Fewer than two AI analyses succeeded",
            "groq_powered": True,
            "analyses_run": len(candidates),
            "analyses_succeeded": len(analyses),
            "failures": failures,
        }

    synthesis = synthesize_sure_picks(analyses, target_odds=target_odds, max_total_odds=max_total_odds)

    # Use signal aggregator to calculate learned probabilities for each analysis
    # and boost conviction for picks with proven win history
    for analysis in analyses:
        try:
            from app.models.probability_learner import get_learned_probabilities
            from app.enrichment.signal_aggregator import normalize_signal

            analysis_signals = []
            if analysis.get("key_factors"):
                for factor in analysis.get("key_factors", []):
                    analysis_signals.append({"name": str(factor), "value": 0.7, "source": "groq"})
            if analysis.get("market_signal"):
                analysis_signals.append({"name": str(analysis["market_signal"]), "value": 0.6, "source": "groq"})
            if analysis.get("btts") is not None:
                analysis_signals.append({"name": "btts", "value": 0.8 if analysis["btts"] else 0.2, "source": "groq"})
            if analysis.get("over_2_5") is not None:
                analysis_signals.append({"name": "over_2_5", "value": 0.8 if analysis["over_2_5"] else 0.2, "source": "groq"})

            normalized_signals = [normalize_signal(s.get("name", ""), s.get("value", 0)) for s in analysis_signals]
            learned = get_learned_probabilities(
                normalized_signals,
                pick_type="match_result",
                league_key=analysis.get("league_name") or "__global__",
                min_samples=5,
            )
            analysis["learned_probabilities"] = learned
        except Exception:
            pass

    return {
        "status": "success",
        "groq_powered": True,
        "request": {"target_odds": target_odds, "max_total_odds": max_total_odds},
        "candidate_count": len(candidates),
        "candidate_limit": candidate_limit,
        "analyses_run": len(candidates),
        "analyses_succeeded": len(analyses),
        "failures": failures,
        "picks": synthesis.get("ranked_picks") or [],
        "selections": synthesis.get("ranked_picks") or [],
        "combined_odds": synthesis.get("combined_odds") or 0,
        "avg_confidence": synthesis.get("avg_confidence") or 0,
        "confidence": synthesis.get("avg_confidence") or 0,
        "target_met": not synthesis.get("target_not_met"),
        "target_not_met": bool(synthesis.get("target_not_met")),
        "confirmed_count": synthesis.get("confirmed_count") or 0,
        "no_consensus": bool(synthesis.get("no_consensus")),
        "synthesis_reasoning": synthesis.get("synthesis_reasoning"),
        "analyses": analyses,
    }


def synthesize_sure_picks(
    analyses: list[dict[str, Any]],
    *,
    target_odds: float,
    max_total_odds: float,
    deterministic: bool = False,
) -> dict[str, Any]:
    clean = [item for item in analyses if item.get("status") == "success" or item.get("groq_recommendation")]
    if len(clean) < 2:
        raise ValueError("At least two completed enriched analyses are required")

    ranked = []
    for item in clean[:100]:  # support up to 100 match pool
        groq_conf = _to_int(item.get("groq_confidence") or item.get("confidence"), 0)
        similar_used = _to_int(item.get("similar_matches_used"), 0)
        engine_pick = item.get("prediction_engine_pick") or {}
        confirmed = bool(item.get("confirmed")) or _same_outcome(engine_pick.get("selection"), item.get("groq_recommendation"))
        odds = round(_to_float(item.get("estimated_odds")) or _pick_decimal_odds(engine_pick), 3)
        try:
            from app.storage.league_memory import betbuilder_pick_memory
            learning = betbuilder_pick_memory(
                engine_pick.get("type") or item.get("pick_type"),
                item.get("groq_recommendation") or engine_pick.get("selection"),
                item.get("league_name"),
                item.get("country_name"),
                odds,
            )
        except Exception:
            learning = {"samples": 0, "win_rate": None, "adjustment": 0}

        # Use signal aggregator to calculate learned probabilities
        learned_prob = None
        try:
            from app.models.probability_learner import get_learned_probabilities
            from app.enrichment.signal_aggregator import normalize_signal

            # Build signals from the analysis
            analysis_signals = []
            if item.get("key_factors"):
                for factor in item.get("key_factors", []):
                    analysis_signals.append({"name": str(factor), "value": 0.7, "source": "groq"})
            if item.get("market_signal"):
                analysis_signals.append({"name": str(item["market_signal"]), "value": 0.6, "source": "groq"})
            if item.get("btts") is not None:
                analysis_signals.append({"name": "btts", "value": 0.8 if item["btts"] else 0.2, "source": "groq"})
            if item.get("over_2_5") is not None:
                analysis_signals.append({"name": "over_2_5", "value": 0.8 if item["over_2_5"] else 0.2, "source": "groq"})

            # Add signal from the engine pick
            if engine_pick.get("selection"):
                analysis_signals.append({
                    "name": str(engine_pick["selection"]),
                    "value": engine_pick.get("confidence", 50) / 100,
                    "source": "engine",
                })

            # Normalize signals
            normalized_signals = []
            for sig in analysis_signals:
                norm = normalize_signal(sig.get("name", ""), sig.get("value", 0))
                normalized_signals.append(norm)

            learned_prob = get_learned_probabilities(
                normalized_signals,
                pick_type=engine_pick.get("type") or item.get("pick_type") or "match_result",
                league_key=item.get("league_name") or "__global__",
                min_samples=5,
            )
        except Exception:
            learned_prob = None

        # Calculate conviction score with learned probability boost
        base_conviction = groq_conf * (1 + 0.10 * similar_used) + float(learning.get("adjustment") or 0)

        # Apply learned probability boost
        learned_boost = 0.0
        if learned_prob and learned_prob.get("samples", 0) >= 5:
            # Boost based on how well the learned distribution matches the Groq confidence
            learned_away = learned_prob.get("away_prob", 0.25)
            learned_home = learned_prob.get("home_prob", 0.45)
            learned_draw = learned_prob.get("draw_prob", 0.30)
            groq_selection = (item.get("groq_recommendation") or engine_pick.get("selection") or "").lower()

            if "away" in groq_selection or "2" == groq_selection:
                learned_boost = (learned_away - 0.25) * 20  # boost up to +5
            elif "home" in groq_selection or "1" == groq_selection:
                learned_boost = (learned_home - 0.45) * 20  # boost up to +5
            elif "draw" in groq_selection or "x" == groq_selection:
                learned_boost = (learned_draw - 0.30) * 20  # boost up to +5

            # Extra boost for proven away wins (54% baseline)
            if learned_prob.get("away_prob", 0) >= 0.54 and "away" in groq_selection:
                learned_boost += 3.0

        # Apply league accuracy adjustment — boost leagues where we consistently
        # win, penalise leagues where we consistently lose.  This feeds graded
        # prediction history directly into Maya's selection ranking so she
        # naturally surfaces picks from high-accuracy leagues and deprioritises
        # picks from leagues the engine doesn't understand well.
        league_acc_boost = 0.0
        try:
            from app.monitoring.self_learner import get_league_accuracy
            _lacc = get_league_accuracy(item.get("league_name") or "")
            if _lacc.get("known"):
                for _lt in (_lacc.get("by_pick_type") or []):
                    _pt = engine_pick.get("type") or item.get("pick_type") or "match_result"
                    if _lt.get("pick_type") in (_pt, "__all__"):
                        _samples = int(_lt.get("samples") or 0)
                        _wr = float(_lt.get("win_rate") or 0)  # in %
                        if _samples >= 8:
                            if _wr > 65.0:
                                # Outperforming league — boost conviction
                                league_acc_boost = min(8.0, (_wr - 65.0) / 5.0)
                            elif _wr < 50.0:
                                # Underperforming league — suppress conviction
                                league_acc_boost = max(-10.0, -((50.0 - _wr) / 5.0))
                        break
        except Exception:
            pass

        conviction = round(base_conviction + learned_boost + league_acc_boost, 2)

        # ── Research conviction adjustment ──────────────────────────
        research_conviction_adj = 0
        selection = item.get("groq_recommendation") or engine_pick.get("selection") or ""
        sel_lower = str(selection).lower()
        country_name = str(item.get("country_name") or "").lower().strip()
        source = str(item.get("source") or "")

        # Home or Away selection → +4
        if "home or away" in sel_lower or sel_lower == "home_or_away":
            research_conviction_adj += 4
        # Away or Draw with groq_conf >= 72 → -3
        if "away or draw" in sel_lower or sel_lower == "away_or_draw":
            if groq_conf >= 72:
                research_conviction_adj -= 3
        # Country in dynamic trust countries → +3
        if country_name in _get_dynamic_rules()["trust_countries"]:
            research_conviction_adj += 3
        # Source sportybet_market_signal → +5
        if source == "sportybet_market_signal":
            research_conviction_adj += 5

        # ── Optimal profile score (0-6) ─────────────────────────────
        optimal_profile_score = 0
        if "home or away" in sel_lower or sel_lower == "home_or_away":
            optimal_profile_score += 1
        if groq_conf >= 72:
            optimal_profile_score += 1
        if 1.50 <= odds <= 1.99:
            optimal_profile_score += 1
        if country_name in _get_dynamic_rules()["trust_countries"]:
            optimal_profile_score += 1
        if source == "sportybet_market_signal":
            optimal_profile_score += 1
        if "home or away" in sel_lower or sel_lower == "home_or_away":
            optimal_profile_score += 1  # second point for home-away being strongest

        # Cap optimal_profile_score at 6
        optimal_profile_score = min(optimal_profile_score, 6)

        adjusted_conviction = round(conviction + research_conviction_adj, 2)

        ranked.append({
            "match_id": item.get("match_id") or item.get("sportybet_id"),
            "match": item.get("match_name") or item.get("match"),
            "league": item.get("league_name"),
            "country": item.get("country_name"),
            "type": engine_pick.get("type") or item.get("pick_type") or "ai_pick",
            "pick_type": engine_pick.get("type") or item.get("pick_type") or "ai_pick",
            "selection": item.get("groq_recommendation") or engine_pick.get("selection"),
            "groq_confidence": groq_conf,
            "confidence": groq_conf,
            "odds": odds,
            "estimated_odds": odds,
            "conviction_score": adjusted_conviction,
            "research_conviction_adj": research_conviction_adj,
            "optimal_profile_score": optimal_profile_score,
            "optimal_profile": optimal_profile_score >= 4,
            "learning": learning,
            "learned_probabilities": learned_prob,
            "league_accuracy_boost": round(league_acc_boost, 2),
            "confirmed": confirmed,
            "similar_matches_used": similar_used,
            "source": "groq",
            "synthesis_reasoning": "",
        })
    ranked.sort(key=lambda item: (item["confirmed"], item["conviction_score"] + item.get("optimal_profile_score", 0) * 0.5, item["groq_confidence"]), reverse=True)

    try:
        if deterministic:
            raise RuntimeError("deterministic mode requested — skipping LLM synthesis")
        model_plan = _run_groq_synthesis(ranked, target_odds, max_total_odds)
        selected_ids = [str(item.get("match_id") or "") for item in model_plan.get("selected_picks") or []]
        selected = [item for item in ranked if str(item.get("match_id") or "") in selected_ids] if selected_ids else []
        reasoning = model_plan.get("synthesis_reasoning") or "Groq ranked the completed analyses by consensus and conviction."
    except Exception as exc:
        selected = []
        reasoning = f"Deterministic synthesis used after Groq synthesis failed: {exc}"

    if not selected:
        selected = _select_by_odds(ranked, target_odds, max_total_odds)
    selected = _trim_to_ceiling(selected, max_total_odds)
    if not selected:
        selected = ranked[:1]
    combined = _combined_odds(selected)
    target_not_met = combined < target_odds
    avg_confidence = round(sum(float(item.get("groq_confidence") or 0) for item in selected) / len(selected)) if selected else 0
    confirmed_count = len([item for item in selected if item.get("confirmed")])
    for item in selected:
        item["synthesis_reasoning"] = item.get("synthesis_reasoning") or (
            "Prediction engine and Groq agree." if item.get("confirmed") else "Included as a high-conviction AI pick."
        )
    return {
        "status": "success",
        "ranked_picks": selected,
        "combined_odds": round(combined, 3),
        "avg_confidence": avg_confidence,
        "confirmed_count": confirmed_count,
        "no_consensus": confirmed_count == 0,
        "target_not_met": target_not_met,
        "synthesis_reasoning": reasoning,
    }


def upcoming_prediction_candidates(limit: int = 50) -> list[dict[str, Any]]:
    try:
        from app.utils.current_predictions import list_recent_dashboard_predictions

        rows = list_recent_dashboard_predictions(hours=72, limit=max(limit, 200))
    except Exception:
        rows = list_prediction_history(limit=max(limit, 200)).get("predictions") or []
    today = date.today()
    tomorrow = today + timedelta(days=1)
    allowed_dates = {today.isoformat(), tomorrow.isoformat()}
    now_ts = time.time()
    candidates = []
    for row in rows:
        # Skip finished, cancelled or already-started matches
        if row.get("is_finished") or str(row.get("result") or "").lower() in {"cancelled", "finished"}:
            continue
        match_date = str(row.get("match_date") or "")[:10]
        if match_date and match_date not in allowed_dates:
            continue
        # Skip matches that have already kicked off (start_time in the past)
        start_time = row.get("start_time")
        if start_time:
            try:
                kick_ts = float(start_time)
                if kick_ts > 1e12:
                    kick_ts /= 1000  # ms → seconds
                if kick_ts < now_ts:
                    continue  # already started
            except (TypeError, ValueError):
                pass
        # Skip if match is currently live
        if row.get("is_live") or str(row.get("period") or "").lower() in {"h1", "h2", "et", "live", "ht"}:
            continue
        pick = row.get("best_pick") or _best_pick(row.get("picks") or [])
        if not pick:
            continue
        # Enforce minimum odds of 1.30 per leg — sub-1.30 picks don't contribute
        # meaningful value to an accumulator
        pick_odds = _pick_decimal_odds(pick)
        if pick_odds < 1.30:
            continue
        # ── Research filter candidate check ──────────────────────────────────
        try:
            country = str(row.get("country_name") or row.get("category") or "").lower().strip()
            league_key = str(row.get("league_name") or "").lower().strip()
            if not league_key and row.get("tournament"):
                league_key = _normalise_league_key(str(row["tournament"]))
            odds_profile = _extract_betbuilder_odds_profile(row)
            if not _research_filter_candidate(pick, odds_profile, country, league_key):
                continue
        except Exception:
            pass
        # ── SportyBet availability check ──────────────────────────────────────
        # Verify the match is still in the active buffer with live markets before
        # including it as a Maya candidate.  Matches that were archived or had
        # their markets pulled will fail here and be silently skipped.
        match_id = str(row.get("match_id") or row.get("sportybet_id") or "")
        if match_id:
            try:
                from app.storage.buffer import get_buffered_match, refresh_sporty_match_state
                # Refresh buffer state inline (non-blocking — uses cached data if recent)
                try:
                    refresh_sporty_match_state(match_id)
                except Exception:
                    pass
                buf_doc = get_buffered_match(match_id)
                if not buf_doc:
                    continue  # not in buffer — skip
                # Must have live SportyBet markets
                markets = buf_doc.get("sportybet_markets") or buf_doc.get("markets") or []
                if not markets:
                    continue  # no markets available — skip
                # Must not be finished or live
                if buf_doc.get("is_finished") or buf_doc.get("is_live"):
                    continue
            except Exception:
                # If buffer check fails, allow through — don't block on infra errors
                pass
        candidates.append({**row, "best_pick": pick})
    candidates.sort(key=lambda item: int((item.get("best_pick") or {}).get("confidence") or 0), reverse=True)
    return candidates[:limit]


def similarity_gate(doc: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.enrichment.similar_matches import _extract_target_odds_implied

    target_implied = _extract_target_odds_implied(doc)
    team_terms = [term for term in (_team_name(doc, "home"), _team_name(doc, "away")) if len(term) >= 4]
    scored_candidates = [item for item in candidates if float(item.get("similarity_score") or 0) >= 0.40]
    totals = [_total_goals(item.get("final_score")) for item in scored_candidates]
    totals = [value for value in totals if value is not None]
    avg_total = sum(totals) / len(totals) if totals else None
    passing = []
    for item in scored_candidates:
        dimensions: list[str] = []
        if target_implied and _odds_dimension(target_implied, item.get("odds")):
            dimensions.append("odds")
        total = _total_goals(item.get("final_score"))
        if avg_total is not None and total is not None and abs(total - avg_total) <= 1:
            dimensions.append("score_pattern")
        name = str(item.get("match_name") or "").lower()
        if any(term.lower() in name for term in team_terms):
            dimensions.append("team")
        if dimensions:
            prediction = item.get("prediction") or {}
            passing.append({
                **item,
                "similarity_dimension": "+".join(dimensions),
                "prediction_made": {
                    "type": prediction.get("pick_type") or prediction.get("type"),
                    "selection": prediction.get("selection"),
                    "confidence": prediction.get("confidence"),
                    "result": prediction.get("result"),
                },
            })
    return passing


def _run_openrouter_enriched(doc: dict[str, Any]) -> dict[str, Any]:
    from app.ai.groq_agent import run_groq_match_analysis

    return run_groq_match_analysis(doc)


def _run_openrouter_synthesis(ranked: list[dict[str, Any]], target_odds: float, max_total_odds: float) -> dict[str, Any]:
    from app.ai.llm import get_llm

    prompt = (
        "You are an expert football betting slip analyst. Read these completed Groq enriched analyses. "
        "Return strict JSON with selected_picks (array of objects with match_id) and synthesis_reasoning. "
        "Prefer confirmed picks where the prediction engine and Groq agree. Rank by conviction_score. "
        "IMPORTANT: Each pick has a league_accuracy_boost field — positive means the system historically "
        "wins in that league, negative means it loses. Strongly prefer picks with positive league_accuracy_boost. "
        "Avoid picks with league_accuracy_boost below -5 unless no alternatives exist. "
        f"Select enough picks to reach target odds {target_odds} without exceeding max odds {max_total_odds}. "
        "If target cannot be reached, return the best available picks.\n\n"
        + json.dumps(ranked[:100], ensure_ascii=False)
    )
    response = get_llm().invoke([
        {"role": "system", "content": "Return only valid JSON."},
        {"role": "user", "content": prompt},
    ])
    raw = response.content if hasattr(response, "content") else str(response)
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def _analysis_doc(doc: dict[str, Any], engine_pick: dict[str, Any], similar: list[dict[str, Any]]) -> dict[str, Any]:
    prompt_context = {
        "prediction_engine_best_pick": engine_pick,
        "similar_matches": [
            {
                "match_name": item.get("match_name"),
                "final_score": item.get("final_score"),
                "prediction_made": item.get("prediction_made"),
                "similarity_dimension": item.get("similarity_dimension"),
                "similarity_score": item.get("similarity_score"),
            }
            for item in similar
        ] if len(similar) >= 2 else [],
    }
    return {**doc, "ai_enriched_context": prompt_context, "web_context": {**(doc.get("web_context") or {}), "snippets": [{"snippet": json.dumps(prompt_context)[:900]}]}}


def _similar_matches(doc: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    from app.enrichment.similar_matches import find_similar_matches

    return find_similar_matches(doc, limit=limit)


def _best_engine_pick(match_id: str) -> dict[str, Any] | None:
    history = list_prediction_history(limit=5, match_id=match_id).get("predictions") or []
    for row in history:
        pick = row.get("best_pick") or _best_pick(row.get("picks") or [])
        if pick:
            return {
                "type": pick.get("type") or pick.get("pick_type") or row.get("pick_type"),
                "selection": pick.get("selection") or row.get("selection"),
                "confidence": pick.get("confidence") or row.get("confidence"),
                "reason": pick.get("reason") or row.get("reason"),
                "signals": row.get("signals") or pick.get("signals") or [],
                "odds": _pick_decimal_odds(pick),
            }
    return None


def _best_pick_from_doc(doc: dict[str, Any]) -> dict[str, Any] | None:
    prediction = doc.get("prediction") or {}
    return prediction.get("best_pick") or _best_pick(prediction.get("picks") or [])


def _best_pick(picks: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [pick for pick in picks if pick.get("type") != "no_bet"]
    if not usable:
        return None
    return max(usable, key=lambda pick: int(pick.get("confidence") or 0))


def _pick_decimal_odds(pick: dict[str, Any] | None) -> float:
    pick = pick or {}
    stake = pick.get("stake") if isinstance(pick.get("stake"), dict) else {}
    odds = _to_float(stake.get("decimal_odds")) or _to_float(pick.get("odds")) or _to_float(pick.get("decimal_odds"))
    if odds and odds > 1:
        return odds
    confidence = max(1, min(95, _to_int(pick.get("confidence"), 55))) / 100
    return round(1 / confidence, 3)


def _select_by_odds(candidates: list[dict[str, Any]], target_odds: float, max_total_odds: float, min_leg_odds: float = 1.30) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    combined = 1.0
    for item in candidates:
        match_id = str(item.get("match_id") or "")
        odds = float(item.get("odds") or item.get("estimated_odds") or 1)
        # Skip legs below the minimum odds threshold
        if odds < min_leg_odds:
            continue
        if match_id in used or combined * odds > max_total_odds:
            continue
        selected.append(item)
        used.add(match_id)
        combined *= odds
        if combined >= target_odds:
            break
    return selected


def _trim_to_ceiling(selected: list[dict[str, Any]], max_total_odds: float) -> list[dict[str, Any]]:
    items = list(selected)
    while len(items) > 1 and _combined_odds(items) > max_total_odds:
        items.sort(key=lambda item: float(item.get("conviction_score") or 0))
        items.pop(0)
    return items


def _combined_odds(items: list[dict[str, Any]]) -> float:
    if not items:
        return 0.0
    return math.prod(float(item.get("odds") or item.get("estimated_odds") or 1) for item in items)


def _same_outcome(left: Any, right: Any) -> bool:
    return _normalise_selection(left) == _normalise_selection(right)


def _normalise_selection(value: Any) -> str:
    text = str(value or "").lower().strip()
    text = re.sub(r"[^a-z0-9+ .-]+", "", text)
    aliases = {
        "home": "home",
        "home win": "home",
        "1": "home",
        "away": "away",
        "away win": "away",
        "2": "away",
        "draw": "draw",
        "x": "draw",
        "over 2.5": "over_2_5",
        "over 25": "over_2_5",
        "under 2.5": "under_2_5",
        "under 25": "under_2_5",
        "yes": "yes",
        "no": "no",
    }
    return aliases.get(text, text.replace(" ", "_"))


def _odds_dimension(target_implied: tuple[float, float, float], odds: Any) -> bool:
    if not isinstance(odds, dict):
        return False
    values = [odds.get("home"), odds.get("draw"), odds.get("away")]
    for target, raw in zip(target_implied, values):
        decimal = _to_float(raw)
        if decimal and decimal > 1 and abs(target - (1 / decimal)) <= 0.08:
            return True
    return False


def _team_name(doc: dict[str, Any], side: str) -> str:
    value = doc.get(f"{side}_team")
    if isinstance(value, dict):
        return str(value.get("name") or "")
    if value:
        return str(value)
    name = str(doc.get("sportybet_name") or doc.get("name") or "")
    parts = re.split(r"\s+v(?:s)?\.?\s+", name, flags=re.I)
    if len(parts) == 2:
        return parts[0 if side == "home" else 1].strip()
    return ""


def _total_goals(score: Any) -> int | None:
    if not score:
        return None
    match = re.search(r"(\d+)\D+(\d+)", str(score))
    if not match:
        return None
    return int(match.group(1)) + int(match.group(2))


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _extract_betbuilder_odds_profile(row: dict[str, Any]) -> dict[str, float]:
    """Extract a normalized odds profile from a betbuilder buffer doc or prediction row."""
    odds: dict[str, float] = {}
    # Try signals_json first (contains odds_profile)
    signals_json = row.get("signals_json") or row.get("signals") or "[]"
    if isinstance(signals_json, str):
        try:
            signals = json.loads(signals_json)
        except (json.JSONDecodeError, TypeError):
            signals = []
    else:
        signals = signals_json or []
    for sig in signals:
        if isinstance(sig, dict) and sig.get("name") == "odds_profile":
            profile = sig.get("value") or {}
            if isinstance(profile, dict):
                odds.update(profile)
                break
    # Fallback: try odds_profile directly on the row
    if not odds:
        direct = row.get("odds_profile") or {}
        if isinstance(direct, dict):
            odds.update(direct)
    # Normalize keys
    normalized: dict[str, float] = {}
    for k, v in odds.items():
        try:
            normalized[k] = float(v)
        except (TypeError, ValueError):
            pass
    return normalized

