"""Evidence-first Ollama prediction pipeline with a rules-engine fallback.

The module deliberately keeps each evidence step independent: a missing source or
model failure produces a useful sentence and never prevents the final decision.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import sqlite3
import time
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db
from app.market.season_stage import detect_season_stage

logger = logging.getLogger(__name__)

SPECIALIST_NAMES = [
    "H2H Analyst",
    "Common Opponent Analyst",
    "Form Analyst",
    "Market Odds Analyst",
    "Similar Match Analyst",
    "Team Previous Matches Analyst",
]

MIN_SPECIALIST_SAMPLES = 10   # minimum graded predictions before trusting a specialist's ratio


def get_specialist_weights(league: str | None = None, pick_type: str | None = None) -> dict[str, float]:
    """
    Return {specialist_name: weight} for the given scope.
    Falls back to global weights, then to 1.0 (neutral) if no data.
    """
    _init_db()
    league_key = (league or "").lower().strip().replace(" ", "_")[:60] or "__global__"
    with db_conn(timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            select specialist_name, weight, league_key, samples
            from specialist_performance
            where league_key in (?, '__global__')
              and pick_type in (?, '__all__')
              and samples >= ?
            order by
                case when league_key = ? then 0 else 1 end,
                case when pick_type = ? then 0 else 1 end
        """, (league_key, pick_type or "__all__", MIN_SPECIALIST_SAMPLES, league_key, pick_type or "__all__")).fetchall()
    weights: dict[str, float] = {}
    for row in rows:
        name = row["specialist_name"]
        if name not in weights:  # league-specific wins over global
            weights[name] = round(float(row["weight"]), 4)
    # Fill missing specialists with neutral weight 1.0
    for name in SPECIALIST_NAMES:
        weights.setdefault(name, 1.0)
    return weights


def record_specialist_outcome(
    specialist_name: str,
    result: str,           # 'win' or 'loss'
    league: str | None = None,
    pick_type: str | None = None,
) -> None:
    """
    Record one graded outcome for a specialist.
    Called after a prediction is graded — the specialist contributed if its
    evidence_status was 'available' for that prediction.
    """
    _init_db()
    league_key = (league or "").lower().strip().replace(" ", "_")[:60] or "__global__"
    pt = pick_type or "__all__"
    win = 1 if result == "win" else 0
    loss = 1 if result == "loss" else 0
    now = datetime.now(timezone.utc).isoformat()
    with db_conn(timeout=20) as conn:
        for lk in (league_key, "__global__"):
            for pk in ({pt, "__all__"}):
                conn.execute("""
                    insert into specialist_performance
                        (specialist_name, league_key, pick_type, samples, wins, losses, win_rate, weight, last_updated)
                    values (?, ?, ?, 1, ?, ?, null, 1.0, ?)
                    on conflict(specialist_name, league_key, pick_type) do update set
                        samples      = samples + 1,
                        wins         = wins + excluded.wins,
                        losses       = losses + excluded.losses,
                        last_updated = excluded.last_updated
                """, (specialist_name, lk, pk, win, loss, now))
        # Recompute win_rate and weight for touched rows
        conn.execute("""
            update specialist_performance
            set win_rate = cast(wins as real) / samples,
                weight   = max(0.3, min(2.0, 0.3 + (cast(wins as real) / samples) * 1.7))
            where specialist_name = ? and samples >= ?
        """, (specialist_name, MIN_SPECIALIST_SAMPLES))
        conn.commit()


def grade_specialist_contributions(
    reasoning_context: dict[str, Any],
    result: str,
    league: str | None = None,
    pick_type: str | None = None,
) -> int:
    """
    After a prediction is graded, credit each specialist whose evidence was
    'available' (i.e. actually contributed, not a fallback placeholder).
    Returns the number of specialists credited.
    """
    analysts: list[dict[str, Any]] = reasoning_context.get("analysts") or []
    if not analysts:
        return 0
    credited = 0
    for analyst in analysts:
        name = str(analyst.get("name") or "")
        status = str(analyst.get("evidence_status") or "available")  # default available for legacy rows
        if not name or status == "unavailable":
            continue
        try:
            record_specialist_outcome(name, result, league=league, pick_type=pick_type)
            credited += 1
        except Exception:
            pass
    return credited


def get_specialist_summary() -> list[dict[str, Any]]:
    """Return global specialist performance for the analytics endpoint."""
    _init_db()
    with db_conn(timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            select specialist_name, samples, wins, losses, win_rate, weight
            from specialist_performance
            where league_key = '__global__' and pick_type = '__all__'
            order by coalesce(win_rate, 0) desc
        """).fetchall()
    return [
        {
            "specialist": row["specialist_name"],
            "samples":    row["samples"],
            "wins":       row["wins"],
            "losses":     row["losses"],
            "win_rate":   round(float(row["win_rate"]) * 100, 1) if row["win_rate"] is not None else None,
            "weight":     round(float(row["weight"]), 3),
            "status":     "trusted" if (row["samples"] or 0) >= MIN_SPECIALIST_SAMPLES else "learning",
        }
        for row in rows
    ]



H2H_FALLBACK = "No H2H history found between these teams."
COMMON_FALLBACK = "No common opponents found in recent history."
FORM_FALLBACK = "Standings data unavailable; form data not present."
ODDS_FALLBACK = "Odds movement data unavailable."
SIMILAR_FALLBACK = "No tier-comparable similar matches found."


@dataclass
class TeamBehaviourProfile:
    team_name: str
    btts_rate: float = 0.0
    over_2_5_rate: float = 0.0
    clean_sheet_rate: float = 0.0
    comeback_rate: float = 0.0
    high_scorer_flag: int = 0
    loss_to_nil_rate: float = 0.0
    sample_size: int = 0
    computed_at: str = ""


@dataclass
class ReasoningContext:
    match_name: str
    h2h_statement: str
    common_opponent_statement: str
    form_statement: str
    odds_statement: str
    similar_matches_statement: str
    team_history_statement: str


@dataclass
class MarketCandidate:
    market_key: str
    label: str
    score: float


def _evidence_status(statement: str) -> str:
    """Expose absent specialist evidence to both the decider and the UI."""
    text = str(statement or "").lower()
    unavailable_markers = (
        "no h2h history",
        "no common opponents",
        "unavailable",
        "not present",
        "0 previous finished matches",
    )
    return "unavailable" if any(marker in text for marker in unavailable_markers) else "available"


def _name(doc: dict[str, Any]) -> str:
    return str(doc.get("name") or doc.get("sportybet_name") or doc.get("match_name") or "Unknown fixture")


def _teams(doc: dict[str, Any]) -> tuple[str, str]:
    detail = doc.get("sofascore_detail") or {}
    home = detail.get("home_team") or detail.get("homeTeam") or doc.get("home_team") or doc.get("home") or ""
    away = detail.get("away_team") or detail.get("awayTeam") or doc.get("away_team") or doc.get("away") or ""
    home = home.get("name", "") if isinstance(home, dict) else home
    away = away.get("name", "") if isinstance(away, dict) else away
    if not home and " vs " in _name(doc):
        home, away = _name(doc).split(" vs ", 1)
    return str(home), str(away)


def _tournament(doc: dict[str, Any]) -> str:
    return str(doc.get("tournament") or doc.get("league_name") or ((doc.get("sofascore_detail") or {}).get("tournament") or {}).get("name") or "")


def _best_odds(doc: dict[str, Any]) -> float:
    # Prefer the stored prematch 1X2 odds to avoid inflated live odds (e.g. 100.00
    # for a team losing 5-1) skewing the low_value_odds flag and tier classification.
    odds_1x2 = doc.get("odds_1x2") or {}
    if odds_1x2:
        try:
            candidates = [float(v) for v in odds_1x2.values() if v is not None]
            # Only use prematch 1X2 if values look like prematch (all < 20)
            if candidates and all(v < 20 for v in candidates):
                return max(candidates)
        except (TypeError, ValueError):
            pass
    odds = doc.get("odds") or doc.get("markets") or ((doc.get("sportybet") or {}).get("odds")) or {}
    values: list[float] = []
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values(): walk(item)
        elif isinstance(value, list):
            for item in value: walk(item)
        else:
            try:
                n = float(value)
                # Cap at 20 to ignore live blowout odds (100.00 etc.)
                if 1 < n <= 20: values.append(n)
            except (TypeError, ValueError): pass
    walk(odds)
    return max(values) if values else 0.0


def classify_tournament_tier(tournament_name: str) -> int:
    name = (tournament_name or "").lower()
    if any(x in name for x in ("champions league", "premier league", "laliga", "serie a", "bundesliga", "ligue 1")):
        return 1
    if any(x in name for x in ("europa league", "championship", "eredivisie", "primeira liga", "super lig")):
        return 2
    return 3


def sort_gate(matches: list[dict]) -> list[dict]:
    for doc in matches:
        best = _best_odds(doc)
        doc["low_value_odds"] = best < 1.3
    def key(doc: dict) -> tuple:
        best = _best_odds(doc)
        start = doc.get("start_time") or doc.get("match_date") or ""
        return (str(start), classify_tournament_tier(_tournament(doc)), 0 if 1.3 <= best <= 3.5 else 1, -best)
    return sorted(matches, key=key)


def _apply_recency_decay(h2h_matches: list[dict]) -> list[dict]:
    result = []
    for index, match in enumerate(h2h_matches):
        m = dict(match) if isinstance(match, dict) else {}
        m["weight"] = 1.0 if index < 2 else 0.6 if index < 5 else 0.3
        result.append(m)
    return result


def _score(match: dict, side: str) -> float | None:
    keys = (f"{side}_goals", f"score_{side}", side)
    score = match.get("score") or {}
    for key in keys:
        value = match.get(key, score.get(side) if isinstance(score, dict) else None)
        try: return float(value)
        except (TypeError, ValueError): continue
    return None


def _build_h2h_statement(h2h_matches: list[dict], team_duel: dict | None = None) -> str:
    if not h2h_matches:
        # Fall back to teamDuel aggregate when individual events are absent
        if team_duel and isinstance(team_duel, dict):
            hw = int(team_duel.get("homeWins") or 0)
            aw = int(team_duel.get("awayWins") or 0)
            dr = int(team_duel.get("draws") or 0)
            total = hw + aw + dr
            if total:
                return (f"H2H aggregate ({total} meetings): home wins {hw/total:.0%}, "
                        f"draws {dr/total:.0%}, away wins {aw/total:.0%}.")
        return H2H_FALLBACK
    rows = _apply_recency_decay(h2h_matches)
    total = home = draw = away = 0.0
    for row in rows:
        hg, ag, weight = _score(row, "home"), _score(row, "away"), float(row.get("weight", 1))
        if hg is None or ag is None: continue
        total += weight
        if hg > ag: home += weight
        elif ag > hg: away += weight
        else: draw += weight
    if not total: return H2H_FALLBACK
    return f"Decay-weighted H2H: home wins {home/total:.0%}, draws {draw/total:.0%}, away wins {away/total:.0%} across {len(rows)} meetings."


def _llm_model() -> str | None:
    """Return the first available model via the router."""
    from app.ai.ai_router import get_router
    return get_router().best_available()


def _call_provider(model: str, prompt: str, timeout: int) -> str:
    """Route through AIRouter — OpenRouter primary."""
    from app.ai.ai_router import get_router
    task = "reasoning"
    return get_router().call_auto(prompt, task=task)


# Kept as an internal compatibility alias for existing step tests/call sites.
def _call_llm(model: str, prompt: str, timeout: int) -> str:
    from app.ai.ai_router import get_router
    # Step functions pass the model name as a hint for task routing
    task = "reasoning"
    return get_router().call_auto(prompt, task=task)


def _step_h2h(doc: dict, model: str, timeout: int = 20) -> str:
    try:
        # h2h may be a dict {events: [...], teamDuel: {...}} or a bare list
        raw_h2h = doc.get("h2h") or (doc.get("sofascore_detail") or {}).get("h2h") or doc.get("h2h_matches")
        if isinstance(raw_h2h, dict):
            matches = list(raw_h2h.get("events") or [])
            team_duel = raw_h2h.get("teamDuel") or raw_h2h.get("team_duel")
        elif isinstance(raw_h2h, list):
            matches = list(raw_h2h)
            team_duel = None
        else:
            matches = []
            team_duel = None
        event_id = doc.get("sofascore_id") or ((doc.get("sofascore_detail") or {}).get("id"))
        if not matches and not team_duel and event_id:
            from app.data_clients.sofascore_client import fetch_h2h
            fetched = fetch_h2h(int(event_id)) or {}
            if isinstance(fetched, dict):
                matches = list(fetched.get("events") or [])
                team_duel = fetched.get("teamDuel") or fetched.get("team_duel")
            else:
                matches = list(fetched) if isinstance(fetched, list) else []
        evidence = _build_h2h_statement(matches, team_duel=team_duel)
        if evidence == H2H_FALLBACK: return evidence
        return _call_llm(model, f"Summarise this H2H evidence for a football prediction in one factual sentence: {evidence}", timeout) or evidence
    except Exception as exc:
        logger.warning("AI step h2h failed: %s", exc)
        return H2H_FALLBACK


def _step_common_opponent(doc: dict, model: str, timeout: int = 20) -> str:
    try:
        home, away = _teams(doc)
        if not home or not away: return COMMON_FALLBACK
        with db_conn(timeout=10) as conn:
            rows = conn.execute("""select home_team, away_team, final_home_goals, final_away_goals from matches
                where is_finished=1 and (lower(home_team)=lower(?) or lower(away_team)=lower(?) or lower(home_team)=lower(?) or lower(away_team)=lower(?))
                order by last_seen_at desc limit 40""", (home, home, away, away)).fetchall()
        opponents = set()
        for h, a, *_ in rows:
            if str(h).lower() == home.lower(): opponents.add(str(a).lower())
            if str(a).lower() == home.lower(): opponents.add(str(h).lower())
        away_opponents = {str(a).lower() for h, a, *_ in rows if str(h).lower() == away.lower()} | {str(h).lower() for h, a, *_ in rows if str(a).lower() == away.lower()}
        common = sorted(opponents & away_opponents)
        if not common: return COMMON_FALLBACK
        evidence = f"Both teams have faced common recent opponents: {', '.join(common[:5])}."
        return _call_llm(model, f"Give one cautious football insight from: {evidence}", timeout) or evidence
    except Exception as exc:
        logger.warning("AI step common_opponent failed: %s", exc)
        return COMMON_FALLBACK


def _step_form(doc: dict, model: str, timeout: int = 20) -> str:
    try:
        standings = doc.get("standings") or ((doc.get("sofascore_detail") or {}).get("standings"))
        # SofaScore serialises missing standings as the string "None" — treat it as absent
        if not standings or standings == "None" or standings == "null": return FORM_FALLBACK
        if isinstance(standings, list) and len(standings) == 0: return FORM_FALLBACK
        home, away = _teams(doc)
        if not home or not away: return FORM_FALLBACK

        # Detect season stage so we don't treat 0-point / bottom-of-table
        # standings as meaningful when the season hasn't started or is just beginning.
        season_stage = detect_season_stage(standings)
        if season_stage.get("season_not_started"):
            # Standings are completely meaningless — all teams have 0 points.
            return FORM_FALLBACK

        # Filter standings to only include the two teams of interest to prevent
        # the LLM from hallucinating or discussing the wrong teams.
        def _team_in_standings(team_name: str, standings_data: list) -> dict | None:
            if not team_name:
                return None
            for row in standings_data:
                row_team = (row.get("team") or {}).get("name") or ""
                if team_name.lower() in row_team.lower() or row_team.lower() in team_name.lower():
                    return row
            return None

        home_row = _team_in_standings(home, standings)
        away_row = _team_in_standings(away, standings)

        # Build season context note for the LLM
        season_note = ""
        if season_stage.get("season_beginning"):
            season_note = (
                f"\nNOTE: The season is just beginning (avg {season_stage.get('avg_matches_played', 0)} matches played). "
                f"League positions are not yet reliable — treat standings as indicative only.\n"
            )

        if home_row and away_row:
            evidence = (
                f"League standings for the match {home} vs {away}:\n"
                f"{home}: position {home_row.get('position')}, points {home_row.get('points')}\n"
                f"{away}: position {away_row.get('position')}, points {away_row.get('points')}\n"
                f"{season_note}"
            )
        elif home_row or away_row:
            team = home if home_row else away
            row = home_row or away_row
            evidence = (
                f"League standings for the match {home} vs {away}:\n"
                f"{team}: position {row.get('position')}, points {row.get('points')}\n"
                f"{season_note}"
            )
        else:
            # Neither team found in standings - provide truncated standings but emphasize teams
            evidence = (
                f"League standings for the match {home} vs {away}:\n"
                f"{str(standings)[:380]}\n"
                f"{season_note}"
            )

        prompt = (
            f"You are a football form analyst. Analyse the league standings for the match {home} vs {away}. "
            f"CRITICAL: Only discuss {home} and {away}. Do NOT mention any other teams.\n\n"
            f"{evidence}\n"
            f"Summarise in one factual sentence which team has the better league position and why."
        )
        return _call_llm(model, prompt, timeout) or evidence
    except Exception as exc:
        logger.warning("AI step form failed: %s", exc)
        return FORM_FALLBACK


def _classify_odds_movement(opening: float, current: float) -> str:
    return "shortened" if current < opening else "drifted" if current > opening else "stable"


def _step_odds(doc: dict, model: str, timeout: int = 20) -> str:
    try:
        match_id = str(doc.get("sportybet_id") or doc.get("id") or "")
        if not match_id: return ODDS_FALLBACK
        from app.ai.agent_tools import get_odds_movement
        data = get_odds_movement(match_id) or {}
        if not data: return ODDS_FALLBACK
        evidence = f"Odds movement: {json.dumps(data, default=str)[:350]}"
        return _call_llm(model, f"State the market signal in one cautious sentence: {evidence}", timeout) or evidence
    except Exception as exc:
        logger.warning("AI step odds failed: %s", exc)
        return ODDS_FALLBACK


def apply_tier_filter(candidates: list[dict], target_tier: int) -> list[dict]:
    return [item for item in candidates if target_tier != 1 or classify_tournament_tier(str(item.get("league_name") or item.get("tournament") or "")) != 3]


def _step_similar_matches(doc: dict, model: str, timeout: int = 20) -> str:
    try:
        from app.enrichment.similar_matches import find_similar_matches
        raw = find_similar_matches(doc) or {}
        candidates = raw.get("matches", raw.get("similar_matches", [])) if isinstance(raw, dict) else raw
        candidates = apply_tier_filter(list(candidates or []), classify_tournament_tier(_tournament(doc)))
        if not candidates: return SIMILAR_FALLBACK
        evidence = f"Comparable historical matches: {json.dumps(candidates[:3], default=str)[:420]}"
        return _call_llm(model, f"Give one evidence-only insight: {evidence}", timeout) or evidence
    except Exception as exc:
        logger.warning("AI step similar_matches failed: %s", exc)
        return SIMILAR_FALLBACK


def _history_for_team(team_name: str, conn: sqlite3.Connection) -> list[tuple[int, int, bool]]:
    if not team_name: return []
    rows = conn.execute("""select home_team, away_team, final_home_goals, final_away_goals from matches
        where is_finished=1 and (lower(home_team)=lower(?) or lower(away_team)=lower(?))
        order by last_seen_at desc limit 10""", (team_name, team_name)).fetchall()
    result = []
    for home, away, hg, ag in rows:
        if hg is None or ag is None: continue
        is_home = str(home).lower() == team_name.lower()
        result.append((int(hg if is_home else ag), int(ag if is_home else hg), is_home))
    return result


def _previous_matches_for_team(team_name: str, conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    if not team_name:
        return []
    rows = conn.execute(
        """select home_team, away_team, final_home_goals, final_away_goals, league_name, country_name, start_time
           from matches
           where is_finished=1 and (lower(home_team)=lower(?) or lower(away_team)=lower(?))
           order by coalesce(start_time, last_seen_at) desc
           limit ?""",
        (team_name, team_name, limit),
    ).fetchall()
    matches: list[dict[str, Any]] = []
    for home, away, hg, ag, league, country, match_date in rows:
        try:
            home_goals = int(hg)
            away_goals = int(ag)
        except (TypeError, ValueError):
            continue
        is_home = str(home).lower() == team_name.lower()
        scored = home_goals if is_home else away_goals
        conceded = away_goals if is_home else home_goals
        matches.append(
            {
                "date": match_date,
                "league": league,
                "country": country,
                "home": home,
                "away": away,
                "score": f"{home_goals}-{away_goals}",
                "team_side": "home" if is_home else "away",
                "team_goals": scored,
                "opponent_goals": conceded,
                "result": "W" if scored > conceded else "D" if scored == conceded else "L",
            }
        )
    return matches


def _team_history_summary(team_name: str, matches: list[dict[str, Any]]) -> str:
    if not matches:
        return f"{team_name}: no finished previous-match history found."
    played = len(matches)
    wins = sum(1 for row in matches if row["result"] == "W")
    draws = sum(1 for row in matches if row["result"] == "D")
    losses = sum(1 for row in matches if row["result"] == "L")
    goals_for = sum(float(row["team_goals"]) for row in matches)
    goals_against = sum(float(row["opponent_goals"]) for row in matches)
    btts = sum(1 for row in matches if row["team_goals"] > 0 and row["opponent_goals"] > 0)
    over25 = sum(1 for row in matches if row["team_goals"] + row["opponent_goals"] > 2)
    recent = ", ".join(f"{row['result']} {row['score']} {row['home']} vs {row['away']}" for row in matches[:6])
    return (
        f"{team_name}: {played} previous finished matches, W-D-L {wins}-{draws}-{losses}, "
        f"GF {goals_for:.0f}, GA {goals_against:.0f}, BTTS {btts/played:.0%}, over 2.5 {over25/played:.0%}. "
        f"Recent: {recent}."
    )


def _parse_sofa_last_matches(events: list[dict], team_name: str) -> list[dict[str, Any]]:
    """Convert SofaScore home_last_matches / away_last_matches events to the
    same normalised format that _team_history_summary expects."""
    result: list[dict[str, Any]] = []
    for ev in events:
        status = (ev.get("status") or {}).get("type", "")
        if status not in ("finished", "ended"):
            continue
        ht = ev.get("homeTeam") or ev.get("home_team") or {}
        at = ev.get("awayTeam") or ev.get("away_team") or {}
        hn = ht.get("name", "") if isinstance(ht, dict) else str(ht)
        an = at.get("name", "") if isinstance(at, dict) else str(at)
        hsc = ev.get("homeScore") or {}
        asc = ev.get("awayScore") or {}
        try:
            hg = int(hsc.get("current") if isinstance(hsc, dict) else hsc)
            ag = int(asc.get("current") if isinstance(asc, dict) else asc)
        except (TypeError, ValueError):
            continue
        is_home = hn.lower() == team_name.lower()
        scored = hg if is_home else ag
        conceded = ag if is_home else hg
        result.append({
            "date": ev.get("startTimestamp") or ev.get("start_timestamp"),
            "league": (ev.get("tournament") or {}).get("name", ""),
            "country": "",
            "home": hn,
            "away": an,
            "score": f"{hg}-{ag}",
            "team_side": "home" if is_home else "away",
            "team_goals": scored,
            "opponent_goals": conceded,
            "result": "W" if scored > conceded else "D" if scored == conceded else "L",
        })
    return result


def _step_team_history(doc: dict, model: str, timeout: int = 25) -> str:
    try:
        home, away = _teams(doc)
        if not home or not away:
            return "Team history unavailable because team names are missing."
        # Primary: query SQLite finished matches
        with db_conn(timeout=10) as conn:
            home_matches = _previous_matches_for_team(home, conn)
            away_matches = _previous_matches_for_team(away, conn)
        # Fallback: use SofaScore last_matches arrays already in the doc
        if not home_matches:
            raw = doc.get("home_last_matches") or (doc.get("sofascore_detail") or {}).get("home_last_matches") or []
            home_matches = _parse_sofa_last_matches(raw, home)
        if not away_matches:
            raw = doc.get("away_last_matches") or (doc.get("sofascore_detail") or {}).get("away_last_matches") or []
            away_matches = _parse_sofa_last_matches(raw, away)
        evidence = _team_history_summary(home, home_matches) + " " + _team_history_summary(away, away_matches)
        prompt = (
            "You are the dedicated Team Previous Matches Analyst. Your trained knowledge is team trend reading: "
            "recent W-D-L, goals for/against, BTTS, over/under profile, home/away context, and opponent quality. "
            "Use only the supplied previous-match evidence and give one cautious prediction insight. "
            f"Evidence: {evidence}"
        )
        return _call_llm(model, prompt, timeout) or evidence
    except Exception as exc:
        logger.warning("AI step team_history failed: %s", exc)
        return "Team previous-match history analyst unavailable."


def derive_team_profile(team_name: str, conn: sqlite3.Connection) -> TeamBehaviourProfile:
    history = _history_for_team(team_name, conn)
    n = len(history); now = datetime.now(timezone.utc).isoformat()
    if n < 3: return TeamBehaviourProfile(team_name=team_name, sample_size=n, computed_at=now)
    scored = [x[0] for x in history]; conceded = [x[1] for x in history]
    return TeamBehaviourProfile(team_name, sum(a > 0 and b > 0 for a,b,_ in history)/n, sum(a+b > 2 for a,b,_ in history)/n,
        sum(b == 0 for b in conceded)/n, 0.0, int(sum(a >= 2 for a in scored)/n >= .6), sum(a == 0 and b > 0 for a,b,_ in history)/n, n, now)


def persist_team_profile(profile: TeamBehaviourProfile, conn: sqlite3.Connection) -> None:
    conn.execute("""insert or replace into team_behaviour_profiles values (?, ?, ?, ?, ?, ?, ?, ?, ?)""", tuple(asdict(profile).values()))


def shortlist_markets(home: TeamBehaviourProfile, away: TeamBehaviourProfile) -> list[MarketCandidate]:
    one_x_two = [MarketCandidate("home_win", "Home win", .5 + (home.clean_sheet_rate-away.loss_to_nil_rate)/2), MarketCandidate("draw", "Draw", .55), MarketCandidate("away_win", "Away win", .5 + (away.clean_sheet_rate-home.loss_to_nil_rate)/2)]
    if min(home.sample_size, away.sample_size) < 3: return sorted(one_x_two, key=lambda x: x.score, reverse=True)[:3]
    both = (home.btts_rate + away.btts_rate)/2; over = (home.over_2_5_rate + away.over_2_5_rate)/2
    candidates = one_x_two + [MarketCandidate("btts_yes", "Both teams to score", both), MarketCandidate("over_2_5", "Over 2.5 goals", over), MarketCandidate("under_2_5", "Under 2.5 goals", 1-over), MarketCandidate("btts_no", "BTTS No", 1-both)]
    selected = [item for item in candidates if item.score >= .55]
    if not any(item.market_key in {"home_win", "draw", "away_win"} for item in selected): selected.append(max(one_x_two, key=lambda x: x.score))
    return sorted(selected, key=lambda x: x.score, reverse=True)[:5]


def _truncate_competition_context(text: str) -> str:
    """Truncate at nearest sentence boundary at or before 100-token limit."""
    if len(text) // 4 <= 100:
        return text
    target = 400  # 100 tokens * 4 chars
    truncated = text[:target]
    boundary = truncated.rfind(". ")
    return (truncated[:boundary + 1] if boundary >= 0 else truncated).strip()


def _get_competition_context(doc: dict, conn) -> str | None:
    """Return truncated competition analysis text, or None on any failure."""
    try:
        key = (
            (doc.get("competition_special") or {}).get("key")
            or (doc.get("known_competition") or {}).get("key")
        )
        if not key:
            return None
        from app.competition.competition_analyser import get_latest_analysis, init_competition_analysis_table
        init_competition_analysis_table(conn)
        row = get_latest_analysis(key, conn)
        if not row:
            return None
        text = row.get("analysis_text") or ""
        if not text:
            return None
        truncated = _truncate_competition_context(text)
        logger.debug(
            "_get_competition_context: key=%s generated_at=%s",
            key, row.get("generated_at"),
        )
        return truncated
    except Exception as exc:
        logger.debug("_get_competition_context failed (non-critical): %s", exc)
        return None


def _call_decider(response_chain: list[str], home_profile: TeamBehaviourProfile, away_profile: TeamBehaviourProfile, shortlisted_markets: list[MarketCandidate], similar_match_history: Any, match_name: str, model: str, timeout: int = 45, competition_context: str | None = None, specialist_weights: dict[str, float] | None = None) -> dict:
    from app.ai.ai_router import get_router, parse_json_response
    context_block = f"Competition context: {competition_context} | " if competition_context else ""
    unavailable = [
        label for label, statement in zip(
            ("H2H", "common opponents", "form", "odds", "similar matches", "team previous matches"),
            response_chain,
        ) if _evidence_status(statement) == "unavailable"
    ]
    # Build weighted analyst block so the AI agent knows which specialists to trust more
    weights = specialist_weights or {}
    analyst_labels = [
        "H2H Analyst", "Common Opponent Analyst", "Form Analyst",
        "Market Odds Analyst", "Similar Match Analyst", "Team Previous Matches Analyst",
    ]
    weighted_evidence = [
        {
            "analyst": analyst_labels[i],
            "finding": response_chain[i],
            "weight": round(weights.get(analyst_labels[i], 1.0), 3),
            "available": _evidence_status(response_chain[i]) == "available",
        }
        for i in range(len(response_chain))
    ]
    prompt = (
        f"Decide football prediction for {match_name}. {context_block}"
        f"Weighted analyst findings (weight reflects historical accuracy — higher = more reliable): "
        f"{json.dumps(weighted_evidence, default=str)[:600]}. "
        f"Unavailable evidence: {unavailable or 'none'}. Do not present unavailable evidence as a positive factor. "
        f"Markets: {[asdict(x) for x in shortlisted_markets]}. "
        "Return JSON only with market,outcome,confidence,value_bet,btts,over_2_5,key_factors,reasoning."
    )
    started = time.monotonic()
    raw = get_router().call_analysis(prompt)
    logger.debug("AI decider elapsed_ms=%d", (time.monotonic()-started)*1000)
    parsed = parse_json_response(raw)
    required = {"market", "outcome", "confidence", "value_bet", "btts", "over_2_5", "key_factors", "reasoning"}
    if not required <= parsed.keys(): raise ValueError("Decider response missing required keys")
    return parsed


def _convert_confidence(raw: float) -> int:
    return max(0, min(100, round(float(raw) * 100)))


def _rules_fallback(doc: dict, reason: str, **kwargs: Any) -> dict:
    from app.utils.prediction_flow import apply_prediction_state
    logger.warning("Rules-engine fallback invoked: %s", reason)
    kwargs["use_llm_pipeline"] = False
    result = apply_prediction_state(doc, **kwargs)
    result["prediction_source"] = "rules_engine_fallback"
    if isinstance(result.get("prediction"), dict): result["prediction"]["prediction_source"] = "rules_engine_fallback"
    return result


def run_ai_prediction_with_fallback(doc: dict[str, Any], *, match_id: str | None = None, match_date: str | None = None, source: str = "enriched_ensemble", attach_brain: bool = False, allow_repeat: bool = False, use_llm_pipeline: bool | None = None) -> dict[str, Any]:
    kwargs = dict(
        match_id=match_id,
        match_date=match_date,
        source=source,
        attach_brain=attach_brain,
        allow_repeat=allow_repeat,
        use_llm_pipeline=True if use_llm_pipeline is None else use_llm_pipeline,
    )
    try:
        from app.ai.ai_router import get_router
        router = get_router()
        model = router.best_available()
        if not model:
            result = _rules_fallback(doc, "unavailable", **kwargs)
            result["competition_analysis_used"] = False
            result["competition_analysis_key"] = None
            return result
        doc["low_value_odds"] = _best_odds(doc) < 1.3
        logger.info("AI pipeline match=%s sportybet_id=%s tier=%s odds=%s low_value=%s", _name(doc), doc.get("sportybet_id"), classify_tournament_tier(_tournament(doc)), _best_odds(doc), doc["low_value_odds"])
        _init_db()
        competition_context: str | None = None
        competition_analysis_key: str | None = None
        with db_conn(timeout=20) as conn:
            home, away = _teams(doc); hp, ap = derive_team_profile(home, conn), derive_team_profile(away, conn)
            persist_team_profile(hp, conn); persist_team_profile(ap, conn); conn.commit()
            competition_context = _get_competition_context(doc, conn)
            if competition_context:
                competition_analysis_key = (
                    (doc.get("competition_special") or {}).get("key")
                    or (doc.get("known_competition") or {}).get("key")
                )
        # Run all 5 evidence steps in parallel — they are fully independent.
        # Sequential execution was the single biggest AI pipeline bottleneck
        # (5 × 20s timeout = up to 100s; parallel = ~20s worst case).
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        _steps = [
            (_step_h2h,             H2H_FALLBACK,     0),
            (_step_common_opponent, COMMON_FALLBACK,  1),
            (_step_form,            FORM_FALLBACK,    2),
            (_step_odds,            ODDS_FALLBACK,    3),
            (_step_similar_matches, SIMILAR_FALLBACK, 4),
            (_step_team_history,     "Team previous-match history unavailable.", 5),
        ]
        chain = [fallback for _, fallback, _ in _steps]  # pre-fill with fallbacks
        with ThreadPoolExecutor(max_workers=len(_steps)) as _pool:
            _futures = {
                _pool.submit(fn, doc, model): (fallback, idx)
                for fn, fallback, idx in _steps
            }
            for _future in _as_completed(_futures):
                _fallback, _idx = _futures[_future]
                try:
                    sentence = _future.result()
                    chain[_idx] = sentence or _fallback
                    logger.debug("AI step idx=%d: %s", _idx, chain[_idx])
                except Exception as exc:
                    logger.warning("AI step idx=%d failed: %s", _idx, exc)
        markets = shortlist_markets(hp, ap); logger.debug("Response chain: %s", chain)
        specialist_weights = get_specialist_weights(league=_tournament(doc))
        decision = _call_decider(chain, hp, ap, markets, [], _name(doc), model, competition_context=competition_context, specialist_weights=specialist_weights)
        from app.utils.prediction_flow import apply_prediction_state

        result = apply_prediction_state(doc, **kwargs)
        if result.get("status") != "predicted": return result
        prediction = result["prediction"]
        reasoning_context = {
            **asdict(ReasoningContext(_name(doc), *chain)),
            "response_chain": chain,
            "evidence_availability": {
                label: _evidence_status(statement)
                for label, statement in zip(
                    ("h2h", "common_opponents", "form", "odds", "similar_matches", "team_previous_matches"),
                    chain,
                )
            },
            "analysts": [
                {"name": "H2H Analyst",                   "trained_knowledge": "Historical meetings and rivalry pattern reading",          "finding": chain[0], "evidence_status": _evidence_status(chain[0]), "weight": specialist_weights.get("H2H Analyst", 1.0)},
                {"name": "Common Opponent Analyst",        "trained_knowledge": "Shared-opponent performance comparison",                   "finding": chain[1], "evidence_status": _evidence_status(chain[1]), "weight": specialist_weights.get("Common Opponent Analyst", 1.0)},
                {"name": "Form Analyst",                   "trained_knowledge": "Standings, recent form, ratings, and table pressure",      "finding": chain[2], "evidence_status": _evidence_status(chain[2]), "weight": specialist_weights.get("Form Analyst", 1.0)},
                {"name": "Market Odds Analyst",            "trained_knowledge": "Odds movement, pricing pressure, and market signal quality","finding": chain[3], "evidence_status": _evidence_status(chain[3]), "weight": specialist_weights.get("Market Odds Analyst", 1.0)},
                {"name": "Similar Match Analyst",          "trained_knowledge": "Tier-comparable historical match outcomes",                 "finding": chain[4], "evidence_status": _evidence_status(chain[4]), "weight": specialist_weights.get("Similar Match Analyst", 1.0)},
                {"name": "Team Previous Matches Analyst",  "trained_knowledge": "Both teams' full recent finished-match profiles",          "finding": chain[5], "evidence_status": _evidence_status(chain[5]), "weight": specialist_weights.get("Team Previous Matches Analyst", 1.0)},
            ],
        }
        prediction.update({
            "prediction_source": "llm_pipeline",
            "ai_provider": model,
            "reasoning_context": reasoning_context,
            "market": decision["market"],
            "outcome": decision["outcome"],
            "key_factors": decision["key_factors"],
            "reasoning": decision["reasoning"],
            "confidence": _convert_confidence(decision["confidence"]),
            "value_bet": decision["value_bet"],
            "btts": decision["btts"],
            "over_2_5": decision["over_2_5"],
        })
        result["prediction_source"] = "llm_pipeline"
        result["reasoning_context"] = prediction["reasoning_context"]
        result["competition_analysis_used"] = competition_context is not None
        result["competition_analysis_key"] = competition_analysis_key
        logger.info("AI pipeline completed match=%s outcome=%s", _name(doc), decision["outcome"])
        return result
    except Exception as exc:
        logger.exception("AI pipeline failed: %s", exc)
        result = _rules_fallback(doc, "exception", **kwargs)
        result["competition_analysis_used"] = False
        result["competition_analysis_key"] = None
        return result


def job_ai_prediction_queue(batch_size: int = 10) -> dict:
    from app.utils.activity_log import record_activity
    from app.scheduling.pipeline_registry import is_pipeline_enabled
    from app.storage.buffer import get_buffered_match as _get_buffered_match, store_enriched as _store
    if not is_pipeline_enabled("ai_prediction_queue"):
        return {"status": "skipped", "reason": "pipeline_disabled"}
    record_activity("AI prediction queue started", job="ai_prediction_queue", status="running")
    _init_db()
    summary = {"status": "ok", "job": "job_ai_prediction_queue", "batch_size": batch_size, "processed": 0, "llm_used": 0, "fallback_used": 0, "errors": 0}
    with db_conn(timeout=30) as conn:
        rows = conn.execute(
            """
            select match_id, match_date, raw_enriched
            from match_buffer
            where raw_enriched is not null
              and is_finished = 0
              and json_extract(raw_enriched, '$.sofascore_match_status') != 'srl_skip'
              and (
                    json_extract(raw_enriched, '$.manual_prediction_state') is not null
                 or json_extract(raw_enriched, '$.prediction') is not null
                 or json_extract(raw_enriched, '$.ai_prediction_queue_pending') = 1
                 or (
                      -- Pick up any enriched match that has no prediction yet
                      json_extract(raw_enriched, '$.enriched_at') is not null
                      and json_extract(raw_enriched, '$.prediction') is null
                      and json_extract(raw_enriched, '$.prediction_error') is null
                    )
              )
              and json_extract(raw_enriched, '$.ai_prediction_state') is null
            """,
        ).fetchall()
    docs = []
    for match_id, date, raw in rows:
        try:
            docs.append({**json.loads(raw), "match_id": match_id, "match_date": date})
        except Exception:
            summary["errors"] += 1
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac

    def _process_one(doc: dict) -> dict:
        if not doc.get("prediction_readiness") or not doc.get("sofascore_detail"):
            try:
                from app.enrichment.match_enrichment import enrich_buffered_match
                enrich_buffered_match(str(doc.get("match_id") or ""), auto_predict=False)
                refreshed = _get_buffered_match(str(doc.get("match_id") or ""))
                if refreshed:
                    doc.update(refreshed)
            except Exception:
                pass
        outcome = run_ai_prediction_with_fallback(
            doc,
            match_id=str(doc.get("match_id") or ""),
            match_date=doc.get("match_date"),
            use_llm_pipeline=True,
        )
        doc["ai_prediction_queue_pending"] = False
        _store(str(doc.get("match_id") or ""), doc)
        return outcome

    with _TPE(max_workers=3) as pool:
        futures = {pool.submit(_process_one, doc): doc for doc in sort_gate(docs)[:batch_size]}
        for future in _ac(futures):
            try:
                outcome = future.result()
                summary["processed"] += 1
                summary["llm_used" if outcome.get("prediction_source") == "llm_pipeline" else "fallback_used"] += 1
            except Exception as exc:
                logger.exception("AI queue match failed: %s", exc)
                summary["errors"] += 1
    record_activity("AI prediction queue completed", job="ai_prediction_queue", status="ok" if not summary["errors"] else "error", details=summary)
    return summary


