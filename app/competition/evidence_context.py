"""Shared guards for competition-aware football evidence.

Ranks and raw points only have directional meaning inside the same competition.
This module keeps deterministic signals and AI evidence from treating a team's
position in a lower division as directly comparable with a top-flight team's.
"""
from __future__ import annotations

from typing import Any


def competition_name(event: dict[str, Any]) -> str:
    tournament = event.get("tournament") or event.get("uniqueTournament") or {}
    return str(tournament.get("name") or "") if isinstance(tournament, dict) else str(tournament or "")


def history_competition_name(event: dict[str, Any]) -> str:
    return competition_name(event)


def competition_comparability(target: dict[str, Any], home_history: list[dict] | None, away_history: list[dict] | None) -> dict[str, Any]:
    """Describe whether both teams' recent form is comparable to this fixture.

    A same-competition sample is safe for raw standings/form. Cross-league
    samples retain only a small strength-adjusted role and never create a raw
    table-position edge.
    """
    target_name = competition_name(target)
    target_key = _key(target_name)
    home_leagues = _recent_leagues(home_history)
    away_leagues = _recent_leagues(away_history)
    home_same = sum(1 for name in home_leagues if _key(name) == target_key)
    away_same = sum(1 for name in away_leagues if _key(name) == target_key)
    return {
        "target_competition": target_name,
        "home_same_competition_matches": home_same,
        "away_same_competition_matches": away_same,
        "same_competition_form": bool(target_key and home_same >= 2 and away_same >= 2),
        "home_recent_competitions": home_leagues[:5],
        "away_recent_competitions": away_leagues[:5],
    }


def _recent_leagues(history: list[dict] | None) -> list[str]:
    names: list[str] = []
    for event in history or []:
        if (event.get("status") or {}).get("type") not in {"finished", "ended"}:
            continue
        name = history_competition_name(event)
        if name:
            names.append(name)
    return names


def _key(value: str) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())
