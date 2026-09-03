"""Translate AI bet-builder picks into SportyBet's booking contract.

This module deliberately does not place a wager.  It prepares the exact
``selections`` contract and can optionally call a configured *share-code*
endpoint.  A provider response is the only authoritative source of a share
code; one must never be invented locally.
"""
from __future__ import annotations

import os
import re
from typing import Any

from app.storage.buffer import get_buffered_match, refresh_sporty_match_state
from app.market.market_intent import classify_market_intent
from app.storage.league_memory._helpers import _side_from_selection_and_match
from app.data_clients.sportybet_client import _browser_headers, _get_session


def _resolve_sportybet_id(raw_id: str) -> str:
    """Extract the numeric SportyBet event ID from any match_id format.

    Handles:
      sr:match:72180092          → sr:match:72180092  (already canonical)
      competition:eliteserien:15260867 → 15260867
      15260867                   → 15260867
    """
    if not raw_id:
        return raw_id
    if raw_id.startswith("sr:match:"):
        return raw_id
    # competition:league-slug:NUMERIC_ID  →  take the last segment
    parts = raw_id.split(":")
    if len(parts) >= 2 and parts[-1].isdigit():
        return parts[-1]
    return raw_id


def build_booking_payload(
    selections: list[dict[str, Any]],
    *,
    stake: int,
    loading_share_code: str | None = None,
    force_refresh: bool = True,
) -> dict[str, Any]:
    """Resolve builder selections against current SportyBet market data."""
    if not selections:
        raise ValueError("At least one selection is required")
    if stake <= 0:
        raise ValueError("stake must be a positive integer in the provider's smallest unit")

    legs = [_resolve_leg(selection, stake, force_refresh=force_refresh) for selection in selections]
    return {
        "selections": legs,
        "loadingShareCode": loading_share_code,
        "orderType": 2 if len(legs) > 1 else 1,
        "betType": "MULTIPLE" if len(legs) > 1 else "SINGLE",
    }


def request_share_code(payload: dict[str, Any]) -> dict[str, Any]:
    """Send a prepared slip only to an explicitly configured code endpoint."""
    endpoint = os.getenv("SPORTYBET_SHARE_CODE_URL", "").strip()
    if not endpoint:
        return {
            "status": "payload_ready",
            "booking_payload": payload,
            "message": "Set SPORTYBET_SHARE_CODE_URL to enable SportyBet share-code creation.",
        }

    response = _get_session().post(
        endpoint,
        params={"throwInvalidEvent": True},
        json=payload,
        headers=_browser_headers(referer="https://www.sportybet.com/ng/sport/football"),
        timeout=20,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"message": response.text[:500]}
    if not response.ok:
        raise RuntimeError(_provider_error(body, response.status_code))
    code = _find_share_code(body)
    if not code:
        raise RuntimeError("SportyBet did not return a share code")
    return {"status": "share_code_created", "share_code": code, "booking_payload": payload}


def _resolve_leg(selection: dict[str, Any], stake: int, *, force_refresh: bool = True) -> dict[str, Any]:
    raw_id = str(selection.get("sportybet_id") or selection.get("eventId") or selection.get("match_id") or "")
    if not raw_id or raw_id.startswith("sofa:") or raw_id.startswith("competition:"):
        raise ValueError(f"Match {raw_id!r} has no SportyBet event ID — cannot book")
    event_id = _resolve_sportybet_id(raw_id)
    if force_refresh:
        refresh_sporty_match_state(event_id)
    doc = get_buffered_match(event_id) or {}
    markets = doc.get("sportybet_markets") or doc.get("markets") or []
    if not markets:
        raise ValueError(f"No current SportyBet markets are available for event {event_id}")

    intent = classify_market_intent(selection.get("type") or selection.get("pick_type"), selection.get("selection"), selection)
    match_name = str(doc.get("name") or doc.get("sportybet_name") or "")
    market, outcome = _find_market_outcome(markets, intent, selection, match_name=match_name)
    if not market or not outcome:
        label = str(selection.get("selection") or "selection")
        raise ValueError(f"{label!r} is no longer available for event {event_id}")
    return {
        "eventId": event_id,
        "marketId": str(market.get("id")),
        "specifier": market.get("specifier") or None,
        "outcomeId": str(outcome.get("id")),
        "stake": stake,
    }


def _find_market_outcome(
    markets: list[dict[str, Any]],
    intent: dict[str, Any],
    source: dict[str, Any],
    *,
    match_name: str = "",
):
    market_kind = str(intent.get("market") or "")
    direction = str(intent.get("direction") or "")
    intent_name = intent.get("intent")
    line = intent.get("line")
    requested_market = str(source.get("marketId") or "")
    requested_outcome = str(source.get("outcomeId") or "")
    raw_selection = str(intent.get("raw_selection") or source.get("selection") or "")

    # Team-name-phrased picks ("Arsenal to win (grid)", "Arsenal or Draw
    # (grid)") can't resolve through _outcome_matches's home/away/draw
    # keyword check below -- verified against a real live match's stored
    # 1X2/Double Chance markets, whose outcome labels are the generic
    # "Home"/"Draw"/"Away" / "Home or Draw"/"Home or Away"/"Draw or Away",
    # not team names -- because the SELECTION names the actual team, not
    # the word "home"/"away". Resolve the side from match_name up front,
    # the same way grading does (_side_from_selection_and_match,
    # app/storage/league_memory/_helpers.py), before falling into the
    # keyword matcher. "ambiguous" (both team names present, e.g. "Arsenal
    # or Chelsea") is exactly the ~home-or-away~ double-chance case -- not
    # a failure to resolve.
    dc_unresolved = intent_name in (None, "double_chance")
    if (market_kind == "1x2" and not direction) or (market_kind == "double_chance" and dc_unresolved):
        clean_selection = re.sub(r"\(grid\)", "", raw_selection, flags=re.IGNORECASE).strip()
        if market_kind == "1x2" and clean_selection.lower() == "draw":
            direction = "draw"
        elif match_name:
            side = _side_from_selection_and_match(clean_selection, match_name)
            if market_kind == "1x2" and side in ("home", "away"):
                direction = side
            elif market_kind == "double_chance":
                if side == "home":
                    intent_name = "home_or_draw"
                elif side == "away":
                    intent_name = "away_or_draw"
                elif side == "ambiguous":
                    intent_name = "home_or_away"

    for market in markets:
        outcomes = market.get("selections") or market.get("outcomes") or []
        if requested_market and str(market.get("id")) != requested_market:
            continue
        if market_kind == "1x2" and not _market_named(market, "1x2"):
            continue
        if market_kind == "double_chance" and not _market_named(market, "double chance"):
            continue
        if market_kind == "btts" and not _market_named(market, "both teams to score", "btts", "gg/ng", "gg ng"):
            continue
        if market_kind == "total_goals" and not _market_named(market, "over/under", "total goals"):
            continue
        # "Next Goal" / "No Goal" -- unlike 1x2/double-chance/over-under/btts
        # above (confirmed identical to their prematch market names on a
        # real live match), this market name is a best guess, not verified
        # against real stored SportyBet data for this account -- the live
        # match checked while building this had no "next goal"-style market
        # in its stored snapshot to confirm against. Spot-check this against
        # a real live slip before relying on it for actual staking.
        if market_kind in ("live_next_goal", "live_no_goal") and not _market_named(market, "next goal"):
            continue
        if line is not None and str(line) not in str(market.get("specifier") or market.get("name") or ""):
            continue
        for outcome in outcomes:
            if requested_outcome and str(outcome.get("id")) == requested_outcome:
                return market, outcome
            outcome_text = outcome.get("name") or outcome.get("desc")
            if _outcome_matches(outcome_text, market_kind, direction, intent_name):
                return market, outcome
            if market_kind in ("live_next_goal", "live_no_goal") and _next_goal_outcome_matches(
                outcome_text, market_kind, direction, raw_selection, match_name,
            ):
                return market, outcome
    return None, None


def _next_goal_outcome_matches(raw: Any, market_kind: str, direction: str, raw_selection: str, match_name: str) -> bool:
    """Team-name-aware resolution for the live next-goal-scorer market.

    Unlike 1x2/double-chance (where the pick's own selection text and the
    provider's outcome text both reduce to home/away/draw keywords via
    _outcome_matches/_side_from_text), a next-goal-scorer selection is
    phrased with the actual team name ("Arsenal to score next (grid)"), not
    the literal word "home"/"away" -- so it needs match_name to resolve
    which side that team is on, the same way grading does
    (_side_from_selection_and_match, app/storage/league_memory/_helpers.py).
    """
    value = " ".join(str(raw or "").lower().replace("-", " ").split())
    if market_kind == "live_no_goal":
        return "no goal" in value or "no more goal" in value or value in {"none", "no"}
    side = _side_from_selection_and_match(raw_selection, match_name) if match_name else None
    if side not in ("home", "away"):
        return False
    home_name, away_name = ([p.strip().lower() for p in match_name.split(" vs ", 1)] if " vs " in match_name else ("", ""))
    expected_name = home_name if side == "home" else away_name
    return bool(expected_name) and expected_name in value


def _market_named(market: dict[str, Any], *needles: str) -> bool:
    name = str(market.get("name") or market.get("desc") or "").lower()
    return any(needle in name for needle in needles)


def _outcome_matches(raw: Any, market: str, direction: str, intent_name: Any) -> bool:
    value = " ".join(str(raw or "").lower().replace("-", " ").split())
    if market == "1x2":
        return (direction == "home" and value in {"1", "home", "home win"}) or (direction == "away" and value in {"2", "away", "away win"}) or (direction == "draw" and value in {"x", "draw"})
    if market == "double_chance":
        dc_map = {
            "home_or_draw": {"1x", "home or draw", "draw or home"},
            "away_or_draw": {"x2", "away or draw", "draw or away"},
            "home_or_away": {"12", "home or away", "away or home"},
        }
        return value in dc_map.get(str(intent_name or ""), set())
    if market == "btts":
        return value == direction
    if market == "total_goals":
        return value.startswith(direction)
    return False


def _find_share_code(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("shareCode", "share_code", "bookingCode", "booking_code", "loadingShareCode"):
            if value.get(key):
                return str(value[key])
        for child in value.values():
            found = _find_share_code(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_share_code(child)
            if found:
                return found
    return None


def _provider_error(body: Any, status: int) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body.get("detail") or f"SportyBet returned HTTP {status}")
    return f"SportyBet returned HTTP {status}"

