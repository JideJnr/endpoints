"""
Portfolio Correlation Filter
-----------------------------
Prevents concentration risk by detecting and capping correlated picks.

A group of picks is "correlated" when they share:
  - Same market direction (e.g. 5× Home Win)
  - Same league / tournament
  - Same kick-off window (within 2 hours of each other)

Any two of the three is enough to flag correlation.

Rules:
  MAX_PER_DIRECTION  — max picks with same selection (Home/Away/Draw/Over/Under)
  MAX_PER_LEAGUE     — max picks from the same tournament
  MAX_PER_WINDOW     — max picks kicking off within the same 2-hour window
  MAX_PORTFOLIO_SIZE — hard cap on total picks returned

Within each correlated group, the highest-confidence picks survive.
Dropped picks are flagged with correlated=True so the frontend can show them
as dimmed/filtered rather than hiding them entirely.

Usage:
    from app.utils.portfolio import filter_correlated

    filtered = filter_correlated(predictions)
    # Each prediction now has:
    #   correlated: bool
    #   correlation_reason: str | None
    #   portfolio_rank: int
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.utils.match_helpers import _normalise_selection

# ── Limits ────────────────────────────────────────────────────────────────────

MAX_PER_DIRECTION  = 4   # max picks with same selection direction (Home/Away/Draw/Over/Under)
MAX_PER_LEAGUE     = 5   # max picks from the same tournament
MAX_PER_WINDOW     = 4   # max picks in the same 2-hour kick-off window
MAX_PORTFOLIO_SIZE = 20  # hard cap on total picks in the portfolio

WINDOW_SECONDS = 2 * 3600  # 2-hour windows


# ── Main filter ───────────────────────────────────────────────────────────────

def filter_correlated(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Apply correlation filters to a list of prediction dicts.
    Each dict must have: match_id, league_name/tournament, best_pick, start_time (optional).

    Returns the same list with added fields:
      correlated: bool
      correlation_reason: str | None  — why it was flagged
      portfolio_rank: int             — rank within its correlated group (1 = best)
    """
    if not predictions:
        return predictions

    # Annotate each prediction with derived fields
    annotated = [_annotate(p) for p in predictions]

    # Sort by confidence descending — highest confidence picks survive within groups
    annotated.sort(key=lambda p: p["_conf"], reverse=True)

    # Track how many picks we've accepted per group
    direction_counts: dict[str, int] = defaultdict(int)
    league_counts:    dict[str, int] = defaultdict(int)
    window_counts:    dict[int, int]  = defaultdict(int)
    total_accepted = 0

    result = []
    for p in annotated:
        direction = p["_direction"]
        league    = p["_league"]
        window    = p["_window"]

        reasons = []

        # Check each limit
        if direction and direction_counts[direction] >= MAX_PER_DIRECTION:
            reasons.append(f"max {MAX_PER_DIRECTION} {direction} picks reached")
        if league and league_counts[league] >= MAX_PER_LEAGUE:
            reasons.append(f"max {MAX_PER_LEAGUE} picks from {league}")
        if window is not None and window_counts[window] >= MAX_PER_WINDOW:
            reasons.append(f"max {MAX_PER_WINDOW} picks in this kick-off window")
        if total_accepted >= MAX_PORTFOLIO_SIZE:
            reasons.append(f"portfolio cap of {MAX_PORTFOLIO_SIZE} reached")

        correlated = bool(reasons)

        # Update counts only for accepted picks
        if not correlated:
            if direction:
                direction_counts[direction] += 1
            if league:
                league_counts[league] += 1
            if window is not None:
                window_counts[window] += 1
            total_accepted += 1

        # Clean up internal fields, add public fields
        out = {k: v for k, v in p.items() if not k.startswith("_")}
        out["correlated"]         = correlated
        out["correlation_reason"] = "; ".join(reasons) if reasons else None
        result.append(out)

    # Add portfolio_rank within each direction group
    direction_rank: dict[str, int] = defaultdict(int)
    for p in result:
        d = p.get("_direction_raw") or "other"
        direction_rank[d] += 1
        p["portfolio_rank"] = direction_rank[d]
        p.pop("_direction_raw", None)

    return result


# ── Portfolio summary ─────────────────────────────────────────────────────────

def portfolio_summary(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Return a summary of the portfolio composition.
    Call after filter_correlated().
    """
    accepted  = [p for p in predictions if not p.get("correlated")]
    filtered  = [p for p in predictions if p.get("correlated")]

    direction_dist: dict[str, int] = defaultdict(int)
    league_dist:    dict[str, int] = defaultdict(int)

    for p in accepted:
        pick = (p.get("best_pick") or {})
        sel  = _normalise_selection(pick.get("selection") or "")
        direction_dist[sel] += 1
        league = _normalise_league(p.get("league_name") or p.get("tournament") or "")
        league_dist[league] += 1

    return {
        "total":          len(predictions),
        "accepted":       len(accepted),
        "filtered_out":   len(filtered),
        "by_direction":   dict(direction_dist),
        "by_league":      dict(league_dist),
        "max_per_direction": MAX_PER_DIRECTION,
        "max_per_league":    MAX_PER_LEAGUE,
        "max_per_window":    MAX_PER_WINDOW,
        "max_portfolio_size": MAX_PORTFOLIO_SIZE,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _annotate(p: dict[str, Any]) -> dict[str, Any]:
    """Add internal _fields used for grouping."""
    pick = p.get("best_pick") or {}
    conf = int(pick.get("confidence") or 0)
    sel  = pick.get("selection") or ""
    direction = _normalise_selection(sel)
    league    = _normalise_league(p.get("league_name") or p.get("tournament") or "")
    start     = _start_time(p)
    window    = _time_window(start) if start else None

    return {
        **p,
        "_conf":          conf,
        "_direction":     direction,
        "_direction_raw": direction,
        "_league":        league,
        "_window":        window,
    }


