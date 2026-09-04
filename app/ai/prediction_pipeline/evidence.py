"""The six independent evidence-gathering steps, plus the match/odds/tier
helpers they and the rest of the pipeline share.

Each ``_step_*`` function is deliberately independent: a missing source or
model failure produces a useful fallback sentence and never raises, so a
single flaky step can never take down the whole pipeline (see
orchestration.py, which runs all six in parallel with its own deadline).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from app.storage.db import db_conn
from app.market.season_stage import detect_season_stage
from app.ai.ai_router import _call_llm

logger = logging.getLogger(__name__)

H2H_FALLBACK = "No H2H history found between these teams."
COMMON_FALLBACK = "No common opponents found in recent history."
FORM_FALLBACK = "Standings data unavailable; form data not present."
ODDS_FALLBACK = "Odds movement data unavailable."
SIMILAR_FALLBACK = "No tier-comparable similar matches found."


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
    try:
        from app.monitoring.self_learner import get_tournament_priority
        priority = int(get_tournament_priority(tournament_name).get("priority", 4))
    except Exception:
        priority = 4
    return priority


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
