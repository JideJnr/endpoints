from __future__ import annotations

from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from app.buffer import get_buffered_match, refresh_sporty_match_state, store_enriched
from app.enrichment import FUZZY_THRESHOLD, LLM_FALLBACK_THRESHOLD, _fuzzy_match, _is_junk, _llm_match
from app.market import snapshot_odds
from app.match_state import classify_match_state
from app.sofascore_client import fetch_all_scheduled_events, fetch_event_detail, fetch_live_events, is_usable_event_for_mode
from app.time_context import match_time_context
from app.web_context import search_match_context


class MatchEnrichmentError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


def enrich_buffered_match(sportybet_id: str, *, auto_predict: bool = True) -> dict[str, Any]:
    """
    Enrich one active SportyBet-buffered match with SportyBet detail, SofaScore
    match/detail, web context, time context, odds snapshot, and optional prediction.
    """
    existing_doc = get_buffered_match(sportybet_id)
    if not existing_doc:
        raise MatchEnrichmentError(404, f"Match {sportybet_id} not found in buffer")

    refresh_state = refresh_sporty_match_state(sportybet_id)
    sporty_fallback_used = False
    if not refresh_state.get("active"):
        can_use_buffer_fallback = (
            refresh_state.get("reason") == "not_found_in_fresh_sporty"
            and bool(existing_doc.get("raw_sporty") or existing_doc.get("sportybet_id") or existing_doc.get("id"))
            and not _is_finished_doc(existing_doc)
        )
        if can_use_buffer_fallback:
            sporty_fallback_used = True
            refresh_state = {
                **refresh_state,
                "active": True,
                "fallback_used": True,
                "fallback_reason": "fresh_sporty_lookup_failed_used_buffered_sporty",
            }
        else:
            raise MatchEnrichmentError(
                409,
                {
                    "message": "Match is not active on fresh SportyBet check",
                    "refresh": refresh_state,
                },
            )

    doc = get_buffered_match(sportybet_id) if not sporty_fallback_used else existing_doc
    if not doc:
        raise MatchEnrichmentError(
            404,
            f"Match {sportybet_id} not found in buffer after SportyBet refresh",
        )

    sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
    match_date = doc.get("match_date") or date.today().isoformat()
    sofa, score, source = _resolve_sofascore_match(doc, sporty, match_date)

    detail = None
    if sofa:
        try:
            detail = fetch_event_detail(sofa)
        except Exception:
            detail = None

    web_context = {}
    try:
        web_context = search_match_context(
            _home_team(sporty),
            _away_team(sporty),
            sporty.get("tournament") or "",
        )
    except Exception:
        pass

    now = datetime.now(timezone.utc).isoformat()
    markets = sporty.get("markets") or []
    match_status = "matched" if sofa else "no_match"
    match_state = classify_match_state(sporty)
    enriched_doc = {
        **doc,
        **sporty,
        "sportybet_id": sporty.get("id") or sportybet_id,
        "sportybet_name": sporty.get("sportybet_name") or sporty.get("name"),
        "match_date": match_date,
        "sportybet_markets": markets,
        "markets": markets,
        "sportybet_detail": _sporty_detail(sporty, sportybet_id, markets, now),
        "sportybet_data_status": "stale_buffer_fallback" if sporty_fallback_used else "available",
        "data_sources": _data_sources(sofa, detail, sporty, markets, fresh=not sporty_fallback_used),
        "sofascore_id": sofa.get("id") if sofa else None,
        "sofascore_name": sofa.get("name") if sofa else None,
        "sofascore_event": sofa,
        "sofascore_detail": detail,
        "sofascore_match_status": match_status,
        "sofascore_best_score": round(score, 3),
        "sofascore_no_match_at": None if sofa else now,
        "minimum_enrichment_status": "full_provider_match" if sofa else "sporty_only",
        "web_context": web_context,
        "match_score": round(score, 3),
        "match_source": source,
        "manual_match": bool(doc.get("sofascore_id")),
        "manual_matched_at": doc.get("manual_matched_at"),
        "raw_sporty": doc.get("raw_sporty") or sporty,
        "raw_sofascore_event": sofa.get("raw_event") if isinstance(sofa, dict) else None,
        "time_context": match_time_context({**sporty, "sofascore_event": sofa}),
        "match_state": match_state,
        "enriched_at": now,
    }

    snapshot_odds(enriched_doc)

    if auto_predict:
        from app.prediction_flow import apply_prediction_state

        apply_prediction_state(enriched_doc, match_id=sportybet_id)

    store_enriched(sportybet_id, enriched_doc)

    return {
        "status": "success",
        "sportybet_id": sportybet_id,
        "matched_sofascore": bool(sofa),
        "minimum_enriched": True,
        "minimum_enrichment_status": enriched_doc.get("minimum_enrichment_status"),
        "sofascore_id": sofa.get("id") if sofa else None,
        "fuzzy_score": round(score, 3),
        "match_source": source,
        "has_detail": bool(detail),
        "has_web_context": bool(web_context.get("snippets")),
        "web_context_query": web_context.get("query"),
        "sporty_refresh": refresh_state,
        "enriched_at": now,
    }


def _resolve_sofascore_match(doc: dict[str, Any], sporty: dict[str, Any], match_date: str) -> tuple[dict[str, Any] | None, float, str]:
    saved_sofa_id = doc.get("sofascore_id")
    if saved_sofa_id:
        sofa = _find_sofascore_event(str(saved_sofa_id), match_date, _is_live_doc(doc))
        if not sofa and isinstance(doc.get("sofascore_event"), dict):
            sofa = doc["sofascore_event"]
        score = _candidate_score(sofa, doc) if sofa else float(doc.get("match_score") or 1.0)
        return sofa, score, "manual"

    try:
        sofa_events = fetch_live_events() if _is_live_doc(doc) else fetch_all_scheduled_events(match_date)
        sofa_events = [
            event for event in sofa_events
            if is_usable_event_for_mode(event, live=_is_live_doc(doc))
        ]
    except Exception:
        sofa_events = []

    sofa, score = _fuzzy_match(sporty, sofa_events)
    live_doc = _is_live_doc(doc)
    threshold = 0.62 if live_doc else FUZZY_THRESHOLD
    llm_threshold = 0.55 if live_doc else LLM_FALLBACK_THRESHOLD
    source = "auto"
    if score < threshold and score >= llm_threshold and not _is_junk(sporty.get("name") or ""):
        llm_sofa = _llm_match(sporty, sofa_events)
        if llm_sofa:
            sofa = llm_sofa
            source = "llm"
    if score < threshold and source != "llm":
        sofa = None
        source = "no_match"
    return sofa, score, source


def _find_sofascore_event(sofa_id: str, match_date: str, live: bool) -> dict[str, Any] | None:
    if live:
        try:
            match = next((event for event in fetch_live_events() if str(event.get("id")) == sofa_id), None)
            if match and is_usable_event_for_mode(match, live=True):
                return match
        except Exception:
            pass

    dates: list[str] = []
    for value in (match_date, date.today().isoformat()):
        if value and value not in dates:
            dates.append(value)
    for target_date in dates:
        try:
            events = fetch_all_scheduled_events(target_date)
        except Exception:
            continue
        match = next((event for event in events if str(event.get("id")) == sofa_id), None)
        if match and is_usable_event_for_mode(match, live=False):
            return match
    return None


def _sporty_detail(sporty: dict[str, Any], sportybet_id: str, markets: list[dict[str, Any]], refreshed_at: str) -> dict[str, Any]:
    return {
        "source": "sportybet",
        "id": str(sporty.get("id") or sportybet_id),
        "name": sporty.get("sportybet_name") or sporty.get("name"),
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
        "home_red_cards": sporty.get("home_red_cards"),
        "away_red_cards": sporty.get("away_red_cards"),
        "venue": sporty.get("venue"),
        "markets": markets,
        "market_count": len(markets),
        "odds_1x2": _extract_1x2(markets),
        "raw_event": sporty.get("raw_event"),
        "refreshed_at": refreshed_at,
    }


def _data_sources(
    sofa: dict[str, Any] | None,
    detail: dict[str, Any] | None,
    sporty: dict[str, Any],
    markets: list[dict[str, Any]],
    *,
    fresh: bool = True,
) -> dict[str, Any]:
    return {
        "sportybet": {
            "available": True,
            "detail": True,
            "fresh": fresh,
            "markets": bool(markets),
            "market_count": len(markets),
            "live_clock": bool(classify_match_state(sporty).get("is_live")),
        },
        "sofascore": {
            "available": bool(sofa or detail),
            "matched": bool(sofa),
            "detail": bool(detail),
            "statistics": bool((detail or {}).get("statistics") or (detail or {}).get("match_statistics")),
            "history": bool((detail or {}).get("home_last_matches") or (detail or {}).get("away_last_matches")),
        },
    }


def _candidate_score(event: dict[str, Any] | None, doc: dict[str, Any]) -> float:
    if not event:
        return 0.0
    try:
        from app.enrichment import _event_score

        sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
        return _event_score(
            {
                **sporty,
                "name": sporty.get("name") or doc.get("sportybet_name") or doc.get("name"),
                "home_team": sporty.get("home_team") or _home_team(doc),
                "away_team": sporty.get("away_team") or _away_team(doc),
                "tournament": sporty.get("tournament") or doc.get("tournament"),
                "category": sporty.get("category") or doc.get("category"),
                "start_time": sporty.get("start_time") or doc.get("start_time"),
            },
            event,
        )
    except Exception:
        sofa_name = event.get("name") or ""
        sporty_name = doc.get("sportybet_name") or doc.get("name") or ""
        direct = SequenceMatcher(None, sofa_name.lower(), sporty_name.lower()).ratio()
        home = SequenceMatcher(None, ((event.get("home_team") or {}).get("name") or "").lower(), _home_team(doc).lower()).ratio()
        away = SequenceMatcher(None, ((event.get("away_team") or {}).get("name") or "").lower(), _away_team(doc).lower()).ratio()
        return round(max(direct, (home + away) / 2), 3)


def _is_live_doc(doc: dict[str, Any]) -> bool:
    return bool(classify_match_state(doc).get("is_live"))


def _is_finished_doc(doc: dict[str, Any]) -> bool:
    state = classify_match_state(doc)
    return bool(doc.get("is_finished") or state.get("is_finished") or state.get("state") in {"postponed", "cancelled"})


def _extract_1x2(markets: list[dict[str, Any]]) -> dict[str, Any]:
    for market in markets:
        name = (market.get("name") or "").lower()
        if market.get("id") == "1" or "1x2" in name or name == "match result":
            odds = {selection.get("name"): selection.get("odds") for selection in market.get("selections", [])}
            return {
                "home": odds.get("Home") or odds.get("1"),
                "draw": odds.get("Draw") or odds.get("X"),
                "away": odds.get("Away") or odds.get("2"),
            }
    return {}


def _home_team(doc: dict[str, Any]) -> str:
    team = doc.get("home_team")
    if isinstance(team, dict):
        return team.get("name") or ""
    if team:
        return str(team)
    return _team_from_name(doc, 0)


def _away_team(doc: dict[str, Any]) -> str:
    team = doc.get("away_team")
    if isinstance(team, dict):
        return team.get("name") or ""
    if team:
        return str(team)
    return _team_from_name(doc, 1)


def _team_from_name(doc: dict[str, Any], index: int) -> str:
    name = doc.get("sportybet_name") or doc.get("name") or ""
    parts = [part.strip() for part in name.split(" vs ", 1)]
    return parts[index] if len(parts) > index else ""
