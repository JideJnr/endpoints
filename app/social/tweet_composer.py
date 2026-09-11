"""
LLM-controlled thread composer for booking-code posts.

Turns a run_smart_bet()-shaped result (selections, combined odds, share
code) into a list of tweet texts: a hook tweet carrying the booking code,
followed by one reply per leg with a short reason, closing with a
responsible-gambling line. Tries the LLM first (reusing app.ai.llm, same
OpenRouter client the rest of the prediction pipeline uses) and falls back
to a deterministic template — same "never block on the LLM" philosophy as
app.utils.bot2 — so the poster job always has something to post even when
OpenRouter is down or unconfigured.
"""
from __future__ import annotations

import json
from typing import Any

import logging

logger = logging.getLogger(__name__)

_TWEET_LIMIT = 280
_BOOKING_PROVIDER = "SportyBet"

_SYSTEM_PROMPT = """You are the social media voice of PredictX, a football prediction service.
Write an X (Twitter) THREAD announcing today's booking code multi-bet. Tone: confident, sharp,
football-fan energy — not corporate, not spammy, no excessive hashtags or emoji spam (max 1-2
emoji total across the whole thread).

Rules:
- Tweet 1 is the hook: mention the number of matches, the combined odds, and end with
  "Booking code: <CODE>" on its own line. It must stand alone and make someone want to read on.
- One tweet per selection after that, each giving a one-sentence reason a fan would find
  credible (not generic "our model says so" — reference form, matchup, or the actual pick).
- Final tweet: a brief responsible-gambling reminder ("18+, bet responsibly, this is not
  financial advice") plus a short call to action.
- Every single tweet MUST be under 280 characters, including the booking code and any tag.
- Output ONLY a JSON array of strings, one per tweet, in posting order. No text outside the array."""


def compose_thread(slip: dict[str, Any], share_code: str | None) -> list[str]:
    """Best-effort LLM composition; falls back to a template on any failure."""
    try:
        from app.ai.llm import get_llm

        llm = get_llm()
        payload = _slip_payload(slip, share_code)
        response = llm.invoke([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, indent=2)},
        ])
        raw = response.content.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        tweets = json.loads(raw.strip())
        cleaned = [str(t).strip() for t in tweets if str(t).strip()]
        if cleaned and all(len(t) <= _TWEET_LIMIT for t in cleaned):
            return cleaned
    except Exception as exc:
        logger.warning("[tweet_composer] LLM composition failed (%s), falling back to template", exc)

    return _template_thread(slip, share_code)


def _slip_payload(slip: dict[str, Any], share_code: str | None) -> dict[str, Any]:
    legs = []
    for item in slip.get("selections") or []:
        legs.append({
            "match": item.get("match_name") or item.get("match"),
            "league": item.get("league_name") or item.get("tournament"),
            "pick": item.get("selection"),
            "confidence": item.get("llm_confidence") or item.get("confidence"),
            "reason": item.get("synthesis_reasoning") or item.get("reason"),
        })
    return {
        "leg_count": len(legs),
        "combined_odds": slip.get("combined_odds"),
        "avg_confidence": slip.get("avg_confidence"),
        "booking_code": share_code,
        "booking_provider": _BOOKING_PROVIDER,
        "legs": legs,
    }


def _template_thread(slip: dict[str, Any], share_code: str | None) -> list[str]:
    legs = slip.get("selections") or []
    combined_odds = slip.get("combined_odds")
    tweets: list[str] = []

    hook = f"Today's {len(legs)}-leg booking code is up — combined odds {combined_odds}."
    if share_code:
        hook += f"\n\nBooking code ({_BOOKING_PROVIDER}): {share_code}"
    else:
        hook += "\n\nFull slip in the thread below."
    tweets.append(_truncate(hook))

    for item in legs:
        match = item.get("match_name") or item.get("match") or "Match"
        pick = item.get("selection") or "Pick"
        reason = item.get("synthesis_reasoning") or item.get("reason") or "Backed by our model's signals."
        tweets.append(_truncate(f"{match}\nPick: {pick}\n{reason}"))

    tweets.append(_truncate(
        "18+. Bet responsibly — this is not financial advice, just where our model landed today. "
        "Gamble within your means."
    ))
    return tweets


def _truncate(text: str) -> str:
    if len(text) <= _TWEET_LIMIT:
        return text
    return text[: _TWEET_LIMIT - 1].rstrip() + "…"
