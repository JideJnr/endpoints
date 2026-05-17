from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any

from app.league_memory import DB_PATH, _init_db


def snapshot_odds(doc: dict[str, Any]) -> bool:
    _init_db()
    markets = doc.get("sportybet_markets") or doc.get("markets") or []
    odds = _extract_1x2_sportybet(markets)
    source = "sportybet"
    if not odds.get("home"):
        detail = doc.get("sofascore_detail") or doc
        odds = _extract_1x2_sofascore(detail.get("odds_featured") or detail.get("oddsFeatured"))
        source = "sofascore"

    match_id = str(doc.get("sportybet_id") or doc.get("id") or doc.get("match_id") or "")
    if not match_id:
        return False

    match_name = doc.get("sportybet_name") or doc.get("name")
    match_date = doc.get("match_date")
    snapshot_time = datetime.now(timezone.utc).isoformat()
    wrote_any = False

    with sqlite3.connect(DB_PATH) as conn:
        _ensure_tables(conn)
        if odds.get("home"):
            conn.execute(
                """
                insert into odds_snapshots (
                    match_id, match_name, match_date, home_odds, draw_odds, away_odds,
                    home_implied, draw_implied, away_implied, source, snapshot_time
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    match_name,
                    match_date,
                    odds.get("home"),
                    odds.get("draw"),
                    odds.get("away"),
                    _implied(odds.get("home")),
                    _implied(odds.get("draw")),
                    _implied(odds.get("away")),
                    source,
                    snapshot_time,
                ),
            )
            wrote_any = True

        market_rows = _extract_market_rows(markets, source="sportybet")
        if not market_rows:
            market_rows = _extract_sofascore_market_rows(doc.get("sofascore_detail") or doc)
        for row in market_rows:
            conn.execute(
                """
                insert into odds_market_snapshots (
                    match_id, match_name, match_date, market_id, market_name, specifier,
                    selection_id, selection_name, odds, probability, source, snapshot_time
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    match_name,
                    match_date,
                    row.get("market_id"),
                    row.get("market_name"),
                    row.get("specifier"),
                    row.get("selection_id"),
                    row.get("selection_name"),
                    row.get("odds"),
                    _implied(row.get("odds")),
                    row.get("source"),
                    snapshot_time,
                ),
            )
            wrote_any = True
        conn.commit()

    if wrote_any:
        _save_mongo_snapshot(match_id, match_name, match_date, odds, source, market_rows, snapshot_time)
    return wrote_any


def get_movement(match_id: str) -> dict[str, Any]:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_tables(conn)
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
        markets = _all_market_movements(conn, str(match_id))

    if len(rows) < 2:
        return {
            "snapshots": len(rows),
            "movement": None,
            "markets": markets,
            "market_snapshots": sum(item.get("snapshots", 0) for item in markets),
        }

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
        "markets": markets,
        "market_snapshots": sum(item.get("snapshots", 0) for item in markets),
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


def _ensure_tables(conn: sqlite3.Connection) -> None:
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
        create table if not exists odds_market_snapshots (
            id integer primary key autoincrement,
            match_id text not null,
            match_name text,
            match_date text,
            market_id text,
            market_name text,
            specifier text,
            selection_id text,
            selection_name text,
            odds real,
            probability real,
            source text,
            snapshot_time text not null default current_timestamp
        )
        """
    )
    conn.execute("create index if not exists idx_odds_market_match on odds_market_snapshots(match_id)")
    conn.execute("create index if not exists idx_odds_market_date on odds_market_snapshots(match_date)")


def _all_market_movements(conn: sqlite3.Connection, match_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select *
        from odds_market_snapshots
        where match_id = ?
          and odds is not null
        order by market_name asc, specifier asc, selection_name asc, snapshot_time asc
        """,
        (match_id,),
    ).fetchall()
    grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (
            row["market_name"] or row["market_id"] or "Market",
            row["specifier"] or "",
            row["selection_name"] or row["selection_id"] or "Selection",
        )
        grouped.setdefault(key, []).append(row)

    result = []
    for (market_name, specifier, selection_name), items in grouped.items():
        opening = items[0]
        current = items[-1]
        result.append(
            {
                "market": market_name,
                "specifier": specifier,
                "selection": selection_name,
                "snapshots": len(items),
                "opening": {"odds": opening["odds"], "time": opening["snapshot_time"]},
                "current": {"odds": current["odds"], "time": current["snapshot_time"]},
                "movement": _move(opening["odds"], current["odds"]),
                "delta": _delta(opening["odds"], current["odds"]),
                "source": current["source"],
            }
        )
    result.sort(key=lambda x: (x["market"], x["specifier"], x["selection"]))
    return result


def _extract_market_rows(markets: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    rows = []
    for market in markets or []:
        market_id = market.get("id") or market.get("market_id") or market.get("marketId")
        market_name = market.get("name") or market.get("market_name") or market.get("marketName") or str(market_id or "Market")
        specifier = market.get("specifier") or market.get("desc") or market.get("line")
        for selection in market.get("selections") or market.get("choices") or []:
            odds = _to_float(selection.get("odds") or selection.get("decimalOdds") or selection.get("decimal_odds"))
            if odds is None and selection.get("fractional_value"):
                odds = _fractional_to_decimal(selection.get("fractional_value"))
            if odds is None:
                continue
            rows.append(
                {
                    "market_id": str(market_id or market_name),
                    "market_name": market_name,
                    "specifier": str(specifier or ""),
                    "selection_id": str(selection.get("id") or selection.get("selection_id") or selection.get("selectionId") or selection.get("name") or ""),
                    "selection_name": selection.get("name") or selection.get("label") or selection.get("outcome") or "Selection",
                    "odds": odds,
                    "source": source,
                }
            )
    return rows


def _extract_sofascore_market_rows(detail: dict[str, Any]) -> list[dict[str, Any]]:
    featured = detail.get("odds_featured") or detail.get("oddsFeatured") or {}
    rows = []
    for key, market in featured.items():
        if not isinstance(market, dict):
            continue
        market_name = market.get("market_name") or market.get("marketName") or key
        for choice in market.get("choices") or []:
            odds = _fractional_to_decimal(choice.get("fractional_value") or choice.get("fractionalValue"))
            if odds is None:
                continue
            rows.append(
                {
                    "market_id": key,
                    "market_name": market_name,
                    "specifier": market.get("market_period") or market.get("marketPeriod") or "",
                    "selection_id": choice.get("name"),
                    "selection_name": choice.get("name"),
                    "odds": odds,
                    "source": "sofascore",
                }
            )
    return rows


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


def _save_mongo_snapshot(
    match_id: str,
    match_name: str | None,
    match_date: str | None,
    odds: dict[str, Any],
    source: str,
    market_rows: list[dict[str, Any]],
    snapshot_time: str,
) -> None:
    try:
        from app.mongo_store import save_odds_snapshot

        save_odds_snapshot({
            "sportybet_id": match_id,
            "match": match_name,
            "match_date": match_date,
            "snapshot_time": snapshot_time,
            "home_odds": odds.get("home"),
            "draw_odds": odds.get("draw"),
            "away_odds": odds.get("away"),
            "home_implied": _implied(odds.get("home")),
            "draw_implied": _implied(odds.get("draw")),
            "away_implied": _implied(odds.get("away")),
            "markets": market_rows,
            "source": source,
        })
    except Exception:
        pass


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


def _delta(opening: float | None, current: float | None) -> float | None:
    if opening is None or current is None:
        return None
    return round(current - opening, 3)


def _odds_row(row: sqlite3.Row) -> dict[str, Any]:
    return {"home": row["home_odds"], "draw": row["draw_odds"], "away": row["away_odds"], "time": row["snapshot_time"]}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
