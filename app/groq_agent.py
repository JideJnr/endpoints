"""
Groq LangChain Prediction Agent
---------------------------------
Ported from migrated predictz/agent.py.
Full 10-step reasoning agent using llama-3.3-70b-versatile via Groq.

This is an OPTIONAL enhancement on top of the existing rules-based prediction_agent.py.
Falls back gracefully if GROQ_API_KEY is not set.

Usage:
    from app.groq_agent import run_groq_predictions
    results = run_groq_predictions(match_date="2026-05-16", docs=enriched_docs)
"""
from __future__ import annotations

import json
from datetime import date as dt, datetime, timezone
from typing import Any

from app.league_memory import record_prediction


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an elite football prediction agent. Your job is to analyse every match
and find the true winner — especially in lower leagues where bookmakers misprice odds.

Your edge is finding matches where:
- One team is clearly dominant across ALL data points
- The odds are still high due to obscure league or low public attention
- This is a VALUE BET — high confidence + high odds = profit

## Your reasoning process for EVERY match:

1. CHECK TIME — compare match start_time to current time
   - If match already started or finished → skip, output status: "skipped"
   - Calculate hours until kickoff — note if team played recently (fatigue risk)

2. ANALYSE STANDINGS — league position gap tells you dominance
   - Top 3 vs bottom 3 = strong signal
   - Mid-table vs mid-table = low confidence, skip unless other signals are strong

3. ANALYSE HEAD TO HEAD — historical dominance matters
   - 4+ wins out of last 5 = strong signal
   - Balanced h2h = reduce confidence

4. POISSON MODEL — call poisson_model(home_team_id, away_team_id)
   - Pure maths based on last 10 matches goals scored/conceded
   - If Poisson says Home Win > 55% AND reasoning agrees = strong signal
   - Use over_2_5 % and btts % to fill those prediction fields

5. MARKET MOVEMENT — call get_odds_movement(sportybet_id)
   - Odds shortened = smart money backing that team = STRONG signal
   - Sharp signal on same side as your prediction = increase confidence by 0.05
   - Sharp signal against your prediction = reduce confidence by 0.08

6. ANALYSE FORM WITH SCHEDULE CONTEXT — call strength_of_schedule(home_team_id, away_team_id)
   - CRITICAL — raw form (W/L/D) is misleading without opponent context
   - soft_losses (losses vs weak teams) = genuine red flag
   - quality_wins (wins vs strong teams) = genuine signal
   - Division gap: higher league tier = structural advantage

7. ANALYSE ODDS — market signal
   - High odds (>3.0) on a dominant team = VALUE BET opportunity

8. READ WEB CONTEXT — what are experts saying?
   - Injury news, suspensions, team news
   - If key player missing → reduce confidence

9. FATIGUE CHECK — if team played 2-3 days ago, reduce confidence by 0.1

10. MAKE DECISION — only predict if confidence >= 0.60
    - Below 0.60 → output status: "low_confidence", skip

## Output ONLY valid JSON — no text outside the JSON block:

{
  "match": "<home> vs <away>",
  "tournament": "<tournament name>",
  "category": "<country>",
  "kickoff_utc": "<ISO timestamp>",
  "status": "<predicted | skipped | low_confidence>",
  "prediction": "<Home Win | Away Win | Draw>",
  "odds": "<best available odds for this prediction as string>",
  "confidence": <float 0.0-1.0>,
  "value_bet": <true if confidence >= 0.70 and odds >= 2.5>,
  "btts": "<Yes | No | Unknown>",
  "over_2_5": "<Yes | No | Unknown>",
  "poisson": {
    "home_win_pct": <float>,
    "draw_pct": <float>,
    "away_win_pct": <float>,
    "over_2_5_pct": <float>,
    "btts_pct": <float>,
    "top_score": "<most likely scoreline e.g. 2-1>"
  },
  "market_signal": "<sharp money on HOME | sharp money on AWAY | stable | unavailable>",
  "key_factors": ["<factor 1>", "<factor 2>", "<factor 3>"],
  "reasoning": {
    "form": "<one sentence on quality-adjusted form>",
    "h2h": "<one sentence on head to head record>",
    "standings": "<one sentence on league position gap>",
    "poisson": "<one sentence on what the maths says>",
    "odds_signal": "<one sentence on odds movement>",
    "web_consensus": "<one sentence on expert previews>",
    "fatigue": "<one sentence on fixture congestion>",
    "verdict": "<one sentence final summary>"
  }
}"""


# ── Agent builder ─────────────────────────────────────────────────────────────

def _build_agent():
    from langchain.agents import create_tool_calling_agent, AgentExecutor
    from langchain_core.prompts import ChatPromptTemplate
    from app.llm import get_llm
    from app.agent_tools import ALL_TOOLS
    from datetime import datetime, timezone

    @__import__("langchain.tools", fromlist=["tool"]).tool
    def get_current_time() -> dict:
        """Returns the current UTC time. Use to check if a match has already started."""
        now = datetime.now(timezone.utc)
        return {"utc": now.isoformat(), "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M")}

    tools = ALL_TOOLS + [get_current_time]
    llm = get_llm()
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=False, max_iterations=8, handle_parsing_errors=True)


# ── Single match prediction ───────────────────────────────────────────────────

def _predict_one(executor: Any, doc: dict[str, Any]) -> dict[str, Any]:
    match_input = (
        f"Analyse and predict this match:\n\n"
        f"match: {doc.get('sportybet_name') or doc.get('name')}\n"
        f"sofascore_event_id: {doc.get('sofascore_id')}\n"
        f"sportybet_id: {doc.get('sportybet_id')}\n"
        f"tournament: {doc.get('tournament')}\n"
        f"category: {doc.get('category')}\n"
        f"start_time_unix_ms: {doc.get('start_time')}\n"
        f"match_date: {doc.get('match_date')}\n"
        f"current_period: {doc.get('period')}\n"
        f"current_score: {json.dumps(doc.get('score'))}\n\n"
        f"sportybet_markets (live odds):\n"
        f"{json.dumps((doc.get('sportybet_markets') or doc.get('markets') or [])[:3], indent=2)}\n\n"
        f"sofascore_detail (h2h, form, standings, odds):\n"
        f"{json.dumps(doc.get('sofascore_detail'), indent=2)}\n\n"
        f"web_context (expert previews):\n"
        f"{json.dumps(doc.get('web_context'), indent=2)}\n\n"
        f"Analyse all available data and output your prediction as JSON."
    )

    try:
        result = executor.invoke({"input": match_input})
        raw = result["output"]
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw.strip())
        parsed["sportybet_id"] = doc.get("sportybet_id")
        parsed["match_id"] = doc.get("sportybet_id")
        parsed["source"] = "groq_agent"
        return parsed
    except Exception as exc:
        return {
            "match": doc.get("sportybet_name") or doc.get("name"),
            "sportybet_id": doc.get("sportybet_id"),
            "status": "error",
            "error": str(exc),
            "source": "groq_agent",
        }


# ── Public entry point ────────────────────────────────────────────────────────

def run_groq_predictions(
    match_date: str | None = None,
    docs: list[dict[str, Any]] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """
    Run the Groq LangChain agent over enriched match documents.

    Args:
        match_date: YYYY-MM-DD, defaults to today
        docs:       pre-loaded enriched docs (skips DB fetch if provided)
        limit:      max matches to predict in one call

    Returns:
        summary dict with predictions list
    """
    from app.llm import is_groq_available
    if not is_groq_available():
        return {"status": "groq_unavailable", "message": "Set GROQ_API_KEY to enable Groq agent"}

    target_date = match_date or dt.today().isoformat()

    if docs is None:
        from app.league_memory import get_enriched_matches
        docs = get_enriched_matches(target_date, limit=limit)

    if not docs:
        return {"status": "no_matches", "date": target_date, "predictions": []}

    docs = docs[:limit]
    print(f"[groq_agent] predicting {len(docs)} matches for {target_date}")

    try:
        executor = _build_agent()
    except Exception as exc:
        return {"status": "agent_build_failed", "error": str(exc)}

    predictions = []
    value_bets = 0
    errors = 0

    for i, doc in enumerate(docs, 1):
        name = doc.get("sportybet_name") or doc.get("name") or "unknown"
        print(f"[groq_agent] [{i}/{len(docs)}] {name}")
        pred = _predict_one(executor, doc)
        predictions.append(pred)

        if pred.get("status") == "error":
            errors += 1
        elif pred.get("status") == "predicted":
            try:
                record_prediction({
                    **pred,
                    "match_id": str(doc.get("sportybet_id") or ""),
                    "match_date": target_date,
                    "source": "groq_agent",
                })
            except Exception:
                pass
            if pred.get("value_bet"):
                value_bets += 1

    return {
        "status": "success",
        "date": target_date,
        "total": len(predictions),
        "predicted": len([p for p in predictions if p.get("status") == "predicted"]),
        "skipped": len([p for p in predictions if p.get("status") in ("skipped", "low_confidence")]),
        "errors": errors,
        "value_bets": value_bets,
        "predictions": predictions,
    }
