from __future__ import annotations

import json
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as dt, datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib import error, request

from app.storage.league_memory import store_enriched_matches
from app.market.market import snapshot_odds
from app.utils.normalise import normalise
from app.utils.match_state import classify_match_state
from app.market.season_stage import detect_season_stage
from app.data_clients.sofascore_client import fetch_all_scheduled_events, fetch_event_detail, is_usable_event_for_mode
from app.data_clients.sportradar_client import fetch_match_intelligence
from app.data_clients.sportybet_client import fetch_live_and_upcoming_matches_post
from app.utils.time_context import match_time_context
from app.enrichment.web_context import search_league_sentiment, search_match_context
from app.storage.buffer import _data_sources
from app.config.config import _hf_token


FUZZY_THRESHOLD = 0.75
LLM_FALLBACK_THRESHOLD = 0.60

# How many SofaScore detail + web-search calls to run in parallel.
# Keep low enough not to get rate-limited by SofaScore.
DETAIL_WORKERS = 2
WEB_WORKERS = 4

JUNK_MARKERS = (
    " srl",
    "esports",
    "simulated",
    "virtual",
)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_enrichment(match_date: str | None = None, force: bool = False, limit: int = 300) -> dict[str, Any]:
    target_date = match_date or dt.today().isoformat()

    sporty_matches = fetch_live_and_upcoming_matches_post()[:limit]
    # Existing links are stable for a fixture.  Do not download the complete
    # candidate schedule again just to rediscover an already stored ID.
    from app.storage.league_memory import get_enriched_match

    existing_by_index: dict[int, dict[str, Any]] = {}
    for index, sporty in enumerate(sporty_matches):
        if force:
            continue
        existing = get_enriched_match(str(sporty.get("id") or ""))
        if existing and existing.get("sofascore_id"):
            existing_by_index[index] = existing

    needs_candidate_search = len(existing_by_index) < len(sporty_matches)
    sofa_events = []
    if needs_candidate_search:
        sofa_events = [
            event for event in fetch_all_scheduled_events(target_date)
            if is_usable_event_for_mode(event, live=False)
        ]

    # ── Step 1: fuzzy + HF match all sporty → sofa (fast, CPU-bound) ──────────
    matched_pairs: list[tuple[dict, dict | None, float]] = []
    cached_details: dict[int, dict] = {}
    matched = unmatched = llm_used = reused = 0

    for index, sporty in enumerate(sporty_matches):
        existing = existing_by_index.get(index)
        if existing:
            saved_event = existing.get("sofascore_event")
            sofa = saved_event if isinstance(saved_event, dict) else None
            if not sofa:
                try:
                    from app.data_clients.sofascore_client import fetch_event

                    sofa = fetch_event(int(existing["sofascore_id"]))
                except Exception:
                    sofa = None
            saved_detail = existing.get("sofascore_detail")
            if sofa and isinstance(saved_detail, dict) and saved_detail:
                detail_id = saved_detail.get("id") or saved_detail.get("event_id")
                if detail_id is None or str(detail_id) == str(sofa.get("id")):
                    cached_details[index] = saved_detail
            matched_pairs.append((sporty, sofa, float(existing.get("match_score") or 1.0)))
            matched += int(bool(sofa))
            unmatched += int(not sofa)
            reused += int(bool(sofa))
            continue

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
    needs_detail = [
        (i, sofa) for i, (_, sofa, _) in enumerate(matched_pairs)
        if sofa and i not in cached_details
    ]
    details: dict[int, dict | None] = {i: cached_details.get(i) for i in range(len(matched_pairs))}

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

    print(f"[enrichment] sofa detail fetched: {len(needs_detail)} requested, {reused} saved matches reused")

    # ── Step 3: web context in parallel — only for sofa-matched matches ───────
    # Web search is optional and slow — skip it for unmatched matches entirely
    web_contexts: dict[int, dict] = {}

    # Only run web search for matched matches (has sofa detail = worth enriching)
    needs_web = [(i, sporty) for i, (sporty, _, _) in enumerate(matched_pairs)]

    def _fetch_web(idx: int, sporty: dict) -> tuple[int, dict]:
        try:
            return idx, search_match_context(
                sporty.get("home_team") or "",
                sporty.get("away_team") or "",
                sporty.get("tournament") or "",
            )
        except Exception:
            return idx, {"query": "", "snippets": [], "scraped": []}

    with ThreadPoolExecutor(max_workers=WEB_WORKERS) as pool:
        futures = {pool.submit(_fetch_web, i, sporty): i for i, sporty in needs_web}
        for future in as_completed(futures):
            idx, ctx = future.result()
            web_contexts[idx] = ctx

    print(f"[enrichment] web context fetched: {len(web_contexts)}/{len(needs_web)}")

    # ── Step 3b: league sentiment in parallel ──────────────────────────
    league_sentiments: dict[int, dict] = {}

    def _fetch_league_sentiment(idx: int, sporty: dict) -> tuple[int, dict]:
        try:
            from app.config.config import get_settings

            settings = get_settings()
            if not settings.web_search_league_sentiment_enabled:
                return idx, {}
            league_name = sporty.get("tournament") or ""
            return idx, search_league_sentiment(league_name)
        except Exception:
            return idx, {}

    with ThreadPoolExecutor(max_workers=WEB_WORKERS) as pool:
        futures = {pool.submit(_fetch_league_sentiment, i, sporty): i for i, (sporty, _, _) in enumerate(matched_pairs)}
        for future in as_completed(futures):
            idx, ctx = future.result()
            league_sentiments[idx] = ctx

    print(f"[enrichment] league sentiment fetched: {len(league_sentiments)}/{len(matched_pairs)}")

    def _fetch_sportradar(idx: int, sporty: dict) -> tuple[int, dict]:
        return idx, fetch_match_intelligence(sporty.get("id"))

    sportradar_details: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = {pool.submit(_fetch_sportradar, i, sporty): i for i, (sporty, _, _) in enumerate(matched_pairs)}
        for future in as_completed(futures):
            idx, sportradar = future.result()
            sportradar_details[idx] = sportradar

    # ── Step 4: assemble documents + snapshot odds ────────────────────────────
    now = datetime.now(timezone.utc).isoformat()
    documents = []

    for i, (sporty, sofa, score) in enumerate(matched_pairs):
        detail = details.get(i)
        sportradar_detail = sportradar_details.get(i) or {}
        web_context = web_contexts.get(i, {"query": "", "snippets": [], "scraped": []})
        match_state = classify_match_state(sporty)
        time_context = match_time_context({**sporty, "sofascore_event": sofa})
        match_status = "matched" if sofa else "no_match"

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
            "sportybet_detail": _sporty_detail_doc(sporty),
            "sportybet_data_status": "available",
            "sportybet_markets": sporty.get("markets", []),
            "markets": sporty.get("markets", []),
            "data_sources": _data_sources(sofa, detail, sporty, sportradar_detail),
            "sportradar_detail": sportradar_detail,
            "sofascore_id": sofa.get("id") if sofa else None,
            "sofascore_name": sofa.get("name") if sofa else None,
            "sofascore_event": sofa,
            "sofascore_detail": detail,
            "sofascore_detail_source": "saved" if i in cached_details else "fetched" if detail else "unavailable",
            "home_last_matches": (detail or {}).get("home_last_matches") or [],
            "away_last_matches": (detail or {}).get("away_last_matches") or [],
            "standings": (detail or {}).get("standings") or [],
            "league_table": (detail or {}).get("standings") or [],
            "season_stage": detect_season_stage((detail or {}).get("standings") or []),
            "sofascore_match_status": match_status,
            "sofascore_no_match_at": None if sofa else now,
            "minimum_enrichment_status": "full_provider_match" if sofa else "sporty_only",
            "web_context": web_context,
            "league_sentiment": league_sentiments.get(i, {}),
            "match_score": round(score, 3),
            "raw_sporty": sporty,
            "raw_sofascore_event": sofa.get("raw_event") if isinstance(sofa, dict) else None,
            "time_context": time_context,
            "match_state": match_state,
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
        "sofascore_reused": reused,
        "matched": matched,
        "llm_fallback": llm_used,
        "unmatched": unmatched,
        "stored": stored,
    }


# ── HuggingFace LLM fallback matching ────────────────────────────────────────


def _sporty_detail_doc(sporty: dict[str, Any] | None) -> dict[str, Any]:
    if not sporty:
        return {}
    markets = sporty.get("markets") or []
    return {
        "source": "sportybet",
        "id": str(sporty.get("id") or ""),
        "name": sporty.get("name"),
        "home_team": sporty.get("home_team"),
        "away_team": sporty.get("away_team"),
        "tournament": sporty.get("tournament"),
        "category": sporty.get("category"),
        "start_time": sporty.get("start_time"),
        "period": sporty.get("period"),
        "played_seconds": sporty.get("played_seconds"),
        "score": sporty.get("score") or {},
        "period_scores": sporty.get("period_scores"),
        "status": sporty.get("status"),
        "venue": sporty.get("venue"),
        "markets": markets,
        "market_count": len(markets),
        "raw_event": sporty.get("raw_event"),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


def _llm_match(sporty: dict[str, Any], sofa_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    token = _hf_token()
    if not token:
        return None

    from app.config.config import get_settings
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
        score = _event_score(sporty, event)
        if score > best_score:
            best = event
            best_score = score
    return best, best_score


def _event_score(sporty: dict[str, Any], event: dict[str, Any]) -> float:
    sporty_name = sporty.get("name") or ""
    sofa_name = event.get("name") or ""
    name_score = _name_score(sporty_name, sofa_name)

    sporty_home = sporty.get("home_team") or _split_team(sporty_name, 0)
    sporty_away = sporty.get("away_team") or _split_team(sporty_name, 1)
    sofa_home = (event.get("home_team") or {}).get("name") or _split_team(sofa_name, 0)
    sofa_away = (event.get("away_team") or {}).get("name") or _split_team(sofa_name, 1)
    direct = (_team_name_score(sporty_home, sofa_home) + _team_name_score(sporty_away, sofa_away)) / 2
    flipped = (_team_name_score(sporty_home, sofa_away) + _team_name_score(sporty_away, sofa_home)) / 2
    team_score = max(direct, flipped)

    tournament_score = _token_score(
        normalise(str(sporty.get("tournament") or "")),
        normalise(str((event.get("tournament") or {}).get("name") or "")),
    )
    country_score = _country_score(sporty, event)
    score = (name_score * 0.50) + (team_score * 0.38) + (tournament_score * 0.07) + (country_score * 0.05)

    time_penalty = _time_penalty(sporty.get("start_time"), event.get("start_timestamp"))
    if time_penalty == 0 and country_score >= 0.75:
        if team_score >= 0.92:
            score = max(score, 0.82)
        elif team_score >= 0.86:
            score = max(score, 0.78)
    score = max(0.0, score - time_penalty)
    if time_penalty >= 0.25 and team_score < 0.92:
        score = min(score, 0.64)
    return round(score, 3)


def _split_team(name: str, index: int) -> str:
    parts = normalise(name).split(" vs ")
    return parts[index] if len(parts) == 2 else ""


def _country_score(sporty: dict[str, Any], event: dict[str, Any]) -> float:
    sporty_country = normalise(str(sporty.get("category") or sporty.get("country") or ""))
    raw = event.get("raw_event") or {}
    tournament = raw.get("tournament") if isinstance(raw, dict) else {}
    category = tournament.get("category") if isinstance(tournament, dict) else {}
    country = category.get("country") if isinstance(category, dict) else {}
    sofa_country = normalise(str(
        (country or {}).get("name")
        or (category or {}).get("name")
        or event.get("category")
        or ""
    ))
    if not sporty_country or not sofa_country:
        return 0.5
    return 1.0 if sporty_country == sofa_country else _token_score(sporty_country, sofa_country)


def _time_penalty(sporty_start: Any, sofa_start: Any) -> float:
    if not sporty_start or not sofa_start:
        return 0.0
    try:
        sporty_ts = float(sporty_start)
        if sporty_ts > 1e12:
            sporty_ts /= 1000
        sofa_ts = float(sofa_start)
        if sofa_ts > 1e12:
            sofa_ts /= 1000
        diff_minutes = abs(sporty_ts - sofa_ts) / 60
    except (TypeError, ValueError):
        return 0.0
    if diff_minutes <= 20:
        return 0.0
    if diff_minutes <= 90:
        return 0.04
    if diff_minutes <= 240:
        return 0.12
    if diff_minutes <= 720:
        return 0.25
    return 0.40


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


def _team_name_score(sporty_name: str, sofa_name: str) -> float:
    base = _name_score(sporty_name, sofa_name)
    source = _team_tokens(sporty_name)
    target = _team_tokens(sofa_name)
    if not source or not target:
        return base
    overlap = len(source & target)
    union = len(source | target)
    token_score = overlap / union if union else 0.0
    coverage = max(overlap / len(source), overlap / len(target))
    score = max(base, token_score)
    if overlap and coverage >= 1.0:
        score = max(score, 0.92)
    elif overlap >= 2 and coverage >= 0.67:
        score = max(score, 0.86)
    elif overlap >= 1 and min(len(source), len(target)) == 1:
        score = max(score, 0.82)
    return min(1.0, score)


def _team_tokens(name: str) -> set[str]:
    noise = {
        "club", "football", "futbol", "soccer", "team",
        "fc", "cf", "cd", "sc", "ac", "afc", "if", "bk", "ec",
        "ca", "sp", "mg", "ba", "am", "go", "ce",
    }
    connectors = {"de", "do", "da", "del", "della", "di", "du", "la", "le", "los", "las", "of", "the"}
    words = [
        word for word in re.findall(r"[a-z0-9]+", _ascii_fold(normalise(str(name or ""))))
        if word not in noise and word not in connectors
    ]
    if "atl" in words and "tico" in words:
        words = [word for word in words if word not in {"atl", "tico"}]
        words.append("atletico")
    return set(words)


def _ascii_fold(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(ch)
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



