from __future__ import annotations

from datetime import date as dt, datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from app.league_memory import store_enriched_matches
from app.market import snapshot_odds
from app.normalise import normalise
from app.sofascore_client import fetch_all_scheduled_events, fetch_event_detail
from app.sportybet_client import fetch_live_and_upcoming_matches_post


FUZZY_THRESHOLD = 0.75
JUNK_MARKERS = (" srl", " u21", " u20", " u19", " u18", " u23", "reserves", "women", "esports", "virtual")


def run_enrichment(match_date: str | None = None, force: bool = False, limit: int = 300) -> dict[str, Any]:
    target_date = match_date or dt.today().isoformat()
    sporty_matches = fetch_live_and_upcoming_matches_post()[:limit]
    sofa_events = fetch_all_scheduled_events(target_date)

    documents = []
    matched = unmatched = 0
    for sporty in sporty_matches:
        sofa, score = _fuzzy_match(sporty, sofa_events)
        if score < FUZZY_THRESHOLD:
            sofa = None
            unmatched += 1
        else:
            matched += 1

        detail = None
        if sofa:
            try:
                detail = fetch_event_detail(sofa)
            except Exception:
                detail = None

        doc = {
            "sportybet_id": sporty.get("id"),
            "sportybet_name": sporty.get("name"),
            "match_date": target_date,
            "tournament": sporty.get("tournament"),
            "category": sporty.get("category"),
            "start_time": sporty.get("start_time"),
            "period": sporty.get("period"),
            "score": sporty.get("score"),
            "venue": sporty.get("venue"),
            "sportybet_markets": sporty.get("markets", []),
            "sofascore_id": sofa.get("id") if sofa else None,
            "sofascore_name": sofa.get("name") if sofa else None,
            "sofascore_detail": detail,
            "web_context": {"query": f"{sporty.get('name')} prediction preview {sporty.get('tournament')}", "snippets": [], "scraped": []},
            "match_score": round(score, 3),
            "enriched_at": datetime.now(timezone.utc).isoformat(),
        }
        documents.append(doc)
        snapshot_odds(doc)

    stored = store_enriched_matches(documents)
    return {
        "status": "success",
        "date": target_date,
        "sporty_count": len(sporty_matches),
        "sofa_count": len(sofa_events),
        "matched": matched,
        "unmatched": unmatched,
        "stored": stored,
    }


def _fuzzy_match(sporty: dict[str, Any], sofa_events: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    name = sporty.get("name") or ""
    if _is_junk(name):
        return None, 0.0
    best = None
    best_score = 0.0
    for event in sofa_events:
        score = _name_score(name, event.get("name") or "")
        if score > best_score:
            best = event
            best_score = score
    return best, best_score


def _name_score(sporty_name: str, sofa_name: str) -> float:
    source = normalise(sporty_name)
    target = normalise(sofa_name)
    parts = target.split(" vs ")
    flipped = f"{parts[1]} vs {parts[0]}" if len(parts) == 2 else target
    return max(
        SequenceMatcher(None, source, target).ratio(),
        SequenceMatcher(None, source, flipped).ratio(),
        _token_score(source, target),
        _token_score(source, flipped),
    )


def _token_score(a: str, b: str) -> float:
    left = set(a.split())
    right = set(b.split())
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _is_junk(name: str) -> bool:
    text = f" {normalise(name)} "
    return any(marker in text for marker in JUNK_MARKERS)
