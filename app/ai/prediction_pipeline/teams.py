"""Team behaviour profiling — derives a lightweight statistical fingerprint
(BTTS rate, over 2.5 rate, clean-sheet rate, etc.) for a team from its recent
finished matches, used by markets.py to shortlist which markets are worth
asking the decider about.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import sqlite3

from app.ai.prediction_pipeline.evidence import _history_for_team


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


def derive_team_profile(team_name: str, conn: sqlite3.Connection) -> TeamBehaviourProfile:
    history = _history_for_team(team_name, conn)
    n = len(history); now = datetime.now(timezone.utc).isoformat()
    if n < 3: return TeamBehaviourProfile(team_name=team_name, sample_size=n, computed_at=now)
    scored = [x[0] for x in history]; conceded = [x[1] for x in history]
    return TeamBehaviourProfile(team_name, sum(a > 0 and b > 0 for a,b,_ in history)/n, sum(a+b > 2 for a,b,_ in history)/n,
        sum(b == 0 for b in conceded)/n, 0.0, int(sum(a >= 2 for a in scored)/n >= .6), sum(a == 0 and b > 0 for a,b,_ in history)/n, n, now)


def persist_team_profile(profile: TeamBehaviourProfile, conn: sqlite3.Connection) -> None:
    conn.execute("""insert or replace into team_behaviour_profiles values (?, ?, ?, ?, ?, ?, ?, ?, ?)""", tuple(asdict(profile).values()))
