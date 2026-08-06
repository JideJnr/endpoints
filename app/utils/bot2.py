"""
Bot 2 — Value Pick Curator
--------------------------
Reviews Bot 1 predictions and selects only the highest-value bets.

Two modes:
  1. Groq LLM mode  — uses llama-3.1-8b-instant to reason over predictions
                      and historical accuracy. Requires GROQ_API_KEY.
  2. Rules mode     — pure Python scoring when Groq is unavailable.
                      Always works, no external dependency.
"""
from __future__ import annotations

import json
from datetime import date as dt
from typing import Any

from app.storage.league_memory import list_prediction_history


# ── System prompt for Groq mode ───────────────────────────────────────────────

_SYSTEM_PROMPT = """You are Bot 2 — an elite betting value analyst.
Your job is to review predictions made by a football prediction agent and select only the
highest-value bets for the day.

You have access to:
- Agent predictions for today (match, prediction, odds, confidence, reasoning, key factors)
- Agent historical accuracy (how often it was right, by tournament, over last 14 days)

## Your selection criteria:
1. ACCURACY CHECK — if agent has < 50% accuracy in this tournament historically, be skeptical
2. ODDS VALUE — sweet spot is odds between 1.80 and 5.00. Below 1.80 = low value. Above 5.00 = too risky unless strong signals
3. CONFIDENCE — only pick predictions with confidence >= 0.68
4. REASONING QUALITY — does the reasoning cite multiple strong signals? Weak reasoning = skip
5. CONSENSUS — if web context and agent agree = stronger pick
6. AVOID — draws (hard to predict), very obscure leagues with no data

## Output a JSON array of your selected picks, ranked by value score (best first):

[
  {
    "match": "<home> vs <away>",
    "tournament": "<tournament>",
    "category": "<country>",
    "kickoff_utc": "<ISO>",
    "prediction": "<Home Win | Away Win | Draw>",
    "odds": "<odds>",
    "bot1_confidence": <float>,
    "value_score": <float 0.0-1.0>,
    "why_selected": "<2 sentence explanation>",
    "risk": "<Low | Medium | High>"
  }
]

Output ONLY the JSON array. No text outside it."""


# ── Groq-powered run ──────────────────────────────────────────────────────────

def _run_bot2_groq(predictions: list[dict], match_date: str) -> list[dict]:
    """Use Groq LLM to curate picks. Falls back to rules on any error."""
    from app.ai.llm import get_fast_llm
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = get_fast_llm()
    user_prompt = (
        f"Today is {match_date}.\n\n"
        f"## Today's Predictions ({len(predictions)} matches):\n"
        f"{json.dumps(predictions, indent=2)}\n\n"
        "Review all predictions and select the best value bets for today. "
        "Quality over quantity — better to pick 3 great bets than 10 mediocre ones."
    )

    response = llm.invoke([
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ])

    raw = response.content.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    return json.loads(raw.strip())


# ── Rules-based run ───────────────────────────────────────────────────────────

def _run_bot2_rules(predictions: list[dict], match_date: str) -> list[dict]:
    """Pure Python scoring — no external LLM required."""
    picks = []
    for prediction in predictions:
        best = prediction.get("best_pick") or {}
        confidence = (best.get("confidence") or 0) / 100
        if confidence < 0.68:
            continue
        if best.get("type") == "no_bet" or "Draw" in str(best.get("selection", "")):
            continue
        odds = _estimate_odds(confidence)
        if odds < 1.8:
            continue
        signal_score = min(len(prediction.get("signals") or []) / 8, 0.25)
        value_score = round(min(0.99, confidence * 0.7 + min(odds / 5, 1) * 0.2 + signal_score), 3)
        risk = (
            "Low" if confidence >= 0.78 and odds <= 3
            else "Medium" if confidence >= 0.70 and odds <= 5
            else "High"
        )
        picks.append({
            "match": prediction.get("match_name"),
            "tournament": prediction.get("league_name"),
            "category": "",
            "kickoff_utc": "",
            "prediction": best.get("selection"),
            "odds": str(odds),
            "bot1_confidence": confidence,
            "value_score": value_score,
            "why_selected": (
                f"{best.get('reason') or 'Strong model pick'} "
                f"Signals count: {len(prediction.get('signals') or [])}."
            ),
            "risk": risk,
            "match_date": match_date,
        })
    picks.sort(key=lambda item: item["value_score"], reverse=True)
    return picks[:20]


# ── Public entry point ────────────────────────────────────────────────────────

def run_bot2(match_date: str | None = None, limit: int = 200) -> dict[str, Any]:
    target_date = match_date or dt.today().isoformat()
    predictions = list_prediction_history(limit=limit)["predictions"]

    if not predictions:
        return {"status": "no_predictions", "date": target_date, "picks": []}

    # try OpenRouter first, fall back to rules
    picks: list[dict] = []
    mode = "rules"
    try:
        from app.ai.llm import get_llm
        llm = get_llm()
        if llm is not None:
            picks = _run_bot2_groq(predictions, target_date)
            mode = "openrouter"
    except Exception as exc:
        print(f"[bot2] OpenRouter failed ({exc}), falling back to rules engine")

    if not picks:
        picks = _run_bot2_rules(predictions, target_date)

    return {
        "status": "success",
        "date": target_date,
        "mode": mode,
        "total_reviewed": len(predictions),
        "picks_selected": len(picks),
        "picks": picks,
    }


def _estimate_odds(confidence: float) -> float:
    if confidence <= 0:
        return 0
    return round(max(1.01, 1 / confidence), 2)
