from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as dt, datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib import error, request

from app.league_memory import store_enriched_matches
from app.market import snapshot_odds
from app.normalise import normalise
from app.sofascore_client import fetch_all_scheduled_events, fetch_event_detail
from app.sportybet_client import fetch_live_and_upcoming_matches_post
from app.web_context import search_match_context


FUZZY_THRESHOLD = 0.75
LLM_FALLBACK_THRESHOLD = 0.60

# How many SofaScore detail + web-search calls to run in parallel.
# Keep low enough not to get rate-limited by SofaScore.
DETAIL_WORKERS = 2
WEB_WORKERS = 4

JUNK_MARKERS = (
    " srl", " u21", " u20", " u19", " u18", " u23",
    "reserves", " ii ", " b ", "women", "wfc", "ladies",
    "esports", "simulated", "virtual",
)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_enrichment(match_date: str | None = None, force: bool = False, limit: int = 300) -> dict[str, Any]:
    target_date = match_date or dt.today().isoformat()

    sporty_matches = fetch_live_and_upcoming_matches_post()[:limit]
    sofa_events = fetch_all_scheduled_events(target_date)

    # ── Step 1: fuzzy + HF match all sporty → sofa (fast, CPU-bound) ──────────
    matched_pairs: list[tuple[dict, dict | None, float]] = []
    matched = unmatched = llm_used = 0

    for sporty in sporty_matches:
        sofa, score = _fuzzy_match(sporty, sofa_events)

        if score < FUZZY_THRESHOLD:
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

        matched_pairs.append((sporty, sofa, score))

    print(f"[enrichment] matched={matched} llm={llm_used} unmatched={unmatched} / {len(sporty_matches)} total")

    # ── Step 2: fetch SofaScore detail in parallel ────────────────────────────
    # Only for matches that have a sofa event — skip the rest
    needs_detail = [(i, sofa) for i, (_, sofa, _) in enumerate(matched_pairs) if sofa]
    details: dict[int, dict | None] = {i: None for i in range(len(matched_pairs))}

    def _fetch_detail(idx: int, sofa: dict) -> tuple[int, dict | None]:
        try:
            return idx, fetch_event_detail(sofa)
        except Exception:
            return idx, None

    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = {pool.submit(_fetch_detail, i, sofa): i for i, sofa in needs_detail}
        for future in as_completed(futures):
            idx, detail = future.result()
            details[idx] = detail

    print(f"[enrichment] sofa detail fetched: {sum(1 for d in details.values() if d)}/{len(needs_detail)}")

    # ── Step 3: web context in parallel — only for sofa-matched matches ───────
    # Web search is optional and slow — skip it for unmatched matches entirely
    web_contexts: dict[int, dict] = {}

    def _fetch_web(idx: int, sporty: dict) -> tuple[int, dict]:
        try:
            ctx = search_match_context(
                sporty.get("home_team") or "",
                sporty.get("away_team") or "",
                sporty.get("tournament") or "",
            )
            return idx, ctx
        except Exception:
            return idx, {"query": "", "snippets": [], "scraped": []}

    # Only run web search for matched matches (has sofa detail = worth enriching)
    needs_web = [(i, sporty) for i, (sporty, sofa, _) in enumerate(matched_pairs) if sofa]

    with ThreadPoolExecutor(max_workers=WEB_WORKERS) as pool:
        futures = {pool.submit(_fetch_web, i, sporty): i for i, sporty in needs_web}
        for future in as_completed(futures):
            idx, ctx = future.result()
            web_contexts[idx] = ctx

    print(f"[enrichment] web context fetched: {len(web_contexts)}/{len(needs_web)}")

    # ── Step 4: assemble documents + snapshot odds ────────────────────────────
    now = datetime.now(timezone.utc).isoformat()
    documents = []

    for i, (sporty, sofa, score) in enumerate(matched_pairs):
        detail = details.get(i)
        web_context = web_contexts.get(i, {"query": "", "snippets": [], "scraped": []})

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
            "enriched_at": now,
        }
        documents.append(doc)
        snapshot_odds(doc)

    stored = store_enriched_matches(documents)
    print(f"[enrichment] stored={stored} for {target_date}")

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


# ── HuggingFace LLM fallback matching ────────────────────────────────────────

def _hf_token() -> str | None:
    return (
        os.getenv("HF_TOKEN")
        or os.getenv("HUGGINGFACE_HUB_TOKEN")
        or os.getenv("HUGGING_FACE_HUB_TOKEN")
    )


def _llm_match(sporty: dict[str, Any], sofa_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    token = _hf_token()
    if not token:
        return None

    from app.config import get_settings
    settings = get_settings()

    candidates = [{"id": e["id"], "name": e.get("name", "")} for e in sofa_events[:60]]
    prompt = (
        f"Match this SportyBet fixture to the correct SofaScore event.\n"
        f"SportyBet: \"{sporty.get('name')}\" | tournament: \"{sporty.get('tournament', '')}\"\n\n"
        f"SofaScore candidates (id + name):\n"
        f"{json.dumps(candidates, indent=2)}\n\n"
        f"Rules:\n"
        f"- Team names may differ slightly (abbreviations, accents, suffixes like FC/CF)\n"
        f"- Match by team names only, ignore tournament name differences\n"
        f"- Reply with ONLY the matching SofaScore event id as a plain integer\n"
        f"- Reply with 0 if you are not confident\n"
        f"- No explanation, no text, just the integer"
    )

    body = {
        "model": settings.hf_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a football match deduplication assistant. "
                    "Reply with a single integer — the SofaScore event id — or 0 if unsure."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 10,
    }

    try:
        req = request.Request(
            settings.hf_url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=settings.ai_timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        choices = data.get("choices") or []
        content = (((choices[0] if choices else {}).get("message") or {}).get("content") or "").strip()
        digits = "".join(ch for ch in content if ch.isdigit() or ch == "-").strip()
        event_id = int(digits) if digits else 0

        if event_id <= 0:
            return None

        result = next((e for e in sofa_events if e["id"] == event_id), None)
        if result:
            print(f"[enrichment] HF matched: \"{sporty.get('name')}\" → \"{result.get('name')}\"")
        return result

    except (OSError, TimeoutError, ValueError, error.URLError) as exc:
        print(f"[enrichment] HF match failed ({type(exc).__name__}): {sporty.get('name')}")
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
