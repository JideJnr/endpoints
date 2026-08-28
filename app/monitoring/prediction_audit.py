from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.market.market_intent import classify_market_intent, grade_market_intent
from app.storage.league_memory._helpers import _contains_word, _side_from_selection_and_match
from app.utils.match_state import classify_match_state
from app.utils.time_context import match_time_context
from app.utils.doc_helpers import _impact
from app.utils.match_helpers import _extract_1x2


AUDIT_VERSION = "prediction_audit_v1"


def build_prediction_audit(prediction: dict[str, Any], doc: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a compact, stable explanation record for a prediction.

    This is intentionally read-only: it records the evidence and model state
    that already exists, without changing the pick itself.
    """
    doc = doc or {}
    picks = prediction.get("picks") or []
    primary = picks[0] if picks else {}
    signals = prediction.get("signals") or []
    models = prediction.get("models") or {}
    ensemble = models.get("ensemble") or {}
    readiness = (
        (prediction.get("data_quality") or {}).get("prediction_readiness")
        or prediction.get("prediction_readiness")
        or doc.get("prediction_readiness")
        or {}
    )
    match_state = prediction.get("match_state") or doc.get("match_state") or classify_match_state(doc)
    time_context = prediction.get("time_context") or doc.get("time_context") or match_time_context(doc)
    rejected = prediction.get("rejected_picks") or _rejected_signals_from(signals, picks)

    return {
        "version": AUDIT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": prediction.get("prediction_mode") or match_state.get("mode"),
        "match_state": _trim(match_state),
        "time_context": _trim(time_context),
        "enrichment": _enrichment_snapshot(prediction, doc, readiness),
        "market": {
            "classification": classify_market_intent(primary.get("type"), primary.get("selection"), primary),
            "pick_classifications": [
                classify_market_intent(pick.get("type"), pick.get("selection"), pick)
                for pick in picks
            ],
            "odds_at_prediction": _odds_snapshot(prediction, doc),
        },
        "signals": {
            "used": [_signal_summary(signal) for signal in signals],
            "count": len(signals),
            "support": [_signal_summary(signal) for signal in signals if _impact(signal) > 0],
            "risk": [_signal_summary(signal) for signal in signals if _impact(signal) < 0],
            "rejected": rejected,
        },
        "models": {
            "weights_used": ensemble.get("weights_used"),
            "weights_source": ensemble.get("weights_source"),
            "models_used": ensemble.get("models_used"),
            "ensemble_prediction": ensemble.get("prediction"),
            "ensemble_confidence": ensemble.get("confidence"),
            "probabilities": ensemble.get("probabilities"),
        },
        "consensus": _consensus_snapshot(signals),
        "contextual_intelligence": prediction.get("contextual_intelligence") or primary.get("contextual_intelligence") or {},
        "risk_management": prediction.get("risk_management") or primary.get("risk_management") or {},
        "confidence": _confidence_snapshot(primary),
        "no_prediction": _no_prediction_snapshot(primary, readiness),
        "temporal": _temporal_snapshot(prediction, doc, time_context),
        "learning": {
            "role_decision": prediction.get("learned_role_decision") or primary.get("learned_role_decision"),
            "role_learning": primary.get("role_learning"),
            "calibration": primary.get("calibration"),
            "regime": prediction.get("regime") or (primary.get("calibration") or {}).get("regime_name"),
        },
    }


def build_pick_audit(prediction: dict[str, Any], pick: dict[str, Any], doc: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an audit where the requested pick is the decision under review."""
    picks = prediction.get("picks") or []
    reordered = [pick, *[item for item in picks if item is not pick]]
    audit = build_prediction_audit({**prediction, "picks": reordered}, doc)
    audit["decision_pick"] = {
        "type": pick.get("type"),
        "selection": pick.get("selection") or pick.get("pick"),
        "confidence": pick.get("confidence"),
        "role": pick.get("role"),
        "reason": pick.get("reason") or pick.get("reasoning"),
    }
    return audit


def build_deferred_prediction_audit(doc: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    state = classify_match_state(doc)
    return {
        "version": AUDIT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": state.get("mode"),
        "match_state": _trim(state),
        "time_context": _trim(doc.get("time_context") or match_time_context(doc)),
        "enrichment": _enrichment_snapshot({}, doc, readiness),
        "market": {"classification": classify_market_intent("no_bet", "Avoid game"), "odds_at_prediction": _odds_snapshot({}, doc)},
        "signals": {"used": [], "count": 0, "support": [], "risk": [], "rejected": []},
        "models": {},
        "consensus": {},
        "confidence": {"final": 0, "evolution": []},
        "no_prediction": {
            "status": "deferred",
            "reason": "incomplete_enrichment_or_signal_contract",
            "missing": readiness.get("missing") or [],
            "assurance": readiness.get("assurance"),
        },
        "temporal": _temporal_snapshot({}, doc, doc.get("time_context") or {}),
        "learning": {},
    }


def grading_reason(
    pick_type: str | None,
    selection: str | None,
    final_home: int,
    final_away: int,
    match_name: str | None = None,
    market_intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = market_intent or classify_market_intent(pick_type, selection)
    result = grade_market_intent(intent, selection, final_home, final_away, match_name)
    if result == "void":
        result = _fallback_grade(pick_type, selection, final_home, final_away, match_name)
    return {
        "version": "grading_reason_v1",
        "result": result,
        "final_score": {"home": final_home, "away": final_away, "total": final_home + final_away},
        "market_intent": intent,
        "reason": _grade_text(result, intent, selection, final_home, final_away),
    }


def _enrichment_snapshot(prediction: dict[str, Any], doc: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    quality = prediction.get("data_quality") or {}
    return {
        "enriched_at": doc.get("enriched_at"),
        "minimum_enrichment_status": readiness.get("minimum_enrichment_status"),
        "assurance": readiness.get("assurance"),
        "missing": readiness.get("missing") or [],
        "data_sources": prediction.get("data_sources") or doc.get("data_sources") or {},
        "has_sofascore_detail": bool(quality.get("has_sofascore_detail") or doc.get("sofascore_detail")),
        "has_sportybet_detail": bool(quality.get("has_sportybet_detail") or doc.get("sportybet_detail") or doc.get("raw_sporty")),
        "has_sportybet_markets": bool(quality.get("has_sportybet_markets") or doc.get("sportybet_markets") or doc.get("markets")),
        "sofascore_match_status": doc.get("sofascore_match_status"),
        "sportybet_data_status": doc.get("sportybet_data_status"),
    }


def _odds_snapshot(prediction: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    movement = prediction.get("odds_movement") or {}
    return {
        "one_x_two": _extract_1x2(doc.get("sportybet_markets") or doc.get("markets") or []),
        "movement": {
            "snapshots": movement.get("snapshots") or movement.get("market_snapshots"),
            "sharp_signal": movement.get("sharp_signal"),
            "strongest_pull": movement.get("strongest_pull"),
        },
    }


def _signal_summary(signal: dict[str, Any]) -> dict[str, Any]:
    value = signal.get("value")
    return {
        "name": signal.get("name"),
        "impact": signal.get("impact"),
        "market_intent": value.get("market_intent") if isinstance(value, dict) else None,
        "selection": value.get("selection") if isinstance(value, dict) else None,
        "value": _trim(value),
    }


def _rejected_signals_from(signals: list[dict[str, Any]], picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used_names = {str(signal.get("name") or "") for signal in signals if _impact(signal) > 0}
    rejected = []
    for signal in signals:
        name = str(signal.get("name") or "")
        if name in used_names:
            continue
        if _impact(signal) < 0:
            rejected.append({"name": name, "reason": "negative_impact", "impact": signal.get("impact")})
    if picks and picks[0].get("type") == "no_bet":
        rejected.append({"name": "pick_pool", "reason": picks[0].get("reason") or "no_pick_cleared_gate"})
    return rejected[:20]


def _consensus_snapshot(signals: list[dict[str, Any]]) -> dict[str, Any]:
    contributors = []
    for signal in signals:
        if signal.get("name") not in {"consensus_longshot_value", "consensus_longshot_market_value"}:
            continue
        value = signal.get("value") if isinstance(signal.get("value"), dict) else {}
        contributors.append({
            "name": signal.get("name"),
            "selection": value.get("selection"),
            "decimal_odds": value.get("decimal_odds"),
            "edge_percent": value.get("edge_percent"),
            "supporting_models": value.get("supporting_models"),
            "opposing_models": value.get("opposing_models"),
            "risk_flags": value.get("risk_flags"),
            "market_intent": value.get("market_intent") or classify_market_intent(signal.get("name"), value.get("selection"), value),
        })
    return {"contributors": contributors, "count": len(contributors)}


def _confidence_snapshot(primary: dict[str, Any]) -> dict[str, Any]:
    calibration = primary.get("calibration") or {}
    raw = primary.get("raw_confidence") or calibration.get("raw_confidence") or primary.get("confidence")
    final = primary.get("confidence")
    evolution = [{"stage": "raw", "confidence": raw}]
    if calibration.get("win_rate") is not None:
        evolution.append({"stage": "historical_calibration", "win_rate": calibration.get("win_rate"), "samples": calibration.get("samples")})
    if calibration.get("memory_weighting"):
        memory = calibration.get("memory_weighting") or {}
        evolution.append({"stage": "league_country_memory", "blended_win_rate": memory.get("blended_win_rate"), "adjustment": memory.get("confidence_adjustment")})
    evolution.append({"stage": "final", "confidence": final})
    return {
        "raw": raw,
        "final": final,
        "calibration": calibration,
        "role_learning": primary.get("role_learning"),
        "evolution": evolution,
    }


def _no_prediction_snapshot(primary: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    if primary.get("type") == "no_bet":
        return {
            "status": "avoid_game",
            "reason": primary.get("reason"),
            "missing": readiness.get("missing") or [],
        }
    if readiness and not readiness.get("ready", True):
        return {"status": "deferred", "missing": readiness.get("missing") or []}
    return {"status": "published"}


def _temporal_snapshot(prediction: dict[str, Any], doc: dict[str, Any], time_context: dict[str, Any]) -> dict[str, Any]:
    movement = prediction.get("odds_movement") or {}
    return {
        "minutes_until_kickoff": time_context.get("minutes_until_kickoff"),
        "local_date": time_context.get("local_date"),
        "local_time": time_context.get("local_time"),
        "prediction_age_seconds": 0,
        "time_decay_applied": prediction.get("time_decay_applied"),
        "time_decay_multiplier": prediction.get("time_decay_multiplier"),
        "odds_snapshot_count": movement.get("snapshots") or movement.get("market_snapshots"),
        "strongest_market_pull": movement.get("strongest_pull"),
    }


def _fallback_grade(pick_type: str | None, selection: str | None, home: int, away: int, match_name: str | None) -> str:
    """Last-resort grade when `grade_market_intent` cannot classify the pick.

    This used to re-implement its own independent (and less careful)
    substring matching against `match_name`. It now delegates the
    team-name-matching case to the single hardened
    `_side_from_selection_and_match`, so there is exactly one place that
    does fuzzy text matching for grading, and exactly one place to audit
    or further improve it.
    """
    sel = str(selection or "").lower()
    pt = str(pick_type or "").lower()
    if pt in {"match_result", "ensemble_1x2", "live_match_winner"}:
        if _contains_word(sel, "home"):
            side: str | None = "home"
        elif _contains_word(sel, "away"):
            side = "away"
        elif _contains_word(sel, "draw"):
            side = "draw"
        else:
            # Selection is most likely a team display name (e.g. "Arsenal
            # Win") rather than a literal home/away/draw label.
            resolved = _side_from_selection_and_match(sel, match_name)
            # "ambiguous" (both team names plausibly matched) must be
            # refused here too, not treated as a guessable side.
            side = resolved if resolved in ("home", "away") else None
        if side == "home":
            return "win" if home > away else "loss"
        if side == "away":
            return "win" if away > home else "loss"
        if side == "draw":
            return "win" if home == away else "loss"
    return "void"


def _grade_text(result: str, intent: dict[str, Any], selection: str | None, home: int, away: int) -> str:
    total = home + away
    market = intent.get("market") or "market"
    if result == "void":
        return f"{selection or market} could not be graded cleanly against {home}-{away}."
    return f"{selection or market} graded {result} on final score {home}-{away} ({total} total goals)."


def _trim(value: Any, *, max_items: int = 8) -> Any:
    if isinstance(value, dict):
        return {str(k): _trim(v, max_items=max_items) for k, v in list(value.items())[:max_items]}
    if isinstance(value, list):
        return [_trim(item, max_items=max_items) for item in value[:max_items]]
    return value

