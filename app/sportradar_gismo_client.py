"""
Sportradar GISMO Client
-----------------------
Fetches pre-match intelligence from SportyBet's embedded Sportradar GISMO widget API.
Uses the same token SportyBet's frontend uses — fetched fresh from their page on startup.

Provides per-match:
  - Form table (W/D/L, position, GF/GA per team for the season)
  - Standings (full league table)
  - Season O/U stats
  - Table slice (standings around this match)

Token is cached in SQLite and refreshed when expired.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

BASE = "https://widgets.fn.sportradar.com/common/en/Etc:UTC/gismo"
HEADERS = {
    "Accept": "*/*",
    "Origin": "https://www.sportybet.com",
    "Referer": "https://www.sportybet.com/",
    "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36",
}

# In-memory season cache — avoids re-fetching form/standings for same season
_season_cache: Dict[str, Tuple[float, dict]] = {}
_SEASON_CACHE_TTL = 300  # 5 minutes


# ── Token management ──────────────────────────────────────────────────────────

def _get_token() -> Optional[str]:
    """Return cached token if still valid, else fetch a fresh one."""
    try:
        from app.league_memory import DB_PATH, _init_db
        _init_db()
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute("""
                create table if not exists gismo_token (
                    id integer primary key check (id = 1),
                    token text not null,
                    expires_at integer not null,
                    fetched_at text not null default current_timestamp
                )
            """)
            row = conn.execute("select token, expires_at from gismo_token where id = 1").fetchone()
            if row and int(row[1]) > int(time.time()) + 300:
                return row[0]
    except Exception:
        pass
    return _fetch_fresh_token()


def _fetch_fresh_token() -> Optional[str]:
    """Scrape a fresh GISMO token from SportyBet's match page."""
    try:
        req = urllib.request.Request(
            "https://www.sportybet.com/ng/sport/football",
            headers={
                "User-Agent": HEADERS["User-Agent"],
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://www.sportybet.com/",
            }
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")

        # Token appears in JS as: ?T=exp=...~acl=...~hmac=...
        tokens = re.findall(r'exp=(\d+)~acl=(/\*|[^~]+)~data=([^~]+)~hmac=([a-f0-9]+)', html)
        if not tokens:
            # Try fetching from a JS bundle
            bundles = re.findall(r'src=["\']([^"\']+index\.[a-f0-9]+\.js)["\']', html)
            for b in bundles[:3]:
                url = b if b.startswith("http") else f"https://s.sporty.net{b}" if not b.startswith("//") else f"https:{b}"
                try:
                    req2 = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
                    with urllib.request.urlopen(req2, timeout=10) as r2:
                        js = r2.read().decode("utf-8", errors="ignore")
                    tokens = re.findall(r'exp=(\d+)~acl=([^~]+)~data=([^~]+)~hmac=([a-f0-9]+)', js)
                    if tokens:
                        break
                except Exception:
                    continue

        if not tokens:
            return None

        exp, acl, data, hmac = tokens[0]
        token = f"exp={exp}~acl={acl}~data={data}~hmac={hmac}"

        # Persist to SQLite
        try:
            from app.league_memory import DB_PATH, _init_db
            _init_db()
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute("""
                    create table if not exists gismo_token (
                        id integer primary key check (id = 1),
                        token text not null,
                        expires_at integer not null,
                        fetched_at text not null default current_timestamp
                    )
                """)
                conn.execute("""
                    insert into gismo_token (id, token, expires_at) values (1, ?, ?)
                    on conflict(id) do update set token=excluded.token, expires_at=excluded.expires_at,
                    fetched_at=current_timestamp
                """, (token, int(exp)))
                conn.commit()
        except Exception:
            pass

        return token
    except Exception:
        return None


def store_token(token: str) -> None:
    """Manually store a known-good token (e.g. from browser capture)."""
    try:
        exp_match = re.search(r'exp=(\d+)', token)
        exp = int(exp_match.group(1)) if exp_match else int(time.time()) + 86400
        from app.league_memory import DB_PATH, _init_db
        _init_db()
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute("""
                create table if not exists gismo_token (
                    id integer primary key check (id = 1),
                    token text not null,
                    expires_at integer not null,
                    fetched_at text not null default current_timestamp
                )
            """)
            conn.execute("""
                insert into gismo_token (id, token, expires_at) values (1, ?, ?)
                on conflict(id) do update set token=excluded.token, expires_at=excluded.expires_at,
                fetched_at=current_timestamp
            """, (token, exp))
            conn.commit()
    except Exception:
        pass


# ── Core fetch ────────────────────────────────────────────────────────────────

def _fetch(path: str, token: str) -> Optional[dict]:
    url = f"{BASE}/{path}?T={token}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status != 200:
                return None
            return json.loads(r.read())
    except Exception:
        return None


def _doc_data(raw: Optional[dict]) -> Optional[Any]:
    if not raw:
        return None
    doc = raw.get("doc") or []
    if not doc:
        return None
    return doc[0].get("data")


# ── Match meta (IDs) ─────────────────────────────────────────────────────────

def fetch_match_meta(br_match_id: str, token: str) -> Optional[dict]:
    """Get season_id, home_id, away_id from match_timelinedelta."""
    raw = _fetch(f"match_timelinedelta/{br_match_id}", token)
    data = _doc_data(raw)
    if not data:
        return None
    match = data.get("match") or {}
    teams = match.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    return {
        "season_id":  match.get("_seasonid"),
        "home_id":    home.get("_id"),
        "away_id":    away.get("_id"),
        "home_name":  home.get("name"),
        "away_name":  away.get("name"),
        "round":      match.get("round"),
        "tournament": match.get("_tid"),
    }


# ── Season-level data (cached) ────────────────────────────────────────────────

def fetch_season_data(season_id: int, token: str) -> dict:
    """Fetch form table + standings for a season. Cached for 5 min."""
    key = str(season_id)
    cached = _season_cache.get(key)
    if cached and (time.monotonic() - cached[0]) < _SEASON_CACHE_TTL:
        return cached[1]

    def safe_fetch(path):
        return _doc_data(_fetch(path, token))

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_form     = pool.submit(safe_fetch, f"stats_formtable/{season_id}")
        f_standings = pool.submit(safe_fetch, f"season_dynamictable/{season_id}")
        f_ou       = pool.submit(safe_fetch, f"stats_season_overunder/{season_id}")

    result = {
        "form_table": f_form.result(),
        "standings":  f_standings.result(),
        "season_ou":  f_ou.result(),
    }
    _season_cache[key] = (time.monotonic(), result)
    return result


# ── Per-team extraction ───────────────────────────────────────────────────────

def extract_team_form(form_table: Optional[dict], team_id: int) -> dict:
    """Extract W/D/L, position, GF/GA, form string for a team from form_table."""
    if not form_table:
        return {}
    teams = form_table.get("teams") or []
    if isinstance(teams, dict):
        teams = list(teams.values())

    for t in teams:
        tid = (t.get("team") or {}).get("_id")
        if tid != team_id:
            continue
        return {
            "position":     (t.get("position") or {}).get("total"),
            "position_home": (t.get("position") or {}).get("home"),
            "position_away": (t.get("position") or {}).get("away"),
            "played":       (t.get("played") or {}).get("total", 0),
            "wins":         (t.get("win") or {}).get("total", 0),
            "draws":        (t.get("draw") or {}).get("total", 0),
            "losses":       (t.get("loss") or {}).get("total", 0),
            "goals_for":    (t.get("goalsfor") or {}).get("total", 0),
            "goals_against": (t.get("goalsagainst") or {}).get("total", 0),
            "goal_diff":    (t.get("goalsdiff") or {}).get("total", 0),
            "points":       (t.get("points") or {}).get("total", 0),
            "form":         t.get("form") or "",
            "wins_home":    (t.get("win") or {}).get("totalhome", 0),
            "wins_away":    (t.get("win") or {}).get("totalaway", 0),
            "goals_for_home":  (t.get("goalsfor") or {}).get("totalhome", 0),
            "goals_for_away":  (t.get("goalsfor") or {}).get("totalaway", 0),
            "goals_against_home": (t.get("goalsagainst") or {}).get("totalhome", 0),
            "goals_against_away": (t.get("goalsagainst") or {}).get("totalaway", 0),
        }
    return {}


def extract_standings_row(standings: Optional[dict], team_id: int) -> dict:
    """Extract standings row for a team."""
    if not standings:
        return {}
    season = standings.get("season") or {}
    tables = season.get("tables") or []
    for table in tables:
        for row in (table.get("tablerows") or []):
            t = row.get("team") or {}
            if t.get("_id") == team_id:
                return {
                    "pos":           row.get("pos"),
                    "points_total":  row.get("pointsTotal"),
                    "played_total":  row.get("total"),
                    "wins_total":    row.get("total") and row.get("pointsTotal"),  # approximate
                    "goals_for":     row.get("goalsForTotal"),
                    "goals_against": row.get("goalsAgainstTotal"),
                    "goal_diff":     row.get("goalDiffTotal"),
                    "draw_total":    row.get("drawTotal"),
                    "loss_total":    row.get("lossTotal"),
                }
    return {}


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_prematch_intelligence(br_match_id: str) -> dict:
    """
    Full pre-match intelligence for one match using GISMO.
    Returns form, standings, season O/U for both teams.
    Returns empty dict on any failure — never raises.
    """
    token = _get_token()
    if not token:
        return {"available": False, "error": "no_token"}

    meta = fetch_match_meta(br_match_id, token)
    if not meta or not meta.get("season_id"):
        return {"available": False, "error": "no_meta"}

    season_id = meta["season_id"]
    home_id   = meta["home_id"]
    away_id   = meta["away_id"]

    season_data = fetch_season_data(season_id, token)

    home_form     = extract_team_form(season_data.get("form_table"), home_id)
    away_form     = extract_team_form(season_data.get("form_table"), away_id)
    home_standing = extract_standings_row(season_data.get("standings"), home_id)
    away_standing = extract_standings_row(season_data.get("standings"), away_id)

    return {
        "available":      True,
        "br_match_id":    br_match_id,
        "season_id":      season_id,
        "home_id":        home_id,
        "away_id":        away_id,
        "home_name":      meta.get("home_name"),
        "away_name":      meta.get("away_name"),
        "round":          meta.get("round"),
        "home_form":      home_form,
        "away_form":      away_form,
        "home_standing":  home_standing,
        "away_standing":  away_standing,
        "season_ou":      season_data.get("season_ou"),
        "fetched_at":     datetime.now(timezone.utc).isoformat(),
    }
