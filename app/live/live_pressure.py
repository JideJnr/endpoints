"""
First-half vs second-half "who was pressing" read, built on top of the
timestamped history in app/live/live_stat_history.py.

WHAT "PRESSING" MEANS HERE
---------------------------
dangerous_attacks + total_shots, summed for a half, per side. This was
chosen over raw possession (a team can dominate the ball without creating
anything) and over live xG (also tracked separately — see live_xg.py — but
xG needs a real chance to register, so it lags behind pressure that hasn't
yet produced a clear opening; dangerous attacks + shots is the earliest
usable signal that a team is currently camped in the opponent's third).

HOW HALVES ARE SPLIT
---------------------
SofaScore's statistics response is checked for its own explicit period
breakdown (see the `periods` list normalize_live_statistics builds in
app/match_facts.py) but as of this being written every match observed in
production only ever returns a single cumulative "ALL" period — no separate
"1ST"/"2ND" groups. So this module does NOT have real per-half source data
to read directly; it derives halves itself by diffing our own timestamped
snapshots:
  - "first half" = the last recorded snapshot at or before minute 45
    (i.e. the cumulative total AT half-time)
  - "second half" = latest snapshot minus that first-half snapshot

If the worker only started watching a match after half-time (no snapshot
with minute <= 45 exists), there is nothing to diff against, so only a
"first_half" key is returned, holding the cumulative total observed so far
— it is not truly first-half-only in that case. Callers should check
`whole_match_only` in the result before treating `first_half` as literal
first-half data.

If SofaScore ever starts returning genuine per-half groups for some
provider/league, `provider_period` on each stored row (see
live_stat_history.py) already carries that tag — a future revision can
prefer reading it directly instead of diffing once real examples exist to
verify the shape against.

THE THRESHOLDS BELOW ARE A FIRST PASS, NOT CALIBRATED
--------------------------------------------------------
PRESSING_MIN_MARGIN / PRESSING_MIN_RATIO are reasonable starting guesses,
not fitted against graded outcomes (there is no "pressing" ground truth to
fit against yet — this data has not existed until now). Once matches have
accumulated with this recorded, revisit these constants against how well
"team was pressing in half X" actually correlated with who scored / won.
"""
from __future__ import annotations

from typing import Any

from app.live.live_stat_history import get_stat_history

PRESSING_MIN_MARGIN = 3.0   # absolute gap required before calling either side "pressing"
PRESSING_MIN_RATIO = 1.3    # leading side must have >=30% more than the trailing side


def get_half_pressure(match_id: str) -> dict[str, Any] | None:
    """
    Return a first_half / second_half pressure read for a match, or None if
    there is no recorded stat history for it yet.
    """
    rows = get_stat_history(match_id)
    if not rows:
        return None

    first_half_row = None
    for row in rows:
        minute = row.get("minute")
        if minute is not None and minute <= 45:
            first_half_row = row

    whole_match_only = first_half_row is None
    if first_half_row is None:
        first_half_row = rows[0]

    latest_row = rows[-1]

    result: dict[str, Any] = {
        "match_id": match_id,
        "whole_match_only": whole_match_only,
        "first_half": _verdict_from_totals(first_half_row),
    }

    if not whole_match_only and latest_row.get("id") != first_half_row.get("id"):
        latest_minute = latest_row.get("minute")
        if latest_minute is not None and latest_minute > 45:
            result["second_half"] = _verdict_from_delta(first_half_row, latest_row)

    return result


def _delta(start: dict[str, Any], end: dict[str, Any], key: str) -> float | None:
    s = start.get(key)
    e = end.get(key)
    if s is None or e is None:
        return None
    # Clamp at 0: a provider correction/reset should never show as negative
    # pressure for the half rather than "no pressure recorded".
    return max(0.0, float(e) - float(s))


def _verdict_from_totals(row: dict[str, Any]) -> dict[str, Any]:
    home = (row.get("home_dangerous_attacks") or 0) + (row.get("home_total_shots") or 0)
    away = (row.get("away_dangerous_attacks") or 0) + (row.get("away_total_shots") or 0)
    return _pressure_verdict(home, away)


def _verdict_from_delta(start: dict[str, Any], end: dict[str, Any]) -> dict[str, Any]:
    home = (_delta(start, end, "home_dangerous_attacks") or 0) + (_delta(start, end, "home_total_shots") or 0)
    away = (_delta(start, end, "away_dangerous_attacks") or 0) + (_delta(start, end, "away_total_shots") or 0)
    return _pressure_verdict(home, away)


def _pressure_verdict(home_score: float, away_score: float) -> dict[str, Any]:
    pressing_team = None
    higher, lower, side = (home_score, away_score, "home") if home_score >= away_score else (away_score, home_score, "away")
    if higher - lower >= PRESSING_MIN_MARGIN and (lower == 0 or higher / lower >= PRESSING_MIN_RATIO):
        pressing_team = side
    return {
        "home_pressure_score": round(home_score, 1),
        "away_pressure_score": round(away_score, 1),
        "pressing_team": pressing_team,
    }
