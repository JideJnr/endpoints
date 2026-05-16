from __future__ import annotations

from typing import Any

from app.config import get_settings


def search_match_context(home: str, away: str, tournament: str = "") -> dict[str, Any]:
    """Search DuckDuckGo for match preview context and scrape the top result pages."""
    query = f"{home} vs {away} prediction preview {tournament}".strip()
    settings = get_settings()

    if not settings.web_search_enabled:
        return {"query": query, "snippets": [], "scraped": [], "disabled": True}

    try:
        results = _search(query, settings.web_search_max_results)
    except Exception as exc:
        return {"query": query, "snippets": [], "scraped": [], "error": str(exc)}

    snippets = []
    urls_to_scrape = []

    for result in results:
        url = result.get("href", "")
        snippets.append({
            "title": _ascii(result.get("title", "")),
            "snippet": _ascii(result.get("body", "")),
            "url": url,
        })
        if url:
            urls_to_scrape.append(url)

    # scrape pages in parallel with a hard wall-clock timeout
    scraped = _scrape_parallel(urls_to_scrape, settings.web_scrape_timeout_seconds)

    return {"query": query, "snippets": snippets, "scraped": scraped}


def _search(query: str, max_results: int) -> list[dict[str, Any]]:
    from ddgs import DDGS

    settings = get_settings()
    last_error: Exception | None = None

    for backend in settings.web_search_backends:
        try:
            with DDGS(timeout=settings.web_search_timeout_seconds) as ddgs:
                results = list(ddgs.text(query, max_results=max_results, backend=backend))
            if results:
                return results
        except Exception as exc:
            last_error = exc

    if last_error:
        raise last_error
    return []


def _scrape_parallel(urls: list[str], timeout_per_url: int) -> list[str]:
    """Scrape multiple URLs in parallel. Each URL gets its own timeout."""
    if not urls:
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: list[str] = []

    def _scrape_one(url: str) -> str:
        return _scrape(url, timeout_per_url)

    # cap total wall time: max_workers × timeout_per_url would be too long
    # run all in parallel so total time ≈ timeout_per_url (not × n)
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        futures = {pool.submit(_scrape_one, url): url for url in urls}
        for future in as_completed(futures, timeout=timeout_per_url + 2):
            try:
                text = future.result()
                if text:
                    results.append(text)
            except Exception:
                pass

    return results


def context_for_match(match: dict[str, Any]) -> dict[str, Any]:
    home, away = _teams_from_match(match)
    tournament = _tournament_from_match(match)
    if not home or not away:
        return {"query": "", "snippets": [], "scraped": [], "error": "missing teams"}
    return search_match_context(home, away, tournament)


def _scrape(url: str, timeout: int | None = None) -> str:
    if not url:
        return ""
    settings = get_settings()
    effective_timeout = timeout or settings.web_scrape_timeout_seconds
    try:
        import trafilatura
        import requests

        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 PredictX/1.0"},
            timeout=effective_timeout,
        )
        response.raise_for_status()
        text = trafilatura.extract(response.text, include_comments=False, include_tables=False)
        if text:
            return _ascii(text)[: settings.web_scrape_max_chars]
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
