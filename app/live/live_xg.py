"""
In-match (live) expected-goals tracking.

Two sources, in priority order:
  1. SofaScore's own xG figure, when the provider actually returns one for
     this match. It is already captured over time in live_stat_snapshots
     (home_xg / away_xg columns — xg is just another entry in the same
     STAT_KEYS list every other live stat uses, see live_stat_history.py),
     so no extra plumbing was needed to start tracking it — it just needed
     reading back out.
  2. A shots-based estimate when SofaScore has no real xG for this match or
     league (common for lower-tier leagues — see the coverage audit notes
     in app/storage/buffer.py). Reuses the exact proxy formula already
     written for the live-projection prototype
     (app/live/live_projection.py::_estimate_live_xg — ~0.30 xG per shot on
     target, ~0.05 per other shot) instead of inventing a second, different
     formula for the same concept.

Either way the result says which source it came from (`source`). A
provider-real number and a rough shots-based proxy should not be trusted
equally by anything reading this.
"""
from __future__ import annotations

from typing import Any

from app.live.live_stat_history import get_stat_history
from app.live.live_projection import _estimate_live_xg


def get_live_xg(match_id: str) -> dict[str, Any] | None:
    """
    Return the current in-match xG for both sides, plus how it built up
    over time ("so far, at minute N"), read from the same timestamped stat
    history the half-pressure feature uses (live_pressure.py).

    Returns None if there is no recorded stat history for this match yet.
    """
    rows = get_stat_history(match_id)
    if not rows:
        return None

    has_real_xg = any(row.get("home_xg") is not None or row.get("away_xg") is not None for row in rows)

    series: list[dict[str, Any]] = []
    for row in rows:
        if has_real_xg:
            home_xg = row.get("home_xg")
            away_xg = row.get("away_xg")
        else:
            home_xg = _estimate_live_xg(
                int(row.get("home_shots_on_target") or 0),
                int(row.get("home_total_shots") or 0),
            )
            away_xg = _estimate_live_xg(
                int(row.get("away_shots_on_target") or 0),
                int(row.get("away_total_shots") or 0),
            )
        series.append(
            {
                "minute": row.get("minute"),
                "snapshot_time": row.get("snapshot_time"),
                "home_xg": home_xg,
                "away_xg": away_xg,
            }
        )

    latest = series[-1]
    return {
        "match_id": match_id,
        "source": "provider" if has_real_xg else "shots_proxy",
        "home_xg": latest.get("home_xg"),
        "away_xg": latest.get("away_xg"),
        "series": series,
    }
