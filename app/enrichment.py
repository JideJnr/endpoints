from __future__ import annotations

import json
from datetime import date as dt, datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from app.league_memory import store_enriched_matches
from app.market import snapshot_odds
from app.normalise import normalise
from app.sofascore_client import fetch_all_scheduled_events, fetch_event_detail
from app.sportybet_client import fetch_live_and_upcoming_matches_post
from app.web_context import search_match_context


FUZZY_THRESHOLD = 0.75
# Raised from migrated version (was 75 int, now 0.75 float — same threshold)
# LLM fallback kicks in when fuzzy score is below this
LLM_FALLBACK_THRESHOLD = 0.60

JUNK_MARKERS = (
    " srl", " u21", " u20", " u19", " u18", " u23",
    "reserves", " ii ", " b ", "women", "wfc", "ladies",
    "esports", "simulated", "virtual",
)


def run_enrichment(match_date: str | None = None, force: bool = False, limit: int = 300) -> dict[str, Any]:
    target_date = match_date or dt.today().isoformat()
    sporty_matches = fetch_live_and_upcoming_matches_post()[:limit]
    sofa_events = fetch_all_scheduled_events(target_date)

    documents = []
    matched = unmatched = llm_used = 0

    for sporty in sporty_matches:
        sofa, score = _fuzzy_match(sporty, sofa_events)

        if score < FUZZY_THRESHOLD:
            # try LLM fallback for borderline matches (not junk)
            if score >= LLM_FALLBACK_THRESHOLD and not _is_junk(sporty.get("name") or ""):
                llm_sofa = _llm_match(sporty, sofa_events)
                if llm_sofa:
                    sofa = llm_sofa
                    llm_used += 1
                    matched += 1
                else:
                    sofa = None
                    unmatched += 1
            else:
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

        web_context = search_match_context(
            sporty.get("home_team") or "",
            sporty.get("away_team") or "",
            sporty.get("tournament") or "",
        )

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
            "web_context": web_context,
            "match_score": round(score, 3),
            "llm_matched": llm_used > 0 and sofa is not None,
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
        "llm_fallback": llm_used,
        "unmatched": unmatched,
        "stored": stored,
    }


# ── LLM fallback matching ─────────────────────────────────────────────────────
# Ported from migrated predictz/enrichment.py
# Only fires when fuzzy score is between LLM_FALLBACK_THRESHOLD and FUZZY_THRESHOLD
# Requires GROQ_API_KEY — silently skips if unavailable

_llm_instance = None


def _get_llm_instance():
    global _llm_instance
    if _llm_instance is None:
        from app.llm import get_fast_llm
        _llm_instance = get_fast_llm()
    return _llm_instance


def _llm_match(sporty: dict[str, Any], sofa_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Ask the LLM to pick the correct SofaScore event when fuzzy matching is borderline."""
    try:
        from langchain_core.messages import HumanMessage
        llm = _get_llm_instance()
        candidates = [{"id": e["id"], "name": e.get("name")} for e in sofa_events[:50]]
        prompt = (
            f"Match this SportyBet fixture to the correct SofaScore event.\n"
            f"SportyBet: {sporty.get('name')} | tournament: {sporty.get('tournament', '')}\n\n"
            f"SofaScore candidates:\n{json.dumps(candidates, indent=2)}\n\n"
            f"Reply with ONLY the matching SofaScore event id as a plain integer, or 0 if no match."
        )
        resp = llm.invoke([HumanMessage(content=prompt)])
        event_id = int(resp.content.strip())
        if event_id == 0:
            return None
        return next((e for e in sofa_events if e["id"] == event_id), None)
    except Exception as exc:
        print(f"[enrichment] LLM match skipped ({type(exc).__name__}): {sporty.get('name')}")
        return None


# ── Fuzzy matching ────────────────────────────────────────────────────────────

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
