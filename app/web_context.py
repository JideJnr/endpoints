from __future__ import annotations

import os
from typing import Any


MAX_RESULTS = 3
MAX_CHARS = 1500
SEARCH_TIMEOUT = 4
SCRAPE_TIMEOUT = 6
SEARCH_BACKENDS = os.getenv("PREDICTX_SEARCH_BACKENDS", "duckduckgo")


def search_match_context(home: str, away: str, tournament: str = "") -> dict[str, Any]:
    """Search DuckDuckGo for match preview context and scrape the top result pages."""
    query = f"{home} vs {away} prediction preview {tournament}".strip()
    snippets: list[dict[str, str]] = []
    scraped: list[str] = []

    try:
        results = _search(query)

        for result in results:
            url = result.get("href", "")
            snippets.append({
                "title": _ascii(result.get("title", "")),
                "snippet": _ascii(result.get("body", "")),
                "url": url,
            })
            text = _scrape(url)
            if text:
                scraped.append(text)
    except Exception as exc:
        return {"query": query, "snippets": [], "scraped": [], "error": str(exc)}

    return {"query": query, "snippets": snippets, "scraped": scraped}


def _search(query: str) -> list[dict[str, Any]]:
    from ddgs import DDGS

    last_error: Exception | None = None
    for backend in [item.strip() for item in SEARCH_BACKENDS.split(",") if item.strip()]:
        try:
            with DDGS(timeout=SEARCH_TIMEOUT) as ddgs:
                results = list(ddgs.text(query, max_results=MAX_RESULTS, backend=backend))
            if results:
                return results
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return []


def context_for_match(match: dict[str, Any]) -> dict[str, Any]:
    home, away = _teams_from_match(match)
    tournament = _tournament_from_match(match)
    if not home or not away:
        return {"query": "", "snippets": [], "scraped": [], "error": "missing teams"}
    return search_match_context(home, away, tournament)


def _scrape(url: str) -> str:
    if not url:
        return ""
    try:
        import trafilatura
        import requests

        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 PredictX/1.0"},
            timeout=SCRAPE_TIMEOUT,
        )
        response.raise_for_status()
        text = trafilatura.extract(response.text, include_comments=False, include_tables=False)
        if text:
            return _ascii(text)[:MAX_CHARS]
    except Exception:
        pass
    return ""


def _teams_from_match(match: dict[str, Any]) -> tuple[str, str]:
    home = match.get("home_team")
    away = match.get("away_team")
    if isinstance(home, dict):
        home = home.get("name")
    if isinstance(away, dict):
        away = away.get("name")
    if home and away:
        return str(home), str(away)

    name = str(match.get("name") or "")
    if " vs " in name:
        left, right = name.split(" vs ", 1)
        return left.strip(), right.strip()
    return "", ""


def _tournament_from_match(match: dict[str, Any]) -> str:
    tournament = match.get("tournament")
    if isinstance(tournament, dict):
        return str(tournament.get("name") or "")
    return str(tournament or "")


def _ascii(value: Any) -> str:
    return str(value or "").encode("ascii", errors="ignore").decode("ascii")
