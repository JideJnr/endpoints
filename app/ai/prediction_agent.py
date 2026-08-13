from __future__ import annotations

from statistics import mean
from typing import Any

from app.utils.match_state import classify_match_state
from app.market.season_stage import (
    classify_table_size,
    detect_season_stage,
    season_aware_table_weight,
)

from app.storage.league_memory import late_goal_memory_signal
from app.competition.league_strength import league_strength_edge
from app.storage.league_memory._helpers import build_pick as _pick


from app.utils.primitives import _to_int, _to_float
from app.utils.match_helpers import _fraction_to_probability, _tournament_name, _team_name, _norm

HIGH_LATE_GOAL_LEAGUES: tuple[str, ...] = ()

import logging as _logging
_pa_logger = _logging.getLogger(__name__)
if not HIGH_LATE_GOAL_LEAGUES:
    _pa_logger.warning(
        "HIGH_LATE_GOAL_LEAGUES is empty -- late-goal boost logic will not activate "
        "until leagues are populated via get_tournament_priority()."
    )

import logging as _logging
_pa_logger = _logging.getLogger(__name__)
if not HIGH_LATE_GOAL_LEAGUES:
    _pa_logger.warning(
        "HIGH_LATE_GOAL_LEAGUES is empty -- late-goal boost logic will not activate "
        "until leagues are populated via get_tournament_priority()."
    )


# ── Time-decay for live confidence ───────────────────────────────────────────
#
# As a match progresses, the window for a prediction to materialise shrinks.
# We apply a decay multiplier to confidence based on the current minute:
#
#   0–45  min  → no decay (full confidence)
#   46–59 min  → mild decay  (×0.90)
#   60–69 min  → moderate    (×0.80)
#   70–79 min  → strong      (×0.70)
#   80–84 min  → heavy       (×0.60)
#   85–89 min  → very heavy  (×0.50)
#   90+   min  → minimal     (×0.35)
#
# Exception: live_goals / late_goal picks in high-late-goal leagues are
# BOOSTED instead of decayed (the window is exactly what we're betting on).

_DECAY_BRACKETS = [
    (90, 0.35),
    (85, 0.50),
    (80, 0.60),
    (70, 0.70),
    (60, 0.80),
    (46, 0.90),
    (0,  1.00),
]


def _time_decay_multiplier(minute: int) -> float:
    for threshold, multiplier in _DECAY_BRACKETS:
        if minute >= threshold:
            return multiplier
    return 1.0


def _apply_time_decay(
    picks: list[dict[str, Any]],
    minute: int,
    is_live: bool,
    late_goal_league: bool,
) -> list[dict[str, Any]]:
    """Apply time-decay to confidence for live matches."""
    if not is_live or minute < 46:
        return picks

    decay = _time_decay_multiplier(minute)
    result = []
    for pick in picks:
        kind = pick.get("type", "")
        conf = pick["confidence"]

        # Late-goal picks in high-late-goal leagues: boost instead of decay
        if kind in ("live_goals", "late_goal") and late_goal_league:
            # Slight boost for being in the prime late-goal window
            boost = 1.05 if minute >= 70 else 1.0
            new_conf = max(1, min(95, round(conf * boost)))
        else:
            new_conf = max(1, min(95, round(conf * decay)))

        result.append({**pick, "confidence": new_conf})
    return result


def predict_sofascore_event(
    event: dict[str, Any],
    home_history: list[dict[str, Any]] | None = None,
    away_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    home_history = home_history or []
    away_history = away_history or []
    home = event.get("home_team") or event.get("homeTeam") or {}
    away = event.get("away_team") or event.get("awayTeam") or {}
    status = event.get("status", {})
    score = event.get("score", {})
    raw_tournament = event.get("tournament", {})
    tournament = raw_tournament if isinstance(raw_tournament, dict) else {}
    tournament_name = tournament.get("name") or (raw_tournament if isinstance(raw_tournament, str) else "")

    signals: list[dict[str, Any]] = []
    picks: list[dict[str, Any]] = []

    home_form = _team_history_features(home.get("id"), home_history)
    away_form = _team_history_features(away.get("id"), away_history)
    form_edge = _form_edge(event, home_form, away_form, signals)
    league_edge = _league_strength_edge(event, home_history, away_history, signals)
    h2h_edge = _h2h_edge(event, signals)
    table_edge = _table_edge(event, signals, event.get("standings") or event.get("league_table") or [])
    odds_edge = _odds_edge(event, signals)
    common_opp_edge = _common_opponent_edge(
        home.get("name") or "",
        away.get("name") or "",
        home_history,
        away_history,
        signals,
        event.get("standings") or event.get("league_table") or [],
    )

    total_goals = _to_int(score.get("home"), 0) + _to_int(score.get("away"), 0)
    minute = _event_minute(event)
    is_live = status.get("type") == "inprogress"
    late_goal_league = _is_high_late_goal_league(tournament_name)
    memory_signal = late_goal_memory_signal(event)
    memory_boost = _late_goal_memory_boost(memory_signal, signals)

    # Form trajectory — opponent-quality-weighted trend (last 3 vs previous 3)
    standings = event.get("standings") or event.get("league_table") or []
    home_trajectory = form_trajectory_signal(home.get("id"), home_history, standings, side="home")
    away_trajectory = form_trajectory_signal(away.get("id"), away_history, standings, side="away")
    if home_trajectory["available"] and home_trajectory["trajectory"] != "flat":
        signals.append({
            "name": "home_form_trajectory",
            "value": home_trajectory,
            "impact": home_trajectory["impact"],
        })
    if away_trajectory["available"] and away_trajectory["trajectory"] != "flat":
        # Away improving hurts home; away declining helps home
        signals.append({
            "name": "away_form_trajectory",
            "value": away_trajectory,
            "impact": -away_trajectory["impact"],
        })

    home_power = form_edge + league_edge + h2h_edge + table_edge + odds_edge + common_opp_edge

    # Team watch signal — opponent tier, goal timing, signal combo history
    try:
        from app.team_watcher.team_watcher import team_watch_signal as _tw_signal
        tw = _tw_signal(event)
        if tw and tw.get("value", {}).get("available"):
            home_power += float(tw.get("impact") or 0)
            signals.append(tw)
    except Exception:
        pass
    if abs(home_power) >= 8:
        side = _side_name(home if home_power > 0 else away, event, "home" if home_power > 0 else "away")
        picks.append(_pick("match_result", f"{side} Win", 55 + min(abs(home_power), 25), "stronger side has a decisive edge"))
    elif abs(home_power) >= 4:
        side = _side_name(home if home_power > 0 else away, event, "home" if home_power > 0 else "away")
        # Try signal aggregator for directional pick with high odds + proven history.
        # If no directional pick qualifies, return no_bet — the logic is strong
        # enough to make a pick or no pick, no double chance fallback needed.
        try:
            from app.enrichment.signal_aggregator import SignalAggregator
            from app.risk.fallback_logic import FallbackHandler

            aggregator = SignalAggregator()
            aggregator.add_signal("home_form", 0.6 if home_power > 0 else 0.4, source="rules")
            aggregator.add_signal("away_form", 0.4 if home_power > 0 else 0.6, source="rules")
            aggregator.add_signal("h2h_home", 0.5 + abs(home_power) / 100, source="rules")
            aggregator.add_signal("h2h_away", 0.5 - abs(home_power) / 100, source="rules")
            aggregator.add_signal("home_odds", 50 + abs(home_power) * 5, source="rules")
            aggregator.add_signal("away_odds", 50 - abs(home_power) * 5, source="rules")

            sig_probs = aggregator.calculate_probabilities()
            handler = FallbackHandler()
            fallback = handler.get_fallback_pick(
                signals=aggregator.signals,
                odds={"home": 1.8, "draw": 3.2, "away": 4.5},
                prob_result=sig_probs,
            )
            if fallback and fallback.get("type") != "no_bet" and fallback.get("confidence", 0) >= 40:
                picks.append(_pick(
                    "match_result",
                    fallback.get("selection", f"{side} Win"),
                    max(52, int(fallback.get("confidence", 52))),
                    f"signal aggregator directional: {fallback.get('selection')} (odds {fallback.get('odds', 0):.2f})",
                ))
            else:
                picks.append(_pick("no_bet", "No strong bet", 50, "signal aggregator could not produce a directional pick with sufficient confidence"))
        except Exception:
            picks.append(_pick("no_bet", "No strong bet", 50, "signal aggregator failed — no directional pick available"))

    goal_pressure = _goal_pressure(home_form, away_form, event, signals)
    live_chase_pressure = _live_chase_pressure(event, home_power, goal_pressure, signals)
    btts_pressure = _btts_pressure(home_form, away_form)
    home_over35 = float(home_form.get("over_3_5_rate") or 0)
    away_over35 = float(away_form.get("over_3_5_rate") or 0)
    avg_total = (
        float(home_form.get("avg_total_goals") or 0) + float(away_form.get("avg_total_goals") or 0)
    ) / 2
    four_goal_rate = (home_over35 + away_over35) / 2
    calm_under_env = (
        home_form.get("sample_size")
        and away_form.get("sample_size")
        and goal_pressure <= 12
        and avg_total <= 2.65
        and four_goal_rate <= 0.22
    )
    if calm_under_env:
        confidence = 61 + min(10, max(0, 12 - int(goal_pressure))) + min(4, int((0.25 - four_goal_rate) * 20))
        picks.append(_pick("goals", "Under 3.5 goals", confidence, "recent finals are low and four-goal volatility is limited"))
    if goal_pressure <= 3 and home_form.get("sample_size") and away_form.get("sample_size"):
        picks.append(_pick("goals", "Under 2.5 goals", 57 + min(10, 5 - int(goal_pressure)), "low scoring and conceding profile from previous matches"))
    if goal_pressure >= 10:
        picks.append(_pick("goals", "Over 1.5 goals", 60 + min(goal_pressure, 20), "both teams show goal trend"))
    if goal_pressure >= 18:
        picks.append(_pick("goals", "Over 2.5 goals", 52 + min(goal_pressure, 22), "high combined scoring/conceding trend"))
    if btts_pressure >= 12:
        picks.append(_pick("goals", "Both teams to score", 52 + min(btts_pressure, 18), "both sides regularly score and concede"))
    elif btts_pressure <= 5 and home_form.get("sample_size") and away_form.get("sample_size"):
        picks.append(_pick("goals", "Both teams to score - No", 56 + min(10, 6 - int(btts_pressure)), "recent finals do not support both teams scoring"))

    # Fallback goal pick from odds when no form data
    if not picks and not home_form.get("sample_size"):
        markets = event.get("sportybet_markets") or event.get("markets") or []
        for mkt in markets:
            if (mkt.get("name") or "").lower() in ("over/under", "total goals") or mkt.get("id") == "18":
                for sel in mkt.get("selections", []):
                    if "over 2.5" in (sel.get("name") or "").lower():
                        dec = _to_float(sel.get("odds"))
                        if dec and 1.5 <= dec <= 2.2:
                            picks.append(_pick("goals", "Over 2.5 goals", 55, "market prices over 2.5 as likely"))
                        break
                break

    if is_live and minute >= 70 and late_goal_league and total_goals <= 2:
        picks.append(_pick("live_goals", "Late goal watch", 61 + memory_boost, "league profile plus learned late-goal memory"))
        signals.append({"name": "late_goal_league", "value": tournament_name, "impact": 7})

    if live_chase_pressure >= 12:
        picks.append(_pick("live_goals", "Next goal / Over 0.5 live", 62 + min(live_chase_pressure, 12) + memory_boost, "close scoreline plus chasing pressure"))

    if is_live and total_goals == 0 and minute >= 55:
        picks.append(_pick("live_goals", "Over 0.5 live", 57, "0-0 after halftime creates one-goal value window"))

    red_card_signal = _red_card_signal(event, home_power)
    if red_card_signal:
        picks.append(red_card_signal)
        signals.append({"name": "red_card_state", "value": red_card_signal["selection"], "impact": 10})

    if not picks:
        picks.append(_pick("no_bet", "No strong bet", 50, "not enough edge from available data"))

    picks = _apply_time_decay(picks, minute, is_live, late_goal_league)

    return {
        "match_id": event.get("id"),
        "name": event.get("name"),
        "source": "sofascore",
        "status": status,
        "minute": minute,
        "score": score,
        "tournament": tournament,
        "teams": {"home": home, "away": away},
        "features": {"home": home_form, "away": away_form},
        "signals": sorted(signals, key=lambda s: abs(s.get("impact", 0)), reverse=True),
        "picks": sorted(picks, key=lambda p: p["confidence"], reverse=True),
        "time_decay_applied": is_live and minute >= 46,
        "time_decay_multiplier": _time_decay_multiplier(minute) if is_live else 1.0,
    }


def predict_sporty_match(match: dict[str, Any]) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    picks: list[dict[str, Any]] = []
    minute = _sporty_minute(match.get("played_seconds"))
    score = match.get("score", {})
    home_goals = _to_int(score.get("home"), 0)
    away_goals = _to_int(score.get("away"), 0)
    total_goals = home_goals + away_goals
    tournament = match.get("tournament") or ""
    category = match.get("category") or ""
    is_live_sporty = bool(classify_match_state(match).get("is_live"))
    odds = _sporty_main_odds(match)
    memory_signal = late_goal_memory_signal(match)
    memory_boost = _late_goal_memory_boost(memory_signal, signals)

    if odds:
        fav = max(odds, key=lambda item: item["implied_probability"])
        signals.append({"name": "market_favorite", "value": fav["name"], "impact": round(fav["implied_probability"] * 20, 2)})
        if is_live_sporty and fav["implied_probability"] >= 0.58:
            picks.append(_pick("market_value", f"{fav['name']} side protection", 58 + int((fav["implied_probability"] - 0.58) * 60), "market shows clear favorite"))

    if minute >= 70 and total_goals <= 2 and _is_high_late_goal_league(f"{category} {tournament}"):
        picks.append(_pick("live_goals", "Late goal watch", 60 + memory_boost, "league profile, late match state, and learned memory"))
        signals.append({"name": "late_goal_window", "value": f"{minute}' with {total_goals} goals", "impact": 8})

    if minute >= 55 and total_goals == 0:
        picks.append(_pick("live_goals", "Over 0.5 live", 56, "goalless second half window"))

    red_card_pick = _red_card_signal(match, _sporty_market_edge(odds))
    if red_card_pick:
        picks.append(red_card_pick)
        signals.append({"name": "red_card_state", "value": red_card_pick["selection"], "impact": 10})

    if not picks:
        picks.append(_pick("no_bet", "No strong bet", 50, "not enough edge from available live data"))

    late_goal_league_sporty = _is_high_late_goal_league(f"{category} {tournament}")
    picks = _apply_time_decay(picks, minute, is_live_sporty, late_goal_league_sporty)

    return {
        "match_id": match.get("id"),
        "name": match.get("name"),
        "source": "sportybet",
        "period": match.get("period"),
        "minute": minute,
        "score": score,
        "tournament": tournament,
        "category": category,
        "signals": sorted(signals, key=lambda s: abs(s.get("impact", 0)), reverse=True),
        "picks": sorted(picks, key=lambda p: p["confidence"], reverse=True),
        "time_decay_applied": is_live_sporty and minute >= 46,
        "time_decay_multiplier": _time_decay_multiplier(minute) if is_live_sporty else 1.0,
    }


def _team_history_features(team_id: int | None, history: list[dict[str, Any]]) -> dict[str, Any]:
    finished = [m for m in history if m.get("status", {}).get("type") == "finished"][:10]
    if not team_id or not finished:
        return {"sample_size": 0}

    goals_for = []
    goals_against = []
    results = []
    btts = []
    over_25 = []
    over_35 = []
    totals = []
    for match in finished:
        is_home = match.get("home_team", {}).get("id") == team_id
        home_goals = _to_int(match.get("score", {}).get("home"), 0)
        away_goals = _to_int(match.get("score", {}).get("away"), 0)
        total_goals = home_goals + away_goals
        gf = home_goals if is_home else away_goals
        ga = away_goals if is_home else home_goals
        goals_for.append(gf)
        goals_against.append(ga)
        btts.append(gf > 0 and ga > 0)
        over_25.append(total_goals > 2)
        over_35.append(total_goals > 3)
        totals.append(total_goals)
        results.append("W" if gf > ga else "D" if gf == ga else "L")

    return {
        "sample_size": len(finished),
        "avg_goals_for": round(mean(goals_for), 2),
        "avg_goals_against": round(mean(goals_against), 2),
        "avg_total_goals": round(mean(totals), 2),
        "over_2_5_rate": round(sum(over_25) / len(over_25), 2),
        "over_3_5_rate": round(sum(over_35) / len(over_35), 2),
        "btts_rate": round(sum(btts) / len(btts), 2),
        "form": results,
        "form_points": sum(3 if r == "W" else 1 if r == "D" else 0 for r in results),
    }


def _common_opponent_edge(
    home_name: str,
    away_name: str,
    home_history: list[dict[str, Any]],
    away_history: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    standings: list[dict[str, Any]] | None = None,
) -> float:
    """
    Find opponents both teams have faced recently and compare results.
    e.g. if both played Barca: home team won 2-0, away team lost 0-3 → strong home edge.

    Scoring per shared opponent:
      Win   = +3 pts,  Draw = +1,  Loss = 0
      Results against better current league-table opponents count more than
      results against teams near the bottom.  This avoids treating a win over
      first place as identical to a win over last place.
      Edge  = table-weighted (home_pts - away_pts) across shared opponents
      Scaled to max ±12 and added to home_power.
    """
    def _opp_name(event: dict[str, Any], team: str) -> str:
        h = event.get("home_team", {}).get("name") or event.get("homeTeam", {}).get("name") or ""
        a = event.get("away_team", {}).get("name") or event.get("awayTeam", {}).get("name") or ""
        return a if h.lower() == team.lower() else h

    def _result_pts(event: dict[str, Any], team: str) -> int:
        h = event.get("home_team", {}).get("name") or event.get("homeTeam", {}).get("name") or ""
        score = event.get("score") or {}
        hs = _to_int(score.get("home"), -1)
        as_ = _to_int(score.get("away"), -1)
        if hs < 0 or as_ < 0:
            return -1
        is_home = h.lower() == team.lower()
        own = hs if is_home else as_
        opp = as_ if is_home else hs
        if own > opp: return 3
        if own == opp: return 1
        return 0

    def _goal_diff(event: dict[str, Any], team: str) -> int:
        h = event.get("home_team", {}).get("name") or event.get("homeTeam", {}).get("name") or ""
        score = event.get("score") or {}
        hs = _to_int(score.get("home"), 0)
        as_ = _to_int(score.get("away"), 0)
        return (hs - as_) if h.lower() == team.lower() else (as_ - hs)

    def _norm(name: str) -> str:
        return " ".join("".join(ch if ch.isalnum() else " " for ch in name.lower()).split())

    table_by_id: dict[str, dict[str, Any]] = {}
    table_by_name: dict[str, dict[str, Any]] = {}
    table_size = len(standings or [])
    for row in standings or []:
        team = row.get("team") or {}
        name = str(team.get("name") or "")
        if not name:
            continue
        entry = {
            "position": _to_int(row.get("position"), 0),
            "points": _to_int(row.get("points"), 0),
            "team": name,
        }
        team_id = team.get("id")
        if team_id is not None:
            table_by_id[str(team_id)] = entry
        table_by_name[_norm(name)] = entry

    # Detect season stage so we don't treat 0-point / bottom-of-table
    # standings as meaningful when the season hasn't started or is just beginning.
    season_stage = detect_season_stage(standings)
    table_size_info = classify_table_size(standings)

    def _opponent_table(event: dict[str, Any], opponent: str) -> dict[str, Any] | None:
        home_team = event.get("home_team") or event.get("homeTeam") or {}
        away_team = event.get("away_team") or event.get("awayTeam") or {}
        for team in (home_team, away_team):
            if _norm(str(team.get("name") or "")) == _norm(opponent):
                team_id = team.get("id")
                if team_id is not None and str(team_id) in table_by_id:
                    return table_by_id[str(team_id)]
        return table_by_name.get(_norm(opponent))

    def _table_weight(entry: dict[str, Any] | None) -> float:
        # 1.0 when table data is unavailable; 1.45 for the leaders down to
        # 0.75 at the foot of a normal table.  The cap keeps a single result
        # from overpowering form, odds and H2H.
        #
        # When the season hasn't started or is just beginning, standings are
        # unreliable — all teams have 0 points and the table is meaningless.
        # We reduce the weight accordingly so table position doesn't dominate
        # the common-opponent comparison.
        if not entry or not entry.get("position") or table_size < 2:
            return 1.0
        position = int(entry["position"])
        return season_aware_table_weight(position, table_size, season_stage)

    # Build lookup: normalised opponent name → best result for each team
    home_opp: dict[str, dict] = {}
    for ev in home_history:
        if (ev.get("status") or {}).get("type") != "finished":
            continue
        opp = _opp_name(ev, home_name)
        if not opp:
            continue
        key = _norm(opp)
        pts = _result_pts(ev, home_name)
        if pts < 0:
            continue
        gd = _goal_diff(ev, home_name)
        rating = pts + (gd * 0.35)
        if key not in home_opp or rating > home_opp[key]["rating"]:
            home_opp[key] = {"pts": pts, "gd": gd, "rating": rating, "opp": opp, "event": ev}

    away_opp: dict[str, dict] = {}
    for ev in away_history:
        if (ev.get("status") or {}).get("type") != "finished":
            continue
        opp = _opp_name(ev, away_name)
        if not opp:
            continue
        key = _norm(opp)
        pts = _result_pts(ev, away_name)
        if pts < 0:
            continue
        gd = _goal_diff(ev, away_name)
        rating = pts + (gd * 0.35)
        if key not in away_opp or rating > away_opp[key]["rating"]:
            away_opp[key] = {"pts": pts, "gd": gd, "rating": rating, "opp": opp, "event": ev}

    shared_keys = set(home_opp) & set(away_opp)
    if not shared_keys:
        return 0.0

    home_total = 0
    away_total = 0
    home_gd = 0
    away_gd = 0
    home_rating = 0.0
    away_rating = 0.0
    comparisons = []
    for key in shared_keys:
        h_entry = home_opp[key]
        a_entry = away_opp[key]
        home_total += h_entry["pts"]
        away_total += a_entry["pts"]
        home_gd += h_entry["gd"]
        away_gd += a_entry["gd"]
        table = _opponent_table(h_entry["event"], h_entry["opp"]) or _opponent_table(a_entry["event"], a_entry["opp"])
        weight = _table_weight(table)
        home_rating += h_entry["rating"] * weight
        away_rating += a_entry["rating"] * weight
        comparisons.append({
            "opponent": h_entry["opp"],
            "home_pts": h_entry["pts"],
            "away_pts": a_entry["pts"],
            "home_goal_diff": h_entry["gd"],
            "away_goal_diff": a_entry["gd"],
            "opponent_table": table,
            "table_weight": weight,
            "winner": "home" if h_entry["rating"] > a_entry["rating"] else "away" if a_entry["rating"] > h_entry["rating"] else "even",
            "home_event": h_entry["event"],
            "away_event": a_entry["event"],
        })

    raw_edge = home_rating - away_rating
    edge = round(max(-12, min(12, raw_edge * 1.5)), 2)

    if abs(edge) >= 1.5:
        signals.append({
            "name": "common_opponent_edge",
            "value": {
                "shared_opponents": len(shared_keys),
                "home_points": home_total,
                "away_points": away_total,
                "home_goal_diff": home_gd,
                "away_goal_diff": away_gd,
                "home_rating": round(home_rating, 2),
                "away_rating": round(away_rating, 2),
                "season_stage": season_stage.get("stage"),
                "season_not_started": season_stage.get("season_not_started"),
                "season_beginning": season_stage.get("season_beginning"),
                "standings_meaningful": season_stage.get("standings_meaningful"),
                "table_size": table_size,
                "table_category": table_size_info.get("category"),
                "comparisons": [
                    {
                        "opponent": c["opponent"],
                        "home_result": _pts_label(c["home_pts"]),
                        "away_result": _pts_label(c["away_pts"]),
                        "home_goal_diff": c["home_goal_diff"],
                        "away_goal_diff": c["away_goal_diff"],
                        "opponent_table": c["opponent_table"],
                        "table_weight": c["table_weight"],
                        "winner": c["winner"],
                        "home_score": _score_str(c["home_event"]),
                        "away_score": _score_str(c["away_event"]),
                        "home_date": _event_date(c["home_event"]),
                        "away_date": _event_date(c["away_event"]),
                    }
                    for c in comparisons
                ],
            },
            "impact": edge,
        })

    return edge


def _pts_label(pts: int) -> str:
    return "W" if pts == 3 else "D" if pts == 1 else "L"


def _score_str(event: dict[str, Any]) -> str:
    score = event.get("score") or {}
    h = score.get("home")
    a = score.get("away")
    if h is None or a is None:
        return "?-?"
    return f"{h}-{a}"


def _event_date(event: dict[str, Any]) -> str:
    ts = event.get("start_timestamp") or event.get("startTimestamp") or event.get("start_time")
    if not ts:
        return ""
    try:
        t = float(ts)
        if t > 1e10:
            t /= 1000
        from datetime import datetime, timezone
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%d %b %Y")
    except Exception:
        return str(ts)[:10]


def _form_edge(event: dict[str, Any], home_form: dict[str, Any], away_form: dict[str, Any], signals: list[dict[str, Any]]) -> float:
    pregame = event.get("pregame_form") or {}
    home_pre = pregame.get("home_team") or {}
    away_pre = pregame.get("away_team") or {}
    edge = 0.0

    if home_form.get("sample_size") and away_form.get("sample_size"):
        edge += (home_form.get("form_points", 0) - away_form.get("form_points", 0)) * 0.8
        edge += (home_form.get("avg_goals_for", 0) - away_form.get("avg_goals_for", 0)) * 5
        edge -= (home_form.get("avg_goals_against", 0) - away_form.get("avg_goals_against", 0)) * 3
        signals.append({"name": "recent_history_edge", "value": round(edge, 2), "impact": round(edge, 2)})

    home_rating = _to_float(home_pre.get("avg_rating"))
    away_rating = _to_float(away_pre.get("avg_rating"))
    if home_rating is not None and away_rating is not None:
        rating_edge = (home_rating - away_rating) * 10
        edge += rating_edge
        signals.append({"name": "avg_rating_edge", "value": round(rating_edge, 2), "impact": round(rating_edge, 2)})

    return edge


def form_trajectory_signal(
    team_id: int | None,
    history: list[dict[str, Any]],
    standings: list[dict[str, Any]] | None = None,
    side: str = "home",
) -> dict[str, Any]:
    """
    Compute form trajectory (improving / declining / flat) weighted by opponent quality.
    Compares last 3 matches vs previous 3 matches.
    Returns a signal dict with impact and trajectory label.
    """
    finished = [m for m in (history or []) if (m.get("status") or {}).get("type") == "finished"][:8]
    if not team_id or len(finished) < 4:
        return {"available": False, "trajectory": "unknown", "impact": 0}

    # Build opponent position lookup from standings
    pos_by_id: dict[str, int] = {}
    pos_by_name: dict[str, int] = {}
    table_size = len(standings or [])
    for row in standings or []:
        team = row.get("team") or {}
        tid = str(team.get("id") or "")
        tname = str(team.get("name") or "").lower()
        pos = _to_int(row.get("position"), 0)
        if tid:
            pos_by_id[tid] = pos
        if tname:
            pos_by_name[tname] = pos

    # Detect season stage so opponent-quality weighting is reduced when
    # standings are unreliable (season not started / just beginning).
    season_stage = detect_season_stage(standings or [])
    table_size_info = classify_table_size(standings or [])
    # When standings are unreliable, flatten opponent weights toward 1.0
    # so form trajectory is driven by results, not meaningless table positions.
    opp_weight_scale = 1.0 if season_stage.get("standings_meaningful") else 0.3

    def _opp_weight(match: dict[str, Any]) -> float:
        """1.5 for top-quarter opponents, 0.6 for bottom-quarter, 1.0 otherwise.

        When the season hasn't started or is just beginning, standings are
        unreliable so we scale weights toward 1.0.  Small leagues also get
        adjusted thresholds because bottom positions are less significant.
        """
        if not table_size:
            return 1.0
        is_home = str((match.get("homeTeam") or match.get("home_team") or {}).get("id") or "") == str(team_id)
        opp = match.get("awayTeam") or match.get("away_team") if is_home else match.get("homeTeam") or match.get("home_team")
        if not isinstance(opp, dict):
            return 1.0
        opp_id = str(opp.get("id") or "")
        opp_name = str(opp.get("name") or "").lower()
        pos = pos_by_id.get(opp_id) or pos_by_name.get(opp_name) or 0
        if not pos:
            return 1.0
        percentile = pos / table_size
        # Adjust thresholds for small leagues: in a 4-team league,
        # the bottom team is still 25% of the table, not 75%.
        if table_size_info.get("is_small_league"):
            top_threshold = 0.30
            bottom_threshold = 0.70
        else:
            top_threshold = 0.25
            bottom_threshold = 0.75
        if percentile <= top_threshold:
            weight = 1.5   # top quarter — harder opponent
        elif percentile >= bottom_threshold:
            weight = 0.6   # bottom quarter — weaker opponent
        else:
            weight = 1.0
        # Scale toward 1.0 when standings are unreliable
        return 1.0 + (weight - 1.0) * opp_weight_scale

    def _match_pts(match: dict[str, Any]) -> int:
        score = match.get("score") or {}
        home_score = _to_int(score.get("home"), -1)
        away_score = _to_int(score.get("away"), -1)
        if home_score < 0 or away_score < 0:
            return -1
        is_home = str((match.get("homeTeam") or match.get("home_team") or {}).get("id") or "") == str(team_id)
        gf = home_score if is_home else away_score
        ga = away_score if is_home else home_score
        return 3 if gf > ga else 1 if gf == ga else 0

    recent = finished[:3]
    older = finished[3:6]

    def _weighted_pts(matches: list[dict[str, Any]]) -> float:
        total = 0.0
        for m in matches:
            pts = _match_pts(m)
            if pts < 0:
                continue
            total += pts * _opp_weight(m)
        return total

    recent_pts = _weighted_pts(recent)
    older_pts = _weighted_pts(older)
    delta = recent_pts - older_pts

    # Classify trajectory
    if delta >= 3.0:
        trajectory = "improving"
        impact = min(6.0, delta * 0.8)
    elif delta <= -3.0:
        trajectory = "declining"
        impact = max(-6.0, delta * 0.8)
    else:
        trajectory = "flat"
        impact = 0.0

    # Detect "losing to everyone" — all recent results are losses
    all_losses = all(_match_pts(m) == 0 for m in recent if _match_pts(m) >= 0)
    if all_losses and len(recent) >= 3:
        trajectory = "poor"
        impact = min(impact, -5.0)

    return {
        "available": True,
        "side": side,
        "trajectory": trajectory,
        "recent_weighted_pts": round(recent_pts, 2),
        "older_weighted_pts": round(older_pts, 2),
        "delta": round(delta, 2),
        "all_recent_losses": all_losses,
        "impact": round(impact, 2),
    }


def _league_strength_edge(
    event: dict[str, Any],
    home_history: list[dict[str, Any]],
    away_history: list[dict[str, Any]],
    signals: list[dict[str, Any]],
) -> float:
    strength = league_strength_edge(event, home_history, away_history)
    edge = strength.get("edge", 0.0)
    signals.append({
        "name": "league_strength_edge",
        "value": {
            "home_recent_avg": strength.get("home_recent_league_strength", {}).get("avg_score"),
            "away_recent_avg": strength.get("away_recent_league_strength", {}).get("avg_score"),
            "match_league": strength.get("match_league"),
            "note": "higher recent league strength means the team has been tested in a stronger competition",
        },
        "impact": edge,
    })
    return edge


def _h2h_edge(event: dict[str, Any], signals: list[dict[str, Any]]) -> float:
    h2h = event.get("h2h") or {}
    team_duel = h2h.get("team_duel") or h2h.get("teamDuel") or {}
    if not isinstance(team_duel, dict):
        return 0.0
    home_wins = _to_int(team_duel.get("homeWins") or team_duel.get("home_wins"), 0)
    away_wins = _to_int(team_duel.get("awayWins") or team_duel.get("away_wins"), 0)
    draws = _to_int(team_duel.get("draws"), 0)
    sample_size = home_wins + away_wins + draws
    if sample_size < 2:
        return 0.0
    raw_edge = (home_wins - away_wins) / sample_size
    edge = round(max(-8, min(8, raw_edge * 10)), 2)
    if abs(edge) < 1:
        return 0.0
    signals.append({
        "name": "h2h_edge",
        "value": {
            "home_wins": home_wins,
            "away_wins": away_wins,
            "draws": draws,
            "sample_size": sample_size,
        },
        "impact": edge,
    })
    return edge


def _table_edge(
    event: dict[str, Any],
    signals: list[dict[str, Any]],
    standings: list[dict[str, Any]] | None = None,
) -> float:
    pregame = event.get("pregame_form") or {}
    home_position = _to_int((pregame.get("home_team") or {}).get("position"), 0)
    away_position = _to_int((pregame.get("away_team") or {}).get("position"), 0)
    if not home_position or not away_position:
        return 0.0

    # When the season hasn't started or is just beginning, table positions
    # are unreliable (all teams have 0 points).  Scale the edge down so
    # meaningless standings don't dominate the prediction.
    standings = standings or []
    season_stage = detect_season_stage(standings)
    stage = season_stage.get("stage", "in_progress")
    if stage == "not_started":
        weight = 0.1
    elif stage == "beginning":
        weight = 0.3
    else:
        weight = 1.0

    edge = max(min((away_position - home_position) * 1.5 * weight, 15), -15)
    signals.append({
        "name": "league_position_edge",
        "value": {
            "home_position": home_position,
            "away_position": away_position,
            "season_stage": stage,
            "standings_meaningful": season_stage.get("standings_meaningful"),
            "table_size": len(standings),
            "edge": round(edge, 2),
        },
        "impact": round(edge, 2),
    })
    return edge


def _odds_edge(event: dict[str, Any], signals: list[dict[str, Any]]) -> float:
    """Extract implied probability edge from available odds sources."""
    # Try SofaScore fractional odds first
    market = ((event.get("odds_featured") or {}).get("default") or {}).get("choices") or []
    if market:
        prices = {choice.get("name"): _fraction_to_probability(choice.get("fractional_value")) for choice in market}
        home_prob = prices.get("1") or prices.get("Home")
        away_prob = prices.get("2") or prices.get("Away")
        if home_prob is not None and away_prob is not None:
            edge = (home_prob - away_prob) * 30
            signals.append({"name": "odds_edge", "value": round(edge, 2), "impact": round(edge, 2)})
            edge += _odds_momentum_edge(market, signals)
            return edge

    # Fallback: use SportyBet decimal odds from sportybet_markets / markets
    markets = event.get("sportybet_markets") or event.get("markets") or []
    for mkt in markets:
        name = (mkt.get("name") or "").lower()
        if mkt.get("id") == "1" or "1x2" in name or name == "match result":
            sels = {s.get("name"): s.get("odds") for s in mkt.get("selections", [])}
            home_dec = _to_float(sels.get("Home") or sels.get("1"))
            away_dec = _to_float(sels.get("Away") or sels.get("2"))
            if home_dec and away_dec and home_dec > 1 and away_dec > 1:
                home_prob = 1 / home_dec
                away_prob = 1 / away_dec
                edge = (home_prob - away_prob) * 30
                signals.append({"name": "odds_edge", "value": round(edge, 2), "impact": round(edge, 2)})
                return edge
    return 0.0


def _odds_momentum_edge(market: list[dict[str, Any]], signals: list[dict[str, Any]]) -> float:
    """Reward a side when current odds shortened meaningfully from opening odds."""
    edge = 0.0
    for choice in market:
        name = choice.get("name")
        current = _fraction_to_probability(choice.get("fractional_value"))
        opening = _fraction_to_probability(choice.get("initial_fractional_value"))
        if current is None or opening is None:
            continue
        move = current - opening
        if abs(move) < 0.035:
            continue
        side_edge = min(abs(move) * 120, 10)
        if name in ("1", "Home"):
            edge += side_edge if move > 0 else -side_edge
        elif name in ("2", "Away"):
            edge -= side_edge if move > 0 else -side_edge
        else:
            continue
        signals.append({
            "name": "market_steam",
            "value": {"side": name, "probability_move": round(move, 3)},
            "impact": round(side_edge if move > 0 else -side_edge, 2),
        })
    return edge


def _goal_pressure(home_form: dict[str, Any], away_form: dict[str, Any], event: dict[str, Any], signals: list[dict[str, Any]]) -> float:
    if not home_form.get("sample_size") or not away_form.get("sample_size"):
        return 0.0
    pressure = (
        home_form.get("avg_goals_for", 0)
        + away_form.get("avg_goals_for", 0)
        + home_form.get("avg_goals_against", 0)
        + away_form.get("avg_goals_against", 0)
    ) * 4
    pressure += (home_form.get("over_2_5_rate", 0) + away_form.get("over_2_5_rate", 0)) * 8
    if _is_high_late_goal_league(_tournament_name(event)):
        pressure += 3
    signals.append({"name": "goal_pressure", "value": round(pressure, 2), "impact": round(pressure, 2)})
    return pressure


def _btts_pressure(home_form: dict[str, Any], away_form: dict[str, Any]) -> float:
    if not home_form.get("sample_size") or not away_form.get("sample_size"):
        return 0.0
    return (home_form.get("btts_rate", 0) + away_form.get("btts_rate", 0)) * 12


def _live_chase_pressure(event: dict[str, Any], home_power: float, goal_pressure: float, signals: list[dict[str, Any]]) -> float:
    status = event.get("status") or {}
    if status.get("type") != "inprogress":
        return 0.0
    minute = _event_minute(event)
    if minute < 58:
        return 0.0

    score = event.get("score") or {}
    home_goals = _to_int(score.get("home"), 0)
    away_goals = _to_int(score.get("away"), 0)
    score_diff = home_goals - away_goals
    if abs(score_diff) > 1:
        return 0.0

    pressure = max(goal_pressure - 12, 0)
    if score_diff == 0:
        pressure += 4
    elif score_diff < 0 and home_power > 8:
        pressure += 8
    elif score_diff > 0 and home_power < -8:
        pressure += 8
    elif score_diff < 0 and home_power > 0:
        pressure += 4
    elif score_diff > 0 and home_power < 0:
        pressure += 4

    if minute >= 75:
        pressure += 3

    if pressure:
        signals.append({
            "name": "live_chase_pressure",
            "value": {"minute": minute, "score_diff": score_diff},
            "impact": round(pressure, 2),
        })
    return pressure


def _red_card_signal(match: dict[str, Any], edge: float) -> dict[str, Any] | None:
    home_red = _to_int(match.get("home_red_cards") or match.get("homeRedCards"), 0)
    away_red = _to_int(match.get("away_red_cards") or match.get("awayRedCards"), 0)
    if home_red == away_red:
        return None
    if home_red > away_red:
        selection = "Away team pressure after home red card" if edge <= 8 else "Favorite weakened by red card, avoid home win"
    else:
        selection = "Home team pressure after away red card" if edge >= -8 else "Underdog weakened by red card, favorite protection"
    return _pick("red_card", selection, 63, "red card changes win probability and goal pressure")


def _late_goal_memory_boost(memory_signal: dict[str, Any] | None, signals: list[dict[str, Any]]) -> int:
    if not memory_signal:
        return 0
    signals.append(memory_signal)
    return max(-8, min(8, round(memory_signal.get("impact", 0))))


def _sporty_main_odds(match: dict[str, Any]) -> list[dict[str, Any]]:
    for market in match.get("markets", []):
        name = (market.get("name") or "").lower()
        if "1x2" in name or "winner" in name or name in {"3 way", "match result"}:
            odds = []
            for selection in market.get("selections", []):
                decimal = _to_float(selection.get("odds"))
                if decimal and decimal > 1:
                    odds.append({
                        "name": selection.get("name"),
                        "decimal": decimal,
                        "implied_probability": 1 / decimal,
                    })
            return odds
    return []


def _sporty_market_edge(odds: list[dict[str, Any]]) -> float:
    if len(odds) < 2:
        return 0.0
    home = odds[0]["implied_probability"]
    away = odds[-1]["implied_probability"]
    return (home - away) * 30


def _side_name(team: dict[str, Any], event: dict[str, Any], side: str) -> str:
    """Resolve a team name, falling back to the match name split if the team dict is empty."""
    name = team.get("name") if isinstance(team, dict) else None
    if name:
        return name
    match_name = str(event.get("name") or "")
    parts = [p.strip() for p in match_name.split(" vs ", 1)]
    if len(parts) == 2:
        return parts[0] if side == "home" else parts[1]
    return match_name


def _event_minute(event: dict[str, Any]) -> int:
    status = event.get("status") or {}
    description = str(status.get("description") or "")
    digits = "".join(ch for ch in description if ch.isdigit())
    return _to_int(digits, 0)


def _sporty_minute(played_seconds: Any) -> int:
    if played_seconds is None:
        return 0
    if isinstance(played_seconds, str) and ":" in played_seconds:
        mins, _ = played_seconds.split(":", 1)
        return _to_int(mins, 0)
    return int(_to_int(played_seconds, 0) / 60)


def _is_high_late_goal_league(name: str | None) -> bool:
    try:
        from app.monitoring.self_learner import get_tournament_priority

        learned = get_tournament_priority(name or "")
        return bool(learned.get("known") and int(learned.get("priority", 4)) <= 3)
    except Exception:
        return False


