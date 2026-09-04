"""Market candidate shortlisting — turns two teams' behaviour profiles into a
ranked shortlist of betting markets worth putting in front of the decider,
instead of always asking about all of them.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.ai.prediction_pipeline.teams import TeamBehaviourProfile


@dataclass
class MarketCandidate:
    market_key: str
    label: str
    score: float


def shortlist_markets(home: TeamBehaviourProfile, away: TeamBehaviourProfile) -> list[MarketCandidate]:
    one_x_two = [MarketCandidate("home_win", "Home win", .5 + (home.clean_sheet_rate-away.loss_to_nil_rate)/2), MarketCandidate("draw", "Draw", .55), MarketCandidate("away_win", "Away win", .5 + (away.clean_sheet_rate-home.loss_to_nil_rate)/2)]
    if min(home.sample_size, away.sample_size) < 3: return sorted(one_x_two, key=lambda x: x.score, reverse=True)[:3]
    both = (home.btts_rate + away.btts_rate)/2; over = (home.over_2_5_rate + away.over_2_5_rate)/2
    candidates = one_x_two + [MarketCandidate("btts_yes", "Both teams to score", both), MarketCandidate("over_2_5", "Over 2.5 goals", over), MarketCandidate("under_2_5", "Under 2.5 goals", 1-over), MarketCandidate("btts_no", "BTTS No", 1-both)]
    selected = [item for item in candidates if item.score >= .55]
    if not any(item.market_key in {"home_win", "draw", "away_win"} for item in selected): selected.append(max(one_x_two, key=lambda x: x.score))
    return sorted(selected, key=lambda x: x.score, reverse=True)[:5]
