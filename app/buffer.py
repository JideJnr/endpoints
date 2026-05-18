"""
Match Buffer
------------
Two-phase pipeline:

Phase 1 — INGEST (fast, every 15 min upcoming / every 5 min live)
  - Dump raw SportyBet matches into `match_buffer` table
  - No SofaScore, no web search, no enrichment
  - Just: id, name, teams, score, period, odds, markets, start_time
  - Frontend can already show these immediately

Phase 2 — ENRICH (background worker, continuous)
  - Picks up matches that have never been enriched OR are stale (enriched_at > STALE_MINUTES ago)
  - Fetches SofaScore detail + web context for each
  - Updates the same row with enriched data
  - Runs in a tight loop with a short sleep between batches

Staleness rules:
  - Never enriched          → enrich immediately
  - Live match              → re-enrich every LIVE_STALE_MINUTES (10 min)
  - Upcoming match          → re-enrich every UPCOMING_STALE_MINUTES (60 min)
  - Finished match          → never re-enrich (skip)
"""
from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any

from app.league_memory import DB_PATH, _init_db
from app.market import snapshot_odds
from app.normalise import normalise

# How many SofaScore detail calls to run in parallel
ENRICH_WORKERS = 8
WEB_WORKERS = 10

# How many matches to enrich per worker cycle
ENRICH_BATCH_SIZE = 10


# ── Table init ────────────────────────────────────────────────────────────────

def _init_buffer_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        create table if not exists match_buffer (
            match_id        text primary key,
            match_date      text,
            tournament      text,
            category        text,
            name            text,
            start_time      integer,
            period          text,
            score_home      text,
            score_away      text,
            is_live         integer not null default 0,
            is_finished     integer not null default 0,
            ingested_at     text not null default current_timestamp,
            enriched_at     text,
            sofascore_id    text,
            raw_sporty      text not null,
            raw_enriched    text
        )
    """)
    conn.execute("create index if not exists idx_buffer_date    on match_buffer(match_date)")
    conn.execute("create index if not exists idx_buffer_live    on match_buffer(is_live)")
    conn.execute("create index if not exists idx_buffer_enrich  on match_buffer(enriched_at)")


# ── Phase 1: Ingest ───────────────────────────────────────────────────────────

def ingest_matches(matches: list[dict[str, Any]], match_date: str) -> int:
    """
    Fast ingest of raw SportyBet matches into the buffer.
    Upserts score/period/markets if match already exists.
    Finished matches are skipped — handled by patch_live_scores on transition.
    Returns number of NEW rows inserted (not upserts).
    """
    if not matches:
        return 0
    _init_db()
    now = datetime.now(timezone.utc).isoformat()
    from datetime import date as _date
    today = _date.today().isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        _init_buffer_table(conn)
        count = 0
        for m in matches:
            match_id = str(m.get("id") or "")
            if not match_id:
                continue

            score = m.get("score") or {}
            period = m.get("period") or "Not start"
            is_live = 1 if (period and period not in ("Not start", "", None)) else 0
            is_finished = 1 if _is_finished_period(period) else 0

            # finished matches are handled by patch_live_scores on transition — skip here
            if is_finished:
                continue

            # 90+ minutes — treat as done, skip entirely
            if _is_90_plus(m.get("played_seconds")):
                continue

            # Reject matches dated before today that are not currently live
            # SportyBet sometimes keeps old fixtures in the upcoming feed indefinitely
            if match_date < today and not is_live:
                continue

            # kick-off time passed 2+ hours ago and still not live — ghost/postponed, skip
            if _is_ghost_match(m.get("start_time"), period):
                continue

            # only count genuinely new rows
            exists = conn.execute(
                "select 1 from match_buffer where match_id = ?", (match_id,)
            ).fetchone()

            conn.execute(
                """
                insert into match_buffer (
                    match_id, match_date, tournament, category, name,
                    start_time, period, score_home, score_away,
                    is_live, is_finished, ingested_at, raw_sporty
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(match_id) do update set
                    match_date  = excluded.match_date,
                    tournament  = excluded.tournament,
                    category    = excluded.category,
                    name        = excluded.name,
                    start_time  = excluded.start_time,
                    period      = excluded.period,
                    score_home  = excluded.score_home,
                    score_away  = excluded.score_away,
                    is_live     = excluded.is_live,
                    is_finished = excluded.is_finished,
                    raw_sporty  = excluded.raw_sporty,
                    ingested_at = excluded.ingested_at
                """,
                (
                    match_id, match_date,
                    m.get("tournament"), m.get("category"), m.get("name"),
                    m.get("start_time"), period,
                    str(score.get("home") or ""), str(score.get("away") or ""),
                    is_live, is_finished, now, json.dumps(m),
                ),
            )
            if not exists:
                count += 1
        conn.commit()
    return count


def patch_live_scores(matches: list[dict[str, Any]]) -> int:
    """
    Fast update of score/period for live matches already in the buffer.
    When a match transitions to finished, archives to MongoDB and removes from buffer.
    """
    if not matches:
        return 0
    _init_db()
    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        _init_buffer_table(conn)
        count = 0
        for m in matches:
            match_id = str(m.get("id") or "")
            if not match_id:
                continue
            score = m.get("score") or {}
            period = m.get("period") or ""
            is_live = 1 if (period and period not in ("Not start", "", None)) else 0
            is_finished = 1 if _is_finished_period(period) else 0
            is_90_plus = _is_90_plus(m.get("played_seconds"))

            if is_finished or is_90_plus:
                # write final score into both raw_sporty and raw_enriched before archiving
                # write final score into both raw_sporty and raw_enriched before archiving
                conn.execute(
                    """
                    update match_buffer set
                        period = ?, score_home = ?, score_away = ?,
                        is_live = 0, is_finished = 1, raw_sporty = ?
                    where match_id = ?
                    """,
                    (
                        period,
                        str(score.get("home") or ""),
                        str(score.get("away") or ""),
                        json.dumps(m),
                        match_id,
                    ),
                )
                row = conn.execute(
                    "select raw_enriched from match_buffer where match_id = ?", (match_id,)
                ).fetchone()
                if row and row[0]:
                    try:
                        enriched_doc = json.loads(row[0])
                        enriched_doc["period"] = period
                        enriched_doc["score"] = score
                        enriched_doc["is_finished"] = True
                        conn.execute(
                            "update match_buffer set raw_enriched = ? where match_id = ?",
                            (json.dumps(enriched_doc), match_id),
                        )
                    except Exception:
                        pass
                conn.commit()
                _try_archive_finished(match_id)
                count += 1
                continue

            # still live — patch score/period in place
            row = conn.execute(
                "select raw_enriched from match_buffer where match_id = ?", (match_id,)
            ).fetchone()
            new_enriched = None
            if row and row[0]:
                try:
                    doc = json.loads(row[0])
                    doc["period"] = period
                    doc["score"] = score
                    doc["played_seconds"] = m.get("played_seconds")
                    new_enriched = json.dumps(doc)
                except Exception:
                    pass

            if new_enriched:
                conn.execute(
                    """
                    update match_buffer set
                        period = ?, score_home = ?, score_away = ?,
                        is_live = ?, is_finished = ?, ingested_at = ?,
                        raw_sporty = ?, raw_enriched = ?
                    where match_id = ?
                    """,
                    (
                        period,
                        str(score.get("home") or ""),
                        str(score.get("away") or ""),
                        is_live, is_finished, now,
                        json.dumps(m), new_enriched,
                        match_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    update match_buffer set
                        period = ?, score_home = ?, score_away = ?,
                        is_live = ?, is_finished = ?, ingested_at = ?, raw_sporty = ?
                    where match_id = ?
                    """,
                    (
                        period,
                        str(score.get("home") or ""),
                        str(score.get("away") or ""),
                        is_live, is_finished, now,
                        json.dumps(m),
                        match_id,
                    ),
                )
            count += 1
        conn.commit()
    return count


# ── Phase 2: Enrichment queue ─────────────────────────────────────────────────

def get_unenriched_batch(limit: int = ENRICH_BATCH_SIZE) -> list[dict[str, Any]]:
    """
    Returns matches that need enrichment:
    - Never enriched (enriched_at IS NULL)
    - Enriched but sofascore_detail is missing (matched but detail fetch failed)
    - Finished matches and already-fully-enriched matches are skipped entirely
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _init_buffer_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select match_id, raw_sporty, raw_enriched, is_live, match_date
            from match_buffer
            where is_finished = 0
              and (
                enriched_at is null
                or sofascore_id is null
                or sofascore_id = ''
              )
            order by
              is_live desc,
              case when enriched_at is null then 0 else 1 end asc,
              start_time asc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "match_id": row["match_id"],
            "is_live": bool(row["is_live"]),
            "match_date": row["match_date"],
            "sporty": json.loads(row["raw_sporty"]),
            "existing": json.loads(row["raw_enriched"]) if row["raw_enriched"] else None,
        }
        for row in rows
    ]


def store_enriched(match_id: str, doc: dict[str, Any]) -> None:
    """Write the enriched document back into the buffer row."""
    _init_db()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        _init_buffer_table(conn)
        row = conn.execute("select raw_sporty, raw_enriched from match_buffer where match_id = ?", (match_id,)).fetchone()
        if row:
            raw_sporty = json.loads(row[0]) if row[0] else {}
            existing = json.loads(row[1]) if row[1] else {}
            doc = _merge_enriched(raw_sporty, existing, doc)
        conn.execute(
            """
            update match_buffer set
                enriched_at  = ?,
                sofascore_id = ?,
                raw_enriched = ?
            where match_id = ?
            """,
            (
                now,
                str(doc.get("sofascore_id") or ""),
                json.dumps(doc),
                match_id,
            ),
        )
        conn.commit()


# ── Read API ──────────────────────────────────────────────────────────────────

def get_buffered_matches(match_date: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """
    Returns matches for the frontend.
    Prefers raw_enriched (full data) but falls back to raw_sporty (basic data)
    so the frontend always gets something even before enrichment runs.
    """
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _init_buffer_table(conn)
        conn.row_factory = sqlite3.Row
        clauses = ["is_finished = 0"]
        params: list[Any] = []
        if match_date:
            clauses.append("match_date = ?")
            params.append(match_date)
        rows = conn.execute(
            f"""
            select raw_enriched, raw_sporty, is_live, enriched_at
            from match_buffer
            where {" and ".join(clauses)}
            order by is_live desc, start_time asc
            limit ?
            """,
            (*params, limit),
        ).fetchall()

    result = []
    for row in rows:
        if row["raw_enriched"]:
            result.append(json.loads(row["raw_enriched"]))
        else:
            # not yet enriched — return raw sporty data so frontend isn't empty
            sporty = json.loads(row["raw_sporty"])
            result.append(_sporty_to_summary(sporty))
    return result


def get_buffered_match(match_id: str) -> dict[str, Any] | None:
    """Returns the enriched doc for a single match, or raw sporty if not yet enriched."""
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _init_buffer_table(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select raw_enriched, raw_sporty from match_buffer where match_id = ?",
            (str(match_id),),
        ).fetchone()
    if not row:
        return None
    if row["raw_enriched"]:
        return json.loads(row["raw_enriched"])
    return _sporty_to_summary(json.loads(row["raw_sporty"]))


def get_live_buffered_matches(limit: int = 200) -> list[dict[str, Any]]:
    """All currently live matches from the buffer."""
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _init_buffer_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select raw_enriched, raw_sporty
            from match_buffer
            where is_live = 1
            order by start_time asc
            limit ?
            """,
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        if row["raw_enriched"]:
            result.append(json.loads(row["raw_enriched"]))
        else:
            result.append(_sporty_to_summary(json.loads(row["raw_sporty"])))
    return result


def purge_ghost_matches() -> int:
    """
    Remove matches from the buffer whose kick-off time has passed by more than
    GHOST_MATCH_GRACE_MINUTES but are still showing as 'Not start'.
    These are postponed, cancelled, or data-gap matches that will never go live.
    Returns the number of rows deleted.
    """
    _init_db()
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff_ts_ms = (now_ts - GHOST_MATCH_GRACE_MINUTES * 60) * 1000  # ms threshold

    with sqlite3.connect(DB_PATH) as conn:
        _init_buffer_table(conn)
        result = conn.execute(
            """
            delete from match_buffer
            where is_live = 0
              and is_finished = 0
              and start_time is not null
              and cast(start_time as real) < ?
              and (period is null or lower(period) in ('not start', 'not started', ''))
            """,
            (cutoff_ts_ms,),
        )
        conn.commit()
    deleted = result.rowcount
    if deleted:
        print(f"[buffer] purge_ghost_matches: removed {deleted} stale not-started matches")
    return deleted


def get_buffer_stats() -> dict[str, Any]:
    """Counts for monitoring."""
    _init_db()
    from datetime import date
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        _init_buffer_table(conn)
        total       = conn.execute("select count(*) from match_buffer").fetchone()[0]
        today_count = conn.execute("select count(*) from match_buffer where match_date = ?", (today,)).fetchone()[0]
        live_count  = conn.execute("select count(*) from match_buffer where is_live = 1").fetchone()[0]
        enriched    = conn.execute("select count(*) from match_buffer where enriched_at is not null").fetchone()[0]
        pending     = conn.execute(
            """
            select count(*) from match_buffer
            where is_finished = 0 and (
                enriched_at is null
                or sofascore_id is null
                or sofascore_id = ''
            )
            """
        ).fetchone()[0]
        last_ingest = conn.execute("select max(ingested_at) from match_buffer").fetchone()[0]
        last_enrich = conn.execute("select max(enriched_at) from match_buffer").fetchone()[0]

    return {
        "total_buffered": total,
        "today": today_count,
        "live": live_count,
        "upcoming": today_count - live_count,
        "enriched": enriched,
        "pending_enrichment": pending,
        "last_ingested_at": last_ingest,
        "last_enriched_at": last_enrich,
    }


# ── Enrichment worker ─────────────────────────────────────────────────────────

def run_enrichment_worker(batch_size: int = ENRICH_BATCH_SIZE) -> dict[str, Any]:
    """
    Pick up a batch of unenriched/stale matches and enrich them.
    Called by the scheduler — runs continuously in background.
    Returns a summary of what was done.
    """
    from app.sofascore_client import fetch_all_scheduled_events, fetch_event_detail, fetch_live_events
    from app.enrichment import _fuzzy_match, _llm_match, _is_junk, FUZZY_THRESHOLD, LLM_FALLBACK_THRESHOLD
    from app.web_context import search_match_context
    from app.time_context import match_time_context
    from datetime import date

    batch = get_unenriched_batch(batch_size)
    if not batch:
        return {"status": "idle", "pending": 0}

    # Fetch SofaScore by the actual kickoff date, with live endpoint for in-play matches.
    dates: dict[str, list[dict]] = {}
    live_needed = False
    for item in batch:
        if item["is_live"]:
            live_needed = True
        for d in _sofascore_date_candidates(item["sporty"], item.get("match_date")):
            dates.setdefault(d, []).append(item)

    sofa_cache: dict[str, list[dict]] = {}
    for d in dates:
        try:
            sofa_cache[d] = fetch_all_scheduled_events(d)
        except Exception:
            sofa_cache[d] = []
    live_events: list[dict] = []
    if live_needed:
        try:
            live_events = fetch_live_events()
        except Exception:
            live_events = []

    # build (item, sofa_event) pairs
    pairs: list[tuple[dict, dict | None, float]] = []
    matched = unmatched = llm_used = 0

    for item in batch:
        sporty = item["sporty"]
        existing = item.get("existing") or {}
        sofa_events = _candidate_sofascore_events(item, sofa_cache, live_events)
        saved_sofa_id = existing.get("sofascore_id")
        sofa = None
        score = 0.0

        if saved_sofa_id:
            sofa = next((event for event in sofa_events if str(event.get("id")) == str(saved_sofa_id)), None)
            if not sofa and isinstance(existing.get("sofascore_event"), dict):
                sofa = existing["sofascore_event"]
            score = float(existing.get("match_score") or 1.0)
            matched += 1
            pairs.append((item, sofa, score))
            continue

        sofa, score = _fuzzy_match(sporty, sofa_events)

        if score < FUZZY_THRESHOLD:
            if score >= LLM_FALLBACK_THRESHOLD and not _is_junk(sporty.get("name") or ""):
                llm_sofa = _llm_match(sporty, sofa_events)
                if llm_sofa:
                    sofa = llm_sofa
                    llm_used += 1
                    matched += 1
                else:
                    sofa = None
                    unmatched += 1
            else:
                sofa = None
                unmatched += 1
        else:
            matched += 1

        pairs.append((item, sofa, score))

    # fetch SofaScore detail in parallel
    def _fetch_detail(idx: int, sofa: dict) -> tuple[int, dict | None]:
        try:
            return idx, fetch_event_detail(sofa)
        except Exception:
            return idx, None

    details: dict[int, dict | None] = {}
    needs_detail = [(i, sofa) for i, (_, sofa, _) in enumerate(pairs) if sofa]

    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        futures = {pool.submit(_fetch_detail, i, sofa): i for i, sofa in needs_detail}
        for future in as_completed(futures):
            idx, detail = future.result()
            details[idx] = detail

    # Fetch web context for every SportyBet match; SofaScore matching is not required.
    def _fetch_web(idx: int, sporty: dict) -> tuple[int, dict]:
        try:
            return idx, search_match_context(
                sporty.get("home_team") or "",
                sporty.get("away_team") or "",
                sporty.get("tournament") or "",
            )
        except Exception:
            return idx, {"query": "", "snippets": [], "scraped": []}

    web_contexts: dict[int, dict] = {}
    needs_web = [(i, item["sporty"]) for i, (item, _, _) in enumerate(pairs)]

    with ThreadPoolExecutor(max_workers=WEB_WORKERS) as pool:
        futures = {pool.submit(_fetch_web, i, sporty): i for i, sporty in needs_web}
        for future in as_completed(futures):
            idx, ctx = future.result()
            web_contexts[idx] = ctx

    # assemble and store
    now = datetime.now(timezone.utc).isoformat()
    stored = 0
    predicted = 0

    for i, (item, sofa, score) in enumerate(pairs):
        sporty = item["sporty"]
        existing = item.get("existing") or {}
        detail = details.get(i)
        web_context = web_contexts.get(i, {"query": "", "snippets": [], "scraped": []})
        time_context = match_time_context({**sporty, "sofascore_event": sofa})

        doc = {
            **existing,
            "sportybet_id":      sporty.get("id"),
            "sportybet_name":    sporty.get("name"),
            "match_date":        time_context.get("local_date") or item["match_date"],
            "tournament":        sporty.get("tournament"),
            "category":          sporty.get("category"),
            "start_time":        sporty.get("start_time"),
            "period":            sporty.get("period"),
            "played_seconds":    sporty.get("played_seconds"),
            "score":             sporty.get("score"),
            "venue":             sporty.get("venue"),
            "sportybet_markets": sporty.get("markets", []),
            "sofascore_id":      sofa.get("id") if sofa else None,
            "sofascore_name":    sofa.get("name") if sofa else None,
            "sofascore_event":   sofa,
            "sofascore_detail":  detail,
            "web_context":       web_context,
            "match_score":       round(score, 3),
            "manual_match":      bool(existing.get("manual_match")),
            "raw_sporty":        sporty,
            "raw_sofascore_event": sofa.get("raw_event") if isinstance(sofa, dict) else None,
            "time_context":      time_context,
            "enriched_at":       now,
        }

        snapshot_odds(doc)
        try:
            from app.enriched_prediction import predict_enriched_match
            from app.league_memory import record_prediction

            prediction = predict_enriched_match(doc)
            doc["prediction"] = prediction
            record_prediction(prediction)
            predicted += 1
        except Exception as exc:
            print(f"[buffer] auto prediction failed for {item['match_id']}: {exc}")
        doc["lifecycle"] = _lifecycle_state(doc)
        store_enriched(item["match_id"], doc)
        stored += 1

    return {
        "status": "ok",
        "batch": len(batch),
        "matched": matched,
        "llm_fallback": llm_used,
        "unmatched": unmatched,
        "stored": stored,
        "predicted": predicted,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sporty_to_summary(m: dict[str, Any]) -> dict[str, Any]:
    """Minimal frontend-compatible doc from raw sporty data (pre-enrichment)."""
    score = m.get("score") or {}
    name = m.get("name") or ""
    parts = name.split(" vs ", 1)
    doc = {
        "sportybet_id":   str(m.get("id") or ""),
        "sportybet_name": name,
        "name":           name,
        "home_team":      parts[0].strip() if len(parts) == 2 else name,
        "away_team":      parts[1].strip() if len(parts) == 2 else "",
        "tournament":     m.get("tournament"),
        "category":       m.get("category"),
        "start_time":     m.get("start_time"),
        "period":         m.get("period"),
        "played_seconds": m.get("played_seconds"),
        "score":          score,
        "venue":          m.get("venue"),
        "odds_1x2":       _extract_1x2(m.get("markets", [])),
        "raw_sporty":     m,
        "has_sofascore":  False,
        "enriched":       False,
    }
    doc["lifecycle"] = _lifecycle_state(doc)
    return doc


def _merge_enriched(raw_sporty: dict[str, Any], existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Preserve sticky user-matched SofaScore state while refreshing volatile match data."""
    merged = {**existing, **incoming}
    merged["raw_sporty"] = raw_sporty or incoming.get("raw_sporty") or existing.get("raw_sporty")
    if existing.get("manual_match") and existing.get("sofascore_id"):
        merged["manual_match"] = True
        merged["manual_matched_at"] = existing.get("manual_matched_at")
        merged["sofascore_id"] = existing.get("sofascore_id")
        merged["sofascore_name"] = incoming.get("sofascore_name") or existing.get("sofascore_name")
        merged["sofascore_event"] = incoming.get("sofascore_event") or existing.get("sofascore_event")
        merged["match_score"] = incoming.get("match_score") or existing.get("match_score") or 1.0
    merged["lifecycle"] = _lifecycle_state(merged)
    return merged


def _lifecycle_state(doc: dict[str, Any]) -> dict[str, Any]:
    period = str(doc.get("period") or "")
    is_live = bool(period and period not in ("Not start", "Not started", "FT", "AET", "Finished", "Ended", ""))
    is_finished = _is_finished_period(period) or bool(doc.get("is_finished"))
    prediction = doc.get("prediction") or {}
    graded = bool(prediction.get("result") or doc.get("graded_at"))
    archived = bool(doc.get("archived_at") or doc.get("finished_at"))
    stages = {
        "discovered": bool(doc.get("sportybet_id") or doc.get("id")),
        "matched": bool(doc.get("sofascore_id") or doc.get("sofascore_event")),
        "enriched": bool(doc.get("enriched_at") or doc.get("sofascore_detail") or doc.get("web_context")),
        "predicted": bool(prediction or doc.get("predicted")),
        "live": is_live,
        "finished": is_finished,
        "graded": graded,
        "archived": archived,
    }
    order = ["archived", "graded", "finished", "live", "predicted", "enriched", "matched", "discovered"]
    current = next((stage for stage in order if stages.get(stage)), "unknown")
    missing = [stage for stage in ("matched", "enriched", "predicted", "graded", "archived") if not stages.get(stage)]
    return {"current": current, "stages": stages, "missing": missing}


def _extract_1x2(markets: list[dict[str, Any]]) -> dict[str, Any]:
    for market in markets:
        name = (market.get("name") or "").lower()
        if market.get("id") == "1" or "1x2" in name:
            odds = {s.get("name"): s.get("odds") for s in market.get("selections", [])}
            return {
                "home": odds.get("Home") or odds.get("1"),
                "draw": odds.get("Draw") or odds.get("X"),
                "away": odds.get("Away") or odds.get("2"),
            }
    return {}


def _is_finished_period(period: str | None) -> bool:
    if not period:
        return False
    p = period.lower().strip()
    return p in ("ft", "finished", "ended", "aet", "ap", "full time", "after penalties", "after extra time")


def _is_90_plus(played_seconds: Any) -> bool:
    """Returns True if the match has reached or passed the 90-minute mark."""
    try:
        return int(played_seconds or 0) >= 90 * 60
    except (TypeError, ValueError):
        return False


# Grace period after kick-off before we consider a non-live match as ghost/abandoned
GHOST_MATCH_GRACE_MINUTES = 120  # 2 hours past start_time


def _is_ghost_match(start_time: Any, period: str | None) -> bool:
    """
    Returns True if a match is still showing 'Not start' but its scheduled
    kick-off was more than GHOST_MATCH_GRACE_MINUTES ago.
    These are matches that never went live — postponed, cancelled, or data gaps.
    """
    if period and period not in ("Not start", "Not started", "", None):
        return False  # it did kick off, not a ghost
    if not start_time:
        return False
    try:
        ts = float(start_time)
        if ts > 1e12:
            ts /= 1000  # ms → seconds
        kickoff = datetime.fromtimestamp(ts, tz=timezone.utc)
        elapsed_minutes = (datetime.now(timezone.utc) - kickoff).total_seconds() / 60
        return elapsed_minutes > GHOST_MATCH_GRACE_MINUTES
    except (TypeError, ValueError, OSError):
        return False


def _sofascore_date_candidates(sporty: dict[str, Any], stored_match_date: str | None) -> list[str]:
    """Dates to try against SofaScore for this SportyBet match."""
    try:
        from app.time_context import match_time_context

        ctx = match_time_context(sporty)
    except Exception:
        ctx = {}

    ordered = [
        ctx.get("local_date"),
        ctx.get("utc_date"),
        stored_match_date,
        date_cls.today().isoformat(),
    ]
    result: list[str] = []
    for value in ordered:
        if value and value not in result:
            result.append(value)

    expanded = list(result)
    for value in result[:2]:
        try:
            d = date_cls.fromisoformat(value)
        except Exception:
            continue
        for neighbor in (d - timedelta(days=1), d + timedelta(days=1)):
            text = neighbor.isoformat()
            if text not in expanded:
                expanded.append(text)
    return expanded


def _candidate_sofascore_events(
    item: dict[str, Any],
    sofa_cache: dict[str, list[dict]],
    live_events: list[dict],
) -> list[dict]:
    events: list[dict] = []
    seen: set[str] = set()

    if item.get("is_live"):
        for event in live_events:
            eid = str(event.get("id") or "")
            if eid and eid not in seen:
                events.append(event)
                seen.add(eid)

    for d in _sofascore_date_candidates(item["sporty"], item.get("match_date")):
        for event in sofa_cache.get(d, []):
            eid = str(event.get("id") or "")
            if eid and eid not in seen:
                events.append(event)
                seen.add(eid)
    return events


def _try_archive_finished(match_id: str) -> None:
    """Archive a finished match to MongoDB and local SQLite, then remove from buffer.
    If MongoDB is not configured, still saves locally and deletes from buffer."""
    try:
        from app.mongo_store import archive_finished_match_from_buffer, is_configured
        if is_configured():
            archive_finished_match_from_buffer(match_id)
        else:
            # No MongoDB — save to local SQLite finished_matches table + delete from buffer
            _archive_finished_locally(match_id)
    except Exception as exc:
        print(f"[buffer] archive failed for {match_id}: {exc}")
        # Last resort: ensure is_finished=1 so get_buffered_matches filters it out
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("update match_buffer set is_finished = 1 where match_id = ?", (match_id,))
                conn.commit()
        except Exception:
            pass


def _archive_finished_locally(match_id: str) -> None:
    """Write finished match to local SQLite finished_matches table and delete from buffer."""
    import json as _json
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        _init_buffer_table(conn)
        # Ensure finished_matches table exists
        conn.execute("""
            create table if not exists finished_matches (
                match_id   text primary key,
                match_date text,
                home_team  text,
                away_team  text,
                tournament text,
                score_home text,
                score_away text,
                finished_at text not null default current_timestamp,
                raw_json   text not null
            )
        """)
        row = conn.execute(
            "select match_date, raw_enriched, raw_sporty, score_home, score_away from match_buffer where match_id = ?",
            (match_id,),
        ).fetchone()
        if row:
            raw = row[1] or row[2]
            doc = _json.loads(raw) if raw else {}
            home = doc.get("home_team") or ""
            if isinstance(home, dict):
                home = home.get("name") or ""
            away = doc.get("away_team") or ""
            if isinstance(away, dict):
                away = away.get("name") or ""
            score = doc.get("score") or {}
            archive_doc = {
                "match_id":   match_id,
                "match_date": row[0],
                "home_team":  home,
                "away_team":  away,
                "tournament": doc.get("tournament") or "",
                "score":      score,
                "period":     doc.get("period") or "FT",
            }
            conn.execute(
                """
                insert or replace into finished_matches
                    (match_id, match_date, home_team, away_team, tournament, score_home, score_away, raw_json)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id, row[0], home, away,
                    doc.get("tournament") or "",
                    str(score.get("home") or row[3] or ""),
                    str(score.get("away") or row[4] or ""),
                    _json.dumps(archive_doc),
                ),
            )
        # Delete from buffer regardless
        conn.execute("delete from match_buffer where match_id = ?", (match_id,))
        conn.commit()
