from __future__ import annotations

from typing import Any


def _fetch_web(idx: int, sporty: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        from app.enrichment.web_context import search_match_context

        home = sporty.get("home_team")
        away = sporty.get("away_team")
        if isinstance(home, dict):
            home = home.get("name")
        if isinstance(away, dict):
            away = away.get("name")
        if not home or not away:
            parts = [part.strip() for part in str(sporty.get("name") or "").split(" vs ", 1)]
            if len(parts) == 2:
                home, away = parts
        return idx, search_match_context(str(home or ""), str(away or ""), str(sporty.get("tournament") or ""))
    except Exception:
        return idx, {}
