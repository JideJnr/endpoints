from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.confidence_calibrator import calibrate_confidence
from app.league_memory import DB_PATH, _init_db
from app.market_intent import classify_market_intent
from app.match_state import classify_match_state
from app.time_context import match_time_context


def build_contextual_intelligence(
    doc: dict[str, Any],
    prediction: dict[str, Any] | None = None,
    *,
    signals: list[dict[str, Any]] | None = None,
    picks: list[dict[str, Any]] | None = None,
    odds_movement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Interpret context, market behavior, signal relationships, aging, and risk."""
    prediction = prediction or {}
    signals = signals if signals is not None else prediction.get("signals") or []
    picks = picks if picks is not None else prediction.get("picks") or []
    odds_movement = odds_movement if odds_movement is not None else prediction.get("odds_movement") or {}
    match_state = prediction.get("match_state") or doc.get("match_state") or classify_match_state(doc)
    time_context = prediction.get("time_context") or doc.get("time_context") or match_time_context(doc)

    context = _match_context(doc, time_context)
    market = _market_behavior(odds_movement)
    relationships = _signal_relationships(signals, picks)
    aging = _prediction_aging(prediction, time_context, market)
    learned = _learned_performance(doc, picks)
    risk = _risk_profile(doc, match_state, context, market, relationships, aging, picks, learned)
    adjustment = _confidence_adjustment(context, market, relationships, aging, risk, learned)

    return {
        "version": "contextual_intelligence_v1",
        "match_context": context,
        "market_behavior": market,
        "signal_relationships": relationships,
        "prediction_aging": aging,
        "learned_performance": learned,
        "risk": risk,
        "confidence_adjustment": adjustment,
        "no_prediction_recommended": risk.get("level") == "high" and adjustment <= -8,
        "explain": _explain(context, market, relationships, aging, learned, risk, adjustment),
    }


def apply_contextual_adjustment(
    picks: list[dict[str, Any]],
    intelligence: dict[str, Any],
) -> list[dict[str, Any]]:
    adjustment = int(intelligence.get("confidence_adjustment") or 0)
    adjusted: list[dict[str, Any]] = []
    for pick in picks:
        if pick.get("type") == "no_bet":
            adjusted.append({**pick, "contextual_intelligence": intelligence})
            continue
        confidence = int(pick.get("confidence") or 0)
        adjusted.append({
            **pick,
            "confidence": max(1, min(99, confidence + adjustment)),
            "pre_context_confidence": confidence,
            "contextual_intelligence": intelligence,
        })
    return adjusted


def builder_relationship_intelligence(selections: list[dict[str, Any]], candidate: dict[str, Any]) -> dict[str, Any]:
    """Score how a candidate interacts with already selected betbuilder legs."""
    candidate_intent = classify_market_intent(candidate.get("type"), candidate.get("selection"), candidate)
    correlation = 0
    conflicts: list[str] = []
    supports: list[str] = []
    volatility = 0

    for existing in selections:
        existing_intent = classify_market_intent(existing.get("type"), existing.get("selection"), existing)
        same_league = bool(existing.get("league") and existing.get("league") == candidate.get("league"))
        same_market = existing_intent.get("market") == candidate_intent.get("market")
        if same_market:
            correlation += 2
        if same_league:
            volatility += 1
        pair = _market_pair(existing_intent, candidate_intent)
        if pair == "support":
            supports.append(f"{existing.get('selection')} supports {candidate.get('selection')}")
            correlation += 1
        elif pair == "conflict":
            conflicts.append(f"{existing.get('selection')} conflicts with {candidate.get('selection')}")
            correlation += 2
            volatility += 2

    score_adjustment = min(4, len(supports)) - min(10, correlation + volatility + len(conflicts) * 2)
    return {
        "correlation_score": correlation,
        "volatility_stack": volatility,
        "supports": supports[:5],
        "conflicts": conflicts[:5],
        "score_adjustment": score_adjustment,
        "risk_level": "high" if conflicts or volatility >= 4 else "medium" if correlation >= 4 else "low",
    }


def _match_context(doc: dict[str, Any], time_context: dict[str, Any]) -> dict[str, Any]:
    tournament = _text(doc.get("tournament") or ((doc.get("sofascore_detail") or {}).get("tournament") or {}).get("name"))
    category = _text(doc.get("category") or doc.get("country"))
    name = _text(doc.get("sportybet_name") or doc.get("name"))
    web_text = _web_text(doc)
    text = f"{tournament} {category} {name} {web_text}".lower()
    tags: list[str] = []
    pressure = "normal"
    confidence_reliability = "normal"
    adjustment = 0

    if any(key in text for key in ("friendly", "club friendly", "international friendly")):
        tags.append("friendly")
        confidence_reliability = "reduced"
        adjustment -= 6
    if any(key in text for key in ("cup", "playoff", "play-off", "knockout", "final", "semi-final", "quarter-final", "libertadores", "champions league", "europa league")):
        tags.append("knockout_or_cup_pressure")
        pressure = "high"
        adjustment -= 2
    if any(key in text for key in ("derby", "rivalry", "rivals")):
        tags.append("derby_or_rivalry")
        pressure = "high"
        adjustment -= 4
    if any(key in text for key in ("relegation", "survival", "drop zone")):
        tags.append("relegation_pressure")
        pressure = "high"
        # Don't apply a flat penalty — form trajectory signals carry the real weight.
        # A team in a relegation battle that is improving vs strong opponents is
        # different from one losing to everyone. Apply a small base penalty only.
        adjustment -= 1
    if any(key in text for key in ("title race", "title decider", "must win", "promotion")):
        tags.append("title_or_promotion_pressure")
        pressure = "high"
        adjustment -= 2
    if any(key in text for key in ("dead rubber", "meaningless", "nothing to play")):
        tags.append("low_motivation")
        confidence_reliability = "reduced"
        adjustment -= 5
    if any(key in text for key in ("revenge", "rematch", "previous meeting")):
        tags.append("revenge_or_rematch")
        adjustment -= 1
    if any(key in text for key in ("rotation", "rest players", "rested", "squad rotation")):
        tags.append("rotation_likelihood")
        adjustment -= 4
    if any(key in text for key in ("rain", "storm", "wind", "snow", "weather")):
        tags.append("weather_watch")
        adjustment -= 2
    minutes = _to_float(time_context.get("minutes_until_kickoff"))
    if minutes is not None and 0 <= minutes <= 90 and not doc.get("lineups") and not (doc.get("sofascore_detail") or {}).get("lineups"):
        tags.append("lineup_window")
        adjustment -= 2

    return {
        "tags": tags or ["standard_fixture"],
        "pressure": pressure,
        "confidence_reliability": confidence_reliability,
        "adjustment": max(-10, min(4, adjustment)),
        "evidence": "provider/web/context only; no invented team news",
    }


def _market_behavior(odds_movement: dict[str, Any]) -> dict[str, Any]:
    snapshot_value = odds_movement.get("snapshots") or odds_movement.get("market_snapshots") or odds_movement.get("history")
    snapshots = len(snapshot_value) if isinstance(snapshot_value, list) else int(_to_float(snapshot_value) or 0)
    strongest = odds_movement.get("strongest_pull") if isinstance(odds_movement.get("strongest_pull"), dict) else {}
    sharp = odds_movement.get("sharp_signal")
    magnitude = abs(_to_float(
        strongest.get("change_percent")
        or strongest.get("odds_change_percent")
        or strongest.get("implied_change_percent")
        or strongest.get("move_percent")
        or strongest.get("change")
    ) or 0)
    flags: list[str] = []
    adjustment = 0
    if snapshots < 2:
        flags.append("thin_market_history")
        adjustment -= 2
    if sharp:
        flags.append("sharp_money_signal")
        adjustment += 1
    if magnitude >= 18:
        flags.append("volatility_spike")
        adjustment -= 5
    elif magnitude >= 10:
        flags.append("meaningful_odds_move")
        adjustment -= 2
    if snapshots >= 5 and magnitude < 0.5:
        flags.append("line_freeze_or_stable_market")
    return {
        "flags": flags,
        "snapshots": snapshots,
        "strongest_pull": strongest,
        "sharp_signal": sharp,
        "volatility_percent": round(magnitude, 2),
        "adjustment": max(-8, min(3, adjustment)),
        "question_answered": _market_question(flags, strongest, sharp),
    }


def _signal_relationships(signals: list[dict[str, Any]], picks: list[dict[str, Any]]) -> dict[str, Any]:
    support = [sig for sig in signals if _impact(sig) > 0]
    risk = [sig for sig in signals if _impact(sig) < 0]
    support_names = {str(sig.get("name") or "") for sig in support}
    risk_names = {str(sig.get("name") or "") for sig in risk}
    conflicts: list[str] = []
    synergies: list[str] = []
    adjustment = 0

    if {"goal_environment", "live_inplay_state"} <= support_names:
        synergies.append("goal environment and live pressure agree")
        adjustment += 2
    if "odds_progression" in risk_names and ("consensus_longshot_value" in support_names or "consensus_longshot_market_value" in support_names):
        conflicts.append("model value exists while market behavior is unstable")
        adjustment -= 3
    if "finished_database_memory" in risk_names and "prediction_memory" in support_names:
        conflicts.append("finished-match memory and prediction memory disagree")
        adjustment -= 2
    if len(risk) >= 3 and len(support) <= 3:
        conflicts.append("risk signals outnumber meaningful support")
        adjustment -= 4
    if picks:
        primary = picks[0]
        intent = classify_market_intent(primary.get("type"), primary.get("selection"), primary)
        if intent.get("market") == "total_goals" and "goal_environment" not in support_names:
            conflicts.append("goal-market pick has no explicit goal-environment support")
            adjustment -= 1
    return {
        "support_count": len(support),
        "risk_count": len(risk),
        "synergies": synergies,
        "conflicts": conflicts,
        "adjustment": max(-8, min(4, adjustment)),
    }


def _prediction_aging(prediction: dict[str, Any], time_context: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    created_at = prediction.get("created_at") or prediction.get("generated_at") or prediction.get("predicted_at")
    age_minutes = 0.0
    if created_at:
        age_minutes = max(0.0, (_now() - _parse_datetime(created_at)).total_seconds() / 60)
    until = _to_float(time_context.get("minutes_until_kickoff"))
    phase = "fresh"
    adjustment = 0
    if age_minutes >= 360:
        phase = "stale"
        adjustment -= 5
    elif age_minutes >= 120:
        phase = "aging"
        adjustment -= 2
    if until is not None and 0 <= until <= 30 and market.get("volatility_percent", 0) >= 8:
        phase = "needs_recheck_before_kickoff"
        adjustment -= 4
    return {
        "age_minutes": round(age_minutes, 1),
        "phase": phase,
        "adjustment": adjustment,
        "minutes_until_kickoff": until,
    }


def _risk_profile(
    doc: dict[str, Any],
    match_state: dict[str, Any],
    context: dict[str, Any],
    market: dict[str, Any],
    relationships: dict[str, Any],
    aging: dict[str, Any],
    picks: list[dict[str, Any]],
    learned: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    score = 0
    if not doc.get("sofascore_detail"):
        reasons.append("missing_sofascore_detail")
        score += 2
    if not (doc.get("sportybet_detail") or doc.get("raw_sporty")):
        reasons.append("missing_sporty_detail")
        score += 2
    if market.get("volatility_percent", 0) >= 18:
        reasons.append("market_volatility_spike")
        score += 3
    if relationships.get("conflicts"):
        reasons.extend(relationships.get("conflicts")[:3])
        score += min(4, len(relationships.get("conflicts") or []) * 2)
    if context.get("confidence_reliability") == "reduced":
        reasons.append("context_reduces_model_reliability")
        score += 2
    if aging.get("phase") in {"stale", "needs_recheck_before_kickoff"}:
        reasons.append(aging.get("phase"))
        score += 2
    if match_state.get("state") not in {"prematch", "live"}:
        reasons.append(f"non_predictable_state:{match_state.get('state')}")
        score += 5
    if picks and max(int(pick.get("confidence") or 0) for pick in picks) < 60:
        reasons.append("low_confidence_pick_pool")
        score += 2
    learned_class = learned.get("classification")
    if learned_class == "smart_bet":
        reasons.append("graded_history_supports_pick")
        score -= 3
    elif learned_class == "learned_high_risk":
        reasons.append("graded_history_warns_against_pick")
        score += 4
    elif learned_class == "history_thin":
        reasons.append("thin_pre_match_or_grading_history")
        score += 1
    if learned.get("pre_match_history", {}).get("classification") == "supportive":
        score -= 1
    elif learned.get("pre_match_history", {}).get("classification") == "weak":
        score += 1

    # Form trajectory risk: a team losing to everyone in a relegation battle
    # is genuinely dangerous to back regardless of other signals.
    if "relegation_pressure" in (context.get("tags") or []):
        signals = doc.get("signals") or []
        for sig in signals:
            name = sig.get("name") or ""
            val = sig.get("value") or {}
            if name in ("home_form_trajectory", "away_form_trajectory") and isinstance(val, dict):
                if val.get("trajectory") == "poor" or val.get("all_recent_losses"):
                    reasons.append(f"{name}_all_losses_in_relegation_battle")
                    score += 3
                elif val.get("trajectory") == "declining":
                    reasons.append(f"{name}_declining_in_relegation_battle")
                    score += 1
                elif val.get("trajectory") == "improving":
                    # Improving form in relegation context reduces risk
                    score -= 1

    score = max(0, score)
    level = "high" if score >= 7 else "medium" if score >= 3 else "low"
    return {"level": level, "score": score, "reasons": reasons[:10]}


def _confidence_adjustment(context: dict[str, Any], market: dict[str, Any], relationships: dict[str, Any], aging: dict[str, Any], risk: dict[str, Any], learned: dict[str, Any]) -> int:
    adjustment = int(context.get("adjustment") or 0)
    adjustment += int(market.get("adjustment") or 0)
    adjustment += int(relationships.get("adjustment") or 0)
    adjustment += int(aging.get("adjustment") or 0)
    adjustment += int(learned.get("adjustment") or 0)
    if risk.get("level") == "high":
        adjustment -= 4
    elif risk.get("level") == "medium":
        adjustment -= 1
    return max(-15, min(6, adjustment))


def _explain(context: dict[str, Any], market: dict[str, Any], relationships: dict[str, Any], aging: dict[str, Any], learned: dict[str, Any], risk: dict[str, Any], adjustment: int) -> dict[str, Any]:
    lines = [
        f"Match context: {', '.join(context.get('tags') or [])}.",
        f"Market behavior: {market.get('question_answered')}.",
    ]
    if learned.get("summary"):
        lines.append(f"Learning: {learned.get('summary')}.")
    if relationships.get("synergies"):
        lines.append(f"Signal synergy: {relationships['synergies'][0]}.")
    if relationships.get("conflicts"):
        lines.append(f"Signal conflict: {relationships['conflicts'][0]}.")
    if aging.get("phase") != "fresh":
        lines.append(f"Prediction timing: {aging.get('phase')}.")
    if risk.get("reasons"):
        lines.append(f"Risk: {risk.get('reasons')[0]}.")
    lines.append(f"Contextual confidence adjustment: {adjustment}.")
    return {
        "why_this_pick": lines[0],
        "why_market_moved": lines[1],
        "why_smart_or_risky": learned.get("summary") or (risk.get("reasons") or ["unproven"])[0],
        "why_confidence": lines[-1],
        "lines": lines,
    }


def _learned_performance(doc: dict[str, Any], picks: list[dict[str, Any]]) -> dict[str, Any]:
    primary = _primary_pick(picks)
    if not primary:
        return {
            "classification": "no_pick",
            "adjustment": 0,
            "summary": "no real pick available for learned risk check",
            "pre_match_history": _pre_match_history_profile(doc, {}),
            "graded": {},
        }

    pick_type = str(primary.get("type") or "")
    selection = str(primary.get("selection") or "")
    confidence = int(_to_float(primary.get("confidence")) or 0)
    league = _text(doc.get("tournament") or ((doc.get("sofascore_detail") or {}).get("tournament") or {}).get("name"))
    country = _text(doc.get("category") or doc.get("country"))
    graded = _graded_pick_performance(pick_type, selection, confidence, league, country)
    pre_history = _pre_match_history_profile(doc, primary)

    best = graded.get("best") or {}
    samples = int(best.get("samples") or 0)
    win_rate = _to_float(best.get("win_rate"))
    calibration = graded.get("calibration") or {}
    cal_samples = int(calibration.get("samples") or 0)
    cal_win = _to_float(calibration.get("win_rate"))

    classification = "unproven"
    adjustment = 0
    summary = "not enough graded history yet"

    if samples >= 8 and win_rate is not None and win_rate < 0.50:
        classification = "learned_high_risk"
        adjustment = -5
        summary = f"graded memory is weak for {best.get('scope')} ({samples} samples, {round(win_rate * 100, 1)}% win rate)"
    elif samples >= 12 and win_rate is not None and win_rate >= 0.68 and pre_history.get("classification") != "weak":
        classification = "smart_bet"
        adjustment = 3
        summary = f"graded memory supports this pick ({best.get('scope')}: {samples} samples, {round(win_rate * 100, 1)}% win rate)"
    elif cal_samples >= 10 and cal_win is not None and cal_win >= 65 and pre_history.get("classification") != "weak":
        classification = "smart_bet"
        adjustment = 2
        summary = f"confidence band has been profitable ({cal_samples} samples, {round(cal_win, 1)}% win rate)"
    elif samples >= 8 and win_rate is not None and win_rate <= 0.55:
        classification = "caution"
        adjustment = -2
        summary = f"graded memory is only {round(win_rate * 100, 1)}% for {best.get('scope')}"
    elif pre_history.get("classification") == "thin" and samples < 8:
        classification = "history_thin"
        adjustment = -1
        summary = "pre-match and graded history are still thin"
    elif pre_history.get("classification") == "supportive" and samples >= 6 and win_rate is not None and win_rate >= 0.60:
        classification = "smart_bet"
        adjustment = 2
        summary = f"pre-match history and graded memory both support the pick ({round(win_rate * 100, 1)}% win rate)"

    return {
        "classification": classification,
        "adjustment": adjustment,
        "summary": summary,
        "primary_pick": {
            "type": pick_type,
            "selection": selection,
            "confidence": confidence,
        },
        "graded": graded,
        "pre_match_history": pre_history,
    }


def _graded_pick_performance(
    pick_type: str,
    selection: str,
    confidence: int,
    league: str,
    country: str,
) -> dict[str, Any]:
    calibration = calibrate_confidence(pick_type, confidence) if pick_type else {"calibrated": False, "samples": 0}
    scopes: list[dict[str, Any]] = []
    try:
        _init_db()
        with sqlite3.connect(DB_PATH, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            exact = _graded_scope(conn, "exact_selection", "pick_type = ? and lower(selection) = lower(?)", (pick_type, selection))
            type_scope = _graded_scope(conn, "pick_type", "pick_type = ?", (pick_type,))
            league_scope = _graded_scope(conn, "league_pick_type", "pick_type = ? and lower(coalesce(league_name, '')) = lower(?)", (pick_type, league)) if league else {}
            country_scope = _graded_scope(conn, "country_pick_type", "pick_type = ? and lower(coalesce(country_name, '')) = lower(?)", (pick_type, country)) if country else {}
            scopes = [scope for scope in (exact, league_scope, country_scope, type_scope) if scope]
    except Exception as exc:
        return {"calibration": calibration, "scopes": [], "best": {}, "error": str(exc)}

    best = max(scopes, key=lambda item: (int(item.get("samples") or 0) >= 8, int(item.get("samples") or 0), float(item.get("win_rate") or 0)), default={})
    return {"calibration": calibration, "scopes": scopes, "best": best}


def _graded_scope(conn: sqlite3.Connection, scope: str, where_sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    row = conn.execute(f"""
        select
            count(*) as samples,
            sum(case when result = 'win' then 1 else 0 end) as wins,
            sum(case when result = 'loss' then 1 else 0 end) as losses
        from (
            select match_id, pick_type, selection, league_name, country_name, result
            from prediction_history
            where graded_at is not null and result in ('win', 'loss') and pick_type != 'no_bet'
            union all
            select match_id, pick_type, selection, league_name, country_name, result
            from prediction_candidate_history
            where graded_at is not null and result in ('win', 'loss') and pick_type != 'no_bet'
        )
        where {where_sql}
    """, params).fetchone()
    samples = int(row["samples"] or 0)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    return {
        "scope": scope,
        "samples": samples,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / samples, 4) if samples else None,
    }


def _pre_match_history_profile(doc: dict[str, Any], pick: dict[str, Any]) -> dict[str, Any]:
    detail = doc.get("sofascore_detail") or {}
    home_history = detail.get("home_last_matches") or doc.get("home_last_matches") or []
    away_history = detail.get("away_last_matches") or doc.get("away_last_matches") or []
    home_id = ((detail.get("home_team") or {}).get("id") or (detail.get("homeTeam") or {}).get("id"))
    away_id = ((detail.get("away_team") or {}).get("id") or (detail.get("awayTeam") or {}).get("id"))
    home = _team_recent_record(home_history, home_id)
    away = _team_recent_record(away_history, away_id)
    intent = classify_market_intent(pick.get("type"), pick.get("selection"), pick) if pick else {}
    selected = home if intent.get("direction") == "home" or intent.get("direction") == "home_draw" else away if intent.get("direction") == "away" or intent.get("direction") == "away_draw" else {}
    classification = "normal"
    if home.get("samples", 0) < 5 or away.get("samples", 0) < 5:
        classification = "thin"
    elif selected and selected.get("loss_rate", 0) >= 0.55:
        classification = "weak"
    elif selected and selected.get("unbeaten_rate", 0) >= 0.65:
        classification = "supportive"
    return {
        "classification": classification,
        "home": home,
        "away": away,
    }


def _team_recent_record(history: list[dict[str, Any]], team_id: Any) -> dict[str, Any]:
    wins = draws = losses = samples = 0
    for match in history or []:
        if samples >= 12:
            break
        status = match.get("status") or {}
        if status.get("type") not in {"finished", "afterextra", "afterpenalties"}:
            continue
        home = match.get("homeTeam") or match.get("home_team") or {}
        away = match.get("awayTeam") or match.get("away_team") or {}
        home_score = ((match.get("homeScore") or {}).get("current") if isinstance(match.get("homeScore"), dict) else None)
        away_score = ((match.get("awayScore") or {}).get("current") if isinstance(match.get("awayScore"), dict) else None)
        if home_score is None or away_score is None:
            continue
        is_home = str(home.get("id")) == str(team_id)
        is_away = str(away.get("id")) == str(team_id)
        if not (is_home or is_away):
            continue
        gf, ga = (home_score, away_score) if is_home else (away_score, home_score)
        samples += 1
        if gf > ga:
            wins += 1
        elif gf == ga:
            draws += 1
        else:
            losses += 1
    return {
        "samples": samples,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "win_rate": round(wins / samples, 3) if samples else None,
        "unbeaten_rate": round((wins + draws) / samples, 3) if samples else None,
        "loss_rate": round(losses / samples, 3) if samples else None,
    }


def _primary_pick(picks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for pick in picks or []:
        if pick.get("type") != "no_bet":
            return pick
    for pick in picks or []:
        for suppressed in pick.get("suppressed_picks") or []:
            if suppressed.get("type") != "no_bet":
                return suppressed
    return None


def _market_pair(left: dict[str, Any], right: dict[str, Any]) -> str:
    left_market = left.get("market")
    right_market = right.get("market")
    left_dir = left.get("direction")
    right_dir = right.get("direction")
    if left_market == right_market == "total_goals":
        if left_dir and right_dir and left_dir != right_dir:
            return "conflict"
        return "support"
    if {left_market, right_market} == {"btts", "total_goals"}:
        total = left if left_market == "total_goals" else right
        btts = left if left_market == "btts" else right
        if total.get("direction") == "over" and btts.get("direction") == "yes":
            return "support"
        if total.get("direction") == "under" and btts.get("direction") == "yes":
            return "conflict"
    return "neutral"


def _market_question(flags: list[str], strongest: dict[str, Any], sharp: Any) -> str:
    if "volatility_spike" in flags:
        return "market is moving hard; recheck cause before trusting stale confidence"
    if sharp:
        return str(sharp)
    if "line_freeze_or_stable_market" in flags:
        return "market is stable with enough snapshots"
    if "thin_market_history" in flags:
        return "not enough odds history to explain movement yet"
    if strongest:
        return f"strongest move is {strongest}"
    return "no meaningful market movement detected"


def _web_text(doc: dict[str, Any]) -> str:
    web = doc.get("web_context") or {}
    snippets = web.get("snippets") or []
    scraped = web.get("scraped") or web.get("articles") or []
    values = []
    for item in snippets[:8]:
        if isinstance(item, dict):
            values.append(str(item.get("title") or ""))
            values.append(str(item.get("snippet") or ""))
        else:
            values.append(str(item))
    for item in scraped[:3]:
        if isinstance(item, dict):
            values.append(str(item.get("title") or ""))
            values.append(str(item.get("text") or item.get("snippet") or ""))
        else:
            values.append(str(item))
    grok = web.get("grok_analysis") or {}
    if grok.get("status") == "ok":
        values.append(str(grok.get("summary") or ""))
        for item in grok.get("evidence") or []:
            if isinstance(item, dict):
                values.append(str(item.get("claim") or ""))
    return " ".join(values)


def _impact(signal: dict[str, Any]) -> float:
    try:
        return float(signal.get("impact") or 0)
    except Exception:
        return 0.0


def _parse_datetime(value: Any) -> datetime:
    if not value:
        return _now()
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return _now()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("slug") or "")
    return str(value or "")


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None
