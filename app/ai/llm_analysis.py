"""
LLM Prediction Analysis Agent
---------------------------------
Ported from migrated predictz/agent.py.
Full match analysis using OpenRouter cloud inference.

This is an OPTIONAL enhancement on top of the existing rules-based prediction_agent.py.
Falls back gracefully if OPENROUTER_API_KEY is not set.

Usage:
    from app.ai.llm_analysis import run_llm_predictions
    results = run_llm_predictions(match_date="2026-05-16", docs=enriched_docs)
"""
from __future__ import annotations

import json
import logging
from datetime import date as dt, datetime, timezone
from typing import Any

from app.storage.league_memory import record_prediction
from app.ai.ai_router import _call_llm, is_llm_available

logger = logging.getLogger(__name__)
from app.market.season_stage import detect_season_stage
from app.research.research_filter import get_research_context_for_prompt


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a football prediction expert. Analyse the match data and output a prediction as valid JSON only — no text outside the JSON block.

Consider: form (W/L/D), H2H record, league standings, 1x2 odds, and any web context provided.
Only predict if confidence >= 0.60, otherwise set status to "low_confidence".

Output format:
{
  "match": "<home> vs <away>",
  "status": "predicted | low_confidence | skipped",
  "prediction": "Home Win | Away Win | Draw",
  "odds": "<decimal odds string>",
  "confidence": <0.0-1.0>,
  "value_bet": <true if confidence>=0.70 and odds>=2.5>,
  "btts": "Yes | No | Unknown",
  "over_2_5": "Yes | No | Unknown",
  "market_signal": "sharp HOME | sharp AWAY | stable | unavailable",
  "key_factors": ["<factor 1>", "<factor 2>", "<factor 3>"],
  "reasoning": {
    "form": "<one sentence>",
    "h2h": "<one sentence>",
    "standings": "<one sentence>",
    "odds_signal": "<one sentence>",
    "verdict": "<one sentence final summary>"
  }
}"""

# LangChain-escaped version for use inside ChatPromptTemplate (agent mode)
_SYSTEM_PROMPT_LANGCHAIN = SYSTEM_PROMPT.replace("{", "{{").replace("}", "}}")


# ── Agent builder ─────────────────────────────────────────────────────────────

def _build_agent():
    from langchain.agents import create_tool_calling_agent, AgentExecutor
    from langchain_core.prompts import ChatPromptTemplate
    from app.ai.llm import get_llm
    from app.ai.agent_tools import ALL_TOOLS
    from datetime import datetime, timezone

    @__import__("langchain.tools", fromlist=["tool"]).tool
    def get_current_time() -> dict:
        """Returns the current UTC time. Use to check if a match has already started."""
        now = datetime.now(timezone.utc)
        return {"utc": now.isoformat(), "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M")}

    tools = ALL_TOOLS + [get_current_time]
    llm = get_llm()
    prompt = ChatPromptTemplate.from_messages([
        ("system", _SYSTEM_PROMPT_LANGCHAIN),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])
    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=False, max_iterations=8, handle_parsing_errors=True)


# ── Single match prediction ───────────────────────────────────────────────────

def _summarise_doc(doc: dict[str, Any]) -> str:
    """
    Distil an enriched match doc into a tight summary for LLM analysis.
    Hard target: < 800 tokens total. No JSON blobs, no full history arrays.
    """
    detail = doc.get("sofascore_detail") or {}
    known_competition = doc.get("known_competition") or {}
    competition_line = "unknown"
    if known_competition.get("known"):
        intelligence = known_competition.get("intelligence") or {}
        competition_line = (
            f"known={known_competition.get('name')} key={known_competition.get('key')} "
            f"importance={((known_competition.get('importance') or {}).get('tier'))} "
            f"table={json.dumps(intelligence.get('table') or {})[:450]} "
            f"team_strength={json.dumps(intelligence.get('team_strength') or {})[:450]}"
        )
    home = detail.get("home_team") or doc.get("home_team") or {}
    away = detail.get("away_team") or doc.get("away_team") or {}
    home_name = (home.get("name") or "") if isinstance(home, dict) else str(home or "")
    away_name = (away.get("name") or "") if isinstance(away, dict) else str(away or "")

    # Form: W/L/D string only, last 5 matches
    def _wld(history: list, team_id: Any) -> str:
        finished = [m for m in (history or []) if (m.get("status") or {}).get("type") == "finished"][:5]
        out = []
        for m in finished:
            s = m.get("score") or {}
            h_id = (m.get("home_team") or {}).get("id")
            is_home = str(h_id) == str(team_id) if h_id else True
            gf = s.get("home", 0) if is_home else s.get("away", 0)
            ga = s.get("away", 0) if is_home else s.get("home", 0)
            try:
                gf, ga = int(gf), int(ga)
                out.append("W" if gf > ga else "D" if gf == ga else "L")
            except Exception:
                out.append("?")
        return "".join(out) or "N/A"

    home_hist = detail.get("home_last_matches") or []
    away_hist = detail.get("away_last_matches") or []
    home_id = home.get("id") if isinstance(home, dict) else None
    away_id = away.get("id") if isinstance(away, dict) else None

    # H2H
    h2h = detail.get("h2h") or {}
    td = h2h.get("team_duel") or {}
    h2h_str = f"HW={td.get('homeWins',0)} AW={td.get('awayWins',0)} D={td.get('draws',0)}"

    # Pregame ratings + positions
    pregame = detail.get("pregame_form") or {}
    hpf = pregame.get("home_team") or {}
    apf = pregame.get("away_team") or {}

    # Standings: just find the two teams' rows
    standings = detail.get("standings") or []
    home_pos = away_pos = "?"
    for row in standings:
        tn = (row.get("team") or {}).get("name") or ""
        if home_name and home_name.lower() in tn.lower():
            home_pos = f"#{row.get('position','?')} {row.get('points','?')}pts"
        if away_name and away_name.lower() in tn.lower():
            away_pos = f"#{row.get('position','?')} {row.get('points','?')}pts"

    # Season stage awareness: when the season hasn't started or is just
    # beginning, standings are unreliable.  Add a note to the prompt.
    season_stage = doc.get("season_stage") or detect_season_stage(standings)
    season_note = ""
    if season_stage:
        stage = season_stage.get("stage")
        if stage == "not_started":
            season_note = " | SEASON_NOT_STARTED: standings are 0 points for all teams — ignore table positions"
        elif stage == "beginning":
            season_note = f" | SEASON_BEGINNING: avg {season_stage.get('avg_matches_played', 0)} matches played — standings not yet reliable"

    # 1x2 odds
    markets = doc.get("sportybet_markets") or doc.get("markets") or []
    odds_str = "unavailable"
    for mkt in markets:
        n = (mkt.get("name") or "").lower()
        if mkt.get("id") == "1" or "1x2" in n or "match result" in n:
            sels = mkt.get("selections") or []
            odds_str = " | ".join(f"{s.get('name')}={s.get('odds')}" for s in sels[:3])
            break
    if odds_str == "unavailable":
        choices = ((detail.get("odds_featured") or {}).get("default") or {}).get("choices") or []
        if choices:
            odds_str = " | ".join(f"{c.get('name')}={c.get('fractional_value')}" for c in choices[:3])

    # Web context: 1 snippet, max 150 chars
    web = doc.get("web_context") or {}
    snippets = web.get("snippets") or []
    web_str = str((snippets[0].get("snippet") if snippets else None) or "none")[:150]
    open_router_web = web.get("open_router_analysis") or {}
    open_router_web_str = str(open_router_web.get("summary") or "none")[:600]
    # Include sentiment and probability signals from Open Router research
    open_router_sentiment = open_router_web.get("sentiment") or {}
    sentiment_str = "none"
    if isinstance(open_router_sentiment, dict) and open_router_sentiment.get("home_sentiment"):
        sentiment_str = (
            f"home={open_router_sentiment.get('home_sentiment')} "
            f"away={open_router_sentiment.get('away_sentiment')} "
            f"overall={open_router_sentiment.get('overall_sentiment')} "
            f"conf={open_router_sentiment.get('confidence')}%"
        )[:300]
    open_router_probability = open_router_web.get("probability") or {}
    probability_str = "none"
    if isinstance(open_router_probability, dict):
        parts = []
        for key in ("implied_home_win", "implied_draw", "implied_away_win"):
            val = open_router_probability.get(key)
            if val is not None:
                parts.append(f"{key}={val}%")
        if parts:
            probability_str = " | ".join(parts)[:300]

    # Inject research context
    research_section = ""
    try:
        research_block = get_research_context_for_prompt()
        if research_block:
            research_section = f"\n## RESEARCH_STATS (live empirical patterns)\n{research_block}\n"
    except Exception:
        pass

    return (
        f"{doc.get('sportybet_name') or doc.get('name')} | "
        f"{doc.get('tournament')} ({doc.get('category')}) | "
        f"date={doc.get('match_date')} period={doc.get('period')} "
        f"score={json.dumps(doc.get('score'))}\n"
        f"IDs: sofa={doc.get('sofascore_id')} sporty={doc.get('sportybet_id')}\n"
        f"\n"
        f"HOME {home_name}: form(last5)={_wld(home_hist, home_id)} "
        f"rating={hpf.get('avg_rating')} standing={home_pos}\n"
        f"AWAY {away_name}: form(last5)={_wld(away_hist, away_id)} "
        f"rating={apf.get('avg_rating')} standing={away_pos}\n"
        f"H2H: {h2h_str}\n"
        f"KNOWN_COMPETITION: {competition_line}\n"
        f"ODDS: {odds_str}\n"
        f"WEB: {web_str}\n"
        f"OPEN_ROUTER_WEB_RESEARCH: {open_router_web_str}\n"
        f"OPEN_ROUTER_SENTIMENT: {sentiment_str}\n"
        f"OPEN_ROUTER_PROBABILITY: {probability_str}"
        f"{research_section}"
        f"\nOutput prediction as JSON."
    )


def _predict_one(executor: Any, doc: dict[str, Any]) -> dict[str, Any]:
    from app.competition.competition_special import apply_known_competition_context
    apply_known_competition_context(doc)
    match_input = _summarise_doc(doc)

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
        parsed["source"] = "llm_analysis"
        return parsed
    except Exception as exc:
        return {
            "match": doc.get("sportybet_name") or doc.get("name"),
            "sportybet_id": doc.get("sportybet_id"),
            "status": "error",
            "error": str(exc),
            "source": "llm_analysis",
        }


# ── Public entry point ────────────────────────────────────────────────────────

def run_llm_match_analysis(doc: dict[str, Any]) -> dict[str, Any]:
    """
    Run a match analysis for one enriched document.
    Routes through AIRouter with OpenRouter first, then falls back as needed.
    """
    from app.ai.ai_router import get_router, parse_json_response

    router = get_router()
    if not router.any_available():
        return {"status": "ai_unavailable", "message": "No AI provider available. Set OPENROUTER_API_KEY or enable a fallback provider."}

    # Try small-context pipeline first if available
    if router.is_pipeline_available():
        try:
            from app.ai.llm_pipeline import run_llm_pipeline
            pipeline_result = run_llm_pipeline(doc, attach_brain=False)
            if pipeline_result.get("status") == "predicted":
                return pipeline_result
        except Exception as exc:
            logger.warning("llm_analysis: pipeline failed, falling back to single-call: %s", exc)

    try:
        from app.competition.competition_special import apply_known_competition_context
        apply_known_competition_context(doc)
        summary = _summarise_doc(doc)
        prompt = f"{SYSTEM_PROMPT}\n\n{summary}"
        raw = router.call_analysis(prompt)
        result = parse_json_response(raw)
    except json.JSONDecodeError as exc:
        return {"status": "error", "message": f"Model returned non-JSON: {exc}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    if result.get("status") == "error":
        return {"status": "error", "message": result.get("error") or "Analysis failed."}

    try:
        raw_confidence = float(result.get("confidence"))
        confidence = round(raw_confidence * 100) if raw_confidence <= 1 else round(raw_confidence)
    except (TypeError, ValueError):
        confidence = None

    status_info = router.status()
    active_model = status_info.get("primary_model") or "openrouter/free"
    active_provider = router.last_provider() or ("openrouter" if status_info.get("primary_model") else "llm")

    return {
        "status": result.get("status") or "predicted",
        "recommendation": result.get("prediction"),
        "confidence": confidence,
        "value_bet": bool(result.get("value_bet")),
        "key_factors": result.get("key_factors") or [],
        "reasoning": result.get("reasoning") or {},
        "market_signal": result.get("market_signal"),
        "btts": result.get("btts"),
        "over_2_5": result.get("over_2_5"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "ai_router",
        "provider": active_provider,
        "model": active_model,
    }


def run_llm_predictions(
    match_date: str | None = None,
    docs: list[dict[str, Any]] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """
    Run the LLM analysis agent over enriched match documents.

    Args:
        match_date: YYYY-MM-DD, defaults to today
        docs:       pre-loaded enriched docs (skips DB fetch if provided)
        limit:      max matches to predict in one call

    Returns:
        summary dict with predictions list
    """
    from app.ai.ai_router import get_router
    if not get_router().any_available():
        return {"status": "ai_unavailable", "message": "No AI provider available. Set OPENROUTER_API_KEY or enable a fallback provider."}

    target_date = match_date or dt.today().isoformat()

    if docs is None:
        from app.storage.league_memory import get_enriched_matches
        docs = get_enriched_matches(target_date, limit=limit)

    if not docs:
        return {"status": "no_matches", "date": target_date, "predictions": []}

    docs = docs[:limit]
    print(f"[llm_analysis] predicting {len(docs)} matches for {target_date}")

    try:
        executor = _build_agent()
    except Exception as exc:
        return {"status": "agent_build_failed", "error": str(exc)}

    predictions = []
    value_bets = 0
    errors = 0

    for i, doc in enumerate(docs, 1):
        name = doc.get("sportybet_name") or doc.get("name") or "unknown"
        print(f"[llm_analysis] [{i}/{len(docs)}] {name}")
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
                    "source": "llm_analysis",
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
