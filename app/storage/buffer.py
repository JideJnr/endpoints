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
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

from app.storage.db import DB_PATH, _conn
from app.storage.db import _init_db
from app.market.market import snapshot_odds
from app.live.live_stat_history import snapshot_live_statistics
from app.match_facts import enrich_match_facts, normalize_live_statistics
from app.utils.match_state import classify_match_state
from app.utils.normalise import normalise
from app.market.season_stage import detect_season_stage

from app.utils.doc_helpers import _data_sources, _date_from_start_time, _is_not_started_period
from app.utils.match_helpers import _extract_1x2, _played_seconds as _played_seconds_local
from app.utils.web_helpers import _fetch_web as _fetch_web_context

# How many SofaScore detail calls to run in parallel. Keep this at one so a
# backlog drains match-by-match instead of several enrichment lanes competing
# for SofaScore and SQLite at the same time.
ENRICH_WORKERS = 1
WEB_WORKERS = 10

# How many matches to enrich per worker cycle
ENRICH_BATCH_SIZE = 10

# If SofaScore does not appear to carry a prematch fixture, do not keep trying
# it every scheduler tick. It will be retried later so late SofaScore listings
# can still attach before kick-off.
NO_MATCH_RETRY_MINUTES = 180
NO_MATCH_MIN_RETRY_LIVE_MINUTES = 15


# ── Table init ────────────────────────────────────────────────────────────────
# _init_buffer_table: removed — was a no-op since all buffer tables are created
# once by _init_db_unlocked at startup. All call sites removed.


def _ensure_buffer_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"alter table {table} add column {column} {ddl}")


# _buffer_table_for: removed — always returned "match_buffer" with no logic.


# ── Phase 1: Ingest ───────────────────────────────────────────────────────────

def ingest_competition_match(event: dict[str, Any], match_date: str, competition_key: str) -> bool:
    """
    Insert a SofaScore-only competition match into match_buffer.
    Uses match_id = sofascore:{event_id} so it never collides with SportyBet rows.
    When SportyBet later ingests the same fixture, ingest_matches merges them by sofascore_id.
    Returns True if a new row was inserted.
    """
    sofa_id = str(event.get("id") or "")
    if not sofa_id:
        return False
    match_id = f"sofascore:{sofa_id}"
    _init_db()
    now = datetime.now(timezone.utc).isoformat()
    status = event.get("status") or {}
    score = event.get("score") or {}
    state = classify_match_state({
        "period": status.get("description") or status.get("type") or "Not start",
        "start_time": int((event.get("start_timestamp") or 0) * 1000),
        "score": score,
    })
    is_live = 1 if state.get("is_live") else 0
    is_finished = 1 if (state.get("is_finished") or state.get("state") in {"postponed", "cancelled"}) else 0
    if is_finished:
        return False
    with _conn() as conn:
        exists = conn.execute("select 1 from match_buffer where match_id = ?", (match_id,)).fetchone()
        conn.execute(
            """
            insert into match_buffer (
                match_id, match_date, tournament, category, name, start_time, period,
                score_home, score_away, is_live, is_finished, ingested_at,
                data_source, sofascore_id, sofascore_only, raw_sporty
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            on conflict(match_id) do update set
                match_date   = excluded.match_date,
                tournament   = excluded.tournament,
                name         = excluded.name,
                start_time   = excluded.start_time,
                period       = excluded.period,
                score_home   = excluded.score_home,
                score_away   = excluded.score_away,
                is_live      = excluded.is_live,
                is_finished  = excluded.is_finished,
                ingested_at  = excluded.ingested_at,
                raw_sporty   = excluded.raw_sporty
            """,
            (
                match_id, match_date,
                (event.get("tournament") or {}).get("name"),
                (event.get("tournament") or {}).get("category", {}).get("name") if isinstance((event.get("tournament") or {}).get("category"), dict) else None,
                event.get("name"),
                int((event.get("start_timestamp") or 0) * 1000),
                status.get("description") or status.get("type") or "Not start",
                str(score.get("home") or ""), str(score.get("away") or ""),
                is_live, is_finished, now,
                "sofascore", sofa_id,
                json.dumps(event),
            ),
        )
        conn.commit()
    return not exists


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

    with _conn() as conn:
        count = 0
        for m in matches:
            match_id = str(m.get("id") or "")
            if not match_id:
                continue

            score = m.get("score") or {}
            period = m.get("period") or "Not start"
            state = classify_match_state(m)
            is_live = 1 if state.get("is_live") else 0
            is_finished = 1 if (state.get("is_finished") or state.get("state") in {"postponed", "cancelled"}) else 0

            if is_finished:
                continue
            if match_date < today and not is_live:
                continue
            if _is_ghost_match(m.get("start_time"), period):
                continue

            # Check if a SofaScore-only row already exists for this fixture.
            # Resolution order: (1) explicit sofascore_id on the sporty payload,
            # if we ever get one directly, (2) team_watcher's learned
            # sporty_team_id -> sofascore_team_id mapping for both teams,
            # (3) name+date fallback, which also teaches team_watcher the
            # mapping it finds so future ingests of these two teams hit the
            # faster path (2) instead.
            # If found, merge: adopt the sofascore row's match_id and attach sporty data.
            sofa_match_id: str | None = None
            sofa_row = None
            sporty_sofa_id = str(m.get("sofascore_id") or "")
            if sporty_sofa_id:
                sofa_row = conn.execute(
                    "select match_id from match_buffer where sofascore_id = ? and sofascore_only = 1",
                    (sporty_sofa_id,),
                ).fetchone()
                if sofa_row:
                    sofa_match_id = sofa_row[0]

            if not sofa_match_id:
                sofa_match_id = _resolve_sofascore_only_match(conn, m, match_date)

            if sofa_match_id:
                # Merge: update the existing SofaScore-only row with SportyBet data
                conn.execute(
                    """
                    update match_buffer set
                        sportybet_id  = ?,
                        data_source   = 'both',
                        sofascore_only = 0,
                        period        = ?,
                        score_home    = ?,
                        score_away    = ?,
                        is_live       = ?,
                        is_finished   = ?,
                        ingested_at   = ?,
                        raw_sporty    = ?
                    where match_id = ?
                    """,
                    (
                        match_id, period,
                        str(score.get("home") or ""), str(score.get("away") or ""),
                        is_live, is_finished, now, json.dumps(m),
                        sofa_match_id,
                    ),
                )
                _sync_enriched_sporty_fields(conn, "match_buffer", sofa_match_id, m, match_date, is_live, is_finished)
                count += 1
                continue

            # This SportyBet match may have already been merged into a
            # SofaScore-keyed row on a PREVIOUS ingest cycle (that row's
            # match_id is "sofascore:...", not this match's own id, with
            # sportybet_id pointing at it). If we don't check for that here
            # and instead always insert/update by the raw sportybet match_id,
            # every later ingest of an already-merged match creates a brand
            # new duplicate row and silently un-merges it.
            already_merged_row = conn.execute(
                "select match_id from match_buffer where sportybet_id = ? and match_id != ?",
                (match_id, match_id),
            ).fetchone()
            target_match_id = already_merged_row[0] if already_merged_row else match_id

            # Detect whether this is a new row by checking existence once before
            # the upsert. We do it as a scalar (1 or None) rather than fetching
            # the full row — cheaper than the old SELECT 1 was not, but at least
            # we're not fetching any data columns. This is the minimal read
            # needed to return an accurate "new rows inserted" count to the caller.
            is_new_row = conn.execute(
                "select 1 from match_buffer where match_id = ?", (target_match_id,)
            ).fetchone() is None

            conn.execute(
                """
                insert into match_buffer (
                    match_id, match_date, tournament, category, name,
                    start_time, period, score_home, score_away,
                    is_live, is_finished, ingested_at, data_source, sportybet_id, raw_sporty
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(match_id) do update set
                    match_date   = excluded.match_date,
                    tournament   = excluded.tournament,
                    category     = excluded.category,
                    name         = excluded.name,
                    start_time   = excluded.start_time,
                    period       = excluded.period,
                    score_home   = excluded.score_home,
                    score_away   = excluded.score_away,
                    is_live      = excluded.is_live,
                    is_finished  = excluded.is_finished,
                    data_source  = case
                        when match_buffer.sofascore_id is not null then 'both'
                        else excluded.data_source
                    end,
                    sportybet_id = excluded.sportybet_id,
                    raw_sporty   = excluded.raw_sporty,
                    ingested_at  = excluded.ingested_at
                """,
                (
                    target_match_id, match_date,
                    m.get("tournament"), m.get("category"), m.get("name"),
                    m.get("start_time"), period,
                    str(score.get("home") or ""), str(score.get("away") or ""),
                    is_live, is_finished, now, "sportybet", match_id, json.dumps(m),
                ),
            )
            _sync_enriched_sporty_fields(conn, "match_buffer", target_match_id, m, match_date, is_live, is_finished)
            if is_new_row:
                count += 1
        conn.commit()

    # Register every new match with team watcher using SportyBet ID.
    # This ensures the watcher knows about every match from ingest, even
    # before SofaScore enrichment runs. SofaScore team IDs are appended
    # later in _register_sofa_team_ids_with_watcher() after enrichment.
    _register_ingest_with_team_watcher(matches)
    return count


def _normalize_team_name_for_match(name: Any) -> str:
    """Lowercase, strip accents, drop punctuation -- for fuzzy team-name matching.

    Every non-alphanumeric character becomes a word-separating space, EXCEPT
    real combining accent marks (the ´ that NFKD splits off "é" into "e" +
    a combining acute) which are simply dropped so accented letters fold to
    their plain ASCII form. A standalone accent-like symbol that isn't
    combined with a letter -- e.g. SofaScore sending "M´gladbach" with a
    bare U+00B4 acute accent instead of an apostrophe -- is NOT a combining
    mark, so plain `.encode("ascii", "ignore")` used to silently delete it
    and collapse the name into one token ("mgladbach"), which then never
    matched SportyBet's "M'gladbach" (plain apostrophe -> "m gladbach", two
    tokens). Treating it as a separator instead keeps both spellings
    equivalent.
    """
    import unicodedata
    if isinstance(name, dict):
        name = name.get("name") or name.get("team_name") or ""
    decomposed = unicodedata.normalize("NFKD", str(name or ""))
    chars = []
    for ch in decomposed:
        if unicodedata.combining(ch):
            continue
        chars.append(ch if (ch.isascii() and ch.isalnum()) else " ")
    return re.sub(r"\s+", " ", "".join(chars).lower()).strip()


def _team_names_match(a: Any, b: Any) -> bool:
    """True if two team names plausibly refer to the same team.

    Handles the common real-world mismatches between SportyBet and SofaScore
    naming (e.g. "Nautico PE" vs "Nautico", "KAA Gent" vs "Gent") via exact
    match, substring containment, or shared significant word token.
    """
    na, nb = _normalize_team_name_for_match(a), _normalize_team_name_for_match(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    tokens_a = {t for t in na.split() if len(t) >= 4}
    tokens_b = {t for t in nb.split() if len(t) >= 4}
    return bool(tokens_a & tokens_b)


def _learn_team_sofascore_id(conn: sqlite3.Connection, sporty_id: str, team_name: str, sofa_id: str) -> None:
    """Backfill ai_team_watchers with a newly-discovered sporty<->sofascore team
    ID pairing, so future ingests of these two teams resolve via the fast
    team-ID lookup in _resolve_sofascore_only_match instead of name matching.
    Best-effort: the team_watcher tables may not exist yet on a cold DB, and
    this must never block ingest -- swallow any failure.
    """
    if not sofa_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        if sporty_id:
            updated = conn.execute(
                """
                update ai_team_watchers set sofascore_team_id = ?, updated_at = ?
                where sporty_team_id = ? and (sofascore_team_id is null or sofascore_team_id = '')
                """,
                (sofa_id, now, sporty_id),
            ).rowcount
            if updated:
                return
        key = re.sub(r"[^a-z0-9]+", "-", str(team_name or sporty_id or sofa_id or "").lower().strip()).strip("-")
        if not key:
            return
        conn.execute(
            """
            insert into ai_team_watchers (team_key, team_name, sporty_team_id, sofascore_team_id, updated_at)
            values (?, ?, ?, ?, ?)
            on conflict(team_key) do update set
                sporty_team_id    = coalesce(ai_team_watchers.sporty_team_id, excluded.sporty_team_id),
                sofascore_team_id = coalesce(ai_team_watchers.sofascore_team_id, excluded.sofascore_team_id),
                updated_at        = excluded.updated_at
            """,
            (key, team_name or key, sporty_id or None, sofa_id, now),
        )
    except Exception:
        pass


def _resolve_sofascore_only_match(conn: sqlite3.Connection, m: dict[str, Any], match_date: str) -> str | None:
    """Find an existing sofascore-only match_buffer row for the same real-world
    fixture as incoming SportyBet match `m`, so ingest_matches() can merge into
    it instead of creating a duplicate row.

    Two strategies, in order:
      1. team_watcher's learned sporty_team_id -> sofascore_team_id mapping
         for BOTH the home and away team (fast, precise, once learned).
      2. Name matching against sofascore-only rows on the same match_date --
         also teaches team_watcher the pairing it discovers via
         _learn_team_sofascore_id, so future ingests of these two teams hit
         strategy 1 immediately.

    Best-effort throughout: the team_watcher tables may not exist yet on a
    cold DB, so every lookup is wrapped and a failure just falls through to
    the next strategy (or gives up and lets ingest_matches insert a fresh row,
    same as before this function existed).
    """
    team_ids = m.get("team_ids") if isinstance(m.get("team_ids"), dict) else {}
    home_sporty_id = str(team_ids.get("home") or "")
    away_sporty_id = str(team_ids.get("away") or "")
    home_name = str(m.get("home_team") or "")
    away_name = str(m.get("away_team") or "")
    if not home_name or not away_name:
        return None

    # ── Strategy 1: team_watcher shared-ID lookup ──────────────────────────
    home_sofa_id = away_sofa_id = None
    if home_sporty_id or away_sporty_id:
        try:
            rows = conn.execute(
                """
                select sporty_team_id, sofascore_team_id from ai_team_watchers
                where sporty_team_id in (?, ?)
                  and sofascore_team_id is not null and sofascore_team_id != ''
                """,
                (home_sporty_id, away_sporty_id),
            ).fetchall()
            by_sporty_id = {r[0]: r[1] for r in rows}
            home_sofa_id = by_sporty_id.get(home_sporty_id)
            away_sofa_id = by_sporty_id.get(away_sporty_id)
        except Exception:
            pass

    if home_sofa_id and away_sofa_id:
        try:
            # Primary: dedicated indexed columns (populated by
            # competition_special.py's mirror at write time -- see
            # idx_buffer_sofa_only_teams in db.py). Direct column equality,
            # no per-row JSON work at all.
            row = conn.execute(
                """
                select match_id from match_buffer
                where sofascore_only = 1 and match_date = ?
                  and sofascore_home_team_id = ? and sofascore_away_team_id = ?
                limit 1
                """,
                (match_date, str(home_sofa_id), str(away_sofa_id)),
            ).fetchone()
            if row:
                return row[0]
            # Fallback for rows written before those columns existed (NULL):
            # json_extract in SQL still avoids pulling the full raw_sporty
            # blob into Python and calling json.loads per row. Key path is
            # home_team.id/away_team.id -- competition_special.py stores
            # event's ALREADY-normalized snake_case team dicts at the top
            # level of raw_sporty (sofascore_client.py converts the raw
            # API's homeTeam/awayTeam to home_team/away_team before this
            # point), so a homeTeam/awayTeam path here would never match
            # anything -- confirmed this was the case before this fix.
            row = conn.execute(
                """
                select match_id from match_buffer
                where sofascore_only = 1 and match_date = ?
                  and sofascore_home_team_id is null
                  and cast(json_extract(raw_sporty, '$.home_team.id') as text) = ?
                  and cast(json_extract(raw_sporty, '$.away_team.id') as text) = ?
                limit 1
                """,
                (match_date, str(home_sofa_id), str(away_sofa_id)),
            ).fetchone()
            if row:
                return row[0]
        except Exception:
            pass

    # ── Strategy 2: name + date fallback ───────────────────────────────────
    try:
        candidates = conn.execute(
            "select match_id, name, raw_sporty from match_buffer where sofascore_only = 1 and match_date = ?",
            (match_date,),
        ).fetchall()
    except Exception:
        return None
    for cand_id, cand_name, raw in candidates:
        cand_home_name = _parse_home_team(cand_name or "").get("name") or ""
        cand_away_name = _parse_away_team(cand_name or "").get("name") or ""
        if not cand_home_name or not cand_away_name:
            continue
        if _team_names_match(home_name, cand_home_name) and _team_names_match(away_name, cand_away_name):
            # Learned a new pairing -- teach team_watcher for next time.
            try:
                event = json.loads(raw or "{}")
                # See the key-path note above: raw_sporty stores event's
                # already-normalized home_team/away_team, not homeTeam/awayTeam.
                cand_home_id = str((event.get("home_team") or {}).get("id") or "")
                cand_away_id = str((event.get("away_team") or {}).get("id") or "")
                _learn_team_sofascore_id(conn, home_sporty_id, home_name, cand_home_id)
                _learn_team_sofascore_id(conn, away_sporty_id, away_name, cand_away_id)
            except Exception:
                pass
            return cand_id
    return None


def _register_ingest_with_team_watcher(matches: list[dict[str, Any]]) -> None:
    """Register ingested SportyBet matches with team watcher on first ingest.

    Best-effort: swallow all errors so a team_watcher problem never blocks
    ingest. If this ever goes silent again, add a temporary record_activity()
    call here rather than assuming it's fine -- that's what surfaced the
    four real bugs (a KeyError, an off-by-one SQL placeholder, a lost DB
    key, and a non-JSON-serializable Row leak) that used to make this
    function silently fail on every single call.
    """
    try:
        from app.team_watcher.team_watcher import observe_match as _tw_observe
        for m in matches:
            match_id = str(m.get("id") or "")
            if not match_id:
                continue
            state = classify_match_state(m)
            if state.get("is_finished"):
                continue
            # Build a minimal doc the team watcher can parse for team identity.
            # Include raw_sporty so _teams_for_doc() can extract team_ids
            # (sporty competitor IDs) on first ingest — before SofaScore
            # enrichment runs and appends sofascore_team_id.
            doc = {
                "sportybet_id": match_id,
                "match_id": match_id,
                "name": m.get("name") or "",
                "sportybet_name": m.get("name") or "",
                "tournament": m.get("tournament") or "",
                "category": m.get("category") or "",
                "match_date": m.get("match_date") or "",
                "start_time": m.get("start_time"),
                "home_team": m.get("home_team") or _parse_home_team(m.get("name") or ""),
                "away_team": m.get("away_team") or _parse_away_team(m.get("name") or ""),
                "data_source": "sportybet",
                # Pass the full sporty dict as raw_sporty so team_ids is accessible
                "raw_sporty": m,
            }
            try:
                _tw_observe(doc)
            except Exception:
                pass
    except Exception:
        pass


def _parse_home_team(name: str) -> dict[str, Any]:
    """Extract home team name from 'Home v Away' format."""
    import re as _re
    parts = _re.split(r"\s+v(?:s)?\.?\s+", name, flags=_re.I)
    return {"name": parts[0].strip()} if len(parts) == 2 else {"name": name}


def _parse_away_team(name: str) -> dict[str, Any]:
    """Extract away team name from 'Home v Away' format."""
    import re as _re
    parts = _re.split(r"\s+v(?:s)?\.?\s+", name, flags=_re.I)
    return {"name": parts[1].strip()} if len(parts) == 2 else {"name": ""}


def backfill_merge_orphaned_matches(dry_run: bool = True, limit: int = 5000) -> dict[str, Any]:
    """One-time cleanup: merge standalone SportyBet rows that predate the
    ingest-time merge fix with their matching SofaScore-only row, using the
    exact same matching logic (_resolve_sofascore_only_match) that new
    ingests use going forward.

    For every match_buffer row with data_source='sportybet' (i.e. never
    merged), tries to find a same-fixture sofascore_only=1 row. On a match:
      - the sofa row is updated with the sportybet row's live fields
        (sportybet_id, data_source='both', sofascore_only=0, score/period/
        live/finished, raw_sporty) -- same fields the live merge touches,
        the sofa row's own raw_enriched/enriched_at is left untouched.
      - the now-redundant standalone sportybet row is deleted.

    dry_run=True (default) makes no writes -- it only reports what would
    merge, so counts can be sanity-checked before actually running it.
    """
    _init_db()
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "candidates_scanned": 0,
        "merged": 0,
        "merged_pairs": [],
        "errors": 0,
    }
    with _conn() as conn:
        candidates = conn.execute(
            """
            select match_id, match_date, name, raw_sporty from match_buffer
            where data_source = 'sportybet'
            order by match_date
            limit ?
            """,
            (limit,),
        ).fetchall()
        report["candidates_scanned"] = len(candidates)
        for cand_id, cand_date, cand_name, raw in candidates:
            try:
                m = json.loads(raw or "{}")
            except Exception:
                report["errors"] += 1
                continue
            if not m:
                continue
            try:
                target = _resolve_sofascore_only_match(conn, m, cand_date)
            except Exception:
                report["errors"] += 1
                continue
            if not target or target == cand_id:
                continue

            score = m.get("score") if isinstance(m.get("score"), dict) else {}
            state = classify_match_state(m)
            pair_info = {
                "sportybet_match_id": cand_id,
                "sofascore_match_id": target,
                "name": cand_name,
                "match_date": cand_date,
            }
            report["merged_pairs"].append(pair_info)
            if dry_run:
                report["merged"] += 1
                continue

            conn.execute(
                """
                update match_buffer set
                    sportybet_id  = ?,
                    data_source   = 'both',
                    sofascore_only = 0,
                    period        = ?,
                    score_home    = ?,
                    score_away    = ?,
                    is_live       = ?,
                    is_finished   = ?,
                    ingested_at   = ?,
                    raw_sporty    = ?
                where match_id = ?
                """,
                (
                    cand_id,
                    m.get("period"),
                    str(score.get("home") or ""), str(score.get("away") or ""),
                    1 if state.get("is_live") else 0,
                    1 if state.get("is_finished") else 0,
                    datetime.now(timezone.utc).isoformat(),
                    raw,
                    target,
                ),
            )
            _sync_enriched_sporty_fields(
                conn, "match_buffer", target, m, cand_date,
                1 if state.get("is_live") else 0, 1 if state.get("is_finished") else 0,
            )
            conn.execute("delete from match_buffer where match_id = ?", (cand_id,))
            report["merged"] += 1
        if not dry_run:
            conn.commit()
    return report


def archive_stale_sofascore_only_matches(
    dry_run: bool = True, hours_past_kickoff: int = 6, limit: int = 2000
) -> dict[str, Any]:
    """One-time cleanup: archive SofaScore-only rows whose real-world match
    kicked off long ago but whose is_finished flag was never updated.

    Root cause: enrich_predict_competition() re-processes at most 8 rows per
    competition per ~5 min cycle, and its priority order always puts brand
    new not-yet-processed fixtures (prediction_json/raw_detail is null) ahead
    of already-processed rows. With a steady trickle of freshly-synced future
    fixtures every cycle (normal for 30+ continuously-tracked competitions),
    an already-processed row that's since finished can be permanently
    starved of its refresh turn -- its is_finished flag just never gets
    updated again, so cleanup_buffer()'s is_finished=1 check never finds it
    and it sits in match_buffer forever.

    Safety: only rows with is_live=0, is_finished=0, sofascore_only=1, and a
    start_time more than `hours_past_kickoff` in the past (default 6h --
    long enough for any match including extra time/penalties/delays) are
    candidates. That's a heuristic, not a live-confirmed check (deliberately
    avoids hammering SofaScore's API again), so dry_run=True (default) only
    reports what would be archived -- call with dry_run=false to apply.
    """
    _init_db()
    cutoff_ms = int((time.time() - hours_past_kickoff * 3600) * 1000)
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "hours_past_kickoff": hours_past_kickoff,
        "candidates": 0,
        "archived": 0,
        "sample": [],
    }
    with _conn() as conn:
        rows = conn.execute(
            """
            select match_id, match_date, name, start_time from match_buffer
            where sofascore_only = 1
              and is_finished = 0
              and is_live = 0
              and start_time is not null
              and cast(start_time as real) < ?
            order by start_time asc
            limit ?
            """,
            (cutoff_ms, limit),
        ).fetchall()
    report["candidates"] = len(rows)
    report["sample"] = [
        {"match_id": r[0], "match_date": r[1], "name": r[2], "start_time": r[3]}
        for r in rows[:20]
    ]
    if dry_run:
        report["archived"] = len(rows)
        return report
    for match_id, *_rest in rows:
        try:
            _archive_finished_locally(str(match_id))
            report["archived"] += 1
        except Exception:
            pass
    return report


def patch_live_scores(matches: list[dict[str, Any]]) -> int:
    """
    Fast update of score/period for live matches already in the buffer.
    When a match transitions to finished, archives to MongoDB and removes from buffer.

    All writes are collected into one transaction and committed once at the end.
    Archive calls (_try_archive_finished) are deferred until after the write lock
    is released so they never hold SQLite locked during the MongoDB/external call.
    """
    if not matches:
        return 0
    _init_db()
    now = datetime.now(timezone.utc).isoformat()

    # IDs of matches that finished this cycle — archived after the single commit
    to_archive: list[str] = []

    with _conn() as conn:
        count = 0
        for m in matches:
            match_id = str(m.get("id") or "")
            if not match_id:
                continue
            score = m.get("score") or {}
            period = m.get("period") or ""
            state = classify_match_state(m)
            is_live = 1 if state.get("is_live") else 0
            is_finished = 1 if (state.get("is_finished") or state.get("state") in {"postponed", "cancelled"}) else 0

            if is_finished:
                # Write final score into raw_sporty first
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
                # Also stamp final score into raw_enriched so the archive doc is accurate
                row = conn.execute(
                    "select raw_enriched from match_buffer where match_id = ?", (match_id,)
                ).fetchone()
                if row and row[0]:
                    try:
                        enriched_doc = json.loads(row[0])
                        enriched_doc["period"] = period
                        enriched_doc["score"] = score
                        enriched_doc["is_finished"] = True
                        enriched_doc = enrich_match_facts(enriched_doc)
                        conn.execute(
                            "update match_buffer set raw_enriched = ? where match_id = ?",
                            (json.dumps(enriched_doc), match_id),
                        )
                    except Exception:
                        pass

                # Resolve open live-timeline snapshots for this match now that it's
                # finished. Snapshots were written under source="sportybet" using
                # SportyBet's own match_id; resolving here closes the gap where the
                # old SofaScore-keyed path could never match them.
                try:
                    from app.storage.league_memory.crud import (
                        _resolve_snapshots,
                        _aggregate_resolved_snapshots,
                    )
                    final_home = int(str(score.get("home") or 0) or 0)
                    final_away = int(str(score.get("away") or 0) or 0)
                    _resolve_snapshots(conn, "sportybet", match_id, final_home, final_away)
                    _aggregate_resolved_snapshots(conn)
                except Exception:
                    logger.warning("snapshot resolution failed for %s", match_id, exc_info=True)

                # Defer archive until after the single commit below
                to_archive.append(match_id)
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
                    doc = enrich_match_facts(doc)
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
            _sync_enriched_sporty_fields(conn, "match_buffer", match_id, m, _date_from_start_time(m.get("start_time")), is_live, is_finished)
            count += 1
        # Single commit for all matches — one write transaction instead of one per finished match
        conn.commit()

    # Archive finished matches AFTER releasing the write lock so MongoDB/external
    # calls never hold SQLite locked.
    for match_id in to_archive:
        _try_archive_finished(match_id)

    return count


# ── Phase 2: Enrichment queue ─────────────────────────────────────────────────

def _tournament_priority_for_row(row: sqlite3.Row) -> int:
    """Extract tournament from a buffer row and return its dynamic priority."""
    try:
        raw_sporty = json.loads(row["raw_sporty"]) if row["raw_sporty"] else {}
    except (json.JSONDecodeError, TypeError):
        raw_sporty = {}

    tournament = raw_sporty.get("tournament") or ""
    if isinstance(tournament, dict):
        tournament = tournament.get("name") or ""

    # Quality filters — always deprioritise these regardless of accuracy
    name = (raw_sporty.get("name") or "").lower()
    tournament_lower = str(tournament).lower()
    combined = f"{name} {tournament_lower}".lower()

    if any(x in combined for x in ["reserve", "u20", "u19"]):
        return 8
    if "friendly" in combined:
        return 7

    # Dynamic accuracy-based priority
    try:
        from app.monitoring.self_learner import get_tournament_priority  # noqa: PLC0415
        pref = get_tournament_priority(tournament)
        return int(pref.get("priority", 4))
    except Exception:
        return 4


def get_unenriched_batch(
    limit: int = ENRICH_BATCH_SIZE,
    live_only: bool = False,
    future_only: bool = False,
    exclude_live: bool = False,
    force_live_retry: bool = False,
) -> list[dict[str, Any]]:
    """
    Returns matches that need enrichment, in priority order:
      1. Live matches (always first)
      2. Today's unenriched matches
      3. Tomorrow's unenriched matches
      4. Future matches (beyond tomorrow) — enriched once per day, not every cycle

    Tournament priority is now dynamic: liked tournaments (high prediction
    accuracy) are processed first, disliked tournaments (low accuracy) are
    processed later.  Quality filters (reserve, youth, friendly) still
    deprioritise those matches regardless of accuracy.

    Future matches are normally included only after the hot queue. The
    scheduled future lane can request them explicitly with future_only=True.
    """
    _init_db()
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff_ts_ms = (now_ts - GHOST_MATCH_GRACE_MINUTES * 60) * 1000
    today = date_cls.today().isoformat()
    tomorrow = (date_cls.today() + timedelta(days=1)).isoformat()

    with _conn() as conn:
        if live_only and not future_only:
            live_clause = "and is_live = 1"
        elif exclude_live:
            live_clause = "and is_live = 0"
        else:
            live_clause = ""
        # 3 minutes: matches the live prediction cooldown in apply_prediction_state
        # (2-3 min) and keeps this well clear of SofaScore's rate limiting — no
        # point re-fetching stats faster than we're willing to act on them.
        stale_clause = "or (is_live = 1 and enriched_at < datetime('now', '-3 minutes'))"
        retry_clause = "1 = 1" if force_live_retry else """
              (
                json_extract(raw_enriched, '$.sofascore_match_status') is null
                or json_extract(raw_enriched, '$.sofascore_match_status') not in ('no_match', 'srl_skip')
                or coalesce(cast(json_extract(raw_enriched, '$.sofascore_retry_after_ts') as real), 0) <= ?
              )
        """
        # Fetch a larger batch so Python-level re-sorting by dynamic priority
        # does not accidentally drop high-accuracy tournaments that would have
        # been beyond the raw SQL limit.
        fetch_limit = max(limit * 3, 30)
        query = f"""
            select match_id, raw_sporty, raw_enriched, is_live, match_date,
                   enriched_at, start_time
            from {{table}}
            where is_finished = 0
              and (start_time is null or cast(start_time as real) >= ?)
              {live_clause}
              and (
                enriched_at is null
                or sofascore_id is null
                or sofascore_id = ''
                {stale_clause}
              )
              and {retry_clause}
        """
        hot_params = (
                (cutoff_ts_ms, today, tomorrow, fetch_limit)
                if force_live_retry
                else (cutoff_ts_ms, now_ts, today, tomorrow, fetch_limit)
            )
        rows = conn.execute(
                (query.format(table="match_buffer") + """
                order by
                  is_live desc,
                  case when match_date = ? then 0 when match_date = ? then 1 else 2 end asc,
                  case when enriched_at is null then 0 else 1 end asc,
                  start_time asc
                limit ?
                """),
                hot_params,
            ).fetchall()

    # Annotate each row with its dynamic tournament priority and re-sort.
    annotated: list[tuple[tuple, sqlite3.Row]] = []
    for row in rows:
        t_pri = _tournament_priority_for_row(row)
        date_pri = 0 if row["match_date"] == today else (1 if row["match_date"] == tomorrow else 2)
        enriched_pri = 0 if row["enriched_at"] is None else 1
        sort_key = (
            -int(row["is_live"]),   # live first
            date_pri,               # today, then tomorrow, then later
            t_pri,                  # dynamic tournament priority (lower = liked)
            enriched_pri,           # unenriched first
            row["start_time"] or 0, # earliest start first
        )
        annotated.append((sort_key, row))

    annotated.sort(key=lambda item: item[0])
    top_rows = [row for _, row in annotated[:limit]]

    return [
        {
            "match_id": row["match_id"],
            "is_live": bool(row["is_live"]),
            "match_date": row["match_date"],
            "source_table": "match_buffer",
            "sporty": json.loads(row["raw_sporty"]) if row["raw_sporty"] else {},
            "existing": json.loads(row["raw_enriched"]) if row["raw_enriched"] else None,
        }
        for row in top_rows
    ]


def store_enriched(match_id: str, doc: dict[str, Any]) -> None:
    """Write the enriched document back into the buffer row."""
    _init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        row = conn.execute("select raw_sporty, raw_enriched from match_buffer where match_id = ?", (match_id,)).fetchone()
        if row:
            raw_sporty = json.loads(row[0]) if row[0] else {}
            existing = json.loads(row[1]) if row[1] else {}
            doc = _merge_enriched(raw_sporty, existing, doc)
        doc = enrich_match_facts(doc)
        conn.execute(
            """
            update match_buffer set
                enriched_at  = ?,
                data_source  = ?,
                sportybet_id = ?,
                sofascore_id = ?,
                raw_enriched = ?
            where match_id = ?
            """,
            (
                now,
                doc.get("data_source") or _provider_state_from_doc(doc),
                _stored_sportybet_id(doc),
                str(doc.get("sofascore_id") or ""),
                json.dumps(doc),
                match_id,
            ),
        )
        conn.commit()

    try:
        snapshot_live_statistics(doc)
    except Exception:
        logger.warning("live-stat snapshot failed for %s", match_id, exc_info=True)

    # When a SofaScore ID is newly resolved, push the full enriched doc to team
    # watcher so it gets sofa team IDs, standings, and last matches appended.
    sofa_id = str(doc.get("sofascore_id") or "")
    if sofa_id:
        _update_team_watcher_sofa_ids(match_id, doc)
        try:
            from app.competition.competition_special import sort_enriched_doc_into_competition
            sort_enriched_doc_into_competition(doc)
        except Exception:
            pass


def _update_team_watcher_sofa_ids(match_id: str, doc: dict[str, Any]) -> None:
    """After SofaScore enrichment resolves team IDs, update team watcher with full context."""
    try:
        from app.team_watcher.team_watcher import observe_match as _tw_observe
        _tw_observe(doc)
    except Exception:
        pass


# ── Read API ──────────────────────────────────────────────────────────────────

def get_buffered_matches(match_date: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """
    Returns matches for the frontend.
    Prefers raw_enriched (full data) but falls back to raw_sporty (basic data)
    so the frontend always gets something even before enrichment runs.
    """
    _init_db()
    with _conn() as conn:
        clauses = ["is_finished = 0"]
        params: list[Any] = []
        if match_date:
            clauses.append("match_date = ?")
            params.append(match_date)
        rows = conn.execute(
            f"""
            select raw_enriched, raw_sporty, is_live, enriched_at, match_date, start_time
            from match_buffer
            where {" and ".join(clauses)}
            order by is_live desc, start_time asc
            limit ?
            """,
            tuple(params) + (limit,),
        ).fetchall()

    result = []
    for row in rows:
        if row["raw_enriched"]:
            doc = json.loads(row["raw_enriched"])
            sporty = json.loads(row["raw_sporty"]) if row["raw_sporty"] else {}
            result.append(_finalize_buffer_doc(_ensure_country_fields(doc, sporty), sporty))
        else:
            # not yet enriched — return raw sporty data so frontend isn't empty
            sporty = json.loads(row["raw_sporty"])
            doc = _sporty_to_summary(sporty)
            doc["match_date"] = row["match_date"]
            result.append(_finalize_buffer_doc(doc, sporty))
    return result


def get_buffered_match(match_id: str) -> dict[str, Any] | None:
    """Returns the enriched doc for a single match, or raw sporty if not yet enriched."""
    _init_db()
    lookup_ids = _buffer_lookup_ids(str(match_id))
    with _conn() as conn:
        row = None
        for lookup_id in lookup_ids:
            row = conn.execute(
                "select raw_enriched, raw_sporty, match_date from match_buffer where match_id = ?",
                (lookup_id,),
            ).fetchone()
            if row:
                break
    if not row:
        return None
    sporty = json.loads(row["raw_sporty"]) if row["raw_sporty"] else {}
    if row["raw_enriched"]:
        doc = json.loads(row["raw_enriched"])
        return _finalize_buffer_doc(_ensure_country_fields(doc, sporty), sporty)
    doc = _sporty_to_summary(sporty)
    doc["match_date"] = row["match_date"]
    return _finalize_buffer_doc(doc, sporty)


def bulk_get_buffered_matches(match_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return buffered docs for many match IDs with one SQLite read.

    The returned mapping includes every requested ID that resolved, plus the
    stored buffer ID when it differs from the requested ID.
    """
    requested = [str(match_id or "") for match_id in match_ids if str(match_id or "")]
    if not requested:
        return {}
    lookup_to_requested: dict[str, list[str]] = {}
    for requested_id in requested:
        for lookup_id in _buffer_lookup_ids(requested_id):
            lookup_to_requested.setdefault(lookup_id, []).append(requested_id)

    _init_db()
    with _conn() as conn:
        placeholders = ",".join("?" for _ in lookup_to_requested)
        rows = conn.execute(
            f"""
            select match_id, raw_enriched, raw_sporty, match_date
            from match_buffer
            where match_id in ({placeholders})
            """,
            tuple(lookup_to_requested.keys()),
        ).fetchall()

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        sporty = json.loads(row["raw_sporty"]) if row["raw_sporty"] else {}
        if row["raw_enriched"]:
            doc = json.loads(row["raw_enriched"])
            doc = _finalize_buffer_doc(_ensure_country_fields(doc, sporty), sporty)
        else:
            doc = _sporty_to_summary(sporty)
            doc["match_date"] = row["match_date"]
            doc = _finalize_buffer_doc(doc, sporty)
        stored_id = str(row["match_id"] or "")
        result[stored_id] = doc
        for requested_id in lookup_to_requested.get(stored_id, []):
            result.setdefault(requested_id, doc)
    return result


def _buffer_lookup_ids(match_id: str) -> list[str]:
    ids = [match_id]
    if match_id and ":" not in match_id:
        ids.append(f"sofascore:{match_id}")
        ids.append(f"competition:world-cup-2026:{match_id}")
    if match_id.startswith("competition:"):
        ids.append(f"sofascore:{match_id.rsplit(':', 1)[-1]}")
    return ids


def get_live_buffered_matches(limit: int = 200) -> list[dict[str, Any]]:
    """All currently live matches from the buffer."""
    _init_db()
    with _conn() as conn:
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
            doc = json.loads(row["raw_enriched"])
            sporty = json.loads(row["raw_sporty"]) if row["raw_sporty"] else {}
            doc = _finalize_buffer_doc(_ensure_country_fields(doc, sporty), sporty)
            if doc.get("is_live"):
                result.append(doc)
        else:
            sporty = json.loads(row["raw_sporty"])
            doc = _finalize_buffer_doc(_sporty_to_summary(sporty), sporty)
            if doc.get("is_live"):
                result.append(doc)
    return result


def refresh_sporty_buffer_scope(scope: str = "upcoming", limit: int = 500) -> dict[str, Any]:
    """Pull fresh SportyBet state and update buffered odds/time/status before prediction."""
    from app.market.market import snapshot_odds
    from app.data_clients.sportybet_client import fetch_live_matches_post, fetch_upcoming_matches_post

    scope = "live" if str(scope).lower() == "live" else "upcoming"
    matches = (fetch_live_matches_post() if scope == "live" else fetch_upcoming_matches_post())[:limit]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for match in matches:
        grouped.setdefault(_date_from_start_time(match.get("start_time")), []).append(match)

    patched = patch_live_scores(matches) if scope == "live" else 0
    ingested = 0
    for match_date, rows in grouped.items():
        ingested += ingest_matches(rows, match_date)

    snapshotted = 0
    match_ids = [str(match.get("id") or "") for match in matches if match.get("id")]
    docs_by_id = bulk_get_buffered_matches(match_ids)
    for match in matches:
        doc = docs_by_id.get(str(match.get("id") or ""))
        if doc:
            try:
                snapshotted += 1 if snapshot_odds(doc) else 0
            except Exception:
                pass
    return {
        "scope": scope,
        "fetched": len(matches),
        "ingested": ingested,
        "patched": patched,
        "odds_snapshotted": snapshotted,
    }


def refresh_sporty_match_state(match_id: str) -> dict[str, Any]:
    """Refresh one buffered match from SportyBet before enrichment/prediction."""
    from app.market.market import snapshot_odds
    from app.data_clients.sportybet_client import fetch_match_info

    match_id = str(match_id)
    info = fetch_match_info(match_id, bypass_cache=True)
    refreshed = info.get("match") if info.get("found") else None
    scope = info.get("scope")
    if not refreshed:
        _mark_missing_from_sporty(match_id)
        return {
            "active": False,
            "match_id": match_id,
            "scope": None,
            "reason": "not_found_in_fresh_sporty",
            "sporty_endpoint": info.get("api_endpoint"),
            "errors": info.get("errors") or [],
        }

    match_date = _date_from_start_time(refreshed.get("start_time"))
    if scope == "live":
        patch_live_scores([refreshed])
    ingest_matches([refreshed], match_date)
    doc = get_buffered_match(match_id)
    snapshotted = False
    if doc:
        try:
            snapshotted = snapshot_odds(doc)
        except Exception:
            snapshotted = False

    state = classify_match_state(refreshed)
    period = refreshed.get("period") or ""
    is_live = bool(state.get("is_live"))
    is_finished = bool(state.get("is_finished") or state.get("state") in {"postponed", "cancelled"})
    return {
        # SportyBet can include a fixture with period "Not start" in its
        # `isLive=true` response shortly before kick-off.  The provider bucket
        # is not authoritative; the parsed match state is.  Rejecting it here
        # made valid prematch requests fail with "not active".
        "active": _is_sporty_match_active(state),
        "match_id": match_id,
        "scope": scope,
        "is_live": scope == "live" and is_live and not is_finished,
        "is_finished": is_finished,
        "match_state": state,
        "period": period,
        "match_date": match_date,
        "start_time": refreshed.get("start_time"),
        "odds_snapshotted": snapshotted,
        "sporty_endpoint": info.get("api_endpoint"),
        "source": info.get("source"),
    }


def _is_sporty_match_active(state: dict[str, Any]) -> bool:
    """Whether a fresh SportyBet event can safely be enriched or predicted."""
    if bool(state.get("is_finished")) or state.get("state") in {"finished", "postponed", "cancelled"}:
        return False
    return bool(state.get("is_prematch") or state.get("is_live"))


def purge_ghost_matches() -> int:
    """
    Remove stale matches from the buffer:
    1. 'Not start' matches whose kick-off passed GHOST_MATCH_GRACE_MINUTES ago
    2. Stuck-live matches from past dates that never transitioned to finished
       (match_date < today and is_live=1 and is_finished=0)
    3. Far-future matches beyond MAX_FUTURE_DAYS that have already been enriched
       once — they'll be re-ingested and re-enriched closer to their date.
       Unmatched far-future matches are kept so enrichment can try them.
    Returns total rows deleted.
    """
    _init_db()
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff_ts_ms = (now_ts - GHOST_MATCH_GRACE_MINUTES * 60) * 1000
    today = date_cls.today().isoformat()
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    kickoff_cutoff_ms = (now_ts - 130 * 60) * 1000

    with _conn() as conn:
        count = 0

        # 1. Ghost not-started: kick-off passed but never went live
        r1 = conn.execute(
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

        # 2. Stuck-live: match_date is in the past but still flagged is_live=1
        r2 = conn.execute(
            """
            delete from match_buffer
            where is_live = 1
              and is_finished = 0
              and match_date < ?
            """,
            (today,),
        )

        # 2b. Self-heal stuck-live matches today
        r2b = conn.execute(
            """
            update match_buffer set
                is_live = 0,
                is_finished = 1,
                period = coalesce(nullif(period, ''), 'FT')
            where is_live = 1
              and is_finished = 0
              and ingested_at < ?
              and (
                cast(coalesce(json_extract(raw_sporty, '$.played_seconds'),
                              json_extract(raw_enriched, '$.played_seconds'), 0) as real) >= 5700
                or (start_time is not null and cast(start_time as real) < ?)
              )
            """,
            (stale_cutoff, kickoff_cutoff_ms),
        )

        # Collect healed IDs before committing so we can archive them after releasing the lock
        healed_ids = [
            row[0] for row in conn.execute(
                "select match_id from match_buffer where is_finished = 1 and is_live = 0 and ingested_at < ?",
                (stale_cutoff,),
            ).fetchall()
        ]

        # 3. Non-live rows that already failed SofaScore matching and passed their retry window
        r4 = conn.execute(
            """
            delete from match_buffer
            where is_live = 0
              and is_finished = 0
              and json_extract(raw_enriched, '$.sofascore_match_status') = 'no_match'
              and coalesce(cast(json_extract(raw_enriched, '$.sofascore_retry_after_ts') as real), 0) <= strftime('%s','now')
            """
        )
        conn.commit()

    # Archive healed matches AFTER releasing the write lock
    for match_id in healed_ids:
        _try_archive_finished(match_id)

    healed = r2b.rowcount
    deleted = r1.rowcount + r2.rowcount + r4.rowcount
    if deleted or healed:
        print(
            f"[buffer] purge_ghost_matches: removed {deleted} total "
            f"(ghost_not_started={r1.rowcount}, stuck_live_past={r2.rowcount}, "
            f"no_match_hot={r4.rowcount}) | self_healed_stuck_today={healed}"
        )
    return deleted


def get_buffer_stats() -> dict[str, Any]:
    """Counts for monitoring.

    Single aggregation pass over match_buffer instead of 16 separate
    COUNT(*)/MAX() scans (each of the original queries held a read lock over
    the whole table for its own full scan).

    Also fixes a real correctness bug found while consolidating: the pending
    count's retry-cooldown check used
    `coalesce(cast(json_extract(...) as real), 0) <= strftime('%s','now')`
    -- the same coalesce()-wrapped-inline-strftime pattern already proven
    buggy elsewhere in this codebase (see the job_ai_prediction_queue fix
    from the Sep 4 2026 session): SQLite silently mis-compares that specific
    combination, so a match still inside its 3h SofaScore retry cooldown was
    incorrectly counted as "pending" (due-now) instead of excluded. Fixed by
    binding the current time as a Python-computed parameter instead, the
    same safe pattern used everywhere else in this file.
    """
    _init_db()
    from datetime import date, datetime as _dt, timezone as _tz
    today = date.today().isoformat()
    now_ts = _dt.now(_tz.utc).timestamp()
    with _conn() as conn:
        row = conn.execute(
            """
            select
              count(*) as total,
              sum(case when match_date = ? then 1 else 0 end) as today_count,
              sum(case when is_live = 1 then 1 else 0 end) as live_count,
              sum(case when enriched_at is not null then 1 else 0 end) as enriched,
              sum(case when is_finished = 0 and (
                    enriched_at is null or sofascore_id is null or sofascore_id = ''
                  ) and (
                    json_extract(raw_enriched, '$.sofascore_match_status') is null
                    or json_extract(raw_enriched, '$.sofascore_match_status') != 'no_match'
                    or coalesce(cast(json_extract(raw_enriched, '$.sofascore_retry_after_ts') as real), 0) <= ?
                  ) then 1 else 0 end) as pending,
              max(ingested_at) as last_ingest,
              max(enriched_at) as last_enrich,
              sum(case when is_finished = 0
                    and json_extract(raw_enriched, '$.sofascore_match_status') = 'no_match'
                  then 1 else 0 end) as no_sofa_match,
              sum(case when is_finished = 0 and enriched_at is null then 1 else 0 end) as needs_enrichment,
              sum(case when is_finished = 0
                    and json_extract(raw_enriched, '$.sofascore_detail') is not null
                    and json_extract(raw_enriched, '$.prediction_error') is null
                  then 1 else 0 end) as ready,
              sum(case when is_finished = 0
                    and json_extract(raw_enriched, '$.prediction_error') is not null
                  then 1 else 0 end) as deferred,
              sum(case when is_finished = 0 and is_live = 1 and (
                    ingested_at is null or datetime(ingested_at) < datetime('now', '-5 minutes')
                  ) then 1 else 0 end) as stale_live,
              sum(case when is_finished = 0 and is_live = 0
                    and (start_time is null or cast(start_time as real) <= (strftime('%s','now') + 86400) * 1000)
                    and (start_time is null or cast(start_time as real) > (strftime('%s','now') - 7200) * 1000)
                  then 1 else 0 end) as hot_upcoming,
              sum(case when is_finished = 0 and is_live = 0
                    and start_time is not null
                    and cast(start_time as real) > (strftime('%s','now') + 86400) * 1000
                  then 1 else 0 end) as future_buffered,
              sum(case when is_finished = 0 and is_live = 0
                    and enriched_at is not null
                    and start_time is not null
                    and cast(start_time as real) > (strftime('%s','now') + 86400) * 1000
                  then 1 else 0 end) as future_enriched,
              sum(case when is_finished = 0 and is_live = 0 and (
                    enriched_at is null or sofascore_id is null or sofascore_id = ''
                  ) and start_time is not null
                    and cast(start_time as real) > (strftime('%s','now') + 86400) * 1000
                  then 1 else 0 end) as future_pending
            from match_buffer
            """,
            (today, now_ts),
        ).fetchone()

    (total, today_count, live_count, enriched, pending, last_ingest, last_enrich,
     no_sofa_match, needs_enrichment, ready, deferred, stale_live,
     hot_upcoming, future_buffered, future_enriched, future_pending) = row

    # SUM() over zero matching rows returns NULL (not 0) in SQLite -- an
    # empty buffer table would otherwise turn every count field into None
    # and break the arithmetic below. Count fields are always non-negative
    # so None unambiguously means "zero rows"; last_ingest/last_enrich are
    # MAX(text) timestamps where NULL is a legitimate "no data yet" value
    # and must stay None, not become the int 0.
    total = total or 0
    today_count = today_count or 0
    live_count = live_count or 0
    enriched = enriched or 0
    pending = pending or 0
    no_sofa_match = no_sofa_match or 0
    needs_enrichment = needs_enrichment or 0
    ready = ready or 0
    deferred = deferred or 0
    stale_live = stale_live or 0
    hot_upcoming = hot_upcoming or 0
    future_buffered = future_buffered or 0
    future_enriched = future_enriched or 0
    future_pending = future_pending or 0

    return {
        "total_buffered": total,
        "visible_buffered": total,
        "hot_buffered": total - future_buffered,
        "future_buffered": future_buffered,
        "today": today_count,
        "live": live_count,
        "upcoming": hot_upcoming,
        "enriched": enriched,
        "hot_enriched": enriched - future_enriched,
        "future_enriched": future_enriched,
        "pending_enrichment": pending,
        "hot_pending_enrichment": pending - future_pending,
        "future_pending_enrichment": future_pending,
        "needs_enrichment": needs_enrichment,
        "no_sofa_match": no_sofa_match,
        "ready": ready,
        "deferred": deferred,
        "stale_live": stale_live,
        "future_queued": future_pending,
        "queue_labels": {
            "upcoming": hot_upcoming,
            "future_queued": future_pending,
            "needs_enrichment": needs_enrichment,
            "no_sofa_match": no_sofa_match,
            "ready": ready,
            "deferred": deferred,
            "stale_live": stale_live,
        },
        "last_ingested_at": last_ingest,
        "last_enriched_at": last_enrich,
    }


# ── Enrichment worker ─────────────────────────────────────────────────────────

def _sporty_live_data(sporty: dict[str, Any] | None) -> dict[str, Any]:
    if not sporty or not classify_match_state(sporty).get("is_live"):
        return {}
    return {
        "source": "sportybet",
        "score": sporty.get("score") or {},
        "period": sporty.get("period"),
        "played_seconds": sporty.get("played_seconds"),
        "markets": sporty.get("markets") or [],
        "home_red_cards": sporty.get("home_red_cards"),
        "away_red_cards": sporty.get("away_red_cards"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _sofa_live_data(detail: dict[str, Any] | None) -> dict[str, Any]:
    if not detail:
        return {}
    statistics = detail.get("statistics") or detail.get("match_statistics") or []
    incidents = detail.get("incidents") or []
    if not statistics and not incidents:
        return {}
    return {
        "source": "sofascore",
        "statistics": statistics,
        "normalized_statistics": normalize_live_statistics(statistics),
        "incidents": incidents,
        "graph": detail.get("graph") or detail.get("momentum") or {},
        "score": detail.get("score") or detail.get("homeScore") or {},
        "status": detail.get("status"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _assemble_enrichment_doc(
    existing: dict[str, Any],
    sporty: dict[str, Any],
    item: dict[str, Any],
    sofa: dict | None,
    detail: dict | None,
    sportradar_detail: dict,
    web_context: dict,
    league_sentiment: dict,
    score: float,
    match_status: str,
    now: str,
    time_context: dict,
    match_state: Any,
    retry_after_ts: float,
    *,
    detail_source: str | None = None,
    match_source: str | None = None,
) -> dict[str, Any]:
    """
    Build the enriched-match doc dict shared by run_enrichment_worker (parallel,
    scheduler-driven) and run_date_aware_enrichment (sequential, manual-action-
    driven). Both callers assemble an (almost) identical ~35-field doc from the
    same inputs — this is the single source of truth for that shape.

    `detail_source` / `match_source` are date-aware-only extras (how the
    SofaScore detail was obtained, and how the match itself was resolved —
    "saved"/"fuzzy"/"llm"). They're omitted from the doc entirely when not
    supplied, matching run_enrichment_worker's original doc shape exactly.
    """
    doc = {
        **existing,
        "data_source":       "both" if (sofa or existing.get("sofascore_id")) else "sportybet",
        "sportybet_id":      sporty.get("id"),
        "match_id":          item.get("match_id"),
        "name":              sporty.get("name"),
        "sportybet_name":    sporty.get("name"),
        "match_date":        time_context.get("local_date") or item["match_date"],
        "tournament":        sporty.get("tournament"),
        "category":          sporty.get("category"),
        "start_time":        sporty.get("start_time"),
        "period":            sporty.get("period"),
        "played_seconds":    sporty.get("played_seconds"),
        "score":             sporty.get("score"),
        "venue":             sporty.get("venue"),
        "sportybet_detail":  _sporty_detail_doc(sporty),
        "sportybet_data_status": "available",
        "data_sources":      _data_sources(sofa, detail, sporty, sportradar_detail),
        "sportradar_detail": sportradar_detail,
        "sportybet_markets": sporty.get("markets", []),
        "markets":           sporty.get("markets", []),
        "live_data_sportybet": _sporty_live_data(sporty) or existing.get("live_data_sportybet") or {},
        "sofascore_id":      sofa.get("id") if sofa else existing.get("sofascore_id"),
        "sofascore_name":    sofa.get("name") if sofa else None,
        "sofascore_event":   sofa,
        "sofascore_detail":  detail,
        "live_data_sofascore": _sofa_live_data(detail) or existing.get("live_data_sofascore") or {},
        "home_last_matches": (detail or {}).get("home_last_matches") or [],
        "away_last_matches": (detail or {}).get("away_last_matches") or [],
        "standings":         (detail or {}).get("standings") or [],
        "league_table":      (detail or {}).get("standings") or [],
        "season_stage":      detect_season_stage((detail or {}).get("standings") or []),
        "web_context":       web_context,
        "league_sentiment":  league_sentiment,
        "match_score":       round(score, 3),
        "sofascore_match_status": match_status,
        "sofascore_candidate_count": int(item.get("sofascore_candidate_count") or 0),
        "sofascore_best_score": round(score, 3),
        "sofascore_dates_scanned": _sofascore_date_candidates(sporty, item.get("match_date")),
        "sofascore_no_match_at": None if sofa else now,
        "sofascore_retry_after_ts": None if sofa else retry_after_ts,
        "manual_match":      bool(existing.get("manual_match")),
        "raw_sporty":        sporty,
        "raw_sofascore_event": sofa.get("raw_event") if isinstance(sofa, dict) else None,
        "time_context":      time_context,
        "match_state":       match_state,
        "enriched_at":       now,
    }
    if detail_source is not None:
        doc["sofascore_detail_source"] = detail_source
    if match_source is not None:
        doc["match_source"] = match_source

    doc["data_sources"] = _data_sources(
        sofa or ({"id": existing.get("sofascore_id")} if existing.get("sofascore_id") else None),
        {**(detail or {}), "live_data_sofascore": doc.get("live_data_sofascore") or {}},
        {**sporty, "live_data_sportybet": doc.get("live_data_sportybet") or {}},
        sportradar_detail,
    )
    doc["data_source_detail"] = doc.get("data_sources") or {}
    _track_live_data_availability(str(item.get("match_id") or ""), doc)

    snapshot_odds(doc)
    return doc


def _finalize_enrichment_prediction(
    doc: dict[str, Any],
    item: dict[str, Any],
    sporty: dict[str, Any],
    sofa: dict | None,
    *,
    job_tag: str,
) -> bool:
    """
    Run the prediction branch shared by run_enrichment_worker and
    run_date_aware_enrichment: attempt a deterministic prediction when a
    SofaScore match (or live match) is available, overlay the AI Prediction
    Queue flag when that pipeline is enabled, set doc["lifecycle"], and
    persist via store_enriched.

    Pipeline ownership: the deterministic engine always predicts here when
    data allows (enrichment owns this lane) — it no longer steps aside for
    the AI Prediction Queue toggle. That exclusivity design (deterministic
    skipped entirely whenever AI was "on") turned out to conflict with the
    dual-engine arbitration built into record_prediction()/engine_arbitration.py:
    each engine now writes its own tagged row (engine='deterministic' vs
    'ai_llm') with its own independent per-engine cooldown, and arbitration
    picks which one is shown (is_final) using each engine's own tracked
    win rate — defaulting to the deterministic engine, the safe incumbent,
    until the AI earns enough graded history to outweigh it. Both keep
    grading independently either way, so this lane just needs to flag the
    match for the AI queue (job_ai_prediction_queue, its own cron) to also
    weigh in — not decide who "wins"; that's arbitration's job now.

    Returns True iff a deterministic prediction was made this call.
    """
    from app.utils.activity_log import record_activity

    predicted = False
    try:
        from app.scheduling.pipeline_registry import is_pipeline_enabled
        ai_queue_enabled = is_pipeline_enabled("ai_prediction_queue")
    except Exception:
        ai_queue_enabled = False

    if sofa or item.get("is_live"):
        from app.utils.prediction_flow import apply_prediction_state

        state = apply_prediction_state(
            doc,
            match_id=str(item.get("match_id") or ""),
            use_llm_pipeline=False,
            attach_brain=True,
        )
        readiness = state.get("readiness") or {}
        if state.get("status") == "predicted":
            predicted = True
            record_activity(
                f"Manual prediction completed for {sporty.get('name') or item['match_id']}",
                job=job_tag,
                status="predicted",
                match_id=str(item.get("match_id") or ""),
                match_name=sporty.get("name"),
                details={"sofascore_id": doc.get("sofascore_id"), "assurance": readiness.get("assurance")},
            )
        elif state.get("status") == "skipped":
            record_activity(
                f"Manual prediction skipped for {sporty.get('name') or item['match_id']}: {state.get('skip_reason')}",
                job=job_tag,
                status="skipped",
                match_id=str(item.get("match_id") or ""),
                match_name=sporty.get("name"),
                details={"skip_reason": state.get("skip_reason"), "existing": state.get("existing")},
            )
        elif state.get("status") == "deferred":
            record_activity(
                f"Manual prediction deferred for {sporty.get('name') or item['match_id']}: missing {', '.join(readiness.get('missing') or [])}",
                job=job_tag,
                status="waiting",
                match_id=str(item.get("match_id") or ""),
                match_name=sporty.get("name"),
                details=readiness,
            )
        else:
            print(f"[buffer:{job_tag}] manual prediction failed for {item['match_id']}: {state.get('error')}")
            record_activity(
                f"Manual prediction failed for {sporty.get('name') or item['match_id']}: {state.get('error')}",
                job=job_tag,
                status="error",
                match_id=str(item.get("match_id") or ""),
                match_name=sporty.get("name"),
            )
        # Overlay: also let the AI queue weigh in on this same match — for
        # both prematch and live, now that arbitration (not a lockout) is
        # what reconciles the two engines' calls.
        if ai_queue_enabled:
            from app.enrichment.enriched_prediction import prediction_readiness as _pred_readiness

            doc["prediction_readiness"] = _pred_readiness(doc)
            doc["ai_prediction_queue_pending"] = True
    elif not item.get("is_live") and ai_queue_enabled:
        from app.enrichment.enriched_prediction import prediction_readiness

        doc["prediction"] = None
        doc["prediction_error"] = None
        doc["prediction_readiness"] = prediction_readiness(doc)
        doc["ai_prediction_queue_pending"] = True
    else:
        from app.enrichment.enriched_prediction import prediction_readiness

        doc["prediction"] = None
        readiness = prediction_readiness(doc)
        doc["prediction_readiness"] = readiness
        doc["prediction_error"] = (
            "Minimum SportyBet enrichment completed; prediction deferred until a confident SofaScore match is found."
        )

    doc["lifecycle"] = _lifecycle_state(doc)
    store_enriched(item["match_id"], doc)
    return predicted


def run_enrichment_worker(
    batch_size: int = 10,
    live_only: bool = False,
    future_only: bool = False,
    exclude_live: bool = False,
    force_live_retry: bool = False,
    fetch_web_context: bool = True,
) -> dict[str, Any]:
    """
    Pick up a batch of unenriched/stale matches and enrich them.
    Called by the scheduler — runs continuously in background.
    Returns a summary of what was done.
    """
    from app.data_clients.sofascore_client import fetch_all_scheduled_events, fetch_event_detail, fetch_live_events
    from app.enrichment.enrichment import _fuzzy_match, _llm_match, _is_junk, FUZZY_THRESHOLD, LLM_FALLBACK_THRESHOLD
    from app.data_clients.sportradar_client import fetch_match_intelligence
    from app.enrichment.web_context import search_league_sentiment, search_match_context
    from app.utils.time_context import match_time_context
    from app.utils.activity_log import record_activity
    from datetime import date

    batch = get_unenriched_batch(
        batch_size,
        live_only=live_only,
        future_only=future_only,
        exclude_live=exclude_live,
        force_live_retry=force_live_retry,
    )
    if not batch:
        return {"status": "idle", "pending": 0}

    # Fetch SofaScore by the actual kickoff date, with live endpoint for in-play matches.
    # Skip list fetching entirely for live matches that already have sofascore_id +
    # existing detail — they only need the lightweight live refresh, not a full re-match.
    dates: dict[str, list[dict]] = {}
    live_needed = False
    live_already_matched: set[str] = set()  # match_ids that can skip list matching
    for item in batch:
        existing = item.get("existing") or {}
        if (
            item["is_live"]
            and existing.get("sofascore_id")
            and existing.get("sofascore_detail")
        ):
            # Already matched + has detail — skip list fetch, go straight to refresh
            live_already_matched.add(str(item["sporty"].get("id") or item.get("match_id") or ""))
        else:
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
        match_name = sporty.get("name") or item.get("match_id")
        match_id_str = str(sporty.get("id") or item.get("match_id") or "")

        # ── Fast path: live match already matched + has detail ────────────────
        # Skip SofaScore list matching entirely. The lightweight refresh in
        # _fetch_detail will use the existing sofascore_id to fetch only
        # statistics + incidents + lineups.
        if match_id_str in live_already_matched:
            sofa_stub = {"id": existing["sofascore_id"]}
            pairs.append((item, sofa_stub, float(existing.get("match_score") or 1.0)))
            matched += 1
            continue

        # ── Prematch guard: raw_sporty must be present ────────────────────────
        # Static fields (home_team, away_team, tournament, etc.) are read
        # directly from raw_sporty for prematch items. If raw_sporty is absent
        # (edge case from legacy ingest), skip and wait for the next cycle.
        # Never call refresh_sporty_match_state for prematch items.
        if not item.get("is_live") and not sporty:
            logger.warning(
                "Skipping prematch match %s: raw_sporty absent",
                item.get("match_id"),
            )
            continue

        # ── SRL / simulated match guard ───────────────────────────────────────
        # SportyBet carries SRL (Simulated Reality League) virtual fixtures that
        # have no real SofaScore counterpart.  Skip the matching attempt entirely
        # so we don't waste API calls and don't leave the row stuck with
        # repeated "no_match" retries.
        if _is_junk(match_name or ""):
            srl_doc = {**(existing or {}), "sofascore_match_status": "srl_skip", "data_source": "sportybet"}
            store_enriched(item["match_id"], srl_doc)
            record_activity(
                f"SRL/virtual match skipped: {match_name}",
                job="enrich_worker",
                status="skipped",
                match_id=str(item.get("match_id") or ""),
                match_name=match_name,
            )
            continue

        record_activity(
            f"Matching SofaScore for {match_name}",
            job="enrich_worker",
            status="running",
            match_id=str(item.get("match_id") or ""),
            match_name=match_name,
        )
        sofa_events = _candidate_sofascore_events(item, sofa_cache, live_events)
        sofa_events = _with_search_fallback_candidates(sporty, sofa_events, bool(item.get("is_live")))
        item["sofascore_candidate_count"] = len(sofa_events)
        saved_sofa_id = existing.get("sofascore_id")
        sofa = None
        score = 0.0

        if saved_sofa_id:
            sofa = next((event for event in sofa_events if str(event.get("id")) == str(saved_sofa_id)), None)
            if not sofa:
                # Not in the pre-fetched cache — do a direct fetch so we don't
                # fall back to stale sofascore_event and skip fetch_event_detail
                try:
                    from app.data_clients.sofascore_client import fetch_event, is_terminal_event
                    direct = fetch_event(str(saved_sofa_id))
                    if direct and not is_terminal_event(direct):
                        sofa = direct
                except Exception:
                    pass
            if not sofa and isinstance(existing.get("sofascore_event"), dict):
                sofa = existing["sofascore_event"]
            score = float(existing.get("match_score") or 1.0)
            matched += 1
            pairs.append((item, sofa, score))
            continue

        sofa, score = _fuzzy_match(sporty, sofa_events)

        threshold = 0.62 if item.get("is_live") else FUZZY_THRESHOLD
        llm_threshold = 0.55 if item.get("is_live") else LLM_FALLBACK_THRESHOLD
        if score < threshold:
            if score >= llm_threshold and not _is_junk(sporty.get("name") or ""):
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
        if sofa:
            record_activity(
                f"SofaScore matched: {match_name}",
                job="enrich_worker",
                status="matched",
                match_id=str(item.get("match_id") or ""),
                match_name=match_name,
                details={"sofascore_id": sofa.get("id"), "score": round(score, 3)},
            )
        else:
            record_activity(
                f"No confident SofaScore match for {match_name}",
                job="enrich_worker",
                status="waiting",
                match_id=str(item.get("match_id") or ""),
                match_name=match_name,
                details={"best_score": round(score, 3), "retry_minutes": NO_MATCH_RETRY_MINUTES},
            )

        pairs.append((item, sofa, score))

    # fetch SofaScore detail in parallel
    # For live matches that already have a sofascore_detail, use the lightweight
    # live refresh (statistics + incidents + lineups only) instead of the full
    # fetch_event_detail (12 sub-calls). This reduces per-cycle API calls from
    # ~12 to ~3 per live match, saving ~9 SofaScore calls per live match per 30s cycle.
    from app.data_clients.sofascore_client import fetch_event_detail_live_refresh

    def _fetch_detail(idx: int, sofa: dict, item: dict) -> tuple[int, dict | None]:
        try:
            existing = item.get("existing") or {}
            existing_detail = existing.get("sofascore_detail")
            is_live = bool(item.get("is_live"))
            sofa_id = sofa.get("id")

            # Live refresh path: match already has full detail, only update live fields
            if is_live and existing_detail and sofa_id:
                return idx, fetch_event_detail_live_refresh(int(sofa_id), existing_detail)

            # Full detail path: first enrichment or prematch
            return idx, fetch_event_detail(sofa)
        except Exception:
            return idx, None

    details: dict[int, dict | None] = {}
    needs_detail = [(i, sofa, item) for i, (item, sofa, _) in enumerate(pairs) if sofa]

    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        futures = {pool.submit(_fetch_detail, i, sofa, item): i for i, sofa, item in needs_detail}
        for future in as_completed(futures):
            idx, detail = future.result()
            details[idx] = detail

    def _fetch_sportradar(idx: int, item: dict) -> tuple[int, dict]:
        return idx, fetch_match_intelligence((item.get("sporty") or {}).get("id") or item.get("match_id"))

    sportradar_details: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        futures = {pool.submit(_fetch_sportradar, i, item): i for i, (item, _, _) in enumerate(pairs)}
        for future in as_completed(futures):
            idx, sportradar = future.result()
            sportradar_details[idx] = sportradar

    web_contexts: dict[int, dict] = {}
    if fetch_web_context:
        needs_web = [(i, item["sporty"]) for i, (item, _, _) in enumerate(pairs)]

        with ThreadPoolExecutor(max_workers=WEB_WORKERS) as pool:
            futures = {pool.submit(_fetch_web_context, i, sporty): i for i, sporty in needs_web}
            for future in as_completed(futures):
                idx, ctx = future.result()
                web_contexts[idx] = ctx

    # assemble and store
    now = datetime.now(timezone.utc).isoformat()
    stored = 0
    predicted = 0

    # league_sentiment is the same for all matches in a single worker cycle.
    # It is intentionally fetched once per cycle rather than per match to limit
    # web-search API usage.  Default to empty dict so the assembly below never
    # raises NameError when the tournament name is absent or when the feature
    # flag is disabled.
    league_sentiment: dict[str, Any] = {}
    try:
        from app.config.config import get_settings as _get_settings
        _cfg = _get_settings()
        if getattr(_cfg, "web_search_league_sentiment_enabled", False) and pairs:
            _first_sporty = pairs[0][0]["sporty"]
            league_sentiment = search_league_sentiment(_first_sporty.get("tournament") or "")
    except Exception:
        pass

    for i, (item, sofa, score) in enumerate(pairs):
        sporty = item["sporty"]
        existing = item.get("existing") or {}
        detail = details.get(i)
        sportradar_detail = sportradar_details.get(i) or {}
        web_context = web_contexts.get(i, {"query": "", "snippets": [], "scraped": []})
        match_state = classify_match_state(sporty)
        time_context = match_time_context({**sporty, "sofascore_event": sofa})

        match_status = "matched" if sofa else "no_match"
        retry_minutes = NO_MATCH_MIN_RETRY_LIVE_MINUTES if item.get("is_live") else NO_MATCH_RETRY_MINUTES
        retry_after_ts = (datetime.now(timezone.utc) + timedelta(minutes=retry_minutes)).timestamp()

        doc = _assemble_enrichment_doc(
            existing, sporty, item, sofa, detail, sportradar_detail,
            web_context, league_sentiment, score, match_status,
            now, time_context, match_state, retry_after_ts,
        )
        if _finalize_enrichment_prediction(doc, item, sporty, sofa, job_tag="enrich_worker"):
            predicted += 1
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

def run_date_aware_enrichment(count: int = 12) -> dict[str, Any]:
    """
    Enrich pending SportyBet rows in kickoff order.

    SofaScore event lists are fetched once per required date, then each match
    is matched, detailed, enriched, and predicted one after another. This is
    the manual page action: predictable, date-aware, and gentle on SofaScore.
    """
    from app.data_clients.sofascore_client import fetch_all_scheduled_events, fetch_event_detail, fetch_live_events
    from app.enrichment.enrichment import _fuzzy_match, _llm_match, _is_junk, FUZZY_THRESHOLD, LLM_FALLBACK_THRESHOLD
    from app.data_clients.sportradar_client import fetch_match_intelligence
    from app.enrichment.web_context import search_league_sentiment, search_match_context
    from app.utils.time_context import match_time_context
    from app.utils.activity_log import record_activity

    queue = get_unenriched_batch(count)
    if not queue:
        return {"status": "idle", "pending": 0, "processed": []}

    date_keys: list[str] = []
    live_needed = False
    for item in queue:
        existing = item.get("existing") or {}
        # Candidate feeds are needed only to discover an unknown SofaScore ID.
        # Saved matches can use their stored event (or one direct ID lookup)
        # below, so including their dates here causes needless full schedule
        # fetches on every live-refresh cycle.
        if existing.get("sofascore_id"):
            continue
        live_needed = live_needed or item.get("is_live", False)
        for date_key in _sofascore_date_candidates(item["sporty"], item.get("match_date")):
            if date_key not in date_keys:
                date_keys.append(date_key)

    sofa_cache: dict[str, list[dict]] = {}
    date_errors: dict[str, str] = {}
    for date_key in date_keys:
        try:
            sofa_cache[date_key] = fetch_all_scheduled_events(date_key)
        except Exception as exc:
            sofa_cache[date_key] = []
            date_errors[date_key] = str(exc)

    live_events: list[dict] = []
    live_error = None
    if live_needed:
        try:
            live_events = fetch_live_events()
        except Exception as exc:
            live_error = str(exc)

    processed: list[dict[str, Any]] = []
    matched = unmatched = llm_used = stored = predicted = 0

    for item in queue:
        sporty = item["sporty"]
        existing = item.get("existing") or {}
        match_name = sporty.get("name") or item.get("match_id")

        # ── SRL / simulated match guard ───────────────────────────────────────
        if _is_junk(match_name or ""):
            srl_doc = {**(existing or {}), "sofascore_match_status": "srl_skip", "data_source": "sportybet"}
            store_enriched(item["match_id"], srl_doc)
            record_activity(
                f"SRL/virtual match skipped: {match_name}",
                job="date_aware_enrichment",
                status="skipped",
                match_id=str(item.get("match_id") or ""),
                match_name=match_name,
            )
            continue

        record_activity(
            f"Matching SofaScore for {match_name}",
            job="date_aware_enrichment",
            status="running",
            match_id=str(item.get("match_id") or ""),
            match_name=match_name,
        )
        saved_sofa_id = existing.get("sofascore_id")
        sofa = None
        score = 0.0
        source = "none"

        if saved_sofa_id:
            if isinstance(existing.get("sofascore_event"), dict):
                sofa = existing["sofascore_event"]
            else:
                try:
                    from app.data_clients.sofascore_client import fetch_event, is_terminal_event
                    direct = fetch_event(str(saved_sofa_id))
                    if direct and not is_terminal_event(direct):
                        sofa = direct
                except Exception:
                    pass
            score = float(existing.get("match_score") or 1.0)
            source = "saved"
        else:
            sofa_events = _candidate_sofascore_events(item, sofa_cache, live_events)
            sofa_events = _with_search_fallback_candidates(sporty, sofa_events, bool(item.get("is_live")))
            item["sofascore_candidate_count"] = len(sofa_events)
            sofa, score = _fuzzy_match(sporty, sofa_events)
            source = "fuzzy"
        threshold = 0.62 if item.get("is_live") else FUZZY_THRESHOLD
        llm_threshold = 0.55 if item.get("is_live") else LLM_FALLBACK_THRESHOLD
        if score < threshold:
            if score >= llm_threshold and not _is_junk(sporty.get("name") or ""):
                llm_sofa = _llm_match(sporty, sofa_events)
                if llm_sofa:
                    sofa = llm_sofa
                    source = "llm"
                    llm_used += 1
                else:
                    sofa = None
            else:
                sofa = None

        if sofa:
            matched += 1
            record_activity(
                f"SofaScore matched: {match_name}",
                job="date_aware_enrichment",
                status="matched",
                match_id=str(item.get("match_id") or ""),
                match_name=match_name,
                details={"sofascore_id": sofa.get("id"), "score": round(score, 3), "source": source},
            )
        else:
            unmatched += 1
            record_activity(
                f"No confident SofaScore match for {match_name}",
                job="date_aware_enrichment",
                status="waiting",
                match_id=str(item.get("match_id") or ""),
                match_name=match_name,
                details={"best_score": round(score, 3), "source": source},
            )

        detail = _reusable_sofascore_detail(existing, sofa, bool(item.get("is_live")))
        detail_source = "saved" if detail else "fetched"
        if sofa and not detail:
            try:
                detail = fetch_event_detail(sofa)
            except Exception as exc:
                detail = None
                detail_source = "unavailable"
                source = f"{source}:detail_failed:{exc}"

        try:
            web_context = search_match_context(
                sporty.get("home_team") or "",
                sporty.get("away_team") or "",
                sporty.get("tournament") or "",
            )
        except Exception:
            web_context = {"query": "", "snippets": [], "scraped": []}

        league_sentiment = {}
        try:
            from app.config.config import get_settings

            settings = get_settings()
            if settings.web_search_league_sentiment_enabled:
                league_sentiment = search_league_sentiment(sporty.get("tournament") or "")
        except Exception:
            pass

        try:
            sportradar_detail = fetch_match_intelligence(sporty.get("id") or item.get("match_id"))
        except Exception:
            sportradar_detail = {}
        time_context = match_time_context({**sporty, "sofascore_event": sofa})
        now = datetime.now(timezone.utc).isoformat()
        match_state = classify_match_state(sporty)
        match_status = "matched" if sofa else "no_match"
        retry_minutes = NO_MATCH_MIN_RETRY_LIVE_MINUTES if item.get("is_live") else NO_MATCH_RETRY_MINUTES
        retry_after_ts = (datetime.now(timezone.utc) + timedelta(minutes=retry_minutes)).timestamp()
        doc = _assemble_enrichment_doc(
            existing, sporty, item, sofa, detail, sportradar_detail,
            web_context, league_sentiment, score, match_status,
            now, time_context, match_state, retry_after_ts,
            detail_source=detail_source, match_source=source,
        )
        if _finalize_enrichment_prediction(doc, item, sporty, sofa, job_tag="date_aware_enrichment"):
            predicted += 1
        stored += 1
        processed.append({
            "sportybet_id": item["match_id"],
            "name": sporty.get("name"),
            "match_date": doc.get("match_date"),
            "sofascore_id": doc.get("sofascore_id"),
            "matched": bool(sofa),
            "has_detail": bool(detail),
            "predicted": bool(sofa and doc.get("prediction")),
            "prediction_error": doc.get("prediction_error"),
            "score": round(score, 3),
            "source": source,
        })

    return {
        "status": "ok",
        "requested": count,
        "processed_count": len(processed),
        "dates_scanned": date_keys,
        "date_errors": date_errors,
        "live_error": live_error,
        "matched": matched,
        "unmatched": unmatched,
        "llm_fallback": llm_used,
        "stored": stored,
        "predicted": predicted,
        "processed": processed,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sporty_to_summary(m: dict[str, Any]) -> dict[str, Any]:
    """Minimal frontend-compatible doc from raw sporty data (pre-enrichment)."""
    score = m.get("score") or {}
    markets = m.get("markets") or []
    name = m.get("name") or ""
    parts = name.split(" vs ", 1)
    doc = {
        "sportybet_id":   str(m.get("id") or ""),
        "sportybet_name": name,
        "name":           name,
        "home_team":      parts[0].strip() if len(parts) == 2 else name,
        "away_team":      parts[1].strip() if len(parts) == 2 else "",
        "tournament":     m.get("tournament"),
        "category":       _country_from_sporty(m),
        "start_time":     m.get("start_time"),
        "period":         m.get("period"),
        "played_seconds": m.get("played_seconds"),
        "score":          score,
        "venue":          m.get("venue"),
        "sportybet_detail": _sporty_detail_doc(m),
        "sportybet_data_status": "available" if m else "missing",
        "data_sources":   _data_sources(None, None, m),
        "sportybet_markets": markets,
        "markets":        markets,
        "odds_1x2":       _extract_1x2(markets),
        "raw_sporty":     m,
        "has_sofascore":  False,
        "enriched":       False,
    }
    doc["lifecycle"] = _lifecycle_state(doc)
    return doc


def _ensure_country_fields(doc: dict[str, Any], sporty: dict[str, Any] | None = None) -> dict[str, Any]:
    country = doc.get("category") or _country_from_sporty(doc) or _country_from_sporty(sporty or {})
    if country:
        doc["category"] = country
        doc["country"] = country
    if sporty and not doc.get("sportybet_detail"):
        doc["sportybet_detail"] = _sporty_detail_doc(sporty)
    if not doc.get("data_sources"):
        doc["data_sources"] = _data_sources(
            doc.get("sofascore_event"),
            doc.get("sofascore_detail"),
            doc.get("raw_sporty") or sporty or doc,
            doc.get("sportradar_detail"),
        )
    return doc


def _finalize_buffer_doc(doc: dict[str, Any], sporty: dict[str, Any] | None = None) -> dict[str, Any]:
    source_doc = {**(sporty or {}), **doc}
    if sporty and "raw_sporty" not in source_doc:
        source_doc["raw_sporty"] = sporty
    state = classify_match_state(source_doc)
    doc["match_state"] = state
    doc["is_live"] = bool(state.get("is_live"))
    doc["is_finished"] = bool(state.get("is_finished") or doc.get("is_finished"))
    if not doc.get("time_context"):
        try:
            from app.utils.time_context import match_time_context

            doc["time_context"] = match_time_context(source_doc)
        except Exception:
            pass
    doc["data_source"] = _provider_state_from_doc(doc)
    doc["data_source_detail"] = doc.get("data_sources") or {}
    return doc


def _sporty_detail_doc(sporty: dict[str, Any] | None) -> dict[str, Any]:
    """Normalized SportyBet detail block used as a first-class enrichment source."""
    if not sporty:
        return {}
    score = sporty.get("score") or {}
    markets = sporty.get("markets") or []
    return {
        "source": "sportybet",
        "id": str(sporty.get("id") or ""),
        "name": sporty.get("name"),
        "home_team": sporty.get("home_team"),
        "away_team": sporty.get("away_team"),
        "tournament": sporty.get("tournament"),
        "category": _country_from_sporty(sporty),
        "start_time": sporty.get("start_time"),
        "period": sporty.get("period"),
        "played_seconds": sporty.get("played_seconds"),
        "score": score,
        "period_scores": sporty.get("period_scores"),
        "status": sporty.get("status"),
        "home_red_cards": sporty.get("home_red_cards"),
        "away_red_cards": sporty.get("away_red_cards"),
        "venue": sporty.get("venue"),
        "team_ids": sporty.get("team_ids") or {},
        "team_icons": sporty.get("team_icons") or {},
        "metadata": sporty.get("sporty_metadata") or {},
        "markets": markets,
        "market_count": len(markets),
        "odds_1x2": _extract_1x2(markets),
        "raw_event": sporty.get("raw_event"),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }


def _track_live_data_availability(match_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Attach a compact provider availability audit without blocking enrichment."""
    try:
        sources = doc.get("data_sources") if isinstance(doc.get("data_sources"), dict) else {}
        sporty_source = sources.get("sportybet") if isinstance(sources.get("sportybet"), dict) else {}
        sofa_source = sources.get("sofascore") if isinstance(sources.get("sofascore"), dict) else {}
        live_sporty = doc.get("live_data_sportybet") if isinstance(doc.get("live_data_sportybet"), dict) else {}
        live_sofa = doc.get("live_data_sofascore") if isinstance(doc.get("live_data_sofascore"), dict) else {}
        audit = {
            "match_id": str(match_id or doc.get("match_id") or doc.get("sportybet_id") or ""),
            "sportybet_live": bool(live_sporty),
            "sofascore_live": bool(live_sofa),
            "sofascore_detail": bool(sofa_source.get("detail")),
            "sportybet_markets": bool(sporty_source.get("markets")) or bool(doc.get("markets") or doc.get("sportybet_markets")),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        doc["live_data_availability"] = audit
        return audit
    except Exception:
        return {"match_id": str(match_id or ""), "status": "unavailable"}


def _provider_state_from_doc(doc: dict[str, Any]) -> str:
    source = str(doc.get("data_source") or "").strip().lower()
    if source in {"sportybet", "sofascore", "both"}:
        return source
    raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else {}
    synthetic_competition = bool(raw_sporty.get("competition_special_proxy") or (doc.get("competition_special") or {}).get("provider") == "sofascore")
    sporty_available = bool(
        doc.get("sportybet_id")
        or doc.get("sportybet_detail")
        or (doc.get("sportybet_markets") and not synthetic_competition)
        or (raw_sporty and not synthetic_competition)
    )
    sofa_available = bool(doc.get("sofascore_id") or doc.get("sofascore_event") or doc.get("sofascore_detail"))
    if sporty_available and sofa_available:
        return "both"
    if sofa_available:
        return "sofascore"
    return "sportybet"


def _stored_sportybet_id(doc: dict[str, Any]) -> str | None:
    value = doc.get("sportybet_id")
    if value:
        return str(value)
    raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else {}
    if raw_sporty.get("competition_special_proxy"):
        return None
    return None


def _country_from_sporty(m: dict[str, Any]) -> str | None:
    if not m:
        return None
    category = m.get("category") or m.get("country")
    if category:
        return str(category)
    raw_event = m.get("raw_event") or {}
    sport_category = (raw_event.get("sport") or {}).get("category") or {}
    if sport_category.get("name"):
        return str(sport_category.get("name"))
    raw_group = m.get("raw_group") or {}
    if raw_group.get("categoryName"):
        return str(raw_group.get("categoryName"))
    sofa_event = m.get("sofascore_event") or {}
    tournament = sofa_event.get("tournament") or {}
    sofa_category = tournament.get("category") or {}
    if sofa_category.get("name"):
        return str(sofa_category.get("name"))
    return None


def _merge_enriched(raw_sporty: dict[str, Any], existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Preserve sticky user-matched SofaScore state while refreshing volatile match data."""
    merged = {**existing, **incoming}
    merged["raw_sporty"] = raw_sporty or incoming.get("raw_sporty") or existing.get("raw_sporty")
    if existing.get("sofascore_id") and not incoming.get("sofascore_id"):
        merged["sofascore_id"] = existing.get("sofascore_id")
        merged["sofascore_name"] = existing.get("sofascore_name")
        merged["sofascore_event"] = existing.get("sofascore_event")
        merged["sofascore_detail"] = existing.get("sofascore_detail")
    if existing.get("live_data_sportybet") and not incoming.get("live_data_sportybet"):
        merged["live_data_sportybet"] = existing.get("live_data_sportybet")
    if existing.get("live_data_sofascore") and not incoming.get("live_data_sofascore"):
        merged["live_data_sofascore"] = existing.get("live_data_sofascore")
    merged["data_source"] = _provider_state_from_doc(merged)
    merged["sportybet_detail"] = incoming.get("sportybet_detail") or _sporty_detail_doc(merged.get("raw_sporty") or raw_sporty)
    merged["sportybet_data_status"] = (
        incoming.get("sportybet_data_status")
        or ("available" if merged.get("sportybet_detail") else existing.get("sportybet_data_status") or "missing")
    )
    merged["data_sources"] = incoming.get("data_sources") or _data_sources(
        merged.get("sofascore_event"),
        {**(merged.get("sofascore_detail") or {}), "live_data_sofascore": merged.get("live_data_sofascore") or {}},
        {**(merged.get("raw_sporty") or {}), "live_data_sportybet": merged.get("live_data_sportybet") or {}},
        merged.get("sportradar_detail"),
    )
    merged["data_source_detail"] = merged["data_sources"]
    _ensure_country_fields(merged, raw_sporty)
    if existing.get("manual_match") and existing.get("sofascore_id"):
        merged["manual_match"] = True
        merged["manual_matched_at"] = existing.get("manual_matched_at")
        merged["sofascore_id"] = existing.get("sofascore_id")
        merged["sofascore_name"] = incoming.get("sofascore_name") or existing.get("sofascore_name")
        merged["sofascore_event"] = incoming.get("sofascore_event") or existing.get("sofascore_event")
        merged["match_score"] = incoming.get("match_score") or existing.get("match_score") or 1.0
    merged["lifecycle"] = _lifecycle_state(merged)
    return merged


def _sync_enriched_sporty_fields(
    conn: sqlite3.Connection,
    table: str,
    match_id: str,
    sporty: dict[str, Any],
    match_date: str,
    is_live: int,
    is_finished: int,
) -> None:
    from app.utils.time_context import match_time_context

    row = conn.execute(f"select raw_enriched from {table} where match_id = ?", (match_id,)).fetchone()
    if not row or not row[0]:
        return
    try:
        doc = json.loads(row[0])
    except Exception:
        return
    score = sporty.get("score") or {}
    state = classify_match_state(sporty)
    doc.update({
        "sportybet_id": str(sporty.get("id") or doc.get("sportybet_id") or match_id),
        "sportybet_name": sporty.get("name") or doc.get("sportybet_name") or doc.get("name"),
        "name": sporty.get("name") or doc.get("name"),
        "home_team": sporty.get("home_team") or doc.get("home_team"),
        "away_team": sporty.get("away_team") or doc.get("away_team"),
        "tournament": sporty.get("tournament") or doc.get("tournament"),
        "category": sporty.get("category") or doc.get("category"),
        "start_time": sporty.get("start_time") or doc.get("start_time"),
        "match_date": match_date or doc.get("match_date"),
        "period": sporty.get("period") or doc.get("period"),
        "played_seconds": sporty.get("played_seconds"),
        "score": score,
        "score_home": score.get("home"),
        "score_away": score.get("away"),
        "home_red_cards": sporty.get("home_red_cards"),
        "away_red_cards": sporty.get("away_red_cards"),
        "sportybet_detail": _sporty_detail_doc(sporty),
        "sportybet_data_status": "available",
        "data_sources": _data_sources(
            doc.get("sofascore_event"),
            doc.get("sofascore_detail"),
            sporty,
            doc.get("sportradar_detail"),
        ),
        "sportybet_markets": sporty.get("markets") or doc.get("sportybet_markets") or doc.get("markets") or [],
        "markets": sporty.get("markets") or doc.get("markets") or [],
        "odds_1x2": _extract_1x2(sporty.get("markets") or doc.get("markets") or []),
        "raw_sporty": sporty,
        "is_live": bool(is_live),
        "is_finished": bool(is_finished),
        "match_state": state,
        "time_context": match_time_context({**sporty, "sofascore_event": doc.get("sofascore_event")}),
        "sporty_refreshed_at": datetime.now(timezone.utc).isoformat(),
    })
    doc = enrich_match_facts(doc)
    doc["lifecycle"] = _lifecycle_state(doc)
    conn.execute(f"update {table} set raw_enriched = ? where match_id = ?", (json.dumps(doc), match_id))


def _mark_missing_from_sporty(match_id: str) -> None:
    _init_db()
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        row = conn.execute("select raw_enriched from match_buffer where match_id = ?", (str(match_id),)).fetchone()
        if row and row[0]:
            try:
                doc = json.loads(row[0])
            except Exception:
                doc = {}
            doc["sporty_active"] = False
            doc["sportybet_data_status"] = "missing"
            doc["data_sources"] = _data_sources(
                doc.get("sofascore_event"),
                doc.get("sofascore_detail"),
                None,
                doc.get("sportradar_detail"),
            )
            doc["sporty_missing_at"] = now
            conn.execute("update match_buffer set raw_enriched = ? where match_id = ?", (json.dumps(doc), str(match_id)))
        conn.commit()


def _lifecycle_state(doc: dict[str, Any]) -> dict[str, Any]:
    state = classify_match_state(doc)
    is_live = bool(state.get("is_live"))
    is_finished = bool(state.get("is_finished") or state.get("state") in {"postponed", "cancelled"} or doc.get("is_finished"))
    prediction = doc.get("prediction") or {}
    graded = bool(prediction.get("result") or doc.get("graded_at"))
    archived = bool(doc.get("archived_at") or doc.get("finished_at"))
    stages = {
        "discovered": bool(doc.get("sportybet_id") or doc.get("id")),
        "matched": bool(doc.get("sofascore_id") or doc.get("sofascore_event")),
        "enriched": bool(doc.get("enriched_at") and (doc.get("sofascore_detail") or doc.get("sportybet_detail"))),
        "predicted": bool(prediction and not doc.get("prediction_error")),
        "live": is_live,
        "finished": is_finished,
        "graded": graded,
        "archived": archived,
    }
    order = ["archived", "graded", "finished", "live", "predicted", "enriched", "matched", "discovered"]
    current = next((stage for stage in order if stages.get(stage)), "unknown")
    missing = [stage for stage in ("matched", "enriched", "predicted", "graded", "archived") if not stages.get(stage)]
    return {"current": current, "state": state.get("state"), "match_state": state, "stages": stages, "missing": missing}


def _is_finished_period(period: str | None) -> bool:
    if not period:
        return False
    p = str(period or "").lower().strip()
    return p in ("ft", "finished", "ended", "aet", "ap", "full time", "after penalties", "after extra time")


def _is_live_period(period: str | None) -> bool:
    return bool(period) and not _is_not_started_period(period) and not _is_finished_period(period)


def _is_90_plus(played_seconds: Any) -> bool:
    """Returns True if the match has reached or passed the 90-minute mark."""
    try:
        return int(played_seconds or 0) >= 90 * 60
    except (TypeError, ValueError):
        return False


# Grace period after kick-off before we consider a non-live match as ghost/abandoned
GHOST_MATCH_GRACE_MINUTES = 120  # 2 hours past start_time

# How many days ahead to keep already-enriched future fixtures in the buffer.
# Unmatched/unenriched future matches are always kept so enrichment can try them.
# Once enriched, matches beyond this window are dropped and re-ingested fresh
# closer to their date (SofaScore data changes as kick-off approaches).
MAX_FUTURE_DAYS = 14


def _is_ghost_match(start_time: Any, period: str | None) -> bool:
    """
    Returns True if a match is still showing 'Not start' but its scheduled
    kick-off was more than GHOST_MATCH_GRACE_MINUTES ago.
    These are matches that never went live — postponed, cancelled, or data gaps.
    """
    if not _is_not_started_period(period):
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
        from app.utils.time_context import match_time_context

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
    from app.data_clients.sofascore_client import is_usable_event_for_mode

    events: list[dict] = []
    seen: set[str] = set()

    if item.get("is_live"):
        for event in live_events:
            if not is_usable_event_for_mode(event, live=True):
                continue
            eid = str(event.get("id") or "")
            if eid and eid not in seen:
                events.append(event)
                seen.add(eid)

    for d in _sofascore_date_candidates(item["sporty"], item.get("match_date")):
        for event in sofa_cache.get(d, []):
            if not is_usable_event_for_mode(event, live=False):
                continue
            eid = str(event.get("id") or "")
            if eid and eid not in seen:
                events.append(event)
                seen.add(eid)
    return events


def _reusable_sofascore_detail(
    existing: dict[str, Any],
    sofa: dict[str, Any] | None,
    is_live: bool,
) -> dict[str, Any] | None:
    """Avoid refetching static pre-match detail for an already matched event."""
    if is_live or not sofa:
        return None
    detail = existing.get("sofascore_detail")
    if not isinstance(detail, dict) or not detail:
        return None
    detail_id = detail.get("id") or detail.get("event_id")
    if detail_id is not None and str(detail_id) != str(sofa.get("id")):
        return None
    return detail


def _with_search_fallback_candidates(
    sporty: dict[str, Any],
    events: list[dict],
    live: bool = False,
) -> list[dict]:
    """Add targeted SofaScore search results when scheduled-events is incomplete.

    Runs for live matches too: the bulk `fetch_live_events()` list SofaScore
    returns does not reliably include every lower-tier league, so a live match
    with no confident candidate in that bulk list gets one more chance via a
    team-name search before we give up on it for this cycle.
    """
    if _is_junk_match_name(sporty.get("name") or ""):
        return events
    best_score = 0.0
    for event in events:
        try:
            best_score = max(best_score, _event_score_for_fallback(sporty, event))
        except Exception:
            continue
    if best_score >= 0.70:
        return events
    try:
        from app.data_clients.sofascore_client import is_usable_event_for_mode, search_events

        search_results = []
        seen_search_ids: set[str] = set()
        for query in _sofascore_search_queries(sporty):
            for event in search_events(query, limit=8):
                if not is_usable_event_for_mode(event, live=live):
                    continue
                eid = str(event.get("id") or "")
                if eid and eid not in seen_search_ids:
                    search_results.append(event)
                    seen_search_ids.add(eid)
            if any(_event_score_for_fallback(sporty, event) >= 0.70 for event in search_results):
                break
    except Exception:
        return events
    if not search_results:
        return events
    merged = list(events)
    seen = {str(event.get("id") or "") for event in merged}
    for event in search_results:
        eid = str(event.get("id") or "")
        if eid and eid not in seen:
            merged.append(event)
            seen.add(eid)
    return merged


def _sofascore_search_query(sporty: dict[str, Any]) -> str:
    queries = _sofascore_search_queries(sporty)
    return queries[0] if queries else ""


def _sofascore_search_queries(sporty: dict[str, Any]) -> list[str]:
    home = str(sporty.get("home_team") or _split_team_name(sporty.get("name") or "", 0) or "").strip()
    away = str(sporty.get("away_team") or _split_team_name(sporty.get("name") or "", 1) or "").strip()
    tournament = str(sporty.get("tournament") or "").strip()
    category = str(sporty.get("category") or sporty.get("country") or "").strip()
    name = str(sporty.get("name") or "").strip()
    home_simple = _simplify_search_team_name(home)
    away_simple = _simplify_search_team_name(away)
    candidates = [
        " ".join(part for part in (home_simple, away_simple) if part),
        " ".join(part for part in (home, away_simple) if part),
        " ".join(part for part in (home_simple, away) if part),
        " ".join(part for part in (home, away) if part),
        name.replace(" vs ", " "),
        " ".join(part for part in (home, away, category, tournament) if part),
    ]
    queries: list[str] = []
    seen: set[str] = set()
    for query in candidates:
        clean = " ".join(str(query or "").split())
        key = normalise(clean)
        if clean and key not in seen:
            queries.append(clean)
            seen.add(key)
    return queries


def _simplify_search_team_name(name: str) -> str:
    text = normalise(str(name or ""))
    words = re.findall(r"[a-z0-9]+", text)
    if not words:
        return ""
    noise = {
        "club", "football", "futbol", "soccer", "team",
        "fc", "cf", "cd", "sc", "ac", "afc", "if", "bk",
    }
    connectors = {"de", "do", "da", "del", "della", "di", "du", "la", "le", "los", "las", "of", "the"}
    words = [word for word in words if word not in noise]
    while words and words[0] in connectors:
        words.pop(0)
    while words and words[-1] in connectors:
        words.pop()
    return " ".join(words)


def _split_team_name(name: str, index: int) -> str:
    parts = str(name or "").split(" vs ", 1)
    return parts[index].strip() if len(parts) == 2 else ""


def _is_junk_match_name(name: str) -> bool:
    try:
        from app.enrichment.enrichment import _is_junk

        return _is_junk(name)
    except Exception:
        return False


def _event_score_for_fallback(sporty: dict[str, Any], event: dict[str, Any]) -> float:
    try:
        from app.enrichment.enrichment import _event_score

        return _event_score(sporty, event)
    except Exception:
        return 0.0


def _try_archive_finished(match_id: str) -> None:
    """Archive a finished match to MongoDB and local SQLite, then remove from buffer.
    If MongoDB is not configured, still saves locally and deletes from buffer."""
    try:
        from app.storage.mongo_store import archive_finished_match_from_buffer, is_configured
        if is_configured():
            try:
                archive_finished_match_from_buffer(match_id)
            except Exception as exc:
                print(f"[buffer] mongo archive failed for {match_id}, falling back locally: {exc}")
                _archive_finished_locally(match_id)
        else:
            # No MongoDB — save to local SQLite finished_matches table + delete from buffer
            _archive_finished_locally(match_id)
    except Exception as exc:
        print(f"[buffer] archive failed for {match_id}: {exc}")
        # Last resort: ensure is_finished=1 so get_buffered_matches filters it out
        try:
            with _conn() as conn:
                conn.execute("update match_buffer set is_finished = 1 where match_id = ?", (match_id,))
                conn.commit()
        except Exception:
            pass


def _archive_finished_locally(match_id: str) -> None:
    """Write finished match to local SQLite finished_matches table and delete from buffer."""
    import json as _json
    _init_db()
    with _conn() as conn:
        try:
            conn.execute("alter table finished_matches add column raw_doc text")
        except Exception:
            pass
        row = conn.execute(
            "select match_date, raw_enriched, raw_sporty, score_home, score_away from match_buffer where match_id = ?",
            (match_id,),
        ).fetchone()
        if row:
            raw = row[1] or row[2]
            doc = _json.loads(raw) if raw else {}
            doc = enrich_match_facts(doc)
            home = doc.get("home_team") or ""
            if isinstance(home, dict):
                home = home.get("name") or ""
            away = doc.get("away_team") or ""
            if isinstance(away, dict):
                away = away.get("name") or ""
            score = doc.get("score") or {}
            detail = doc.get("sofascore_detail") or {}
            home_team_obj = detail.get("home_team") or detail.get("homeTeam") or {}
            away_team_obj = detail.get("away_team") or detail.get("awayTeam") or {}
            tournament = doc.get("tournament") or ""
            if isinstance(tournament, dict):
                tournament = tournament.get("name") or ""
            archive_doc = {
                "_id": match_id,
                "match_id": match_id,
                "match_date": row[0],
                "name": doc.get("sportybet_name") or doc.get("name"),
                "home_team": home,
                "away_team": away,
                "home_team_id": home_team_obj.get("id"),
                "away_team_id": away_team_obj.get("id"),
                "tournament": tournament,
                "score": score,
                "half_time_score": doc.get("half_time_score"),
                "goal_events": doc.get("goal_events") or [],
                "goal_timing": doc.get("goal_timing") or {},
                "average_goal_interval_minutes": doc.get("average_goal_interval_minutes"),
                "live_statistics": doc.get("live_statistics") or {},
                "provider_live_capabilities": doc.get("provider_live_capabilities") or {},
                "period": doc.get("period") or "FT",
                "finished_at": doc.get("finished_at"),
                "sofascore_detail": detail,
                "sportybet_detail": doc.get("sportybet_detail") or {},
                "data_sources": doc.get("data_sources") or {},
                "raw_sporty": doc.get("raw_sporty") or {},
                "sportybet_markets": doc.get("sportybet_markets") or doc.get("markets") or [],
            }
            archive_json = _json.dumps(archive_doc)
            conn.execute(
                """
                insert or replace into finished_matches
                    (match_id, match_date, home_team, away_team, tournament, score_home, score_away, raw_json, raw_doc)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id, row[0], home, away,
                    tournament,
                    str(score.get("home") or row[3] or ""),
                    str(score.get("away") or row[4] or ""),
                    archive_json,
                    archive_json,
                ),
            )
        # Delete from buffer regardless
        conn.execute("delete from match_buffer where match_id = ?", (match_id,))
        conn.commit()





