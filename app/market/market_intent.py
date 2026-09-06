from __future__ import annotations

import re
from typing import Any

from app.storage.league_memory._helpers import _side_from_selection_and_match


def normalise_market_text(value: Any) -> str:
    return " ".join(str(value or "").lower().replace("-", " ").split())


def classify_market_intent(
    pick_type: str | None = None,
    selection: str | None = None,
    value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable market meaning for a pick or signal.

    The prediction engine can produce 1X2, totals, BTTS, live and value picks.
    This helper keeps the market semantics explicit so consensus/value memory
    does not collapse every signal into a home/away decision.
    """
    raw_value = value or {}
    embedded = raw_value.get("market_intent") if isinstance(raw_value.get("market_intent"), dict) else {}
    if embedded:
        return _complete_intent(embedded, pick_type, selection)

    kind = normalise_market_text(pick_type)
    sel = normalise_market_text(selection or raw_value.get("selection"))
    line = parse_total_line(sel)

    # Live picks from the shared-probability-grid model
    # (_live_grid_projection_picks, app/enrichment/enriched_prediction.py)
    # carry a "_grid" suffix on their pick_type (live_match_winner_grid,
    # live_next_goal_grid, live_no_goal_grid, live_total_goals_grid,
    # live_btts_grid, live_double_chance_grid) to tell them apart from the
    # older independent-heuristic live picks below, which don't have the
    # suffix. Route both through the same intent branches by kind rather
    # than depending on exact selection wording -- some grid selection text
    # ("Arsenal to win (grid)", "Arsenal to score next (grid)") doesn't
    # match any of the text heuristics below, so without this a grid pick
    # would fall through to an unclassified "other" market and fail to
    # book ("selection is no longer available"). Same suffix-stripping
    # convention already used for grading in
    # app/storage/league_memory/queries.py's _grade_candidate_row.
    kind_base = kind[:-5] if kind.endswith("_grid") else kind

    if kind_base == "no_bet" or "avoid game" in sel or "no strong bet" in sel:
        return _intent("avoid", "avoid", "avoid", None, line, selection)

    if kind_base == "live_no_goal" or "no more goal" in sel or "no goal" in sel:
        return _intent("live_goal", "live_no_goal", "no_goal", "under", line, selection)

    if kind_base == "live_next_goal" or "next goal" in sel or "to score next" in sel:
        return _intent("live_goal", "live_next_goal", "next_goal", "over", line, selection)

    # live_total_goals and live_match_winner map onto the SAME market
    # names/outcomes SportyBet uses prematch ("Over/Under", "1X2") --
    # confirmed against a real live match's stored markets
    # (match_buffer.raw_sporty for a currently in-play fixture showed
    # "1X2", "Double Chance" and "Over/Under" as the live in-play market
    # names too, identical to prematch). So these deliberately return the
    # existing "total_goals"/"1x2" market values rather than inventing new
    # "live_*" ones _find_market_outcome doesn't know how to resolve --
    # that gap is exactly why these picks were unbookable before this fix.
    if kind_base == "live_total_goals":
        direction = "under" if "under" in sel else "over" if "over" in sel else None
        intent = f"{direction or 'total'}_{_line_key(line)}"
        return _intent("live_goal", "total_goals", intent, direction, line, selection)

    if kind_base == "live_team_to_score":
        return _intent("live_team_goal", "live_team_to_score", "team_to_score", _side_from_text(sel), line, selection)

    if kind_base == "live_match_winner" or "to win (grid)" in sel:
        side = _side_from_text(sel)
        return _intent("live_outcome", "1x2", f"{side or 'unknown'}_win", side, line, selection)

    if kind_base == "live_double_chance":
        intent, direction = _double_chance_intent(sel)
        return _intent("double_chance", "double_chance", intent, direction, line, selection)

    if _is_btts(sel):
        direction = "no" if _is_no_btts(sel) else "yes"
        return _intent("btts", "btts", f"btts_{direction}", direction, line, selection)

    if "over" in sel or "under" in sel or kind_base in {"goals", "live_goals"}:
        direction = "under" if "under" in sel else "over" if "over" in sel else None
        intent = f"{direction or 'total'}_{_line_key(line)}"
        return _intent("goal_total", "total_goals", intent, direction, line, selection)

    if kind_base == "double_chance" or _is_double_chance(sel):
        intent, direction = _double_chance_intent(sel)
        return _intent("double_chance", "double_chance", intent, direction, line, selection)

    # A value pick can still be a conventional 1X2 selection (often a team
    # display name rather than the literal word "home"/"away"). Preserve
    # that market meaning so the grader can resolve the team against the
    # match name instead of marking a perfectly valid result as void.
    if kind in {"match_result", "ensemble_1x2", "value_bet", "market_value", "consensus_longshot_value"} or _is_1x2(sel):
        side = _side_from_text(sel)
        return _intent("outcome", "1x2", f"{side or 'unknown'}_win", side, line, selection)

    return _intent(kind or "other", kind or "other", sel or "unknown", None, line, selection)


def parse_total_line(text: Any) -> float | None:
    match = re.search(r"\b(?:over|under)\s+(\d+(?:\.\d+)?)", normalise_market_text(text))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def grade_market_intent(
    intent: dict[str, Any] | None,
    selection: str | None,
    home: int,
    away: int,
    match_name: str | None = None,
) -> str:
    intent = classify_market_intent(
        str((intent or {}).get("source_pick_type") or ""),
        selection,
        {"market_intent": intent or {}},
    )
    total = home + away
    market = str(intent.get("market") or "")
    direction = str(intent.get("direction") or "")
    line = intent.get("line")

    if market in {"total_goals", "live_total_goals"}:
        if line is None:
            line = parse_total_line(selection)
        if line is None:
            return "void"
        line_float = float(line)
        if direction == "over":
            if line_float.is_integer() and total == int(line_float):
                return "void"
            return "win" if total > line_float else "loss"
        if direction == "under":
            if line_float.is_integer() and total == int(line_float):
                return "void"
            return "win" if total < line_float else "loss"
        return "void"

    if market == "btts":
        yes = home > 0 and away > 0
        return "win" if (yes and direction == "yes") or (not yes and direction == "no") else "loss"

    if market == "double_chance":
        intent_key = str(intent.get("intent") or "")
        if intent_key == "home_or_draw":
            return "win" if home >= away else "loss"
        if intent_key == "away_or_draw":
            return "win" if away >= home else "loss"
        if intent_key == "home_or_away":
            return "win" if home != away else "loss"
        return "void"

    if market in {"1x2", "live_match_winner"}:
        side = direction or _side_from_selection_and_match(selection or "", match_name)
        if side == "home":
            return "win" if home > away else "loss"
        if side == "away":
            return "win" if away > home else "loss"
        if side == "draw":
            return "win" if home == away else "loss"
        return "void"

    # market in {"live_next_goal", "live_no_goal"} deliberately falls through
    # to void below, not by omission: unlike every market handled above,
    # "does a/no team score NEXT" cannot be resolved from the final score
    # alone -- it needs the score AT PICK TIME, which this function's
    # signature doesn't carry (grade_market_intent is also used for
    # pre-match markets, where "at pick time" is meaningless). The real
    # grader for these two, which does have that context via
    # prediction_history.context_json, is _grade_candidate_row in
    # app/storage/league_memory/queries.py. Do not "fix" this by guessing
    # from the final score -- that's exactly the bug this comment replaces
    # (the old code treated any goal at all as a live_next_goal win,
    # regardless of which team or when).
    return "void"


def selection_key(selection: str | None, pick_type: str | None = None, value: dict[str, Any] | None = None) -> str:
    intent = classify_market_intent(pick_type, selection, value)
    return str(intent.get("selection_key") or normalise_market_text(selection))


def _complete_intent(intent: dict[str, Any], pick_type: str | None, selection: str | None) -> dict[str, Any]:
    completed = dict(intent)
    completed.setdefault("source_pick_type", normalise_market_text(pick_type))
    completed.setdefault("raw_selection", selection)
    completed.setdefault("line", parse_total_line(selection))
    completed.setdefault("selection_key", _selection_key_for(completed, selection))
    completed["is_directional_1x2"] = completed.get("market") in {"1x2", "double_chance", "live_match_winner"}
    return completed


def _intent(
    family: str,
    market: str,
    intent: str,
    direction: str | None,
    line: float | None,
    selection: str | None,
) -> dict[str, Any]:
    data = {
        "family": family,
        "market": market,
        "intent": intent,
        "direction": direction,
        "line": line,
        "raw_selection": selection,
    }
    data["selection_key"] = _selection_key_for(data, selection)
    data["is_directional_1x2"] = market in {"1x2", "double_chance", "live_match_winner"}
    return data


def _selection_key_for(intent: dict[str, Any], selection: str | None) -> str:
    market = str(intent.get("market") or "other")
    intent_name = str(intent.get("intent") or normalise_market_text(selection) or "unknown")
    line = intent.get("line")
    if line is not None and market in {"total_goals", "live_total_goals"}:
        return f"{market}:{intent.get('direction')}:{_line_key(float(line))}"
    return f"{market}:{intent_name}"


def _line_key(line: float | None) -> str:
    if line is None:
        return "any"
    return str(int(line)) if float(line).is_integer() else str(line).replace(".", "_")


def _is_btts(text: str) -> bool:
    return "btts" in text or "both teams to score" in text


def _is_no_btts(text: str) -> bool:
    return " no" in f" {text}" or text.endswith(" no") or "btts no" in text


def _is_double_chance(text: str) -> bool:
    return text in {"1x", "x2", "12"} or "or draw" in text or "home or away" in text or "away or home" in text


def _double_chance_intent(text: str) -> tuple[str, str | None]:
    if text in {"1x", "home or draw", "draw or home"} or "home or draw" in text or "draw or home" in text:
        return "home_or_draw", "home_draw"
    if text in {"x2", "away or draw", "draw or away"} or "away or draw" in text or "draw or away" in text:
        return "away_or_draw", "away_draw"
    if text in {"12", "home or away", "away or home"} or "home or away" in text or "away or home" in text:
        return "home_or_away", "no_draw"
    return "double_chance", None


def _is_1x2(text: str) -> bool:
    return text in {"home", "home win", "1", "away", "away win", "2", "draw", "x"}


def _side_from_text(text: str) -> str | None:
    stripped = normalise_market_text(text)
    if stripped in {"home", "home win", "1"} or "home winner" in stripped:
        return "home"
    if stripped in {"away", "away win", "2"} or "away winner" in stripped:
        return "away"
    if stripped in {"draw", "x"} or "draw protection" in stripped:
        return "draw"
    return None

