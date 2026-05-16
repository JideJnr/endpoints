from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any

from app.league_memory import DB_PATH, _init_db


def snapshot_odds(doc: dict[str, Any]) -> bool:
    _init_db()
    odds = _extract_1x2_sportybet(doc.get("sportybet_markets") or doc.get("markets") or [])
    source = "sportybet"
    if not odds.get("home"):
        detail = doc.get("sofascore_detail") or doc
        odds = _extract_1x2_sofascore(detail.get("odds_featured") or detail.get("oddsFeatured"))
        source = "sofascore"
    if not odds.get("home"):
        return False

    match_id = str(doc.get("sportybet_id") or doc.get("id") or doc.get("match_id") or "")
    if not match_id:
        return False

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            create table if not exists odds_snapshots (
                id integer primary key autoincrement,
                match_id text not null,
                match_name text,
                match_date text,
                home_odds real,
                draw_odds real,
                away_odds real,
                home_implied real,
                draw_implied real,
                away_implied real,
                source text,
                snapshot_time text not null default current_timestamp
            )
            """
        )
        conn.execute(
            """
            insert into odds_snapshots (
                match_id, match_name, match_date, home_odds, draw_odds, away_odds,
                home_implied, draw_implied, away_implied, source
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                doc.get("sportybet_name") or doc.get("name"),
                doc.get("match_date"),
                odds.get("home"),
                odds.get("draw"),
                odds.get("away"),
                _implied(odds.get("home")),
                _implied(odds.get("draw")),
                _implied(odds.get("away")),
                source,
            ),
        )
        conn.commit()
    try:
        from app.mongo_store import save_odds_snapshot

        save_odds_snapshot({
            "sportybet_id": match_id,
            "match": doc.get("sportybet_name") or doc.get("name"),
            "match_date": doc.get("match_date"),
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
            "home_odds": odds.get("home"),
            "draw_odds": odds.get("draw"),
            "away_odds": odds.get("away"),
            "home_implied": _implied(odds.get("home")),
            "draw_implied": _implied(odds.get("draw")),
            "away_implied": _implied(odds.get("away")),
            "source": source,
        })
    except Exception:
        pass
    return True


def get_movement(match_id: str) -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select *
            from odds_snapshots
            where match_id = ?
            order by snapshot_time asc
            """,
            (str(match_id),),
        ).fetchall()
    if len(rows) < 2:
        return {"snapshots": len(rows), "movement": None}

    opening = rows[0]
    current = rows[-1]
    home_drop = (opening["home_odds"] or 0) - (current["home_odds"] or 0)
    away_drop = (opening["away_odds"] or 0) - (current["away_odds"] or 0)
    sharp_signal = None
    if home_drop > 0.15:
        sharp_signal = "Sharp money on HOME"
    elif away_drop > 0.15:
        sharp_signal = "Sharp money on AWAY"
    elif home_drop > 0.08:
        sharp_signal = "Moderate money on HOME"
    elif away_drop > 0.08:
        sharp_signal = "Moderate money on AWAY"

    return {
        "match": current["match_name"],
        "snapshots": len(rows),
        "opening": _odds_row(opening),
        "current": _odds_row(current),
        "movement": {
            "home": _move(opening["home_odds"], current["home_odds"]),
            "draw": _move(opening["draw_odds"], current["draw_odds"]),
            "away": _move(opening["away_odds"], current["away_odds"]),
        },
        "sharp_signal": sharp_signal,
    }


def get_all_movements(match_date: str | None = None) -> list[dict[str, Any]]:
    _init_db()
    query = "select distinct match_id from odds_snapshots"
    params: tuple[Any, ...] = ()
    if match_date:
        query += " where match_date = ?"
        params = (match_date,)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(query, params).fetchall()
    return [get_movement(row[0]) for row in rows]


def _extract_1x2_sportybet(markets: list[dict[str, Any]]) -> dict[str, float]:
    for market in markets:
        name = (market.get("name") or "").lower()
        if market.get("id") == "1" or "1x2" in name or name == "match result":
            odds = {selection.get("name"): _to_float(selection.get("odds")) for selection in market.get("selections", [])}
            return {"home": odds.get("Home") or odds.get("1"), "draw": odds.get("Draw") or odds.get("X"), "away": odds.get("Away") or odds.get("2")}
    return {}


def _extract_1x2_sofascore(odds_featured: dict[str, Any] | None) -> dict[str, float]:
    market = ((odds_featured or {}).get("full_time") or (odds_featured or {}).get("default") or {})
    result = {}
    for choice in market.get("choices", []):
        decimal = _fractional_to_decimal(choice.get("fractional_value"))
        if choice.get("name") == "1":
            result["home"] = decimal
        elif choice.get("name") == "X":
            result["draw"] = decimal
        elif choice.get("name") == "2":
            result["away"] = decimal
    return result


def _fractional_to_decimal(value: Any) -> float | None:
    try:
        return round(float(Fraction(str(value))) + 1, 3)
    except Exception:
        return None


def _implied(decimal_odds: float | None) -> float | None:
    if not decimal_odds or decimal_odds <= 0:
        return None
    return round(100 / decimal_odds, 2)


def _move(opening: float | None, current: float | None) -> str | None:
    if not opening or not current:
        return None
    diff = round(current - opening, 3)
    if diff < -0.05:
        return "shortened"
    if diff > 0.05:
        return "drifted"
    return "stable"


def _odds_row(row: sqlite3.Row) -> dict[str, Any]:
    return {"home": row["home_odds"], "draw": row["draw_odds"], "away": row["away_odds"], "time": row["snapshot_time"]}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
