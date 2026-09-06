from __future__ import annotations

import math
import sqlite3
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db


K_FACTOR = 32
HOME_ADVANTAGE_ELO = 0


def _init_elo_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists elo_ratings (
            team_id text primary key,
            team_name text,
            rating real not null default 1500,
            matches_played integer not null default 0,
            updated_at text not null default current_timestamp
        )
        """
    )
    conn.execute(
        """
        create table if not exists elo_match_results (
            source text not null,
            match_id text not null,
            home_id text not null,
            away_id text not null,
            created_at text not null default current_timestamp,
            primary key (source, match_id)
        )
        """
    )


def get_elo(team_id: str) -> float:
    _init_db()
    with db_conn(timeout=30) as conn:
        _init_elo_table(conn)
        row = conn.execute("select rating from elo_ratings where team_id = ?", (str(team_id),)).fetchone()
    return float(row[0]) if row else 1500.0


def update_elo(
    home_id: str,
    away_id: str,
    home_goals: int,
    away_goals: int,
    home_name: str = "",
    away_name: str = "",
) -> dict[str, Any]:
    home_elo = get_elo(home_id)
    away_elo = get_elo(away_id)

    home_expected = 1 / (1 + 10 ** ((away_elo - home_elo) / 400))
    away_expected = 1 - home_expected

    if home_goals > away_goals:
        home_actual, away_actual = 1.0, 0.0
    elif home_goals == away_goals:
        home_actual, away_actual = 0.5, 0.5
    else:
        home_actual, away_actual = 0.0, 1.0

    goal_diff = abs(home_goals - away_goals)
    multiplier = 1 + math.log(1 + goal_diff) * 0.5

    new_home = home_elo + K_FACTOR * multiplier * (home_actual - home_expected)
    new_away = away_elo + K_FACTOR * multiplier * (away_actual - away_expected)

    with db_conn(timeout=30) as conn:
        _init_elo_table(conn)
        for team_id, name, rating in ((home_id, home_name, new_home), (away_id, away_name, new_away)):
            conn.execute(
                """
                insert into elo_ratings (team_id, team_name, rating, matches_played, updated_at)
                values (?, ?, ?, 1, current_timestamp)
                on conflict(team_id) do update set
                    team_name = case when excluded.team_name != '' then excluded.team_name else team_name end,
                    rating = excluded.rating,
                    matches_played = matches_played + 1,
                    updated_at = current_timestamp
                """,
                (str(team_id), name, rating),
            )
        conn.commit()

    return {
        "home": {"id": home_id, "old_elo": round(home_elo), "new_elo": round(new_home)},
        "away": {"id": away_id, "old_elo": round(away_elo), "new_elo": round(new_away)},
        "home_win_probability": round(home_expected * 100, 1),
    }


def record_match_result_once(source: str, event: dict[str, Any]) -> dict[str, Any]:
    match_id = str(event.get("id") or "")
    home = event.get("home_team") or {}
    away = event.get("away_team") or {}
    home_id = str(home.get("id") or "")
    away_id = str(away.get("id") or "")
    score = event.get("score") or {}
    if not match_id or not home_id or not away_id:
        return {"updated": False, "reason": "missing match/team id"}

    _init_db()
    with db_conn(timeout=30) as conn:
        _init_elo_table(conn)
        try:
            conn.execute(
                "insert into elo_match_results (source, match_id, home_id, away_id) values (?, ?, ?, ?)",
                (source, match_id, home_id, away_id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return {"updated": False, "reason": "already_recorded", "match_id": match_id}

    result = update_elo(
        home_id,
        away_id,
        int(score.get("home") or 0),
        int(score.get("away") or 0),
        home.get("name") or "",
        away.get("name") or "",
    )
    return {"updated": True, "match_id": match_id, **result}


def elo_prediction(home_id: str, away_id: str, doc: dict[str, Any] | None = None) -> dict[str, Any]:
    home_elo = get_elo(home_id)
    away_elo = get_elo(away_id)
    context = _elo_doc_context(doc)
    home_elo += float(context.get("home_adjustment") or 0)
    away_elo += float(context.get("away_adjustment") or 0)
    home_expected = 1 / (1 + 10 ** ((away_elo - home_elo) / 400))
    away_expected = 1 - home_expected
    try:
        from app.models.poisson import _apply_bias_corrections
        calibrated = _apply_bias_corrections({
            "home_win": home_expected,
            "draw": 0.0,
            "away_win": away_expected,
        })
        home_expected = calibrated["home_win"]
        away_expected = calibrated["away_win"]
    except Exception:
        pass
    return {
        "model": "elo",
        "home_elo": round(home_elo),
        "away_elo": round(away_elo),
        "home_win_probability": round(home_expected * 100, 1),
        "away_win_probability": round(away_expected * 100, 1),
        "elo_diff": round(home_elo - away_elo),
        "home_advantage_elo": HOME_ADVANTAGE_ELO,
        "context": context,
    }


def _elo_doc_context(doc: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(doc, dict):
        return {"source": "elo_table", "home_adjustment": 0, "away_adjustment": 0}
    has_standings = bool((doc.get("sofascore_detail") or {}).get("standings") if isinstance(doc.get("sofascore_detail"), dict) else doc.get("standings"))
    has_markets = bool(doc.get("odds_1x2") or doc.get("sportybet_markets") or doc.get("markets"))
    return {
        "source": "enriched_doc",
        "home_adjustment": 0,
        "away_adjustment": 0,
        "has_standings": has_standings,
        "has_markets": has_markets,
    }


def get_elo_league_strength(league_name: str, min_teams: int = 4) -> dict[str, Any]:
    """Derive a league strength score (20–98 scale) from the average ELO of
    teams that have played in that league, as recorded in ai_team_watcher_matches.

    Strategy:
    - Join elo_ratings (keyed by team_id/sofascore_team_id) with the distinct
      set of teams seen playing in this league via ai_team_watcher_matches.
    - Only teams with at least 3 matches in this league are included, to
      avoid one-off cup appearances inflating/deflating the average.
    - Requires min_teams distinct teams; returns {"available": False} when
      not enough ELO-rated teams exist for this league yet.

    Mapping avg_elo → score:
      avg_elo 1800+ → 98   (elite/CL level)
      avg_elo 1700  → ~90
      avg_elo 1600  → ~82
      avg_elo 1500  → ~74  (baseline — mid-table in a known league)
      avg_elo 1400  → ~66
      avg_elo 1300  → ~58
      avg_elo 1200  → ~50
      avg_elo 1100  → ~42
      Formula: score = 74 + (avg_elo - 1500) * 0.08, clamped [20, 98]
    """
    if not league_name:
        return {"available": False, "reason": "no_league_name"}
    _init_db()
    try:
        with db_conn(timeout=10) as conn:
            _init_elo_table(conn)
            rows = conn.execute(
                """
                select e.rating
                from elo_ratings e
                inner join (
                    select sofascore_team_id, count(*) as appearances
                    from ai_team_watcher_matches
                    where league_name = ?
                      and sofascore_team_id is not null
                      and sofascore_team_id != ''
                    group by sofascore_team_id
                    having appearances >= 3
                ) league_teams on e.team_id = league_teams.sofascore_team_id
                where e.matches_played >= 3
                """,
                (league_name,),
            ).fetchall()
    except Exception as exc:
        return {"available": False, "reason": f"db_error: {exc}"}

    if len(rows) < min_teams:
        return {
            "available": False,
            "reason": "insufficient_rated_teams",
            "rated_teams": len(rows),
            "min_required": min_teams,
        }

    ratings = [float(row[0]) for row in rows]
    avg_elo = sum(ratings) / len(ratings)
    score = max(20, min(98, round(74 + (avg_elo - 1500) * 0.08)))
    return {
        "available": True,
        "avg_elo": round(avg_elo, 1),
        "rated_teams": len(ratings),
        "score": score,
    }
