from __future__ import annotations

from typing import Any
from copy import deepcopy

from app.config import get_settings
from app.market_intent import classify_market_intent
from app.validation_gate import evaluate_promotion_gate


MAX_SINGLE_BET_STAKE_PER_100 = 5.0
MAX_DEGRADED_STAKE_PER_100 = 1.0
MAX_HIGH_RISK_STAKE_PER_100 = 0.5
LONGSHOT_ODDS = 3.0
EXTREME_LONGSHOT_ODDS = 5.0


def apply_risk_controls(
    doc: dict[str, Any],
    picks: list[dict[str, Any]],
    *,
    signals: list[dict[str, Any]] | None = None,
    contextual_intelligence: dict[str, Any] | None = None,
    odds_movement: dict[str, Any] | None = None,
    models: dict[str, Any] | None = None,
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Desk-style risk controls for published predictions.

    This is governance, not prediction. It only caps confidence/stake or
    converts the slate to no-bet when the data and market controls fail.
    """
    signals = signals or []
    contextual_intelligence = contextual_intelligence or {}
    odds_movement = odds_movement or {}
    models = models or {}
    readiness = readiness or {}
    report = _base_report(doc, readiness, contextual_intelligence, odds_movement)
    settings = get_settings()
    if not picks:
        report["actions"].append({"type": "no_picks", "reason": "empty_pick_pool"})
        return report

    original = [deepcopy(pick) for pick in picks if pick.get("type") != "no_bet"]
    for pick in picks:
        if pick.get("type") == "no_bet":
            _stamp_pick(pick, report)
            continue
        _apply_pick_limits(pick, report, signals=signals, models=models)
        gate = evaluate_promotion_gate(doc, pick)
        pick["validation_gate"] = gate
        report.setdefault("validation_gates", []).append({
            "selection": pick.get("selection"),
            "pick_type": pick.get("type"),
            **gate,
        })
        if gate.get("bootstrap_mode"):
            _cap_pick_confidence(
                pick,
                report,
                settings.risk_manager_bootstrap_confidence_ceiling,
                "validation bootstrap confidence ceiling",
            )
        if not gate.get("allowed", False):
            report["validation_block"] = True
            reasons = gate.get("reasons") or ["promotion_gate_failed"]
            bootstrap_only = set(reasons).issubset({"insufficient_calibration_samples", "insufficient_clv_samples"})
            for reason in reasons:
                _add_violation(report, f"validation_{reason}", hard=not bootstrap_only)
    _refresh_risk_level(report)

    real_picks = [pick for pick in picks if pick.get("type") != "no_bet"]
    highest_conf = max([int(pick.get("confidence") or 0) for pick in real_picks] or [0])
    if (
        report.get("hard_block")
        and set(report.get("hard_block_reasons") or []) == {"contextual_high_risk"}
        and highest_conf >= 75
    ):
        report["hard_block"] = False
        report["actions"].append({"type": "hard_block_demoted", "reason": "contextual_high_risk", "confidence_cap": 72})
        for pick in real_picks:
            _cap_pick_confidence(pick, report, 72, "contextual high risk demoted to confidence cap")
        _refresh_risk_level(report)
    if _publishable_despite_medium_risk(real_picks, report, models):
        report["hard_block"] = False
        _refresh_risk_level(report)
    hard_block = report["hard_block"] and highest_conf < 82
    if hard_block:
        report["block_reason_summary"] = list(report.get("hard_block_reasons") or report["violations"][:4])
        reasons = "; ".join(report["block_reason_summary"]) or "risk controls blocked publication"
        picks[:] = [{
            "type": "no_bet",
            "selection": "Avoid game",
            "confidence": 50,
            "reason": f"Risk committee blocked this pick: {reasons}",
            "risk_management": report,
            "suppressed_picks": original[:5],
            "role": "primary",
        }]
        report["actions"].append({"type": "convert_to_no_bet", "reason": reasons})
    else:
        for pick in picks:
            _stamp_pick(pick, report)

    report["published"] = not any(pick.get("type") == "no_bet" for pick in picks[:1])
    report["max_single_bet_stake_per_100"] = MAX_SINGLE_BET_STAKE_PER_100
    report.setdefault("block_reason_summary", list(report.get("hard_block_reasons") or []))
    return report


def _base_report(
    doc: dict[str, Any],
    readiness: dict[str, Any],
    contextual: dict[str, Any],
    odds_movement: dict[str, Any],
) -> dict[str, Any]:
    assurance = readiness.get("assurance") or ((doc.get("prediction_readiness") or {}).get("assurance"))
    risk = contextual.get("risk") or {}
    learned = contextual.get("learned_performance") or {}
    learned_class = learned.get("classification")
    market = contextual.get("market_behavior") or {}
    flags = list(market.get("flags") or [])
    volatility = float(market.get("volatility_percent") or 0)
    volatility_hard_block_threshold = float(get_settings().risk_manager_volatility_hard_block_threshold)
    strongest = odds_movement.get("strongest_pull") if isinstance(odds_movement.get("strongest_pull"), dict) else {}
    if not volatility and strongest:
        volatility = abs(_to_float(
            strongest.get("odds_change_percent")
            or strongest.get("implied_change_percent")
            or strongest.get("change_percent")
        ) or 0)
    violations: list[str] = []
    hard_block = False

    if assurance in {"sportybet_prematch_minimum", "sportybet_market_signal"}:
        violations.append("degraded_provider_assurance")
    if risk.get("level") == "high" and learned_class != "smart_bet":
        violations.append("contextual_high_risk")
        hard_block = True
    elif risk.get("level") == "high":
        violations.append("contextual_high_risk_tempered_by_learning")
    if learned_class == "learned_high_risk":
        violations.append("learned_history_high_risk")
        hard_block = True
    if volatility >= volatility_hard_block_threshold:
        violations.append("market_volatility_spike")
        hard_block = True
    elif volatility >= 18:
        violations.append("market_volatility_requires_recheck")
    elif volatility >= 18:
        violations.append("market_volatility_tempered_by_learning")
    elif volatility >= 12:
        violations.append("market_volatility_requires_recheck")
    if "thin_market_history" in flags:
        violations.append("thin_market_history")
    if readiness and not readiness.get("ready", True):
        violations.append("readiness_not_ready")
        hard_block = True
    if learned_class == "smart_bet":
        hard_block = False

    return {
        "version": "risk_management_v1",
        "risk_level": "high" if hard_block else "medium" if violations else "low",
        "hard_block": hard_block,
        "validation_block": False,
        "validation_gates": [],
        "assurance": assurance,
        "violations": violations,
        "hard_block_reasons": [reason for reason in violations if reason in {"contextual_high_risk", "learned_history_high_risk", "market_volatility_spike", "readiness_not_ready"}],
        "block_reason_summary": [],
        "market_flags": flags,
        "volatility_percent": round(volatility, 2),
        "learned_classification": learned_class,
        "learned_performance": learned,
        "strengths": ["learned_smart_bet"] if learned_class == "smart_bet" else [],
        "smart_bet": learned_class == "smart_bet" and not hard_block,
        "actions": [],
    }


def _apply_pick_limits(
    pick: dict[str, Any],
    report: dict[str, Any],
    *,
    signals: list[dict[str, Any]],
    models: dict[str, Any],
) -> None:
    original_confidence = int(pick.get("confidence") or 0)
    decimal_odds = _pick_odds(pick)
    cap = 96
    reasons: list[str] = []

    if report["assurance"] in {"sportybet_prematch_minimum", "sportybet_market_signal"}:
        cap = min(cap, 62)
        reasons.append("degraded provider assurance caps confidence")
    if report.get("smart_bet"):
        cap = max(cap, 88)
        reasons.append("learned smart-bet profile protects confidence")
    if report["risk_level"] == "high":
        cap = min(cap, 68)
        reasons.append("high risk context caps confidence")
    elif report["risk_level"] == "medium":
        cap = min(cap, 82)
        reasons.append("medium risk context caps confidence")
    if decimal_odds >= EXTREME_LONGSHOT_ODDS:
        cap = min(cap, 68)
        reasons.append("extreme longshot cap")
    elif decimal_odds >= LONGSHOT_ODDS:
        cap = min(cap, 76)
        reasons.append("longshot cap")

    disagreement = _model_disagreement(pick, models)
    if disagreement["opposing_models"] > 0:
        cap = min(cap, 78 - disagreement["opposing_models"] * 4)
        reasons.append(f"{disagreement['opposing_models']} model(s) oppose pick")
    if decimal_odds >= EXTREME_LONGSHOT_ODDS and disagreement["opposing_models"] >= 2:
        _add_violation(report, "extreme_longshot_model_disagreement", hard=True)
        reasons.append("extreme longshot opposed by multiple models")
    if _negative_signal_count(signals) >= 3 and _positive_signal_count(signals) <= 3:
        cap = min(cap, 72)
        reasons.append("risk signals outnumber support")
        if decimal_odds >= LONGSHOT_ODDS:
            _add_violation(report, "longshot_negative_signals_outnumber_support", hard=False)
    calibration = pick.get("calibration") if isinstance(pick.get("calibration"), dict) else {}
    win_rate = _to_float(calibration.get("win_rate"))
    samples = int(_to_float(calibration.get("samples")) or 0)
    if win_rate is not None and samples >= 10 and original_confidence > win_rate + 12:
        cap = min(cap, int(max(55, min(92, win_rate + 8))))
        reasons.append("historical calibration caps overconfident pick")

    if cap < original_confidence:
        survived_gate = (pick.get("validation_gate") or {}).get("allowed", True)
        pick["confidence"] = max(52 if survived_gate else 1, cap)
        report["actions"].append({
            "type": "confidence_cap",
            "selection": pick.get("selection"),
            "from": original_confidence,
            "to": pick["confidence"],
            "reasons": reasons,
        })
        if original_confidence - int(pick.get("confidence") or 0) >= 20:
            _add_violation(report, "confidence_capped_by_20_plus_points", hard=False)
    _cap_stake(pick, report)


def _cap_stake(pick: dict[str, Any], report: dict[str, Any]) -> None:
    stake = pick.get("stake") if isinstance(pick.get("stake"), dict) else {}
    if not stake:
        return
    limit = MAX_SINGLE_BET_STAKE_PER_100
    if report["assurance"] in {"sportybet_prematch_minimum", "sportybet_market_signal"}:
        limit = min(limit, MAX_DEGRADED_STAKE_PER_100)
    if report["risk_level"] == "high":
        limit = min(limit, MAX_HIGH_RISK_STAKE_PER_100)
    before = float(stake.get("stake_per_100") or 0)
    after = round(min(before, limit), 2)
    stake["risk_cap_per_100"] = limit
    stake["stake_per_100"] = after
    if before > after:
        stake["recommended"] = after > 0 and report["risk_level"] != "high"
        report["actions"].append({
            "type": "stake_cap",
            "selection": pick.get("selection"),
            "from": round(before, 2),
            "to": after,
            "limit": limit,
        })
    if report["risk_level"] == "high":
        stake["recommended"] = False


def _stamp_pick(pick: dict[str, Any], report: dict[str, Any]) -> None:
    pick["risk_management"] = {
        "risk_level": report["risk_level"],
        "violations": report["violations"],
        "strengths": report.get("strengths") or [],
        "smart_bet": report.get("smart_bet", False),
        "learned_classification": report.get("learned_classification"),
        "stake_limit_per_100": (
            MAX_HIGH_RISK_STAKE_PER_100
            if report["risk_level"] == "high"
            else MAX_DEGRADED_STAKE_PER_100
            if report["assurance"] in {"sportybet_prematch_minimum", "sportybet_market_signal"}
            else MAX_SINGLE_BET_STAKE_PER_100
        ),
    }


def _add_violation(report: dict[str, Any], reason: str, *, hard: bool) -> None:
    if reason not in report["violations"]:
        report["violations"].append(reason)
    if hard:
        report["hard_block"] = True
        report.setdefault("hard_block_reasons", [])
        if reason not in report["hard_block_reasons"]:
            report["hard_block_reasons"].append(reason)


def _refresh_risk_level(report: dict[str, Any]) -> None:
    if report.get("hard_block"):
        report["risk_level"] = "high"
    elif report.get("violations"):
        report["risk_level"] = "medium"
    else:
        report["risk_level"] = "low"


def _model_disagreement(pick: dict[str, Any], models: dict[str, Any]) -> dict[str, Any]:
    intent = classify_market_intent(pick.get("type"), pick.get("selection"), pick)
    if intent.get("market") != "1x2":
        return {"opposing_models": 0, "supporting_models": 0}
    side = intent.get("direction")
    opposing = supporting = 0
    for name in ("poisson", "dixon_coles"):
        probs = ((models.get(name) or {}).get("probabilities") or {})
        winner = _winner_from_probs(probs)
        if not winner:
            continue
        if winner == side:
            supporting += 1
        else:
            opposing += 1
    elo = models.get("elo") or {}
    elo_winner = _winner_from_probs({
        "home_win": elo.get("home_win_probability"),
        "away_win": elo.get("away_win_probability"),
        "draw": elo.get("draw"),
    })
    if elo_winner:
        if elo_winner == side:
            supporting += 1
        else:
            opposing += 1
    return {"opposing_models": opposing, "supporting_models": supporting}


def _cap_pick_confidence(pick: dict[str, Any], report: dict[str, Any], cap: int, reason: str) -> None:
    original = int(pick.get("confidence") or 0)
    if original <= cap:
        return
    pick["confidence"] = max(52, min(original, int(cap)))
    report["actions"].append({
        "type": "confidence_cap",
        "selection": pick.get("selection"),
        "from": original,
        "to": pick["confidence"],
        "reasons": [reason],
    })


def _publishable_despite_medium_risk(real_picks: list[dict[str, Any]], report: dict[str, Any], models: dict[str, Any]) -> bool:
    if not real_picks:
        return False
    highest = max(int(pick.get("confidence") or 0) for pick in real_picks)
    if highest < 65:
        return False
    if "learned_history_high_risk" in report.get("violations", []):
        return False
    if "readiness_not_ready" in report.get("violations", []):
        return False
    return _available_model_count(models) >= 2


def _available_model_count(models: dict[str, Any]) -> int:
    count = 0
    for name in ("poisson", "dixon_coles", "elo", "ensemble"):
        value = models.get(name)
        if value and not (isinstance(value, dict) and value.get("error")):
            count += 1
    return count


def _winner_from_probs(probs: dict[str, Any]) -> str | None:
    values = {
        "home": _to_float(probs.get("home_win") or probs.get("home")),
        "draw": _to_float(probs.get("draw")),
        "away": _to_float(probs.get("away_win") or probs.get("away")),
    }
    values = {key: value for key, value in values.items() if value is not None}
    if not values:
        return None
    return max(values.items(), key=lambda item: item[1])[0]


def _pick_odds(pick: dict[str, Any]) -> float:
    stake = pick.get("stake") if isinstance(pick.get("stake"), dict) else {}
    return _to_float(stake.get("decimal_odds") or pick.get("decimal_odds") or pick.get("odds")) or 0.0


def _positive_signal_count(signals: list[dict[str, Any]]) -> int:
    return sum(1 for signal in signals if _to_float(signal.get("impact")) and float(signal.get("impact") or 0) > 0)


def _negative_signal_count(signals: list[dict[str, Any]]) -> int:
    return sum(1 for signal in signals if _to_float(signal.get("impact")) and float(signal.get("impact") or 0) < 0)


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None
