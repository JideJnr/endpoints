from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.db import connect_db, db_conn
from app.league_memory import _init_db


def init_mobile_bridge_db(conn: sqlite3.Connection | None = None) -> None:
    owns_conn = conn is None
    if conn is None:
        _init_db()
        conn = connect_db(timeout=30)
    try:
        conn.execute("pragma busy_timeout = 30000")
        conn.execute("""
            create table if not exists mobile_provider_packets (
                packet_id       text primary key,
                source          text not null,
                endpoint        text,
                scope           text,
                match_id        text,
                device_id       text,
                captured_at     text,
                received_at     text not null,
                status          text not null default 'pending',
                ingest_status   text not null default 'stored',
                ingest_summary  text,
                request_json    text,
                response_json   text not null,
                error           text,
                acknowledged_at text
            )
        """)
        conn.execute("create index if not exists idx_mobile_packets_status on mobile_provider_packets(status)")
        conn.execute("create index if not exists idx_mobile_packets_source on mobile_provider_packets(source, scope)")
        conn.execute("create index if not exists idx_mobile_packets_received on mobile_provider_packets(received_at)")
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def receive_provider_packet(packet: dict[str, Any]) -> dict[str, Any]:
    source = _clean_source(packet.get("source"))
    response = packet.get("response")
    if not source:
        raise ValueError("source is required")
    if response is None:
        raise ValueError("response is required")

    received_at = datetime.now(timezone.utc).isoformat()
    captured_at = str(packet.get("captured_at") or received_at)
    packet_id = str(packet.get("packet_id") or _packet_id(packet, source))
    endpoint = str(packet.get("endpoint") or "")
    scope = str(packet.get("scope") or _scope_from_request(packet.get("request") or {}))
    match_id = str(packet.get("match_id") or "")
    request_payload = packet.get("request") or {}
    response_json = json.dumps(response, separators=(",", ":"), sort_keys=True)
    request_json = json.dumps(request_payload, separators=(",", ":"), sort_keys=True)

    _init_db()
    with db_conn(timeout=30) as conn:
        init_mobile_bridge_db(conn)
        existing = conn.execute(
            "select packet_id, ingest_status, ingest_summary, status from mobile_provider_packets where packet_id = ?",
            (packet_id,),
        ).fetchone()
        if existing:
            return {
                "status": "duplicate",
                "packet_id": packet_id,
                "ingest_status": existing[1],
                "ingest_summary": _json_or_none(existing[2]),
                "sync_status": existing[3],
            }

        conn.execute(
            """
            insert into mobile_provider_packets (
                packet_id, source, endpoint, scope, match_id, device_id,
                captured_at, received_at, status, request_json, response_json
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                packet_id,
                source,
                endpoint,
                scope,
                match_id,
                str(packet.get("device_id") or ""),
                captured_at,
                received_at,
                request_json,
                response_json,
            ),
        )
        conn.commit()

    ingest = ingest_packet(packet_id)
    return {
        "status": "success",
        "packet_id": packet_id,
        "source": source,
        "scope": scope,
        "ingest": ingest,
    }


def ingest_packet(packet_id: str) -> dict[str, Any]:
    _init_db()
    with db_conn(timeout=30) as conn:
        init_mobile_bridge_db(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select * from mobile_provider_packets where packet_id = ?",
            (packet_id,),
        ).fetchone()
    if not row:
        return {"status": "error", "error": "packet_not_found"}

    source = row["source"]
    try:
        response = json.loads(row["response_json"])
        if source == "sportybet":
            summary = _ingest_sportybet_response(response, row["scope"])
        elif source in {"sofascore", "sportradar"}:
            summary = {"status": "stored", "note": f"{source} packet stored; parser hook pending", "ingested": 0}
        else:
            summary = {"status": "stored", "note": f"unknown source stored: {source}", "ingested": 0}
        _update_ingest(packet_id, summary.get("status") or "stored", summary, None)
        return summary
    except Exception as exc:
        summary = {"status": "error", "error": str(exc), "ingested": 0}
        _update_ingest(packet_id, "error", summary, str(exc))
        return summary


def list_provider_packets(status: str | None = None, limit: int = 100) -> dict[str, Any]:
    _init_db()
    with db_conn(timeout=30) as conn:
        init_mobile_bridge_db(conn)
        conn.row_factory = sqlite3.Row
        params: list[Any] = []
        where = ""
        if status:
            where = "where status = ?"
            params.append(status)
        rows = conn.execute(
            f"""
            select packet_id, source, endpoint, scope, match_id, device_id,
                   captured_at, received_at, status, ingest_status, ingest_summary, error
            from mobile_provider_packets
            {where}
            order by datetime(received_at) desc
            limit ?
            """,
            (*params, int(limit)),
        ).fetchall()
    return {
        "status": "success",
        "count": len(rows),
        "packets": [_packet_row(row) for row in rows],
    }


def mobile_bridge_status() -> dict[str, Any]:
    _init_db()
    with db_conn(timeout=30) as conn:
        init_mobile_bridge_db(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select source, status, ingest_status, count(*) as count
            from mobile_provider_packets
            group by source, status, ingest_status
            order by source, status, ingest_status
            """
        ).fetchall()
        latest = conn.execute("select max(received_at) from mobile_provider_packets").fetchone()[0]
    return {
        "status": "success",
        "latest_received_at": latest,
        "groups": [dict(row) for row in rows],
    }


def acknowledge_packets(packet_ids: list[str]) -> dict[str, Any]:
    if not packet_ids:
        return {"status": "success", "acknowledged": 0}
    now = datetime.now(timezone.utc).isoformat()
    _init_db()
    with db_conn(timeout=30) as conn:
        init_mobile_bridge_db(conn)
        count = 0
        for packet_id in packet_ids:
            cur = conn.execute(
                """
                update mobile_provider_packets
                set status = 'acknowledged', acknowledged_at = ?
                where packet_id = ? and status != 'acknowledged'
                """,
                (now, str(packet_id)),
            )
            count += cur.rowcount or 0
        conn.commit()
    return {"status": "success", "acknowledged": count}


def _ingest_sportybet_response(response: dict[str, Any], scope: str | None) -> dict[str, Any]:
    from app.buffer import ingest_matches
    from app.sportybet_client import parse_events_response

    matches = parse_events_response(response)
    by_date: dict[str, list[dict[str, Any]]] = {}
    for match in matches:
        by_date.setdefault(_date_from_start_time(match.get("start_time")), []).append(match)
    ingested = 0
    for match_date, rows in by_date.items():
        ingested += ingest_matches(rows, match_date)
    return {
        "status": "ingested",
        "source": "sportybet",
        "scope": scope,
        "matches": len(matches),
        "dates": sorted(by_date),
        "ingested": ingested,
    }


def _update_ingest(packet_id: str, ingest_status: str, summary: dict[str, Any], error: str | None) -> None:
    with db_conn(timeout=30) as conn:
        init_mobile_bridge_db(conn)
        conn.execute(
            """
            update mobile_provider_packets
            set ingest_status = ?, ingest_summary = ?, error = ?
            where packet_id = ?
            """,
            (ingest_status, json.dumps(summary), error, packet_id),
        )
        conn.commit()


def _packet_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["ingest_summary"] = _json_or_none(item.get("ingest_summary"))
    return item


def _clean_source(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _scope_from_request(request_payload: dict[str, Any]) -> str:
    if request_payload.get("isLive") is True:
        return "live"
    if request_payload.get("isLive") is False:
        return "upcoming"
    return str(request_payload.get("scope") or "unknown")


def _packet_id(packet: dict[str, Any], source: str) -> str:
    basis = {
        "source": source,
        "endpoint": packet.get("endpoint"),
        "scope": packet.get("scope") or _scope_from_request(packet.get("request") or {}),
        "match_id": packet.get("match_id"),
        "captured_at": packet.get("captured_at"),
        "response": packet.get("response"),
    }
    raw = json.dumps(basis, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _date_from_start_time(value: Any) -> str:
    try:
        ts = float(value)
        if ts > 1e12:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def _json_or_none(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value
