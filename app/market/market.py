from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db
from app.config.config import get_settings


def snapshot_odds(doc: dict[str, Any]) -> bool:
    _init_db()
    settings = get_settings()
    markets = doc.get("sportybet_markets") or doc.get("markets") or []
    odds = _extract_1x2_sportybet(markets)
    odds_source = "sportybet"
    if not odds.get("home"):
        detail = doc.get("sofascore_detail") or doc
        odds = _extract_1x2_sofascore(detail.get("odds_featured") or detail.get("oddsFeatured"))
        odds_source = "sofascore"

    match_id = str(doc.get("sportybet_id") or doc.get("id") or doc.get("match_id") or "")
    if not match_id:
        return False

    match_name = doc.get("sportybet_name") or doc.get("name")
    match_date = doc.get("match_date")
    snapshot_time = datetime.now(timezone.utc).isoformat()
    wrote_any = False

    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")
        _ensure_tables(conn)
        odds_state = _get_snapshot_state(conn, match_id, odds_source)
        if odds.get("home"):
            sig_1x2 = _sig_1x2(odds)
            if sig_1x2 and sig_1x2 != (odds_state.get("last_1x2_sig") if odds_state else None):
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
                        odds_source,
                        snapshot_time,
                    ),
                )
                _upsert_snapshot_state(conn, match_id, odds_source, last_1x2_sig=sig_1x2)
                wrote_any = True

        market_rows = _extract_market_rows(markets, source="sportybet")
        if not market_rows:
            market_rows = _extract_sofascore_market_rows(doc.get("sofascore_detail") or doc)
        if market_rows and settings.odds_track_mode != "off":
            filtered = _filter_market_rows(
                market_rows,
                allowed=settings.odds_track_markets,
                max_rows=settings.odds_track_max_market_rows,
            )
            market_source = str(filtered[0].get("source") if filtered else (market_rows[0].get("source") if market_rows else odds_source) or odds_source)
            if settings.odds_track_mode == "full":
                # Backwards-compatible mode: only insert when the entire market grid changes.
                market_state = _get_snapshot_state(conn, match_id, market_source)
                sig_markets = _sig_market_rows(filtered)
                if sig_markets and sig_markets != (market_state.get("last_market_sig") if market_state else None):
                    for row in filtered:
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
                    _upsert_snapshot_state(conn, match_id, market_source, last_market_sig=sig_markets)
                    wrote_any = True
            else:
                # Lean mode: write only per-selection changes for a curated market set.
                wrote_any = _record_market_changes(
                    conn,
                    match_id=match_id,
                    match_name=match_name,
                    match_date=match_date,
                    rows=filtered,
                    snapshot_time=snapshot_time,
                    min_change=settings.odds_track_min_change,
                ) or wrote_any
        conn.commit()

    if wrote_any:
        # If the 1x2 snapshot was deduped but markets changed, we still persist the
        # snapshot in Mongo with the 1x2 odds payload we observed on this run.
        _save_mongo_snapshot(match_id, match_name, match_date, odds, odds_source, market_rows, snapshot_time)
    return wrote_any


def get_movement(match_id: str) -> dict[str, Any]:
    _init_db()
    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")
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
    pulls = {
        "home": _market_pull(opening["home_odds"], current["home_odds"]),
        "draw": _market_pull(opening["draw_odds"], current["draw_odds"]),
        "away": _market_pull(opening["away_odds"], current["away_odds"]),
    }
    strongest_pull = _strongest_pull(pulls)
    sharp_signal = None
    if strongest_pull and strongest_pull.get("direction") == "backed":
        label = strongest_pull["selection"].upper()
        strength = "Sharp" if strongest_pull.get("strength") == "strong" else "Moderate"
        sharp_signal = f"{strength} market backing on {label}"
    elif strongest_pull and strongest_pull.get("direction") == "faded":
        label = strongest_pull["selection"].upper()
        strength = "Sharp" if strongest_pull.get("strength") == "strong" else "Moderate"
        sharp_signal = f"{strength} market fade on {label}"

    return {
        "match": current["match_name"],
        "snapshots": len(rows),
        "opening": _odds_row(opening),
        "current": _odds_row(current),
        # full 1X2 time-series for the chart
        "series": [
            {
                "time": row["snapshot_time"],
                "home": row["home_odds"],
                "draw": row["draw_odds"],
                "away": row["away_odds"],
            }
            for row in rows
        ],
        "movement": {
            "home": _move(opening["home_odds"], current["home_odds"]),
            "draw": _move(opening["draw_odds"], current["draw_odds"]),
            "away": _move(opening["away_odds"], current["away_odds"]),
        },
        "market_pull": pulls,
        "strongest_pull": strongest_pull,
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
    with db_conn(timeout=30) as conn:
        conn.execute("pragma busy_timeout = 30000")
        rows = conn.execute(query, params).fetchall()
    return [get_movement(row[0]) for row in rows]


def _ensure_tables(conn: sqlite3.Connection) -> None:
    settings = get_settings()
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
        create table if not exists odds_market_changes (
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
    conn.execute(
        """
        create table if not exists odds_market_change_state (
            match_id text not null,
            source text not null,
            market_id text not null,
            specifier text not null,
            selection_id text not null,
            last_odds real,
            updated_at text not null default current_timestamp,
            primary key (match_id, source, market_id, specifier, selection_id)
        )
        """
    )
    if settings.odds_track_mode == "full":
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
    conn.execute(
        """
        create table if not exists odds_snapshot_state (
            match_id text not null,
            source text not null,
            last_1x2_sig text,
            last_market_sig text,
            updated_at text not null default current_timestamp,
            primary key (match_id, source)
        )
        """
    )
    if settings.odds_track_mode == "full":
        conn.execute("create index if not exists idx_odds_market_match on odds_market_snapshots(match_id)")
        conn.execute("create index if not exists idx_odds_market_date on odds_market_snapshots(match_date)")
    conn.execute("create index if not exists idx_odds_market_changes_match on odds_market_changes(match_id)")
    conn.execute("create index if not exists idx_odds_market_changes_date on odds_market_changes(match_date)")


def _all_market_movements(conn: sqlite3.Connection, match_id: str) -> list[dict[str, Any]]:
    table = _market_table_for_match(conn, match_id)
    rows = conn.execute(
        f"""
        select *
        from {table}
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
                "market_pull": _market_pull(opening["odds"], current["odds"]),
                "source": current["source"],
                # full time-series for the chart
                "snapshots_data": [
                    {"time": row["snapshot_time"], "odds": row["odds"]}
                    for row in items
                ],
            }
        )
    result.sort(key=lambda x: (x["market"], x["specifier"], x["selection"]))
    return result


def _market_table_for_match(conn: sqlite3.Connection, match_id: str) -> str:
    """Prefer lean per-selection change points when available."""
    try:
        conn.execute("select 1 from odds_market_changes limit 1")
        row = conn.execute(
            "select 1 from odds_market_changes where match_id = ? limit 1",
            (str(match_id),),
        ).fetchone()
        if row:
            return "odds_market_changes"
    except Exception:
        pass
    try:
        legacy = conn.execute(
            "select 1 from sqlite_master where type='table' and name='odds_market_snapshots'",
        ).fetchone()
        if legacy:
            return "odds_market_snapshots"
    except Exception:
        pass
    return "odds_market_changes"


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
        from app.storage.mongo_store import save_odds_snapshot

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
    pct = _percent_change(opening, current)
    if pct is None:
        return None
    if pct <= -3:
        return "shortened"
    if pct >= 3:
        return "drifted"
    return "stable"


def _delta(opening: float | None, current: float | None) -> float | None:
    if opening is None or current is None:
        return None
    return round(current - opening, 3)


def _percent_change(opening: float | None, current: float | None) -> float | None:
    if not opening or not current:
        return None
    return round((current - opening) / opening * 100, 2)


def _market_pull(opening: float | None, current: float | None) -> dict[str, Any] | None:
    if not opening or not current:
        return None
    odds_change = _percent_change(opening, current)
    opening_implied = 100 / opening
    current_implied = 100 / current
    implied_change = round((current_implied - opening_implied) / opening_implied * 100, 2)
    implied_points = round(current_implied - opening_implied, 2)

    magnitude = max(abs(odds_change or 0), abs(implied_change))
    if odds_change is not None and odds_change <= -10:
        direction = "backed"
    elif odds_change is not None and odds_change >= 10:
        direction = "faded"
    elif implied_change >= 7:
        direction = "backed"
    elif implied_change <= -7:
        direction = "faded"
    else:
        direction = "stable"

    if magnitude >= 10:
        strength = "strong"
    elif magnitude >= 5:
        strength = "moderate"
    else:
        strength = "light"

    return {
        "opening_odds": round(opening, 3),
        "current_odds": round(current, 3),
        "odds_change_percent": odds_change,
        "opening_implied": round(opening_implied, 2),
        "current_implied": round(current_implied, 2),
        "implied_change_percent": implied_change,
        "implied_probability_points": implied_points,
        "direction": direction,
        "strength": strength,
        "market_belief": "increasing" if direction == "backed" else "decreasing" if direction == "faded" else "stable",
    }


def _strongest_pull(pulls: dict[str, dict[str, Any] | None]) -> dict[str, Any] | None:
    candidates = []
    for selection, pull in pulls.items():
        if not pull or pull.get("direction") == "stable":
            continue
        candidates.append({**pull, "selection": selection})
    if not candidates:
        return None
    return max(candidates, key=lambda item: abs(float(item.get("implied_change_percent") or 0)))


def _odds_row(row: sqlite3.Row) -> dict[str, Any]:
    return {"home": row["home_odds"], "draw": row["draw_odds"], "away": row["away_odds"], "time": row["snapshot_time"]}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sig_1x2(odds: dict[str, float]) -> str | None:
    try:
        home = odds.get("home")
        draw = odds.get("draw")
        away = odds.get("away")
        if home is None and draw is None and away is None:
            return None
        payload = "|".join(_sig_num(v) for v in (home, draw, away))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()
    except Exception:
        return None


def _sig_market_rows(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    try:
        parts: list[str] = []
        for row in sorted(
            rows,
            key=lambda item: (
                str(item.get("market_id") or ""),
                str(item.get("specifier") or ""),
                str(item.get("selection_id") or ""),
            ),
        ):
            parts.append(
                "|".join(
                    [
                        str(row.get("market_id") or ""),
                        str(row.get("specifier") or ""),
                        str(row.get("selection_id") or ""),
                        _sig_num(row.get("odds")),
                    ]
                )
            )
        payload = "\n".join(parts)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()
    except Exception:
        return None


def _sig_num(value: Any) -> str:
    try:
        if value is None or value == "":
            return ""
        num = float(value)
        return f"{num:.3f}"
    except Exception:
        return str(value or "")


def _get_snapshot_state(conn: sqlite3.Connection, match_id: str, source: str) -> dict[str, Any] | None:
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            select match_id, source, last_1x2_sig, last_market_sig, updated_at
            from odds_snapshot_state
            where match_id = ? and source = ?
            """,
            (str(match_id), str(source)),
        ).fetchone()
        if not row:
            return None
        return {
            "match_id": row["match_id"],
            "source": row["source"],
            "last_1x2_sig": row["last_1x2_sig"],
            "last_market_sig": row["last_market_sig"],
            "updated_at": row["updated_at"],
        }
    except Exception:
        return None


def _upsert_snapshot_state(
    conn: sqlite3.Connection,
    match_id: str,
    source: str,
    *,
    last_1x2_sig: str | None = None,
    last_market_sig: str | None = None,
) -> None:
    if last_1x2_sig is None and last_market_sig is None:
        return
    conn.execute(
        """
        insert into odds_snapshot_state (match_id, source, last_1x2_sig, last_market_sig, updated_at)
        values (?, ?, ?, ?, current_timestamp)
        on conflict(match_id, source) do update set
            last_1x2_sig = coalesce(excluded.last_1x2_sig, odds_snapshot_state.last_1x2_sig),
            last_market_sig = coalesce(excluded.last_market_sig, odds_snapshot_state.last_market_sig),
            updated_at = current_timestamp
        """,
        (str(match_id), str(source), last_1x2_sig, last_market_sig),
    )


def _filter_market_rows(rows: list[dict[str, Any]], *, allowed: list[str], max_rows: int) -> list[dict[str, Any]]:
    allowed_set = {str(item or "").strip().lower() for item in (allowed or []) if str(item or "").strip()}
    if not allowed_set:
        return rows[: max(0, int(max_rows or 0))] if max_rows else rows

    curated = []
    for row in rows:
        family = _market_family(row.get("market_name") or row.get("market_id") or "")
        if family in allowed_set:
            if family == "total_goals" and not _is_core_total_line(row):
                continue
            curated.append(row)
        if max_rows and len(curated) >= int(max_rows):
            break
    return curated


def _market_family(market_name: str) -> str:
    name = str(market_name or "").lower()
    if "1x2" in name or name.strip() == "match result":
        return "1x2"
    if "double chance" in name or name.strip() in {"1x", "x2", "12"}:
        return "double_chance"
    if "both teams" in name or "btts" in name:
        return "btts"
    if "over/under" in name or "total goals" in name or "goals" == name.strip():
        return "total_goals"
    return "other"


def _is_core_total_line(row: dict[str, Any]) -> bool:
    text = f"{row.get('specifier') or ''} {row.get('selection_name') or ''}".lower()
    for line in ("0.5", "1.5", "2.5", "3.5", "4.5"):
        if line in text:
            return True
    return False


def _record_market_changes(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    match_name: str | None,
    match_date: str | None,
    rows: list[dict[str, Any]],
    snapshot_time: str,
    min_change: float,
) -> bool:
    if not rows:
        return False
    wrote = False
    for row in rows:
        source = str(row.get("source") or "unknown")
        market_id = str(row.get("market_id") or row.get("market_name") or "Market")
        specifier = str(row.get("specifier") or "")
        selection_id = str(row.get("selection_id") or row.get("selection_name") or "Selection")
        odds = row.get("odds")
        if odds is None:
            continue

        existing = conn.execute(
            """
            select last_odds
            from odds_market_change_state
            where match_id = ? and source = ? and market_id = ? and specifier = ? and selection_id = ?
            """,
            (match_id, source, market_id, specifier, selection_id),
        ).fetchone()
        last_odds = float(existing[0]) if existing and existing[0] is not None else None
        if last_odds is not None and abs(float(odds) - last_odds) < float(min_change or 0.0):
            continue

        conn.execute(
            """
            insert into odds_market_changes (
                match_id, match_name, match_date, market_id, market_name, specifier,
                selection_id, selection_name, odds, probability, source, snapshot_time
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                match_name,
                match_date,
                market_id,
                row.get("market_name"),
                specifier,
                selection_id,
                row.get("selection_name"),
                odds,
                _implied(odds),
                source,
                snapshot_time,
            ),
        )
        conn.execute(
            """
            insert into odds_market_change_state (
                match_id, source, market_id, specifier, selection_id, last_odds, updated_at
            ) values (?, ?, ?, ?, ?, ?, current_timestamp)
            on conflict(match_id, source, market_id, specifier, selection_id) do update set
                last_odds = excluded.last_odds,
                updated_at = current_timestamp
            """,
            (match_id, source, market_id, specifier, selection_id, float(odds)),
        )
        wrote = True
    return wrote

