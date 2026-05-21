"""
SofaScore Grades
----------------
Extracts player and team performance grades from SofaScore match detail.

SofaScore assigns ratings (1.0–10.0) to players after each match.
These are richer than W/D/L because they capture:
  - Individual player form (is the striker in form? is the keeper shaky?)
  - Team collective performance (high avg = dominant display)
  - Momentum (team improving or declining over last 5 matches?)

What we extract:
  - team_avg_rating     : average player rating for the team in this match
  - top_performer       : highest-rated player + their rating
  - weak_link           : lowest-rated player + their rating (injury/suspension risk)
  - rating_trend        : is the team's avg rating improving over last 5 matches?
  - grade_signal        : composite signal for the prediction engine

Usage:
    from app.sofascore_grades import extract_team_grades, grade_signal_for_match

    # From a SofaScore detail document
    grades = extract_team_grades(sofascore_detail)
    signal = grade_signal_for_match(sofascore_detail)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.league_memory import DB_PATH, _init_db


# ── Table ─────────────────────────────────────────────────────────────────────

def _init_grades_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        create table if not exists sofascore_team_grades (
            match_id        text not null,
            team_id         text not null,
            team_name       text,
            match_date      text,
            avg_rating      real,
            top_performer   text,
            top_rating      real,
            weak_link       text,
            weak_rating     real,
            players_rated   integer,
            recorded_at     text not null default current_timestamp,
            primary key (match_id, team_id)
        )
    """)
    conn.execute("create index if not exists idx_grades_team on sofascore_team_grades(team_id)")
    conn.execute("create index if not exists idx_grades_date on sofascore_team_grades(match_date)")


# ── Extract grades from a SofaScore detail document ──────────────────────────

def extract_team_grades(detail: dict[str, Any]) -> dict[str, Any]:
    """
    Extract home and away team grades from a SofaScore event detail document.
    Returns a dict with 'home' and 'away' grade blocks.
    """
    home_team = detail.get("home_team") or detail.get("homeTeam") or {}
    away_team = detail.get("away_team") or detail.get("awayTeam") or {}
    lineups   = detail.get("lineups") or {}

    home_grades = _extract_side_grades(lineups.get("home") or {}, home_team)
    away_grades = _extract_side_grades(lineups.get("away") or {}, away_team)

    # Also try pregame_form ratings as a fallback
    pregame = detail.get("pregame_form") or {}
    if not home_grades.get("avg_rating"):
        home_pre = pregame.get("home_team") or {}
        if home_pre.get("avg_rating"):
            home_grades["avg_rating"] = float(home_pre["avg_rating"])
            home_grades["source"] = "pregame_form"
    if not away_grades.get("avg_rating"):
        away_pre = pregame.get("away_team") or {}
        if away_pre.get("avg_rating"):
            away_grades["avg_rating"] = float(away_pre["avg_rating"])
            away_grades["source"] = "pregame_form"

    return {"home": home_grades, "away": away_grades}


def _extract_side_grades(lineup_side: dict[str, Any], team: dict[str, Any]) -> dict[str, Any]:
    """Extract grades from one side of a lineup block."""
    players = lineup_side.get("players") or []
    # Flatten: players can be a list of lists (formation rows) or flat list
    flat: list[dict] = []
    for item in players:
        if isinstance(item, list):
            flat.extend(item)
        elif isinstance(item, dict):
            flat.append(item)

    rated = []
    for p in flat:
        player_data = p.get("player") or p
        stats = p.get("statistics") or {}
        rating = stats.get("rating") or p.get("rating")
        if rating is None:
            # Try sofascore_rating key
            rating = stats.get("sofascore_rating") or stats.get("sofascoreRating")
        if rating is not None:
            try:
                r = float(rating)
                if 1.0 <= r <= 10.0:
                    rated.append({
                        "name": player_data.get("name") or player_data.get("shortName") or "Unknown",
                        "rating": r,
                        "position": p.get("position") or stats.get("position"),
                    })
            except (TypeError, ValueError):
                pass

    if not rated:
        return {
            "team_id": str(team.get("id") or ""),
            "team_name": team.get("name") or "",
            "avg_rating": None,
            "top_performer": None,
            "top_rating": None,
            "weak_link": None,
            "weak_rating": None,
            "players_rated": 0,
            "source": "lineups",
        }

    avg = round(sum(p["rating"] for p in rated) / len(rated), 2)
    top = max(rated, key=lambda p: p["rating"])
    weak = min(rated, key=lambda p: p["rating"])

    return {
        "team_id": str(team.get("id") or ""),
        "team_name": team.get("name") or "",
        "avg_rating": avg,
        "top_performer": top["name"],
        "top_rating": top["rating"],
        "weak_link": weak["name"],
        "weak_rating": weak["rating"],
        "players_rated": len(rated),
        "source": "lineups",
    }


# ── Store grades for a match ──────────────────────────────────────────────────

def store_match_grades(
    match_id: str,
    detail: dict[str, Any],
    match_date: str | None = None,
) -> dict[str, Any]:
    """
    Extract and persist team grades for a match.
    Called during enrichment when SofaScore detail is available.
    """
    grades = extract_team_grades(detail)
    _init_db()
    stored = 0

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_grades_table(conn)
        now = datetime.now(timezone.utc).isoformat()

        for side in ("home", "away"):
            g = grades.get(side) or {}
            team_id = g.get("team_id")
            if not team_id or g.get("avg_rating") is None:
                continue
            conn.execute("""
                insert into sofascore_team_grades
                    (match_id, team_id, team_name, match_date, avg_rating,
                     top_performer, top_rating, weak_link, weak_rating,
                     players_rated, recorded_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(match_id, team_id) do update set
                    avg_rating    = excluded.avg_rating,
                    top_performer = excluded.top_performer,
                    top_rating    = excluded.top_rating,
                    weak_link     = excluded.weak_link,
                    weak_rating   = excluded.weak_rating,
                    players_rated = excluded.players_rated,
                    recorded_at   = excluded.recorded_at
            """, (
                str(match_id), team_id, g.get("team_name"), match_date,
                g.get("avg_rating"), g.get("top_performer"), g.get("top_rating"),
                g.get("weak_link"), g.get("weak_rating"), g.get("players_rated"), now,
            ))
            stored += 1

        conn.commit()

    return {"stored": stored, "grades": grades}


# ── Rating trend: is a team improving or declining? ──────────────────────────

def get_team_rating_trend(team_id: str, last_n: int = 5) -> dict[str, Any]:
    """
    Look at a team's last N match ratings and compute a trend.
    Returns: avg_rating, trend ('improving'/'declining'/'stable'), delta
    """
    _init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        _init_grades_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            select avg_rating, match_date
            from sofascore_team_grades
            where team_id = ? and avg_rating is not null
            order by match_date desc, recorded_at desc
            limit ?
        """, (str(team_id), last_n)).fetchall()

    if not rows:
        return {"team_id": team_id, "known": False}

    ratings = [float(r["avg_rating"]) for r in rows]
    avg = round(sum(ratings) / len(ratings), 2)

    # Trend: compare first half vs second half of the window
    if len(ratings) >= 3:
        mid = len(ratings) // 2
        recent_avg = sum(ratings[:mid]) / mid
        older_avg  = sum(ratings[mid:]) / (len(ratings) - mid)
        delta = round(recent_avg - older_avg, 2)
        if delta > 0.3:
            trend = "improving"
        elif delta < -0.3:
            trend = "declining"
        else:
            trend = "stable"
    else:
        delta = 0.0
        trend = "stable"

    return {
        "team_id":   team_id,
        "known":     True,
        "avg_rating": avg,
        "trend":     trend,
        "delta":     delta,
        "samples":   len(ratings),
        "ratings":   ratings,
    }


# ── Grade signal for prediction engine ───────────────────────────────────────

def grade_signal_for_match(
    detail: dict[str, Any],
    match_id: str | None = None,
    match_date: str | None = None,
) -> dict[str, Any]:
    """
    Produce a prediction signal from SofaScore grades.
    Returns a signal dict compatible with the prediction engine's signals list.

    Impact scale:
      +8  → home team significantly higher rated (avg diff > 1.0)
      +4  → home team moderately higher rated (avg diff 0.5–1.0)
       0  → roughly equal
      -4  → away team moderately higher rated
      -8  → away team significantly higher rated

    Also factors in rating trends:
      improving team gets +2, declining team gets -2
    """
    grades = extract_team_grades(detail)
    home_g = grades.get("home") or {}
    away_g = grades.get("away") or {}

    home_avg = home_g.get("avg_rating")
    away_avg = away_g.get("avg_rating")

    # Store grades if we have a match_id
    if match_id and (home_avg or away_avg):
        try:
            store_match_grades(match_id, detail, match_date)
        except Exception:
            pass

    if home_avg is None and away_avg is None:
        return {"name": "sofascore_grade", "value": None, "impact": 0, "available": False}

    # Fallback: if only one side has ratings, use 6.5 as neutral baseline
    home_avg = home_avg or 6.5
    away_avg = away_avg or 6.5

    diff = home_avg - away_avg  # positive = home better

    if abs(diff) >= 1.0:
        impact = 8 if diff > 0 else -8
    elif abs(diff) >= 0.5:
        impact = 4 if diff > 0 else -4
    else:
        impact = 0

    # Rating trend adjustment
    home_id = home_g.get("team_id")
    away_id = away_g.get("team_id")
    trend_adj = 0
    home_trend_info = {}
    away_trend_info = {}

    if home_id:
        home_trend = get_team_rating_trend(home_id)
        home_trend_info = home_trend
        if home_trend.get("trend") == "improving":
            trend_adj += 2
        elif home_trend.get("trend") == "declining":
            trend_adj -= 2

    if away_id:
        away_trend = get_team_rating_trend(away_id)
        away_trend_info = away_trend
        if away_trend.get("trend") == "improving":
            trend_adj -= 2
        elif away_trend.get("trend") == "declining":
            trend_adj += 2

    total_impact = max(-12, min(12, impact + trend_adj))

    return {
        "name": "sofascore_grade",
        "value": {
            "home_avg_rating":  home_avg,
            "away_avg_rating":  away_avg,
            "rating_diff":      round(diff, 2),
            "home_top_player":  home_g.get("top_performer"),
            "away_top_player":  away_g.get("top_performer"),
            "home_weak_link":   home_g.get("weak_link"),
            "away_weak_link":   away_g.get("weak_link"),
            "home_trend":       home_trend_info.get("trend"),
            "away_trend":       away_trend_info.get("trend"),
        },
        "impact":    total_impact,
        "available": True,
    }
