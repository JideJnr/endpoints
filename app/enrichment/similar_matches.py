"""
Similar Matches Engine
----------------------
Finds historical finished matches that are contextually similar to a given
buffered target match, based on:

  - ELO proximity (team strength similarity)       weight 0.45
  - 1x2 odds proximity (market distribution)       weight 0.40
  - League / category bonus                        weight 0.15

When odds are unavailable the weights fall back to 0.85 / 0.00 / 0.15.

All database access is local SQLite only — no external HTTP calls.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from app.db import db_conn
from app.db import DB_PATH
from app.league_memory import _init_db, normalize_league

logger = logging.getLogger(__name__)

# ── Scoring constants ──────────────────────────────────────────────────────────
_ELO_NORM = 800.0      # two teams each at max 400 ELO deviation
_ODDS_NORM = 1.5       # three outcomes each at max 0.5 implied-prob deviation
_W_ELO = 0.45
_W_ODDS = 0.40
_W_LEAGUE = 0.15
_W_ELO_NO_ODDS = 0.85  # fallback when odds unavailable
_DEFAULT_ELO = 1500.0

# ── Candidate pool limits ──────────────────────────────────────────────────────
_POOL_LIMIT = 500
_SAME_LEAGUE_MIN = 50   # fall back to full history if fewer same-league matches


# ─────────────────────────────────────────────────────────────────────────────
# Pure scoring helpers (no I/O — easily unit and property tested)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_elo_proximity(
    target_home_elo: float,
    target_away_elo: float,
    cand_home_elo: float,
    cand_away_elo: float,
) -> float:
    """ELO proximity in [0, 1]. 1.0 = identical strengths, 0.0 = max deviation."""
    deviation = abs(target_home_elo - cand_home_elo) + abs(target_away_elo - cand_away_elo)
    return max(0.0, 1.0 - deviation / _ELO_NORM)


def _compute_odds_proximity(
    target_home_p: float,
    target_draw_p: float,
    target_away_p: float,
    cand_home_p: float,
    cand_draw_p: float,
    cand_away_p: float,
) -> float:
    """Odds proximity in [0, 1] using implied probabilities. 1.0 = identical market."""
    deviation = (
        abs(target_home_p - cand_home_p)
        + abs(target_draw_p - cand_draw_p)
        + abs(target_away_p - cand_away_p)
    )
    return max(0.0, 1.0 - deviation / _ODDS_NORM)


def _compute_league_bonus(
    target_league_key: str,
    target_category: str,
    cand_league_key: str,
    cand_category: str,
) -> float:
    """Returns 1.0 (same league), 0.5 (same category), or 0.0."""
    tl = (target_league_key or "").lower().strip()
    cl = (cand_league_key or "").lower().strip()
    tc = (target_category or "").lower().strip()
    cc = (cand_category or "").lower().strip()

    if tl and tl == cl:
        return 1.0
    if tc and tc == cc:
        return 0.5
    return 0.0


def _compute_similarity_score(
    elo_proximity: float,
    odds_proximity: float | None,
    league_bonus: float,
) -> float:
    """
    Weighted composite similarity in [0.0, 1.0].

    When odds_proximity is None (no odds available for either match), the
    odds weight is redistributed entirely to ELO so the score remains
    meaningful.
    """
    if odds_proximity is None:
        raw = _W_ELO_NO_ODDS * elo_proximity + _W_LEAGUE * league_bonus
    else:
        raw = _W_ELO * elo_proximity + _W_ODDS * odds_proximity + _W_LEAGUE * league_bonus
    return max(0.0, min(1.0, raw))


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_candidate_pool(
    conn: sqlite3.Connection,
    target_match_id: str,
    target_league_key: str,
    target_home_odds: float | None = None,
    target_away_odds: float | None = None,
) -> list[dict[str, Any]]:
    """
    Returns up to _POOL_LIMIT historical predictions as candidate matches.

    Source strategy (in priority order):
    1. prediction_history — uses sr:match:... IDs that JOIN with odds_snapshots
       and carries match_name, league_name, sofascore_id/sportybet_id, graded result.
       This is the primary source because it has real odds alignment.
    2. finished_matches — fallback when prediction_history is thin.
       Uses sr:match:... IDs but only has sparse doc structure.

    When target_home_odds is supplied, candidates are pre-filtered to those whose
    earliest odds snapshot falls within ±_ODDS_RANGE_WINDOW — ensuring structurally
    similar fixtures (same favourite/underdog ratio) are ranked first.
    """
    _ODDS_RANGE_WINDOW = 0.90  # ±0.90 decimal odds on home_odds

    def _odds_filter_join(alias: str) -> tuple[str, str]:
        """Return (join_sql, where_clause) for odds pre-filter on table alias."""
        if not (target_home_odds and target_home_odds > 1.0):
            return "", ""
        lo = target_home_odds - _ODDS_RANGE_WINDOW
        hi = target_home_odds + _ODDS_RANGE_WINDOW
        join_sql = f"""
            join (
                select match_id, min(snapshot_time) as earliest
                from odds_snapshots group by match_id
            ) oe_{alias} on oe_{alias}.match_id = {alias}.match_id
            join odds_snapshots os_{alias}
                on os_{alias}.match_id = {alias}.match_id
               and os_{alias}.snapshot_time = oe_{alias}.earliest
               and os_{alias}.home_odds between {lo} and {hi}
               and os_{alias}.home_odds is not null
        """
        return join_sql, ""

    # ── 1. prediction_history ─────────────────────────────────────────────────
    # Dedupe by sofascore_id (or match_id) so we get one row per fixture even
    # if multiple picks were recorded.
    odds_join, _ = _odds_filter_join("ph")
    ph_sql = f"""
        select
            ph.match_id,
            ph.match_name,
            ph.league_name,
            ph.sofascore_id,
            ph.final_home  as final_home_goals,
            ph.final_away  as final_away_goals,
            ph.created_at  as last_seen_at,
            ph.country_name
        from prediction_history ph
        {odds_join}
        where ph.match_id != ?
          and ph.graded_at is not null
          and ph.final_home is not null
          and ph.final_away is not null
          and ph.pick_type != 'no_bet'
        group by ph.match_id
        order by ph.created_at desc
        limit ?
    """

    league_rows: list = []
    all_rows: list = []

    if target_league_key:
        try:
            league_rows = conn.execute(
                ph_sql.replace(
                    "where ph.match_id != ?",
                    "where ph.match_id != ? and lower(coalesce(ph.league_name,'')) like ?",
                ),
                (target_match_id, f"%{target_league_key}%", _POOL_LIMIT),
            ).fetchall()
        except Exception:
            league_rows = []

    if len(league_rows) < _SAME_LEAGUE_MIN:
        try:
            all_rows = conn.execute(ph_sql, (target_match_id, _POOL_LIMIT)).fetchall()
        except Exception:
            all_rows = []

    rows = league_rows if len(league_rows) >= _SAME_LEAGUE_MIN else all_rows

    # ── 2. Fallback: finished_matches (sparse, sr:match:... IDs) ─────────────
    if len(rows) < 10:
        fm_odds_join, _ = _odds_filter_join("fm")
        fm_sql = f"""
            select
                fm.match_id,
                fm.home_team || ' vs ' || fm.away_team as match_name,
                fm.tournament as league_name,
                null as sofascore_id,
                cast(json_extract(fm.raw_json, '$.score_home') as integer)  as final_home_goals,
                cast(json_extract(fm.raw_json, '$.score_away') as integer)  as final_away_goals,
                fm.finished_at  as last_seen_at,
                null as country_name
            from finished_matches fm
            {fm_odds_join}
            where fm.match_id != ?
              and fm.match_id not in (select match_id from prediction_history)
              and json_extract(fm.raw_json, '$.score_home') is not null
            order by fm.finished_at desc
            limit ?
        """
        try:
            fm_rows = conn.execute(fm_sql, (target_match_id, _POOL_LIMIT - len(rows))).fetchall()
            rows = list(rows) + list(fm_rows)
        except Exception:
            pass

    # ── 3. Fallback without odds filter ──────────────────────────────────────
    if not rows:
        try:
            rows = conn.execute(
                """
                select
                    ph.match_id, ph.match_name, ph.league_name, ph.sofascore_id,
                    ph.final_home as final_home_goals, ph.final_away as final_away_goals,
                    ph.created_at as last_seen_at, ph.country_name
                from prediction_history ph
                where ph.match_id != ?
                  and ph.graded_at is not null
                  and ph.final_home is not null
                  and ph.final_away is not null
                  and ph.pick_type != 'no_bet'
                group by ph.match_id
                order by ph.created_at desc
                limit ?
                """,
                (target_match_id, _POOL_LIMIT),
            ).fetchall()
        except Exception:
            rows = []

    return [dict(row) for row in rows]


def _parse_match_name(match_name: str) -> tuple[str, str]:
    """Split 'Home Team vs Away Team' into (home, away) names."""
    if " vs " in match_name:
        parts = match_name.split(" vs ", 1)
        return parts[0].strip(), parts[1].strip()
    return match_name.strip(), ""


def _batch_load_elos(
    conn: sqlite3.Connection,
    team_names: list[str],
) -> dict[str, float]:
    """Load ELO ratings for a list of team names in one query (case-insensitive)."""
    if not team_names:
        return {}
    placeholders = ",".join("?" * len(team_names))
    rows = conn.execute(
        f"select lower(team_name), rating from elo_ratings where lower(team_name) in ({placeholders})",
        [n.lower() for n in team_names],
    ).fetchall()
    return {row[0]: float(row[1]) for row in rows}


def _batch_load_odds(
    conn: sqlite3.Connection,
    match_ids: list[str],
) -> dict[str, dict[str, float]]:
    """Load the earliest odds snapshot per match in one query."""
    if not match_ids:
        return {}
    placeholders = ",".join("?" * len(match_ids))
    rows = conn.execute(
        f"""
        select match_id, home_odds, draw_odds, away_odds
        from odds_snapshots
        where match_id in ({placeholders})
          and home_odds is not null
          and draw_odds is not null
          and away_odds is not null
        group by match_id
        having snapshot_time = min(snapshot_time)
        """,
        match_ids,
    ).fetchall()
    return {
        row[0]: {"home": float(row[1]), "draw": float(row[2]), "away": float(row[3])}
        for row in rows
    }


def _batch_load_predictions(
    conn: sqlite3.Connection,
    match_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Load the highest-confidence prediction per match in one query."""
    if not match_ids:
        return {}
    placeholders = ",".join("?" * len(match_ids))
    rows = conn.execute(
        f"""
        select match_id, pick_type, selection, confidence, result
        from prediction_history
        where match_id in ({placeholders})
          and pick_type != 'no_bet'
        group by match_id
        having confidence = max(confidence)
        order by match_id
        """,
        match_ids,
    ).fetchall()
    return {
        row[0]: {
            "pick_type": row[1],
            "selection": row[2],
            "confidence": int(row[3]) if row[3] is not None else None,
            "result": row[4],
        }
        for row in rows
    }


# ─────────────────────────────────────────────────────────────────────────────
# Target-match extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_target_elos(
    doc: dict[str, Any],
    elo_lookup: dict[str, float],
) -> tuple[float, float]:
    """
    Extract home + away ELO for the target match.
    Prefers SofaScore team IDs, falls back to name lookup, then default 1500.
    """
    detail = doc.get("sofascore_detail") or {}
    home_team = detail.get("home_team") or doc.get("home_team") or {}
    away_team = detail.get("away_team") or doc.get("away_team") or {}

    # Try ID-based lookup first (elo_ratings.team_id)
    home_id = str(home_team.get("id") or "") if isinstance(home_team, dict) else ""
    away_id = str(away_team.get("id") or "") if isinstance(away_team, dict) else ""

    home_name = (
        (home_team.get("name") or "") if isinstance(home_team, dict)
        else str(home_team or "")
    ).lower().strip()
    away_name = (
        (away_team.get("name") or "") if isinstance(away_team, dict)
        else str(away_team or "")
    ).lower().strip()

    home_elo = elo_lookup.get(home_name, _DEFAULT_ELO)
    away_elo = elo_lookup.get(away_name, _DEFAULT_ELO)
    return home_elo, away_elo


def _extract_target_odds_implied(
    doc: dict[str, Any],
) -> tuple[float, float, float] | None:
    """
    Extract implied probabilities from the target match's 1x2 odds.
    Returns (home_p, draw_p, away_p) or None if odds are missing.

    Resolution order (most specific → most fallback):
    1. doc["odds_1x2"] — explicit 1x2 dict
    2. doc["sportybet_detail"]["odds_1x2"] — detail-level 1x2 dict
    3. doc["sofascore_detail"]["odds_featured"] — SofaScore featured fractional odds
    4. doc["sportybet_markets"] / doc["markets"] — raw market list from SportyBet
       (same structure used by prediction_agent._odds_edge)
    """
    # ── 1. Explicit odds_1x2 dict ─────────────────────────────────────────────
    odds = doc.get("odds_1x2") or {}
    home_o = odds.get("home")
    draw_o = odds.get("draw")
    away_o = odds.get("away")

    # ── 2. sportybet_detail.odds_1x2 ─────────────────────────────────────────
    if not home_o:
        detail = doc.get("sportybet_detail") or {}
        odds1x2 = detail.get("odds_1x2") or {}
        home_o = odds1x2.get("home")
        draw_o = odds1x2.get("draw")
        away_o = odds1x2.get("away")

    # ── 3. SofaScore featured fractional odds ─────────────────────────────────
    if not home_o:
        sofa_detail = doc.get("sofascore_detail") or {}
        featured = sofa_detail.get("odds_featured") or {}
        default_mkt = featured.get("default") or {}
        choices = default_mkt.get("choices") or []
        choice_map: dict[str, Any] = {c.get("name"): c for c in choices if c.get("name")}
        def _frac_to_dec(frac: Any) -> float | None:
            """Convert 'numerator/denominator' fractional odds to decimal."""
            if frac is None:
                return None
            try:
                if "/" in str(frac):
                    n, d = str(frac).split("/")
                    return round(float(n) / float(d) + 1, 4)
                return float(frac) + 1  # already decimal-1 form
            except Exception:
                return None
        h_frac = (choice_map.get("1") or choice_map.get("Home") or {}).get("fractional_value")
        d_frac = (choice_map.get("X") or choice_map.get("Draw") or {}).get("fractional_value")
        a_frac = (choice_map.get("2") or choice_map.get("Away") or {}).get("fractional_value")
        home_o = _frac_to_dec(h_frac)
        draw_o = _frac_to_dec(d_frac)
        away_o = _frac_to_dec(a_frac)

    # ── 4. Raw SportyBet markets list (sportybet_markets / markets) ───────────
    # This is the primary data source on live matches — same structure as
    # prediction_agent._odds_edge() uses. Market id "1" or name containing
    # "1x2" / "match result" is the 1X2 market.
    if not home_o:
        markets = doc.get("sportybet_markets") or doc.get("markets") or []
        for mkt in markets:
            mkt_name = (mkt.get("name") or "").lower()
            if mkt.get("id") == "1" or "1x2" in mkt_name or mkt_name in {"match result", "3 way", "full time result"}:
                sel_map: dict[str, Any] = {
                    (s.get("name") or "").lower(): s
                    for s in mkt.get("selections", [])
                }
                h_sel = sel_map.get("home") or sel_map.get("1") or sel_map.get("home win")
                d_sel = sel_map.get("draw") or sel_map.get("x")
                a_sel = sel_map.get("away") or sel_map.get("2") or sel_map.get("away win")
                if h_sel and d_sel and a_sel:
                    home_o = h_sel.get("odds")
                    draw_o = d_sel.get("odds")
                    away_o = a_sel.get("odds")
                    break

    if not home_o or not draw_o or not away_o:
        return None

    try:
        h, d, a = float(home_o), float(draw_o), float(away_o)
        if h <= 1.0 or d <= 1.0 or a <= 1.0:
            return None
        return 1.0 / h, 1.0 / d, 1.0 / a
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def find_similar_matches(
    doc: dict[str, Any],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Return up to `limit` historical matches most similar to `doc`, sorted
    descending by similarity_score.

    Parameters
    ----------
    doc   : enriched buffer document for the target match
    limit : max results to return (1–25)

    Returns
    -------
    list of SimilarMatchItem dicts
    """
    _init_db()
    limit = max(1, min(25, limit))

    # ── Target match metadata ─────────────────────────────────────────────────
    target_match_id = str(
        doc.get("sportybet_id") or doc.get("id") or doc.get("match_id") or ""
    )
    target_tournament = str(doc.get("tournament") or "")
    target_category = str(doc.get("category") or "")
    target_league_key = normalize_league(target_tournament) if target_tournament else ""
    target_category_key = normalize_league(target_category) if target_category else ""
    target_name = str(doc.get("sportybet_name") or doc.get("name") or "")

    # ── Implied odds for target ───────────────────────────────────────────────
    target_implied = _extract_target_odds_implied(doc)

    with db_conn(timeout=30) as conn:
        conn.row_factory = sqlite3.Row

        # ── Candidate pool ────────────────────────────────────────────────────
        # Pass target decimal home odds so the SQL can pre-filter by odds range,
        # keeping only fixtures with a structurally similar favourite/underdog ratio.
        target_home_odds_dec: float | None = None
        if target_implied:
            # Convert implied probability back to decimal for the range filter
            try:
                target_home_odds_dec = round(1.0 / target_implied[0], 3) if target_implied[0] > 0 else None
            except (ZeroDivisionError, TypeError):
                pass

        candidates = _build_candidate_pool(
            conn,
            target_match_id,
            target_league_key,
            target_home_odds=target_home_odds_dec,
        )
        if not candidates:
            return []

        # ── Resolve target team names ─────────────────────────────────────────
        home_raw = (doc.get("home_team") or "")
        away_raw = (doc.get("away_team") or "")
        target_home_name = (
            home_raw.get("name") if isinstance(home_raw, dict) else str(home_raw or "")
        ).lower().strip()
        target_away_name = (
            away_raw.get("name") if isinstance(away_raw, dict) else str(away_raw or "")
        ).lower().strip()

        # Also try from sofascore_detail if top-level names are empty
        if not target_home_name or not target_away_name:
            detail = doc.get("sofascore_detail") or {}
            if not target_home_name:
                target_home_name = str(
                    (detail.get("home_team") or {}).get("name") or ""
                ).lower().strip()
            if not target_away_name:
                target_away_name = str(
                    (detail.get("away_team") or {}).get("name") or ""
                ).lower().strip()

        # Fallback: parse from match name
        if not target_home_name or not target_away_name:
            match_name = str(doc.get("sportybet_name") or doc.get("name") or "")
            parsed_home, parsed_away = _parse_match_name(match_name)
            if not target_home_name:
                target_home_name = parsed_home.lower()
            if not target_away_name:
                target_away_name = parsed_away.lower()

        # ── Collect all team names needed for ELO lookup ──────────────────────
        all_team_names: set[str] = set()
        all_team_names.update(n for n in (target_home_name, target_away_name) if n)
        for c in candidates:
            # candidates now have match_name "Home vs Away" — parse it
            mn = str(c.get("match_name") or "")
            ch, ca = _parse_match_name(mn)
            if ch:
                all_team_names.add(ch.lower().strip())
            if ca:
                all_team_names.add(ca.lower().strip())

        elo_lookup = _batch_load_elos(conn, list(all_team_names))

        # ── Target ELOs ───────────────────────────────────────────────────────
        target_home_elo = elo_lookup.get(target_home_name, _DEFAULT_ELO)
        target_away_elo = elo_lookup.get(target_away_name, _DEFAULT_ELO)

        # ── Batch load odds and predictions for candidates ────────────────────
        cand_ids = [c["match_id"] for c in candidates]
        odds_map = _batch_load_odds(conn, cand_ids)
        pred_map = _batch_load_predictions(conn, cand_ids)

    # ── Score every candidate ─────────────────────────────────────────────────
    scored: list[dict[str, Any]] = []
    for c in candidates:
        # Parse home/away from match_name (candidates from prediction_history use this)
        mn = str(c.get("match_name") or "")
        cand_home_str, cand_away_str = _parse_match_name(mn)
        cand_home_name = cand_home_str.lower().strip()
        cand_away_name = cand_away_str.lower().strip()
        cand_home_elo = elo_lookup.get(cand_home_name, _DEFAULT_ELO)
        cand_away_elo = elo_lookup.get(cand_away_name, _DEFAULT_ELO)

        elo_prox = _compute_elo_proximity(
            target_home_elo, target_away_elo,
            cand_home_elo, cand_away_elo,
        )

        cand_odds_raw = odds_map.get(c["match_id"])
        odds_prox: float | None = None
        cand_odds_out: dict[str, float] | None = None
        if target_implied and cand_odds_raw:
            try:
                ch = cand_odds_raw["home"]
                cd = cand_odds_raw["draw"]
                ca = cand_odds_raw["away"]
                if ch > 1.0 and cd > 1.0 and ca > 1.0:
                    cp_h, cp_d, cp_a = 1.0 / ch, 1.0 / cd, 1.0 / ca
                    odds_prox = _compute_odds_proximity(
                        target_implied[0], target_implied[1], target_implied[2],
                        cp_h, cp_d, cp_a,
                    )
                    cand_odds_out = cand_odds_raw
            except (KeyError, ZeroDivisionError, TypeError):
                pass

        # League bonus: use league_name from prediction_history
        cand_league_raw = str(c.get("league_name") or "")
        cand_league_key = normalize_league(cand_league_raw) if cand_league_raw else ""
        cand_category = str(c.get("country_name") or "")
        league_bonus = _compute_league_bonus(
            target_league_key, target_category_key,
            cand_league_key, cand_category,
        )

        sim_score = _compute_similarity_score(elo_prox, odds_prox, league_bonus)

        home_goals = c.get("final_home_goals")
        away_goals = c.get("final_away_goals")
        final_score = (
            f"{home_goals}-{away_goals}"
            if home_goals is not None and away_goals is not None
            else "?-?"
        )

        scored.append({
            "match_id": c["match_id"],
            "match_name": mn or f"{cand_home_str} vs {cand_away_str}",
            "home_team": cand_home_str,
            "away_team": cand_away_str,
            "league_name": cand_league_raw,
            "final_score": final_score,
            "match_date": c.get("last_seen_at", "")[:10] if c.get("last_seen_at") else None,
            "similarity_score": round(sim_score, 3),
            "similarity_breakdown": {
                "elo_proximity": round(elo_prox, 3),
                "odds_proximity": round(odds_prox, 3) if odds_prox is not None else None,
                "league_bonus": round(league_bonus, 3),
            },
            "odds": cand_odds_out,
            "prediction": pred_map.get(c["match_id"]),
        })

    # ── Sort descending, apply minimum quality threshold, and truncate ─────────
    # A score below 0.25 means neither ELO nor odds are meaningfully similar —
    # these are random-looking results and should be excluded.
    _MIN_SIMILARITY = 0.25
    scored.sort(key=lambda x: x["similarity_score"], reverse=True)
    filtered = [s for s in scored if s["similarity_score"] >= _MIN_SIMILARITY]
    return filtered[:limit] if filtered else scored[:limit]
