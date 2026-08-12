"""
match_helpers.py
~~~~~~~~~~~~~~~~
Consolidated match-data helper functions.

Single source of truth for:
  - _team_name, _tournament_name
  - _normalise_selection
  - _fraction_to_probability
  - _norm, _played_seconds, _to_datetime_utc

All modules should import from here instead of defining local copies.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


# ── Team / tournament name extraction ────────────────────────────────────────


def _team_name(match: dict[str, Any], side: str) -> str | None:
    """Extract a team name from *match* for *side* (``"home"`` / ``"away"``).

    Handles both ``{home_team: "Arsenal"}`` and
    ``{home_team: {"name": "Arsenal", "id": 1}}`` shapes.
    """
    team = match.get(f"{side}_team")
    if isinstance(team, dict):
        return team.get("name")
    return team


def _tournament_name(doc: dict[str, Any]) -> str | None:
    """Safely extract tournament name whether it's a string or a dict."""
    t = doc.get("tournament")
    if isinstance(t, dict):
        return t.get("name")
    return t


# ── Selection normalisation ───────────────────────────────────────────────────


def _normalise_selection(selection: str, match_name: str = "", pick_type: str = "") -> str:
    """Map selection names to a canonical direction string.

    Handles live-match-winner, double-chance, goals, BTTS, and plain
    home/away/draw selections.
    """
    text = " ".join(str(selection or "").lower().replace("-", " ").split())
    pick_type = str(pick_type or "").lower()

    if pick_type == "live_match_winner" or "live winner" in text:
        side = _side_from_team_selection(selection, match_name)
        suffix = "_lean" if "lean" in text else ""
        if side:
            return f"{side}_live_winner{suffix}"
        if "draw protection" in text:
            return "live_draw_protection"
        return f"live_winner{suffix}"

    if pick_type == "live_team_to_score" or "next team to score" in text:
        side = _side_from_team_selection(selection, match_name)
        if side:
            return f"{side}_next_team_to_score"
        return "next_team_to_score"

    if "or draw protection" in text or "double chance" in text:
        side = _side_from_team_selection(selection, match_name)
        if side == "home":
            return "home_or_draw"
        if side == "away":
            return "away_or_draw"
        return "team_or_draw"

    if "home" in text:
        return "home_win"
    if "away" in text:
        return "away_win"
    if "draw" in text:
        return "draw"
    if "over" in text:
        return "over"
    if "under" in text:
        return "under"
    if "btts" in text or "both teams" in text:
        return "btts"
    if "value" in text:
        return "value"
    return pick_type or "other"


def _side_from_team_selection(selection: str, match_name: str) -> str:
    """Detect whether a selection refers to the home or away side."""
    text = _norm(selection)
    home, away = _match_sides(match_name)
    if home and home in text:
        return "home"
    if away and away in text:
        return "away"
    return ""


def _match_sides(match_name: str) -> tuple[str, str]:
    """Split a match name like ``"Arsenal vs Chelsea"`` into normalised tokens."""
    raw = str(match_name or "")
    if " vs " in raw:
        home, away = raw.split(" vs ", 1)
    elif " v " in raw:
        home, away = raw.split(" v ", 1)
    else:
        return "", ""
    return _norm(home), _norm(away)


# ── Odds / probability helpers ───────────────────────────────────────────────


def _fraction_to_probability(value: Any) -> float | None:
    """Convert fractional odds (``"5/2"``) to implied probability."""
    if not value or "/" not in str(value):
        return None
    top, bottom = str(value).split("/", 1)
    numerator = _to_float(top)
    denominator = _to_float(bottom)
    if numerator is None or denominator in (None, 0):
        return None
    decimal = numerator / denominator + 1
    return 1 / decimal


# ── String / time helpers ─────────────────────────────────────────────────────


def _norm(value: Any) -> str:
    """Lower-case, strip, and replace underscores with spaces."""
    return str(value or "").lower().strip().replace("_", " ")


def _played_seconds(value: Any) -> int:
    """Convert a played-seconds value (int, float, or ``"MM:SS"`` / ``"HH:MM:SS"``) to total seconds."""
    if value is None or value == "":
        return 0
    if isinstance(value, str) and ":" in value:
        parts = value.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0] or 0) * 60 + int(parts[1] or 0)
            if len(parts) == 3:
                return int(parts[0] or 0) * 3600 + int(parts[1] or 0) * 60 + int(parts[2] or 0)
        except ValueError:
            return 0
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _to_datetime_utc(value: Any) -> datetime | None:
    """Parse *value* into a UTC ``datetime``.

    Accepts ISO strings, timestamps (seconds or milliseconds), and
    digit-only strings.
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 1e10:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        text = str(value)
        if text.isdigit():
            return _to_datetime_utc(int(text))
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None
