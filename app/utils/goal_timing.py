"""
Shared goal-timing extraction utilities.

Previously duplicated between:
  - app/competition/competition_special.py::_extract_goal_timing
  - app/team_watcher/team_watcher.py::_extract_goal_timing_from_doc

Both parsed sofascore incidents into the same structured dict.
This module is the single canonical implementation; both callers import from here.
"""
from __future__ import annotations

from typing import Any

from app.utils.doc_helpers import _band


def extract_goal_timing_from_incidents(incidents: list[Any]) -> dict[str, Any]:
    """Parse a list of sofascore incident dicts into structured goal-timing data.

    Returns a dict with total_goals, half splits, 10-minute bands,
    first_goal_minute, avg_interval_minutes, and the raw goal_minutes list.
    Returns a minimal dict with total_goals=0 when no goals are found.
    """
    goal_minutes: list[int] = []
    for inc in incidents or []:
        if not isinstance(inc, dict):
            continue
        inc_type = str(inc.get("incidentType") or inc.get("type") or "").lower()
        if inc_type not in ("goal", "penalty"):
            continue
        minute = inc.get("time") or inc.get("minute")
        try:
            goal_minutes.append(int(minute))
        except (TypeError, ValueError):
            pass

    goal_minutes.sort()
    total = len(goal_minutes)

    if total == 0:
        return {
            "total_goals": 0,
            "first_half_goals": 0,
            "second_half_goals": 0,
            "band_1_10": 0, "band_11_20": 0, "band_21_30": 0,
            "band_31_40": 0, "band_41_45": 0, "band_46_55": 0,
            "band_56_65": 0, "band_66_75": 0, "band_76_85": 0, "band_86_90": 0,
            "first_goal_minute": None,
            "avg_interval_minutes": None,
            "goal_minutes": [],
        }

    first_half = sum(1 for m in goal_minutes if m <= 45)
    second_half = sum(1 for m in goal_minutes if m > 45)

    intervals: list[float] = [
        float(goal_minutes[i] - goal_minutes[i - 1])
        for i in range(1, len(goal_minutes))
    ]
    avg_interval = round(sum(intervals) / len(intervals), 2) if intervals else None

    return {
        "total_goals": total,
        "first_half_goals": first_half,
        "second_half_goals": second_half,
        "band_1_10":  _band(goal_minutes, 1,  10),
        "band_11_20": _band(goal_minutes, 11, 20),
        "band_21_30": _band(goal_minutes, 21, 30),
        "band_31_40": _band(goal_minutes, 31, 40),
        "band_41_45": _band(goal_minutes, 41, 45),
        "band_46_55": _band(goal_minutes, 46, 55),
        "band_56_65": _band(goal_minutes, 56, 65),
        "band_66_75": _band(goal_minutes, 66, 75),
        "band_76_85": _band(goal_minutes, 76, 85),
        "band_86_90": _band(goal_minutes, 86, 90),
        "first_goal_minute": goal_minutes[0],
        "avg_interval_minutes": avg_interval,
        "goal_minutes": goal_minutes,
    }


def extract_goal_timing_from_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Convenience wrapper: pull incidents from a sofascore detail dict and parse."""
    incidents = detail.get("incidents") or detail.get("match_incidents") or []
    return extract_goal_timing_from_incidents(incidents)


def extract_goal_timing_from_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Convenience wrapper: pull incidents from a full match doc's sofascore_detail."""
    detail = doc.get("sofascore_detail") if isinstance(doc.get("sofascore_detail"), dict) else {}
    return extract_goal_timing_from_detail(detail)
