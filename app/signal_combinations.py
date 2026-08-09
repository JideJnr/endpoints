from __future__ import annotations

import hashlib
import json
import re
from typing import Any


EXCLUDED_SIGNAL_NAMES = {
    "learned_signal_adjustment",
    "learned_signal_combination",
    "prediction_memory",
    "risk_management",
    "calibration_gap_moderate",
    "calibration_gap_severe",
}


def build_signal_combination(
    *,
    signals: list[dict[str, Any]],
    pick_type: str | None,
    selection: str | None,
    prediction_mode: str | None = None,
    live_context: dict[str, Any] | None = None,
    max_signals: int = 8,
) -> dict[str, Any]:
    names = _signal_names(signals, max_signals=max_signals)
    context = _normalise_live_context(live_context or {})
    payload = {
        "pick_type": _clean_token(pick_type),
        "selection": _selection_family(selection),
        "prediction_mode": _clean_token(prediction_mode or context.get("prediction_mode") or "prematch"),
        "signals": names,
        "minute_bucket": context.get("minute_bucket"),
        "score_state": context.get("score_state"),
        "live_stats": bool(context.get("live_stats")),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {
        "key": hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16],
        "payload": payload,
        "signal_names": names,
    }


def live_context_from_prediction(prediction: dict[str, Any]) -> dict[str, Any]:
    score = prediction.get("score") or {}
    readiness = (prediction.get("data_quality") or {}).get("prediction_readiness") or prediction.get("prediction_readiness") or {}
    return _normalise_live_context(
        {
            "prediction_mode": prediction.get("prediction_mode") or readiness.get("prediction_mode"),
            "minute": prediction.get("minute"),
            "score": score,
            "live_stats": bool(
                prediction.get("live_statistics_summary")
                or prediction.get("live_data_sources")
                or readiness.get("live_data_sources")
                or (prediction.get("data_quality") or {}).get("live_stats_available")
            ),
        }
    )


def live_context_from_doc(doc: dict[str, Any], minute: Any = None) -> dict[str, Any]:
    score = doc.get("score") or {}
    if minute is None:
        played_seconds = doc.get("played_seconds")
        try:
            minute = int(float(played_seconds or 0) / 60) if played_seconds else 0
        except (TypeError, ValueError):
            minute = 0
    return _normalise_live_context(
        {
            "prediction_mode": "live" if (doc.get("match_state") or {}).get("is_live") else doc.get("prediction_mode"),
            "minute": minute,
            "score": score,
            "live_stats": bool(doc.get("live_statistics") or doc.get("live_data_sofascore") or doc.get("live_data_sportybet")),
        }
    )


def _signal_names(signals: list[dict[str, Any]], *, max_signals: int) -> list[str]:
    ranked: list[tuple[float, str]] = []
    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        name = _clean_token(signal.get("name"))
        if not name or name in EXCLUDED_SIGNAL_NAMES:
            continue
        try:
            impact = abs(float(signal.get("impact") or 0))
        except (TypeError, ValueError):
            impact = 0.0
        ranked.append((impact, name))
    names: list[str] = []
    for _, name in sorted(ranked, key=lambda item: (-item[0], item[1])):
        if name not in names:
            names.append(name)
        if len(names) >= max_signals:
            break
    return sorted(names)


def _normalise_live_context(context: dict[str, Any]) -> dict[str, Any]:
    minute = _to_int(context.get("minute"), 0)
    score = context.get("score") or {}
    home = _to_int(score.get("home"), 0)
    away = _to_int(score.get("away"), 0)
    return {
        "prediction_mode": _clean_token(context.get("prediction_mode") or "prematch"),
        "minute_bucket": context.get("minute_bucket") or _minute_bucket(minute),
        "score_state": context.get("score_state") or _score_state(home, away),
        "live_stats": bool(context.get("live_stats")),
    }


def _selection_family(selection: str | None) -> str:
    text = str(selection or "").lower()
    for phrase in ("over 0.5", "over 1.5", "over 2.5", "over 3.5", "under 1.5", "under 2.5", "under 3.5"):
        if phrase in text:
            return phrase.replace(" ", "_")
    if "both teams to score" in text or "btts" in text:
        return "btts_no" if " no" in text or "- no" in text else "btts_yes"
    if "next goal" in text or "late goal" in text:
        return "next_or_late_goal"
    if "draw" in text:
        return "draw_family"
    if "away" in text:
        return "away_family"
    if "home" in text:
        return "home_family"
    return _clean_token(selection)


def _minute_bucket(minute: int) -> str:
    if minute <= 0:
        return "prematch"
    if minute <= 15:
        return "00-15"
    if minute <= 30:
        return "16-30"
    if minute <= 45:
        return "31-45"
    if minute <= 60:
        return "46-60"
    if minute <= 70:
        return "61-70"
    if minute <= 80:
        return "71-80"
    return "81-90+"


def _score_state(home: int, away: int) -> str:
    if home == away:
        return "level_0_0" if home == 0 else "level_scored"
    if abs(home - away) == 1:
        return "one_goal_game"
    return "multi_goal_game"


def _clean_token(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9+.]+", "_", text)
    return text.strip("_")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
