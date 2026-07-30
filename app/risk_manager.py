from __future__ import annotations

from typing import Any
from copy import deepcopy

from app.db import db_conn
from app.config import get_settings
from app.market_intent import classify_market_intent
from app.risk_learner import get_learned_risk_controls, get_learned_risk_controls_for_pick, LearnedRiskControls
from app.validation_gate import evaluate_promotion_gate


# ── Static Fallbacks (used when learned data is insufficient) ──────────────────
# These remain as safety nets but are progressively replaced by learned values.

MAX_SINGLE_BET_STAKE_PER_100 = 5.0
MAX_DEGRADED_STAKE_PER_100 = 1.0
MAX_HIGH_RISK_STAKE_PER_100 = 0.5
LONGSHOT_ODDS = 3.0
EXTREME_LONGSHOT_ODDS = 5.0

# Minimum samples required before trusting learned risk controls
LEARNED_RISK_MIN_SAMPLES = 8


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

    Uses learned risk controls when sufficient historical data exists,
    falling back to static rules for bootstrap/unproven conditions.
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

    real_picks = [pick for pick in picks if pick.get("type") != "no_bet"]
    original = [deepcopy(pick) for pick in real_picks]

    # Get learned risk controls for each pick (if enough data exists)
    learned_controls: dict[int, LearnedRiskControls] = {}
    for idx, pick in enumerate(picks):
        if pick.get("type") == "no_bet":
            continue
        try:
            controls = get_learned_risk_controls_for_pick(
                doc, pick, contextual_intelligence, report
            )
            if controls.samples >= LEARNED_RISK_MIN_SAMPLES:
                learned_controls[idx] = controls
                report.setdefault("learned_controls", []).append({
                    "pick_index": idx,
                    "selection": pick.get("selection"),
                    "source": controls.source,
                    "samples": controls.samples,
                    "win_rate": controls.win_rate,
                    "confidence_cap": controls.confidence_cap,
                    "stake_cap": controls.stake_cap_per_100,
                })
        except Exception:
            pass  # Fall back to static rules if learner fails

    for idx, pick in enumerate(picks):
        if pick.get("type") == "no_bet":
            _stamp_pick(pick, report)
            continue

        controls = learned_controls.get(idx)
        _apply_pick_limits(pick, report, signals=signals, models=models, learned_controls=controls)
        gate = evaluate_promotion_gate(doc, pick)
        pick["validation_gate"] = gate
        report.setdefault("validation_gates", []).append({
            "selection": pick.get("selection"),
            "pick_type": pick.get("type"),
            **gate,
        })
        if gate.get("bootstrap_mode"):
            bootstrap_cap = (
                settings.risk_manager_bootstrap_confidence_ceiling + 6
                if _pick_has_model_consensus(pick, models)
                else settings.risk_manager_bootstrap_confidence_ceiling
            )
            _cap_pick_confidence(
                pick,
                report,
                bootstrap_cap,
                "validation bootstrap confidence ceiling",
            )
        if not gate.get("allowed", False):
            report["validation_block"] = True
            reasons = gate.get("reasons") or ["promotion_gate_failed"]
            bootstrap_only = set(reasons).issubset({"insufficient_calibration_samples", "insufficient_clv_samples"})
            for reason in reasons:
                _add_violation(report, f"validation_{reason}", hard=not bootstrap_only)
    _refresh_risk_level(report)

    # Apply learned hard-block overrides
    _apply_learned_hard_blocks(real_picks, report, learned_controls)

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
    hard_block = report["hard_block"] and not _strong_signal_override(real_picks, report, models)
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

    # Record risk control application for learning
    _record_risk_control_application(doc, picks, report, learned_controls)

    return report


def _apply_learned_hard_blocks(
    real_picks: list[dict[str, Any]],
    report: dict[str, Any],
    learned_controls: dict[int, LearnedRiskControls],
) -> None:
    """Apply learned hard-block decisions when static rules didn't already block."""
    if not real_picks:
        return

    for idx, controls in learned_controls.items():
        if idx >= len(real_picks):
            continue
        if controls.hard_block and not report.get("hard_block"):
            report["hard_block"] = True
            report.setdefault("hard_block_reasons", [])
            reason = controls.block_reason or "learned_poor_performance"
            if reason not in report["hard_block_reasons"]:
                report["hard_block_reasons"].append(reason)
            report["actions"].append({
                "type": "learned_hard_block",
                "pick_index": idx,
                "selection": real_picks[idx].get("selection"),
                "reason": reason,
                "samples": controls.samples,
                "win_rate": controls.win_rate,
            })


def _base_report(
    doc: dict[str, Any],
    readiness: dict[str, Any],
    contextual_intelligence: dict[str, Any],
    odds_movement: dict[str, Any],
) -> dict[str, Any]:
    assurance = str(readiness.get("assurance") or "deferred")
    learned_classification = contextual_intelligence.get("learned_classification")
    smart_bet = bool(contextual_intelligence.get("smart_bet") or learned_classification == "smart_bet")
    report = {
        "match_id": str(doc.get("sportybet_id") or doc.get("id") or doc.get("match_id") or ""),
        "assurance": assurance,
        "risk_level": "low",
        "hard_block": False,
        "hard_block_reasons": [],
        "violations": [],
        "actions": [],
        "strengths": [],
        "smart_bet": smart_bet,
        "learned_classification": learned_classification,
        "contextual_intelligence": contextual_intelligence,
        "odds_movement": odds_movement,
        "readiness": readiness,
        "validation_block": False,
        "published": False,
        "block_reason_summary": [],
    }
    if readiness.get("missing"):
        report["violations"].append("readiness_not_ready")
        if not readiness.get("ready"):
            report["hard_block"] = True
            report["hard_block_reasons"].append("readiness_not_ready")
    if contextual_intelligence.get("risk") and isinstance(contextual_intelligence.get("risk"), dict):
        report["contextual_risk"] = contextual_intelligence.get("risk")
    if odds_movement:
        report["odds_movement_summary"] = {
            "sharp_signal": odds_movement.get("sharp_signal"),
            "strongest_pull": odds_movement.get("strongest_pull"),
        }
    return report


def _apply_pick_limits(
    pick: dict[str, Any],
    report: dict[str, Any],
    *,
    signals: list[dict[str, Any]],
    models: dict[str, Any],
    learned_controls: LearnedRiskControls | None = None,
) -> None:
    original_confidence = int(pick.get("confidence") or 0)
    decimal_odds = _pick_odds(pick)

    # Start with learned cap if available, otherwise use static defaults
    if learned_controls and learned_controls.samples >= LEARNED_RISK_MIN_SAMPLES:
        cap = learned_controls.confidence_cap
        cap_source = f"learned (samples={learned_controls.samples}, win_rate={learned_controls.win_rate})"
    else:
        cap = 96
        cap_source = "static default"

    reasons: list[str] = []

    # Learned controls override static degradation caps
    if learned_controls and learned_controls.samples >= LEARNED_RISK_MIN_SAMPLES:
        if learned_controls.confidence_cap < cap:
            cap = learned_controls.confidence_cap
            reasons.append(f"learned confidence cap ({cap_source})")
    else:
        # Static fallback rules
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
            cap = min(cap, 90)
            reasons.append("medium risk context caps confidence")

    # Longshot and model disagreement rules (always apply)
    if decimal_odds >= EXTREME_LONGSHOT_ODDS:
        cap = min(cap, 68)
        reasons.append("extreme longshot cap")
    elif decimal_odds >= LONGSHOT_ODDS:
        cap = min(cap, 76)
        reasons.append("longshot cap")

    disagreement = _model_disagreement(pick, models)
    if disagreement["opposing_models"] > 0:
        cap = min(cap, 82 - disagreement["opposing_models"] * 3)
        reasons.append(f"{disagreement['opposing_models']} model(s) oppose pick")
    if decimal_odds >= EXTREME_LONGSHOT_ODDS and disagreement["opposing_models"] >= 2:
        _add_violation(report, "extreme_longshot_model_disagreement", hard=True)
        reasons.append("extreme longshot opposed by multiple models")
    if _negative_signal_count(signals) >= 4 and _positive_signal_count(signals) <= 3:
        cap = min(cap, 72)
        reasons.append("risk signals outnumber support")
        if decimal_odds >= LONGSHOT_ODDS:
            _add_violation(report, "longshot_negative_signals_outnumber_support", hard=False)

    # Historical calibration (always applies as a floor check)
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
            "to": pick.get("confidence"),
            "reasons": reasons,
            "source": cap_source,
        })
        if original_confidence - int(pick.get("confidence") or 0) >= 20:
            _add_violation(report, "confidence_capped_by_20_plus_points", hard=False)

    # Stake capping with learned values
    _cap_stake(pick, report, learned_controls=learned_controls)


def _cap_stake(
    pick: dict[str, Any],
    report: dict[str, Any],
    *,
    learned_controls: LearnedRiskControls | None = None,
) -> None:
    stake = pick.get("stake") if isinstance(pick.get("stake"), dict) else {}
    if not stake:
        return

    # Use learned stake cap if available, otherwise static fallback
    if learned_controls and learned_controls.samples >= LEARNED_RISK_MIN_SAMPLES:
        limit = learned_controls.stake_cap_per_100
        limit_source = f"learned (samples={learned_controls.samples})"
    else:
        limit = MAX_SINGLE_BET_STAKE_PER_100
        if report["assurance"] in {"sportybet_prematch_minimum", "sportybet_market_signal"}:
            limit = min(limit, MAX_DEGRADED_STAKE_PER_100)
        if report["risk_level"] == "high":
            limit = min(limit, MAX_HIGH_RISK_STAKE_PER_100)
        limit_source = "static"

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
            "source": limit_source,
        })
    if report["risk_level"] == "high":
        stake["recommended"] = False


def _stamp_pick(pick: dict[str, Any], report: dict[str, Any]) -> None:
    learned = report.get("learned_controls", [])
    stake_limit = MAX_SINGLE_BET_STAKE_PER_100
    if report["risk_level"] == "high":
        stake_limit = MAX_HIGH_RISK_STAKE_PER_100
    elif report["assurance"] in {"sportybet_prematch_minimum", "sportybet_market_signal"}:
        stake_limit = MAX_DEGRADED_STAKE_PER_100

    # Use learned stake limit if available for this pick
    if learned:
        for lc in learned:
            if lc.get("selection") == pick.get("selection") and lc.get("samples", 0) >= LEARNED_RISK_MIN_SAMPLES:
                stake_limit = lc.get("stake_cap", stake_limit)
                break

    pick["risk_management"] = {
        "risk_level": report["risk_level"],
        "violations": report["violations"],
        "strengths": report.get("strengths") or [],
        "smart_bet": report.get("smart_bet", False),
        "learned_classification": report.get("learned_classification"),
        "stake_limit_per_100": stake_limit,
        "learned_controls_applied": bool(learned),
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
    if highest < 55:
        return False
    if "learned_history_high_risk" in report.get("violations", []):
        return False
    if "readiness_not_ready" in report.get("violations", []):
        return False
    model_count = _available_model_count(models)
    return model_count >= 2 or (model_count >= 1 and highest >= 72)


def _strong_signal_override(real_picks: list[dict[str, Any]], report: dict[str, Any], models: dict[str, Any]) -> bool:
    """Allow hard-block bypass only when confidence AND model consensus are both strong."""
    if not real_picks:
        return False
    highest = max(int(pick.get("confidence") or 0) for pick in real_picks)
    if highest < 72:
        return False
    if "learned_history_high_risk" in report.get("hard_block_reasons", []):
        return False
    if "readiness_not_ready" in report.get("hard_block_reasons", []):
        return False
    # Require at least one pick with no opposing models
    for pick in real_picks:
        disagreement = _model_disagreement(pick, models)
        if disagreement["opposing_models"] == 0 and disagreement["supporting_models"] >= 1:
            return True
    return False


def _pick_has_model_consensus(pick: dict[str, Any], models: dict[str, Any]) -> bool:
    disagreement = _model_disagreement(pick, models)
    return disagreement["opposing_models"] == 0 and disagreement["supporting_models"] >= 2


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


def _record_risk_control_application(
    doc: dict[str, Any],
    picks: list[dict[str, Any]],
    report: dict[str, Any],
    learned_controls: dict[int, LearnedRiskControls],
) -> None:
    """Record risk control application for later learning."""
    try:
        from app.db import DB_PATH
        from app.league_memory import _init_db
        import json

        match_id = str(doc.get("sportybet_id") or doc.get("id") or doc.get("match_id") or "")
        if not match_id:
            return

        _init_db()
        with db_conn(timeout=10) as conn:
            from app.risk_learner import _init_risk_learner_tables
            _init_risk_learner_tables(conn)

            for idx, pick in enumerate(picks):
                if pick.get("type") == "no_bet":
                    continue
                controls = learned_controls.get(idx)
                conditions = []
                if controls:
                    # Extract conditions from the report
                    for v in report.get("violations", []):
                        conditions.append(v)
                    if not conditions:
                        conditions = ["standard"]

                conn.execute("""
                    insert into risk_control_history
                        (match_id, risk_conditions, pick_type, raw_confidence,
                         applied_confidence_cap, applied_stake_cap, hard_blocked)
                    values (?, ?, ?, ?, ?, ?, ?)
                """, (
                    match_id,
                    json.dumps(conditions),
                    pick.get("type", "match_result"),
                    int(pick.get("confidence") or 0),
                    int(pick.get("confidence") or 0),  # After capping
                    float(pick.get("stake", {}).get("stake_per_100") or 0),
                    1 if report.get("hard_block") else 0,
                ))
            conn.commit()
    except Exception:
        pass  # Non-critical — don't break prediction flow
