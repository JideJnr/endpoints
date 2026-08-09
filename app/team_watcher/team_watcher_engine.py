"""
Team Watcher Prediction Engine
================================
Autonomous prediction subsystem that sits alongside the existing team_watcher.py
passive memory module.  Generates independent predictions from team-profile data,
contributes a graded TW_Signal to every main prediction, produces structured
weekly analysis reports, tracks its own accuracy, and improves its weights over
time using the same formula already in self_learner.py.

This module is designed to be additive — the existing team_watcher.py passive
memory remains untouched.  Any exception inside the engine must not abort the
main pipeline.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.storage.db import _ensure_column, _init_db, db_conn
from app.competition.league_strength import league_strength_score
from app.competition.competition_registry import get_team_competition_stats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_tw_tables(conn: sqlite3.Connection) -> None:
    """
    Safe idempotent schema creation.

    Creates the two new engine-specific tables (team_watcher_predictions and
    team_watcher_weights) and adds the new columns to ai_team_watchers and
    ai_team_watcher_matches.  All DDL uses IF NOT EXISTS / _ensure_column so
    the function is safe to call on every start-up or on every DB connection.

    Mirrors the busy_timeout pragma setting used by init_team_watcher_tables
    in team_watcher.py (Requirement 8.5).
    """
    conn.execute("pragma busy_timeout = 30000")

    # ------------------------------------------------------------------
    # Prediction tracking table (Requirement 8.1 / 4.3)
    # ------------------------------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_watcher_predictions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            team_key    TEXT NOT NULL,
            match_id    TEXT NOT NULL,
            pick_type   TEXT NOT NULL,
            selection   TEXT NOT NULL,
            confidence  INTEGER NOT NULL,
            sub_model   TEXT NOT NULL,
            result      TEXT,
            created_at  TEXT NOT NULL DEFAULT current_timestamp,
            graded_at   TEXT
        )
        """
    )

    # ------------------------------------------------------------------
    # Per-team, per-model weight table (Requirement 8.2 / 5.4)
    # ------------------------------------------------------------------
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_watcher_weights (
            team_key     TEXT NOT NULL,
            sub_model    TEXT NOT NULL,
            samples      INTEGER NOT NULL DEFAULT 0,
            wins         INTEGER NOT NULL DEFAULT 0,
            losses       INTEGER NOT NULL DEFAULT 0,
            win_rate     REAL,
            weight_adj   REAL NOT NULL DEFAULT 0.0,
            last_updated TEXT NOT NULL DEFAULT current_timestamp,
            PRIMARY KEY (team_key, sub_model)
        )
        """
    )

    # ------------------------------------------------------------------
    # Indexes on team_watcher_predictions
    # ------------------------------------------------------------------
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tw_preds_team "
        "ON team_watcher_predictions(team_key, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tw_preds_match "
        "ON team_watcher_predictions(match_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tw_preds_graded "
        "ON team_watcher_predictions(graded_at)"
    )

    # ------------------------------------------------------------------
    # New columns on ai_team_watchers (Requirement 8.3)
    # ------------------------------------------------------------------
    _ensure_column(
        conn,
        "ai_team_watchers",
        "weekly_analysis_json",
        "TEXT DEFAULT '{}'",
    )
    _ensure_column(
        conn,
        "ai_team_watchers",
        "weekly_analysis_at",
        "TEXT",
    )

    # ------------------------------------------------------------------
    # New column on ai_team_watcher_matches (Requirement 8.4)
    # ------------------------------------------------------------------
    _ensure_column(
        conn,
        "ai_team_watcher_matches",
        "tw_signal_json",
        "TEXT DEFAULT '{}'",
    )


# ---------------------------------------------------------------------------
# Internal helpers — profile loading
# ---------------------------------------------------------------------------

def _get_profile(conn: sqlite3.Connection, team_key: str) -> dict[str, Any] | None:
    """Load the profile_json from ai_team_watchers for a team key.

    Returns the parsed profile dict, or None if no watcher row exists.
    """
    try:
        row = conn.execute(
            "SELECT profile_json FROM ai_team_watchers WHERE team_key = ?",
            (team_key,),
        ).fetchone()
        if row is None:
            return None
        profile_raw = row[0] if isinstance(row, (list, tuple)) else row["profile_json"]
        if not profile_raw:
            return None
        parsed = json.loads(profile_raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.debug("_get_profile error team_key=%s: %s", team_key, exc)
        return None


# ---------------------------------------------------------------------------
# Internal helpers — competition stats loader
# ---------------------------------------------------------------------------

def _get_competition_stats(
    team_key: str,
    competition_key: str,
) -> dict[str, Any] | None:
    """Load team_competitions row for a team in a specific competition.

    Returns the row dict when matches_played >= 5, else None so the caller
    falls back to the normalised overall profile.
    """
    if not team_key or not competition_key:
        return None
    try:
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            row = get_team_competition_stats(conn, team_key, competition_key)
        if row and int(row.get("matches_played") or 0) >= 5:
            return row
    except Exception as exc:
        logger.debug("_get_competition_stats error team_key=%s comp=%s: %s", team_key, competition_key, exc)
    return None


# ---------------------------------------------------------------------------
# Internal helpers — deterministic rules model
# ---------------------------------------------------------------------------

def _rules_model(
    home_profile: dict[str, Any] | None,
    away_profile: dict[str, Any] | None,
    match_doc: dict[str, Any],
) -> dict[str, Any]:
    """Learned matchup-strength rules model.

    Architecture (Phases 1-3):
      1. League-strength normalization — adjusts historical rates by the gap
         between the team's average opponent strength and the match league.
      2. Competition-specific stats — when >= 5 matches exist in this exact
         competition, those stats replace the normalised overall profile.
      3. Venue-aware stats — home/away split applied for all markets.
      4. Recency-weighted form — last 5 results weighted more than older ones.
      5. Sample-size credibility — low sample shrinks scores toward 0.5.
      6. Matchup differential — home_strength vs away_strength drives market
         selection; draw detected from balance.

    Returns a dict with pick_type, selection, confidence, strength_context,
    competition_stats, sample_size, and reasoning.
    """
    hp = home_profile or {}
    ap = away_profile or {}

    home_sample = int(hp.get("sample_size") or 0)
    away_sample = int(ap.get("sample_size") or 0)

    if home_sample < 3 and away_sample < 3:
        return {"pick_type": "no_bet", "reason": "insufficient_sample",
                "sample_size": {"home": home_sample, "away": away_sample}}

    # ── League strength context ──────────────────────────────────────────────
    raw_sporty = match_doc.get("raw_sporty") if isinstance(match_doc.get("raw_sporty"), dict) else match_doc
    tournament = match_doc.get("tournament") or raw_sporty.get("tournament") or ""
    if isinstance(tournament, dict):
        tournament = tournament.get("name") or ""
    match_league_strength = league_strength_score(str(tournament))["score"]

    home_opp_avg = hp.get("avg_opponent_league_strength")
    away_opp_avg = ap.get("avg_opponent_league_strength")

    # Normalization factor: how much harder/easier was this team's history
    # vs the current match level.  Clamped to [0.6, 1.4] to avoid extremes.
    def _norm_factor(opp_avg: float | None) -> float:
        if opp_avg is None or match_league_strength == 0:
            return 1.0
        return max(0.6, min(1.4, opp_avg / match_league_strength))

    home_norm = _norm_factor(home_opp_avg)
    away_norm = _norm_factor(away_opp_avg)

    # Step-up/step-down delta — large gaps reduce credibility
    def _step_delta(opp_avg: float | None) -> float:
        if opp_avg is None:
            return 0.0
        return abs(match_league_strength - opp_avg)

    home_step_delta = _step_delta(home_opp_avg)
    away_step_delta = _step_delta(away_opp_avg)

    strength_context = {
        "match_league_strength": match_league_strength,
        "home_opp_avg_strength": home_opp_avg,
        "away_opp_avg_strength": away_opp_avg,
        "home_norm_factor": round(home_norm, 3),
        "away_norm_factor": round(away_norm, 3),
        "home_step_delta": round(home_step_delta, 1),
        "away_step_delta": round(away_step_delta, 1),
    }

    # ── Competition-specific stats (Phase 2) ─────────────────────────────────
    from app.storage.league_memory import normalize_league  # noqa: PLC0415
    competition_key = normalize_league(str(tournament))

    home_key = _slug_from_profile(hp)
    away_key = _slug_from_profile(ap)

    home_comp = _get_competition_stats(home_key, competition_key) if home_key else None
    away_comp = _get_competition_stats(away_key, competition_key) if away_key else None

    comp_stats_meta = {
        "home_used": home_comp is not None,
        "away_used": away_comp is not None,
    }

    # ── Extract per-side stats ───────────────────────────────────────────────
    def _side_stats(
        profile: dict[str, Any],
        comp: dict[str, Any] | None,
        venue: str,          # "home" or "away"
        norm: float,
        step_delta: float,
        sample: int,
    ) -> dict[str, Any]:
        """Build a normalised stat block for one side."""
        goals = profile.get("goals") or {}
        record = profile.get("record") or {}
        venue_split = (profile.get("venue_split") or {}).get(venue) or {}

        if comp:
            mp = int(comp.get("matches_played") or 1)
            win_rate   = comp["wins"] / mp
            draw_rate  = comp["draws"] / mp
            loss_rate  = comp["losses"] / mp
            gf_avg     = comp["goals_for"] / mp
            ga_avg     = comp["goals_against"] / mp
            btts_rate  = comp["btts_count"] / mp
            over25_rate = comp["over_25_count"] / mp
            cs_rate    = comp["clean_sheets"] / mp
            blank_rate = comp["failed_to_score"] / mp
            eff_sample = mp
        else:
            # Use venue-split stats when available, fall back to overall
            vp = int(venue_split.get("played") or 0)
            if vp >= 3:
                win_rate   = float(venue_split.get("win_rate") or 0)
                draw_rate  = float(venue_split.get("draw_rate") or 0)
                loss_rate  = float(venue_split.get("loss_rate") or 0)
                gf_avg     = float(venue_split.get("gf_avg") or 0)
                ga_avg     = float(venue_split.get("ga_avg") or 0)
                btts_rate  = float(venue_split.get("btts_rate") or 0)
                over25_rate = float(venue_split.get("over_25_rate") or 0)
                cs_rate    = float(venue_split.get("clean_sheet_rate") or 0)
                blank_rate = float(venue_split.get("blank_rate") or 0)
                eff_sample = vp
            else:
                win_rate   = (record.get("wins") or 0) / max(sample, 1)
                draw_rate  = (record.get("draws") or 0) / max(sample, 1)
                loss_rate  = (record.get("losses") or 0) / max(sample, 1)
                gf_avg     = float(goals.get("for_avg") or 0)
                ga_avg     = float(goals.get("against_avg") or 0)
                btts_rate  = float(goals.get("btts_rate") or 0)
                over25_rate = float(goals.get("over_2_5_rate") or 0)
                cs_rate    = float(goals.get("clean_sheet_rate") or 0)
                blank_rate = float(goals.get("blank_rate") or 0)
                eff_sample = sample

        # Apply league-strength normalization as an additive adjustment
        # (not a direct multiplier) so rates stay in [0, 1].
        # A norm_factor > 1 means the team played weaker opponents → discount.
        # A norm_factor < 1 means they played stronger opponents → boost.
        strength_adj = (1.0 - norm) * 0.15   # max ±6pp at the clamp limits
        win_rate_n  = max(0.0, min(1.0, win_rate  + strength_adj))
        gf_avg_n    = max(0.0, gf_avg  * (2.0 - norm))   # scale goals by inverse norm
        ga_avg_n    = max(0.0, ga_avg  * norm)            # conceding scales with norm

        # Sample-size credibility: shrink toward 0.5 / league average when thin
        credibility = min(1.0, eff_sample / 12.0)
        # Step-delta penalty: large league-level jumps reduce credibility further
        if step_delta > 20:
            credibility *= max(0.5, 1.0 - (step_delta - 20) / 80)

        win_rate_c  = 0.40 + (win_rate_n  - 0.40) * credibility
        draw_rate_c = 0.25 + (draw_rate   - 0.25) * credibility
        gf_avg_c    = gf_avg_n  * credibility + 1.2 * (1 - credibility)
        ga_avg_c    = ga_avg_n  * credibility + 1.2 * (1 - credibility)
        btts_c      = 0.45 + (btts_rate   - 0.45) * credibility
        over25_c    = 0.45 + (over25_rate  - 0.45) * credibility
        cs_c        = 0.25 + (cs_rate      - 0.25) * credibility

        # Recency-weighted form from profile string (W=1, D=0.5, L=0)
        form_str = str((record.get("form") or ""))[:8]
        form_weights = [0.30, 0.22, 0.17, 0.12, 0.08, 0.05, 0.04, 0.02]
        form_score = 0.0
        for i, ch in enumerate(form_str):
            w = form_weights[i] if i < len(form_weights) else 0.01
            form_score += w * (1.0 if ch == "W" else 0.5 if ch == "D" else 0.0)
        # Normalise form_score to [0, 1] (max possible ≈ sum of weights)
        max_form = sum(form_weights[:len(form_str)]) or 1.0
        form_score = form_score / max_form if max_form else 0.5

        return {
            "win_rate": round(win_rate_c, 3),
            "draw_rate": round(draw_rate_c, 3),
            "loss_rate": round(loss_rate, 3),
            "gf_avg": round(gf_avg_c, 2),
            "ga_avg": round(ga_avg_c, 2),
            "btts_rate": round(btts_c, 3),
            "over25_rate": round(over25_c, 3),
            "cs_rate": round(cs_c, 3),
            "blank_rate": round(blank_rate, 3),
            "form_score": round(form_score, 3),
            "credibility": round(credibility, 3),
            "sample": eff_sample,
        }

    home_stats = _side_stats(hp, home_comp, "home", home_norm, home_step_delta, home_sample)
    away_stats = _side_stats(ap, away_comp, "away", away_norm, away_step_delta, away_sample)

    # ── Matchup strength scores ──────────────────────────────────────────────
    # Each market gets a home_score and away_score; the gap drives the pick.

    # Match result: weighted combination of win_rate, form, and goal differential
    def _result_score(s: dict[str, Any]) -> float:
        return (
            s["win_rate"]   * 0.50
            + s["form_score"] * 0.25
            + min(1.0, max(0.0, (s["gf_avg"] - s["ga_avg"]) / 3.0 + 0.5)) * 0.25
        )

    home_result_score = _result_score(home_stats)
    away_result_score = _result_score(away_stats)
    result_gap = home_result_score - away_result_score

    # Draw detection: scores are close AND both draw_rates are elevated
    avg_draw_rate = (home_stats["draw_rate"] + away_stats["draw_rate"]) / 2
    draw_score = avg_draw_rate * 0.6 + (1.0 - abs(result_gap) * 4) * 0.4
    draw_score = max(0.0, draw_score)

    # Goals markets: average of both sides' tendencies
    btts_score  = (home_stats["btts_rate"]  + away_stats["btts_rate"])  / 2
    over25_score = (home_stats["over25_rate"] + away_stats["over25_rate"]) / 2
    # Under 2.5: driven by clean sheet rates
    under25_score = (home_stats["cs_rate"] + away_stats["cs_rate"]) / 2

    # ── Market selection ─────────────────────────────────────────────────────
    # Build candidate markets with their scores and minimum credibility
    min_cred = min(home_stats["credibility"], away_stats["credibility"])

    candidates: list[tuple[str, str, float]] = []  # (pick_type, selection, score)

    # Match result — need a meaningful gap
    if abs(result_gap) >= 0.06 and min_cred >= 0.25:
        if result_gap > 0:
            candidates.append(("match_result", "home_win", home_result_score))
        else:
            candidates.append(("match_result", "away_win", away_result_score))

    # Draw — only when balance is genuine
    if draw_score >= 0.38 and abs(result_gap) < 0.10 and min_cred >= 0.30:
        candidates.append(("match_result", "draw", draw_score))

    # BTTS
    if btts_score >= 0.50 and min_cred >= 0.25:
        candidates.append(("btts", "yes", btts_score))

    # Over 2.5
    if over25_score >= 0.50 and min_cred >= 0.25:
        candidates.append(("goals", "over_25", over25_score))

    # Under 2.5 — only when both sides show defensive strength
    if under25_score >= 0.40 and min_cred >= 0.30:
        candidates.append(("goals", "under_25", under25_score))

    if not candidates:
        return {
            "pick_type": "no_bet",
            "reason": "no_strong_signal",
            "strength_context": strength_context,
            "competition_stats": comp_stats_meta,
            "sample_size": {"home": home_sample, "away": away_sample},
        }

    # Pick the highest-scoring candidate
    best = max(candidates, key=lambda c: c[2])
    pick_type, selection, raw_score = best

    # ── Confidence ───────────────────────────────────────────────────────────
    # Map raw_score [0.4, 1.0] → confidence [40, 88], then scale by credibility
    base_conf = 40 + (raw_score - 0.40) * 80
    confidence = int(base_conf * (0.6 + min_cred * 0.4))
    confidence = max(1, min(88, confidence))

    return {
        "pick_type": pick_type,
        "selection": selection,
        "confidence": confidence,
        "strength_context": strength_context,
        "competition_stats": comp_stats_meta,
        "sample_size": {"home": home_sample, "away": away_sample},
        "home_stats": home_stats,
        "away_stats": away_stats,
        "scores": {
            "home_result": round(home_result_score, 3),
            "away_result": round(away_result_score, 3),
            "result_gap": round(result_gap, 3),
            "draw": round(draw_score, 3),
            "btts": round(btts_score, 3),
            "over25": round(over25_score, 3),
            "under25": round(under25_score, 3),
        },
        "reasoning": (
            f"{selection} selected (score={raw_score:.3f}, gap={result_gap:.3f}, "
            f"credibility={min_cred:.2f}, norm_home={home_norm:.2f}/away={away_norm:.2f})"
        ),
    }


def _slug_from_profile(profile: dict[str, Any]) -> str:
    """Best-effort team_key from a profile dict (used to look up competition stats)."""
    # The profile doesn't store team_key directly; the caller must pass it.
    # This is a no-op placeholder — competition stats lookup is keyed by the
    # team_key resolved in team_watcher_signal, not from the profile itself.
    return ""


# ---------------------------------------------------------------------------
# Internal helpers — venue context (kept for _ai_model compatibility)
# ---------------------------------------------------------------------------

def _build_venue_context(
    home_profile: dict[str, Any] | None,
    away_profile: dict[str, Any] | None,
    match_doc: dict[str, Any],  # noqa: ARG001
) -> dict[str, Any]:
    """Thin venue context for _ai_model / _merge_signal compatibility."""
    hp = home_profile or {}
    ap = away_profile or {}
    home_split = (hp.get("venue_split") or {}).get("home") or {}
    away_split = (ap.get("venue_split") or {}).get("away") or {}
    return {
        "home_win_rate": float(home_split.get("win_rate") or 0),
        "home_goals_avg": float(home_split.get("gf_avg") or 0),
        "away_win_rate": float(away_split.get("win_rate") or 0),
        "away_goals_avg": float(away_split.get("gf_avg") or 0),
    }


# ---------------------------------------------------------------------------
# Internal helpers — AI model
# ---------------------------------------------------------------------------

def _ai_model(
    home_profile: dict[str, Any] | None,
    away_profile: dict[str, Any] | None,
    match_doc: dict[str, Any],
) -> dict[str, Any]:
    """Groq LLM pick.

    Serialises both profiles and key match fields into a prompt string,
    calls get_router().call_analysis(prompt), and parses the JSON response.

    On any exception falls back to _rules_model result augmented with
    ``ai_model_available: False``.
    """
    try:
        from app.ai.ai_router import get_router, parse_json_response  # noqa: PLC0415

        home_name = match_doc.get("home_team") or "Home"
        away_name = match_doc.get("away_team") or "Away"
        tournament = match_doc.get("tournament") or match_doc.get("league") or ""
        match_date = match_doc.get("match_date") or ""

        prompt = (
            "You are a football prediction analyst. "
            "Given the team profiles below, produce a JSON prediction.\n\n"
            f"Match: {home_name} vs {away_name}\n"
            f"Tournament: {tournament}\n"
            f"Date: {match_date}\n\n"
            f"Home team profile ({home_name}):\n"
            f"{json.dumps(home_profile or {}, indent=2)}\n\n"
            f"Away team profile ({away_name}):\n"
            f"{json.dumps(away_profile or {}, indent=2)}\n\n"
            "Respond with ONLY a JSON object containing these keys:\n"
            "  pick_type: one of 'match_result', 'goals', 'btts', or 'no_bet'\n"
            "  selection: e.g. 'home_win', 'away_win', 'draw', 'over_25', 'under_25', 'yes', 'no'\n"
            "  confidence: integer 1-95\n"
            "  reasoning: brief explanation string\n"
        )

        raw = get_router().call_analysis(prompt)
        parsed = parse_json_response(raw)

        if not isinstance(parsed, dict):
            raise ValueError("LLM returned non-dict response")

        pick_type = parsed.get("pick_type", "no_bet")
        selection = parsed.get("selection", "")
        confidence_raw = parsed.get("confidence", 50)
        reasoning = parsed.get("reasoning", "")

        # Validate required fields
        if not pick_type or not selection:
            raise ValueError(f"Missing pick_type or selection in LLM response: {parsed}")

        confidence = max(1, min(95, int(confidence_raw)))

        venue_context = _build_venue_context(home_profile, away_profile, match_doc)
        return {
            "pick_type": pick_type,
            "selection": selection,
            "confidence": confidence,
            "reasoning": reasoning,
            "ai_model_available": True,
            "venue_context": venue_context,
        }

    except Exception as exc:
        logger.warning("_ai_model fallback to rules: %s", exc)
        rules_result = _rules_model(home_profile, away_profile, match_doc)
        rules_result["ai_model_available"] = False
        return rules_result


# ---------------------------------------------------------------------------
# Signal merge and weights
# ---------------------------------------------------------------------------

def get_team_weights(team_key: str) -> dict[str, float]:
    """Return the current TW_Weights for a team.

    Returns ``{"rules": weight_adj, "ai": weight_adj}``.
    Defaults to ``{"rules": 0.0, "ai": 0.0}`` when no data exists.
    """
    try:
        _init_db()
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            init_tw_tables(conn)
            rows = conn.execute(
                "SELECT sub_model, weight_adj FROM team_watcher_weights WHERE team_key = ?",
                (team_key,),
            ).fetchall()
        result: dict[str, float] = {"rules": 0.0, "ai": 0.0}
        for row in rows:
            sub_model = row["sub_model"]
            if sub_model in result:
                result[sub_model] = float(row["weight_adj"] or 0.0)
        return result
    except Exception as exc:
        logger.debug("get_team_weights error team_key=%s: %s", team_key, exc)
        return {"rules": 0.0, "ai": 0.0}


def _merge_signal(
    rules_out: dict[str, Any],
    ai_out: dict[str, Any],
    weights: dict[str, float],
) -> dict[str, Any]:
    """Weighted merge of rules and AI outputs into a single TW_Signal dict.

    combined_confidence = clamp(rules_conf * w_rules + ai_conf * w_ai, 1, 95)
    Adds models_agree + up-to-10-pt boost when both picks match.
    Adds rules_model_suppressed / ai_model_suppressed flags when weight < -0.6.
    """
    now = datetime.now(timezone.utc).isoformat()

    r_adj = float(weights.get("rules") or 0.0)
    a_adj = float(weights.get("ai") or 0.0)

    rules_sup = r_adj < -0.6
    ai_sup = a_adj < -0.6

    # Both models suppressed → no_bet
    if rules_sup and ai_sup:
        return {
            "name": "team_watcher_engine",
            "pick_type": "no_bet",
            "reason": "all_models_suppressed",
            "rules_model_suppressed": True,
            "ai_model_suppressed": True,
            "models_agree": False,
            "ai_model_available": ai_out.get("ai_model_available", True),
            "venue_context": rules_out.get("venue_context") or ai_out.get("venue_context") or {},
            "rules_pick": rules_out,
            "ai_pick": ai_out,
            "generated_at": now,
        }

    # Compute normalised weights (zeroing suppressed models)
    w_rules_raw = 0.0 if rules_sup else (0.5 + r_adj / 2.0)
    w_ai_raw = 0.0 if ai_sup else (0.5 + a_adj / 2.0)

    # Clamp individual weights to [0, 1]
    w_rules_raw = max(0.0, min(1.0, w_rules_raw))
    w_ai_raw = max(0.0, min(1.0, w_ai_raw))

    total = w_rules_raw + w_ai_raw
    if total <= 0:
        # Fallback — equal weights
        w_rules = 0.5
        w_ai = 0.5
    else:
        w_rules = w_rules_raw / total
        w_ai = w_ai_raw / total

    rules_conf = float(rules_out.get("confidence") or 50)
    ai_conf = float(ai_out.get("confidence") or 50)

    rules_pick_type = rules_out.get("pick_type", "no_bet")
    ai_pick_type = ai_out.get("pick_type", "no_bet")
    rules_selection = rules_out.get("selection", "")
    ai_selection = ai_out.get("selection", "")

    # Determine dominant pick (weight-adjusted)
    if rules_sup:
        dominant = ai_out
    elif ai_sup:
        dominant = rules_out
    elif w_rules >= w_ai:
        dominant = rules_out if rules_pick_type != "no_bet" else ai_out
    else:
        dominant = ai_out if ai_pick_type != "no_bet" else rules_out

    pick_type = dominant.get("pick_type", "no_bet")
    selection = dominant.get("selection", "")

    # Weighted confidence (only include non-suppressed, non-no_bet models)
    weighted_conf = 0.0
    effective_weight = 0.0
    if not rules_sup and rules_pick_type != "no_bet":
        weighted_conf += rules_conf * w_rules
        effective_weight += w_rules
    if not ai_sup and ai_pick_type != "no_bet":
        weighted_conf += ai_conf * w_ai
        effective_weight += w_ai

    if effective_weight > 0:
        combined_confidence = weighted_conf / effective_weight
    else:
        combined_confidence = 50.0

    combined_confidence = round(combined_confidence)

    # Agreement boost (up to 10 points)
    models_agree = (
        rules_pick_type == ai_pick_type
        and rules_selection == ai_selection
        and rules_pick_type != "no_bet"
        and ai_pick_type != "no_bet"
        and not rules_sup
        and not ai_sup
    )
    if models_agree:
        boost = min(10, int((rules_conf + ai_conf) / 2 * 0.15))
        combined_confidence = min(95, combined_confidence + boost)

    combined_confidence = max(1, min(95, combined_confidence))

    venue_context = (
        rules_out.get("venue_context")
        or ai_out.get("venue_context")
        or {}
    )

    return {
        "name": "team_watcher_engine",
        "pick_type": pick_type,
        "selection": selection,
        "confidence": combined_confidence,
        "models_agree": models_agree,
        "ai_model_available": ai_out.get("ai_model_available", True),
        "rules_model_suppressed": rules_sup,
        "ai_model_suppressed": ai_sup,
        "venue_context": venue_context,
        "rules_pick": rules_out,
        "ai_pick": ai_out,
        "generated_at": now,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def team_watcher_signal(match_doc: dict[str, Any]) -> dict[str, Any]:
    """Main entry point. Produces a TW_Signal dict for a match.

    Returns ``{"name": "team_watcher_engine", "pick_type": "no_bet", ...}``
    when no profile exists or on any internal error.  Always returns a dict;
    never raises.
    """
    try:
        # ------------------------------------------------------------------
        # Derive team keys from match_doc (same pattern as team_watcher._teams_for_doc)
        # ------------------------------------------------------------------
        raw_sporty = match_doc.get("raw_sporty") if isinstance(match_doc.get("raw_sporty"), dict) else match_doc
        home_name_raw = raw_sporty.get("home_team") or match_doc.get("home_team") or ""
        away_name_raw = raw_sporty.get("away_team") or match_doc.get("away_team") or ""

        def _slug(value: str) -> str:
            return "-".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())

        home_key = _slug(str(home_name_raw).strip()) if home_name_raw else ""
        away_key = _slug(str(away_name_raw).strip()) if away_name_raw else ""

        # ------------------------------------------------------------------
        # Load profiles
        # ------------------------------------------------------------------
        _init_db()
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            init_tw_tables(conn)
            home_profile = _get_profile(conn, home_key) if home_key else None
            away_profile = _get_profile(conn, away_key) if away_key else None

        # If neither profile exists, bail out
        if home_profile is None and away_profile is None:
            return {
                "name": "team_watcher_engine",
                "pick_type": "no_bet",
                "reason": "no_profile",
            }

        # ------------------------------------------------------------------
        # Run sub-models
        # ------------------------------------------------------------------
        rules_out = _rules_model(home_profile, away_profile, match_doc)
        ai_out = _ai_model(home_profile, away_profile, match_doc)

        # Determine which team key to use for weights (prefer home if profile exists)
        team_key_for_weights = home_key if home_profile is not None else away_key
        weights = get_team_weights(team_key_for_weights)

        # ------------------------------------------------------------------
        # Merge into final TW_Signal
        # ------------------------------------------------------------------
        tw_signal = _merge_signal(rules_out, ai_out, weights)

        # ------------------------------------------------------------------
        # Compute and embed confidence_impact (Requirement 2.7)
        # ------------------------------------------------------------------
        combined_confidence = float(tw_signal.get("confidence") or 50)
        confidence_impact = max(-8, min(8, round((combined_confidence - 50) / 5.625)))
        tw_signal["confidence_impact"] = confidence_impact

        return tw_signal

    except Exception as exc:
        logger.exception("team_watcher_signal unhandled error: %s", exc)
        return {
            "name": "team_watcher_engine",
            "pick_type": "no_bet",
            "reason": "engine_error",
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Prediction persistence
# ---------------------------------------------------------------------------

def record_tw_prediction(
    team_key: str,
    match_id: str,
    tw_signal: dict[str, Any],
) -> dict[str, Any]:
    """Persist a TW_Signal prediction to ``team_watcher_predictions``.

    Skips writes when ``tw_signal.get("pick_type") == "no_bet"`` and returns
    ``{"status": "ok", "inserted": False}``.

    On ``sqlite3.OperationalError`` the error is logged and
    ``{"status": "error", "reason": str(exc)}`` is returned so that write
    failures never surface to the prediction caller.

    Returns ``{"status": "ok", "inserted": True}`` on a successful insert.
    """
    if tw_signal.get("pick_type") == "no_bet":
        return {"status": "ok", "inserted": False}

    try:
        _init_db()
        with db_conn() as conn:
            init_tw_tables(conn)
            conn.execute(
                """
                INSERT INTO team_watcher_predictions
                    (team_key, match_id, pick_type, selection, confidence, sub_model)
                VALUES
                    (?, ?, ?, ?, ?, ?)
                """,
                (
                    team_key,
                    match_id,
                    tw_signal["pick_type"],
                    tw_signal["selection"],
                    tw_signal["confidence"],
                    "combined",
                ),
            )
        return {"status": "ok", "inserted": True}

    except sqlite3.OperationalError as exc:
        logger.error("record_tw_prediction OperationalError team_key=%s match_id=%s: %s", team_key, match_id, exc)
        return {"status": "error", "reason": str(exc)}


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def _resolve_outcome_from_result(result: dict[str, Any]) -> str | None:
    """
    Normalise a result dict into a canonical outcome string.

    Returns one of ``"home_win"``, ``"away_win"``, ``"draw"``, or ``None``
    when the outcome cannot be determined.

    The ``result`` dict may carry:
    - An explicit ``"outcome"`` key with value ``"home_win"`` / ``"away_win"`` / ``"draw"``.
    - Score keys ``"home_score"`` and ``"away_score"`` from which the outcome is derived.
    """
    # Prefer explicit outcome key
    outcome = result.get("outcome")
    if outcome:
        outcome_norm = str(outcome).lower().replace(" ", "_")
        if outcome_norm in ("home_win", "away_win", "draw"):
            return outcome_norm
        # Tolerate minor variations
        if outcome_norm in ("home", "1", "1x2_home"):
            return "home_win"
        if outcome_norm in ("away", "2", "1x2_away"):
            return "away_win"
        if outcome_norm in ("x", "draw", "1x2_draw"):
            return "draw"

    # Derive from scores if present
    home_score = result.get("home_score")
    away_score = result.get("away_score")
    if home_score is not None and away_score is not None:
        try:
            h = int(home_score)
            a = int(away_score)
            if h > a:
                return "home_win"
            if a > h:
                return "away_win"
            return "draw"
        except (TypeError, ValueError):
            pass

    return None


def _grade_tw_selection(
    selection: str,
    pick_type: str,
    actual_outcome: str | None,
    result: dict[str, Any] | None = None,
) -> str:
    """
    Resolve whether a TW prediction row's selection was a ``"win"``, ``"loss"``, or ``"void"``.

    For ``match_result`` picks the selection is directly compared to the canonical
    outcome (``"home_win"`` / ``"away_win"`` / ``"draw"``).

    For ``goals`` picks:
    - ``"over_25"`` wins if total goals > 2.5 (i.e. home_score + away_score >= 3)
    - ``"under_25"`` wins if total goals < 2.5 (i.e. home_score + away_score <= 2)
    Falls back to ``"void"`` if score data is absent.

    For ``btts`` picks:
    - ``"yes"`` wins if both teams scored (home_score >= 1 and away_score >= 1)
    - ``"no"`` wins if at least one team did not score
    Falls back to ``"void"`` if score data is absent.

    Returns ``"void"`` when the outcome is undetermined.
    """
    if actual_outcome is None and (result is None or not result):
        return "void"

    sel = (selection or "").lower().strip()
    pt = (pick_type or "").lower().strip()

    if pt == "match_result":
        if actual_outcome is None:
            return "void"
        # Direct selection→outcome comparison
        # Normalise selection to match canonical outcome strings
        sel_norm = sel.replace(" ", "_")
        if sel_norm == actual_outcome:
            return "win"
        # Tolerate common synonyms
        mapping = {
            "home": "home_win",
            "1": "home_win",
            "away": "away_win",
            "2": "away_win",
            "x": "draw",
        }
        if mapping.get(sel_norm) == actual_outcome:
            return "win"
        if actual_outcome in ("home_win", "away_win", "draw"):
            return "loss"
        return "void"

    # For goals / btts picks we need numeric scores
    res = result or {}
    home_score_raw = res.get("home_score")
    away_score_raw = res.get("away_score")

    if home_score_raw is None or away_score_raw is None:
        return "void"

    try:
        home_goals = int(home_score_raw)
        away_goals = int(away_score_raw)
    except (TypeError, ValueError):
        return "void"

    total_goals = home_goals + away_goals

    if pt == "goals":
        if sel == "over_25":
            # Wins if total goals > 2.5 (i.e. 3 or more goals)
            return "win" if total_goals > 2 else "loss"
        if sel == "under_25":
            # Wins if total goals < 2.5 (i.e. 2 or fewer goals)
            return "win" if total_goals < 3 else "loss"
        return "void"

    if pt == "btts":
        both_scored = home_goals >= 1 and away_goals >= 1
        if sel == "yes":
            return "win" if both_scored else "loss"
        if sel == "no":
            return "win" if not both_scored else "loss"
        return "void"

    return "void"


def grade_tw_predictions(match_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """
    Grade all open TW predictions for a match.

    Opens a DB connection, calls ``init_tw_tables(conn)``, then queries all rows
    in ``team_watcher_predictions`` where ``match_id = ?`` and ``graded_at IS NULL``
    (only ungraded rows).

    For each open row the actual match outcome is resolved from *result*:
    - If ``result`` has an ``"outcome"`` key, use it directly.
    - Otherwise derive the outcome from ``"home_score"`` / ``"away_score"``.

    ``selection == actual_outcome``  → ``"win"``
    ``selection != actual_outcome``  → ``"loss"``
    Undetermined / ambiguous        → ``"void"``

    Already-graded rows are automatically skipped via the ``graded_at IS NULL``
    WHERE clause, making this function idempotent.

    Returns ``{"status": "ok", "graded": <count_of_rows_updated>}``.
    Requirements: 4.2, 6.3, 6.4, 6.5
    """
    try:
        _init_db()
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            init_tw_tables(conn)

            # Fetch all open (ungraded) rows for this match
            rows = conn.execute(
                """
                SELECT id, pick_type, selection
                FROM team_watcher_predictions
                WHERE match_id = ? AND graded_at IS NULL
                """,
                (str(match_id),),
            ).fetchall()

            if not rows:
                return {"status": "ok", "graded": 0}

            # Resolve the actual match outcome once (shared across all rows)
            actual_outcome = _resolve_outcome_from_result(result)

            now = datetime.now(timezone.utc).isoformat()
            graded_count = 0

            for row in rows:
                row_grade = _grade_tw_selection(
                    selection=row["selection"],
                    pick_type=row["pick_type"],
                    actual_outcome=actual_outcome,
                    result=result,
                )
                conn.execute(
                    """
                    UPDATE team_watcher_predictions
                    SET result = ?, graded_at = ?
                    WHERE id = ?
                    """,
                    (row_grade, now, row["id"]),
                )
                graded_count += 1

        logger.debug("grade_tw_predictions match_id=%s graded=%d", match_id, graded_count)
        return {"status": "ok", "graded": graded_count}

    except Exception as exc:
        logger.error("grade_tw_predictions error match_id=%s: %s", match_id, exc)
        return {"status": "error", "reason": str(exc), "graded": 0}


# ---------------------------------------------------------------------------
# Weight update
# ---------------------------------------------------------------------------

def update_tw_weights(team_key: str) -> dict[str, Any]:
    """
    Recompute TW_Weights for a team from graded ``team_watcher_predictions``.

    Only runs if >= 10 graded rows (``graded_at IS NOT NULL``) exist for the
    team across all sub_models — mirrors the ``MIN_SAMPLES`` guard in
    ``self_learner.py``.

    Per-sub_model stats are computed (wins, losses, samples, win_rate) and
    ``weight_adj = round((win_rate - 0.50) * 2.0, 3)`` is stored in
    ``team_watcher_weights`` using an idempotent ``INSERT … ON CONFLICT … DO
    UPDATE`` (same pattern as ``signal_weights`` in ``self_learner.py``).

    Returns ``{"status": "skipped", "reason": "insufficient_samples"}`` when
    the total graded row count is below 10, or
    ``{"status": "ok", "updated": [{"sub_model": ..., "weight_adj": ...}, ...]}``
    on success.

    Requirements: 5.1, 5.2, 5.4
    """
    try:
        _init_db()
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            init_tw_tables(conn)

            # ------------------------------------------------------------------
            # 1. Count total graded rows for this team_key (all sub_models)
            # ------------------------------------------------------------------
            total_row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM team_watcher_predictions
                WHERE team_key = ? AND graded_at IS NOT NULL
                """,
                (team_key,),
            ).fetchone()

            total_graded = int(total_row["cnt"]) if total_row else 0

            if total_graded < 10:
                return {"status": "skipped", "reason": "insufficient_samples"}

            # ------------------------------------------------------------------
            # 2. Compute per-sub_model stats
            # ------------------------------------------------------------------
            rows = conn.execute(
                """
                SELECT
                    sub_model,
                    COUNT(*)                                     AS samples,
                    SUM(CASE WHEN result = 'win'  THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses
                FROM team_watcher_predictions
                WHERE team_key = ? AND graded_at IS NOT NULL
                GROUP BY sub_model
                """,
                (team_key,),
            ).fetchall()

            now = datetime.now(timezone.utc).isoformat()
            updated: list[dict[str, Any]] = []

            for row in rows:
                sub_model = row["sub_model"]
                samples = int(row["samples"])
                wins = int(row["wins"])
                losses = int(row["losses"])

                if samples == 0:
                    continue

                win_rate = wins / samples
                weight_adj = round((win_rate - 0.50) * 2.0, 3)

                # Upsert into team_watcher_weights
                conn.execute(
                    """
                    INSERT INTO team_watcher_weights
                        (team_key, sub_model, samples, wins, losses, win_rate, weight_adj, last_updated)
                    VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(team_key, sub_model) DO UPDATE SET
                        samples      = excluded.samples,
                        wins         = excluded.wins,
                        losses       = excluded.losses,
                        win_rate     = excluded.win_rate,
                        weight_adj   = excluded.weight_adj,
                        last_updated = current_timestamp
                    """,
                    (
                        team_key,
                        sub_model,
                        samples,
                        wins,
                        losses,
                        round(win_rate, 4),
                        weight_adj,
                        now,
                    ),
                )

                updated.append({"sub_model": sub_model, "weight_adj": weight_adj})

        logger.debug("update_tw_weights team_key=%s updated=%d sub_models", team_key, len(updated))
        return {"status": "ok", "updated": updated}

    except Exception as exc:
        logger.error("update_tw_weights error team_key=%s: %s", team_key, exc)
        return {"status": "error", "reason": str(exc)}


# ---------------------------------------------------------------------------
# Weekly analysis
# ---------------------------------------------------------------------------

def generate_weekly_analysis(team_key: str) -> dict[str, Any]:
    """Build and persist the Weekly_Analysis_Report for a team.

    Reads the last 30 finished match rows from ``ai_team_watcher_matches``
    (finished = ``goals_for IS NOT NULL AND goals_against IS NOT NULL``).

    When fewer than 5 finished rows are found, returns early with
    ``sufficient_data: False`` and all trend fields set to ``None``.

    Otherwise computes:
    - ``rolling_form``         — last 8 results as a "WWDLWWLW" string
    - ``record``               — {wins, draws, losses}
    - ``points_per_game``      — float (3 per win, 1 per draw)
    - ``goals_for_avg``        — float
    - ``goals_against_avg``    — float
    - ``btts_rate``            — fraction of matches where both teams scored
    - ``over_25_rate``         — fraction of matches with total goals > 2.5
    - ``clean_sheet_rate``     — fraction of matches where team conceded 0
    - ``venue_split``          — separate home/away sub-dicts
    - ``market_lean_trend``    — {direction, magnitude} default neutral
    - ``trend_summary``        — human-readable string
    - ``upcoming_pick_confidence`` — int from _rules_model, or None

    Persists the report to ``ai_team_watchers.weekly_analysis_json`` and sets
    ``weekly_analysis_at = current_timestamp``.

    Returns the full report dict with ``sufficient_data: True``.

    Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.7
    """
    _init_db()
    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        init_tw_tables(conn)

        # ------------------------------------------------------------------
        # 1. Fetch the last 30 finished match rows for this team
        # ------------------------------------------------------------------
        rows = conn.execute(
            """
            SELECT
                result,
                goals_for,
                goals_against,
                venue,
                match_date
            FROM ai_team_watcher_matches
            WHERE team_key = ?
              AND goals_for  IS NOT NULL
              AND goals_against IS NOT NULL
            ORDER BY match_date DESC, created_at DESC
            LIMIT 30
            """,
            (team_key,),
        ).fetchall()

        sample_size = len(rows)
        now_iso = datetime.now(timezone.utc).isoformat()

        if sample_size < 5:
            return {
                "sufficient_data": False,
                "generated_at": now_iso,
                "market_lean_trend": None,
                "trend_summary": None,
                "rolling_form": None,
                "record": None,
                "points_per_game": None,
                "goals_for_avg": None,
                "goals_against_avg": None,
                "btts_rate": None,
                "over_25_rate": None,
                "clean_sheet_rate": None,
                "venue_split": None,
                "upcoming_pick_confidence": None,
            }

        # ------------------------------------------------------------------
        # 2. Compute aggregate stats
        # ------------------------------------------------------------------
        wins = sum(1 for r in rows if r["result"] == "win")
        draws = sum(1 for r in rows if r["result"] == "draw")
        losses = sum(1 for r in rows if r["result"] == "loss")

        total_gf = sum(int(r["goals_for"] or 0) for r in rows)
        total_ga = sum(int(r["goals_against"] or 0) for r in rows)

        btts_count = sum(
            1 for r in rows
            if int(r["goals_for"] or 0) >= 1 and int(r["goals_against"] or 0) >= 1
        )
        over_25_count = sum(
            1 for r in rows
            if int(r["goals_for"] or 0) + int(r["goals_against"] or 0) >= 3
        )
        clean_sheet_count = sum(1 for r in rows if int(r["goals_against"] or 0) == 0)

        points_per_game = round((wins * 3 + draws) / sample_size, 3)
        goals_for_avg = round(total_gf / sample_size, 3)
        goals_against_avg = round(total_ga / sample_size, 3)
        btts_rate = round(btts_count / sample_size, 3)
        over_25_rate = round(over_25_count / sample_size, 3)
        clean_sheet_rate = round(clean_sheet_count / sample_size, 3)

        # Rolling form — last 8 results (rows are already newest-first)
        form_chars = []
        for r in rows[:8]:
            res = r["result"]
            if res == "win":
                form_chars.append("W")
            elif res == "draw":
                form_chars.append("D")
            else:
                form_chars.append("L")
        rolling_form = "".join(form_chars)

        # ------------------------------------------------------------------
        # 3. Venue split
        # ------------------------------------------------------------------
        def _venue_stats(venue_rows: list) -> dict[str, Any]:
            n = len(venue_rows)
            if n == 0:
                return {"wins": 0, "draws": 0, "losses": 0, "goals_for_avg": 0.0, "goals_against_avg": 0.0, "win_rate": 0.0}
            vw = sum(1 for r in venue_rows if r["result"] == "win")
            vd = sum(1 for r in venue_rows if r["result"] == "draw")
            vl = sum(1 for r in venue_rows if r["result"] == "loss")
            vgf = sum(int(r["goals_for"] or 0) for r in venue_rows)
            vga = sum(int(r["goals_against"] or 0) for r in venue_rows)
            return {
                "wins": vw,
                "draws": vd,
                "losses": vl,
                "goals_for_avg": round(vgf / n, 3),
                "goals_against_avg": round(vga / n, 3),
                "win_rate": round(vw / n, 3),
            }

        home_rows = [r for r in rows if (r["venue"] or "").lower() == "home"]
        away_rows = [r for r in rows if (r["venue"] or "").lower() == "away"]

        venue_split = {
            "home": _venue_stats(home_rows),
            "away": _venue_stats(away_rows),
        }

        # ------------------------------------------------------------------
        # 4. Market lean trend (default neutral; can be enriched in future)
        # ------------------------------------------------------------------
        market_lean_trend: dict[str, Any] = {"direction": "neutral", "magnitude": 0.0}

        # ------------------------------------------------------------------
        # 5. Trend summary — human-readable description based on form & venue
        # ------------------------------------------------------------------
        recent_4 = rows[:4]
        recent_wins = sum(1 for r in recent_4 if r["result"] == "win")
        home_wr = venue_split["home"]["win_rate"]
        away_wr = venue_split["away"]["win_rate"]

        if recent_wins >= 3:
            if home_wr > away_wr + 0.20:
                trend_summary = "Improving at home over the last four matches."
            else:
                trend_summary = "Strong recent form with multiple wins in the last four matches."
        elif recent_wins == 0:
            if sample_size >= 8:
                trend_summary = "Poor recent form with no wins in the last four matches."
            else:
                trend_summary = "Struggling for wins recently."
        elif abs(home_wr - away_wr) > 0.20:
            better_venue = "home" if home_wr > away_wr else "away"
            trend_summary = f"Inconsistent form overall; performs noticeably better {better_venue}."
        else:
            trend_summary = "Mixed recent form; results split between wins, draws, and losses."

        # ------------------------------------------------------------------
        # 6. Upcoming pick confidence from _rules_model
        # ------------------------------------------------------------------
        upcoming_pick_confidence: int | None = None
        try:
            # Use the stored profile for the team to get a representative pick
            profile = _get_profile(conn, team_key)
            if profile is not None and int(profile.get("sample_size") or 0) >= 5:
                rules_result = _rules_model(profile, None, {})
                if rules_result.get("pick_type") != "no_bet":
                    upcoming_pick_confidence = int(rules_result.get("confidence") or 0) or None
        except Exception as exc:
            logger.debug("generate_weekly_analysis: _rules_model error for %s: %s", team_key, exc)

        # ------------------------------------------------------------------
        # 7. Assemble report
        # ------------------------------------------------------------------
        report: dict[str, Any] = {
            "sufficient_data": True,
            "generated_at": now_iso,
            "rolling_form": rolling_form,
            "record": {"wins": wins, "draws": draws, "losses": losses},
            "points_per_game": points_per_game,
            "goals_for_avg": goals_for_avg,
            "goals_against_avg": goals_against_avg,
            "btts_rate": btts_rate,
            "over_25_rate": over_25_rate,
            "clean_sheet_rate": clean_sheet_rate,
            "venue_split": venue_split,
            "market_lean_trend": market_lean_trend,
            "trend_summary": trend_summary,
            "upcoming_pick_confidence": upcoming_pick_confidence,
        }

        # ------------------------------------------------------------------
        # 8. Persist to ai_team_watchers (Requirement 3.5)
        # ------------------------------------------------------------------
        conn.execute(
            """
            UPDATE ai_team_watchers
            SET weekly_analysis_json = ?,
                weekly_analysis_at   = current_timestamp
            WHERE team_key = ?
            """,
            (json.dumps(report), team_key),
        )

    logger.debug("generate_weekly_analysis persisted report for team_key=%s", team_key)
    return report


def _maybe_generate_weekly_analysis(team_key: str) -> None:
    """Generate a new Weekly_Analysis_Report only when needed.

    Reads ``weekly_analysis_at`` from ``ai_team_watchers``.  Calls
    ``generate_weekly_analysis(team_key)`` when:
    - ``weekly_analysis_at IS NULL`` (first-ever report for this team), or
    - the stored timestamp is more than 7 days older than
      ``datetime.now(timezone.utc)``.

    Returns ``None`` silently in all cases — including when the 7-day gate
    blocks generation or when the team has no watcher row yet.

    Requirements: 3.1, 6.1, 6.2
    """
    try:
        _init_db()
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            init_tw_tables(conn)
            row = conn.execute(
                "SELECT weekly_analysis_at FROM ai_team_watchers WHERE team_key = ?",
                (team_key,),
            ).fetchone()

        if row is None:
            # No watcher row at all — nothing to generate
            return

        weekly_analysis_at = row["weekly_analysis_at"]

        if weekly_analysis_at is None:
            # First-ever report — generate immediately
            generate_weekly_analysis(team_key)
            return

        # Parse the stored timestamp and check the 7-day gate
        try:
            last_generated = datetime.fromisoformat(weekly_analysis_at)
            # Ensure timezone-aware for comparison
            if last_generated.tzinfo is None:
                last_generated = last_generated.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age_days = (now - last_generated).total_seconds() / 86400.0
            if age_days > 7:
                generate_weekly_analysis(team_key)
        except (ValueError, TypeError) as exc:
            logger.debug(
                "_maybe_generate_weekly_analysis: bad timestamp for %s: %s", team_key, exc
            )
            # If the stored timestamp is unparseable, regenerate to be safe
            generate_weekly_analysis(team_key)

    except Exception as exc:
        logger.debug("_maybe_generate_weekly_analysis error team_key=%s: %s", team_key, exc)


# ---------------------------------------------------------------------------
# Post-match AI monitoring — context-rich performance notes
# ---------------------------------------------------------------------------

def monitor_team_performance(
    team_key: str,
    match_id: str,
    match_doc: dict[str, Any],
    tw_signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Post-match AI monitoring: generate granular performance notes for a team.

    This function is called after every completed match to produce
    context-rich records that a dedicated sports fan would maintain,
    continuously improving future prediction accuracy.

    The function:
    1. Determines the actual match outcome from the match document.
    2. Evaluates the team's performance relative to expectations.
    3. Generates typed performance notes (strength, risk, situational).
    4. Records notes via the competition registry for persistence.

    Returns a dict with ``status`` and ``notes_generated`` count.
    Never raises — all errors are caught and logged.
    """
    try:
        _init_db()
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            init_tw_tables(conn)

            # ── 1. Resolve the actual outcome ────────────────────────────────
            score = match_doc.get("score") or {}
            home_goals = _to_int(score.get("home"))
            away_goals = _to_int(score.get("away"))
            if home_goals is None or away_goals is None:
                return {"status": "skipped", "reason": "no_score"}

            # Determine which side this team played
            team_row = conn.execute(
                "select team_side, goals_for, goals_against, league_name "
                "from ai_team_watcher_matches where team_key = ? and match_id = ?",
                (team_key, match_id),
            ).fetchone()
            if not team_row:
                return {"status": "skipped", "reason": "no_match_record"}

            side = str(team_row["team_side"] or "").lower()
            own_goals = int(team_row["goals_for"] or 0)
            opp_goals = int(team_row["goals_against"] or 0)
            actual_result = "win" if own_goals > opp_goals else "loss" if own_goals < opp_goals else "draw"
            competition_key = _normalize_league_name(str(team_row["league_name"] or ""))

            # ── 2. Load team profile for context ─────────────────────────────
            profile = _get_profile(conn, team_key) or {}
            sample_size = int(profile.get("sample_size") or 0)

            notes: list[dict[str, Any]] = []

            # ── 3. Generate typed notes ─────────────────────────────────────
            # 3a. Result note
            result_note = _note_for_result(actual_result, own_goals, opp_goals, side, profile)
            if result_note:
                notes.append(result_note)

            # 3b. Performance vs expectation note
            expectation_note = _note_for_expectation(team_key, tw_signal, actual_result, own_goals, opp_goals)
            if expectation_note:
                notes.append(expectation_note)

            # 3c. Situational note (venue, competition context)
            situational_note = _note_for_situation(side, own_goals, opp_goals, profile, match_doc)
            if situational_note:
                notes.append(situational_note)

            # 3d. Trend note (if sufficient history)
            if sample_size >= 5:
                trend_note = _note_for_trend(conn, team_key, actual_result)
                if trend_note:
                    notes.append(trend_note)

            # ── 4. Persist notes via competition registry ────────────────────
            from app.competition.competition_registry import (
                add_performance_note as _add_note,
                ensure_team_competition,
                update_team_competition_stats,
                record_team_prediction_outcome,
            )

            if competition_key:
                ensure_team_competition(conn, team_key, competition_key)
                update_team_competition_stats(
                    conn,
                    team_key=team_key,
                    competition_key=competition_key,
                    goals_for=own_goals,
                    goals_against=opp_goals,
                    result=actual_result,
                    match_date=str(match_doc.get("match_date") or ""),
                )

                # Record prediction accuracy if we have a TW signal
                if tw_signal and tw_signal.get("pick_type") != "no_bet":
                    try:
                        pred_correct = _was_prediction_correct(tw_signal, actual_result, own_goals, opp_goals)
                        record_team_prediction_outcome(conn, team_key, competition_key, pred_correct)
                    except Exception:
                        pass

                for note in notes:
                    try:
                        _add_note(
                            conn,
                            team_key=team_key,
                            competition_key=competition_key,
                            match_id=match_id,
                            note_type=note.get("note_type", "info"),
                            title=note.get("title", ""),
                            description=note.get("description", ""),
                            context=note.get("context", {}),
                            severity=note.get("severity", "info"),
                        )
                    except Exception:
                        pass

        return {"status": "ok", "notes_generated": len(notes), "notes": notes}

    except Exception as exc:
        logger.debug("monitor_team_performance error team_key=%s: %s", team_key, exc)
        return {"status": "error", "reason": str(exc)}


def _normalize_league_name(name: str) -> str:
    """Normalize a league name to a stable key."""
    from app.storage.league_memory import normalize_league
    return normalize_league(name)


def _to_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _note_for_result(result: str, gf: int, ga: int, side: str, profile: dict[str, Any]) -> dict[str, Any] | None:
    """Generate a result-based performance note."""
    side_label = "at home" if side == "home" else "away"
    if result == "win":
        title = f"Win ({gf}-{ga}) {side_label}"
        description = f"Secured a {gf}-{ga} victory {side_label}."
        severity = "positive"
        note_type = "result"
    elif result == "loss":
        title = f"Loss ({gf}-{ga}) {side_label}"
        description = f"Fell to a {gf}-{ga} defeat {side_label}."
        severity = "negative"
        note_type = "result"
    else:
        title = f"Draw ({gf}-{ga}) {side_label}"
        description = f"Shared the spoils in a {gf}-{ga} draw {side_label}."
        severity = "neutral"
        note_type = "result"

    context = {
        "result": result,
        "goals_for": gf,
        "goals_against": ga,
        "side": side,
        "sample_size": profile.get("sample_size", 0),
    }
    return {"note_type": note_type, "title": title, "description": description,
            "context": context, "severity": severity}


def _note_for_expectation(
    team_key: str,
    tw_signal: dict[str, Any] | None,
    actual_result: str,
    gf: int,
    ga: int,
) -> dict[str, Any] | None:
    """Generate a note comparing actual result to TW signal expectation."""
    if not tw_signal or tw_signal.get("pick_type") == "no_bet":
        return None

    pick_type = tw_signal.get("pick_type", "")
    selection = tw_signal.get("selection", "")
    confidence = int(tw_signal.get("confidence") or 0)

    # Determine if prediction was correct
    correct = _was_prediction_correct(tw_signal, actual_result, gf, ga)
    outcome_label = "as expected" if correct else "against expectations"

    if pick_type == "match_result":
        title = f"Prediction {'correct' if correct else 'incorrect'}: {selection} ({confidence}%)"
        description = (
            f"Team watcher predicted {selection} at {confidence}% confidence. "
            f"Actual result: {actual_result}. Prediction was {outcome_label}."
        )
    elif pick_type == "goals":
        title = f"Goals prediction {'correct' if correct else 'incorrect'}: {selection} ({confidence}%)"
        description = (
            f"Team watcher predicted {selection} at {confidence}% confidence. "
            f"Actual total: {gf + ga} goals. Prediction was {outcome_label}."
        )
    elif pick_type == "btts":
        title = f"BTTS prediction {'correct' if correct else 'incorrect'}: {selection} ({confidence}%)"
        both_scored = gf > 0 and ga > 0
        actual_btts = "yes" if both_scored else "no"
        description = (
            f"Team watcher predicted BTTS {selection} at {confidence}% confidence. "
            f"Actual BTTS: {actual_btts}. Prediction was {outcome_label}."
        )
    else:
        return None

    context = {
        "pick_type": pick_type,
        "selection": selection,
        "confidence": confidence,
        "actual_result": actual_result,
        "correct": correct,
        "goals_for": gf,
        "goals_against": ga,
    }
    severity = "positive" if correct else "negative"
    return {"note_type": "prediction_accuracy", "title": title, "description": description,
            "context": context, "severity": severity}


def _note_for_situation(side: str, gf: int, ga: int, profile: dict[str, Any], match_doc: dict[str, Any]) -> dict[str, Any] | None:
    """Generate a situational performance note based on venue and context."""
    notes: list[str] = []
    side_label = "home" if side == "home" else "away"

    # Venue context
    venue_split = profile.get("venue_split") or {}
    side_stats = venue_split.get(side_label) or {}
    side_ppg = float(side_stats.get("ppg") or 0)
    overall_ppg = float(profile.get("ppg") or 0)

    if side_ppg > 0 and overall_ppg > 0:
        if side_ppg > overall_ppg * 1.2:
            notes.append(f"Performed above season average {side_label} (PPG {side_ppg:.2f} vs {overall_ppg:.2f}).")
        elif side_ppg < overall_ppg * 0.8:
            notes.append(f"Underperformed relative to season average {side_label} (PPG {side_ppg:.2f} vs {overall_ppg:.2f}).")

    # Goal patterns
    if gf == 0 and ga == 0:
        notes.append("Goalless draw — neither side could break through.")
    elif gf >= 3:
        notes.append(f"High-scoring performance with {gf} goals scored.")
    elif ga >= 3:
        notes.append(f"Conceded {ga} goals — defensive vulnerability exposed.")

    if not notes:
        return None

    title = f"Situational note: {side_label} performance"
    description = " ".join(notes)
    context = {
        "side": side,
        "venue_split": side_stats,
        "overall_ppg": overall_ppg,
        "goals_for": gf,
        "goals_against": ga,
    }
    return {"note_type": "situational", "title": title, "description": description,
            "context": context, "severity": "info"}


def _note_for_trend(conn: sqlite3.Connection, team_key: str, actual_result: str) -> dict[str, Any] | None:
    """Generate a trend-based note comparing this result to recent form."""
    rows = conn.execute(
        """
        select result from ai_team_watcher_matches
        where team_key = ? and goals_for is not null and goals_against is not null
        order by match_date desc, created_at desc
        limit 8
        """,
        (team_key,),
    ).fetchall()

    if len(rows) < 3:
        return None

    recent_results = [r["result"] for r in rows[:5]]
    prev_form = "".join("W" if r == "win" else "D" if r == "draw" else "L" for r in recent_results)

    # Compare current result to recent form
    if actual_result == "win" and prev_form.count("W") <= 1:
        title = "Form reversal: win after poor recent results"
        description = f"Secured a win following a run of form: {prev_form}. This could signal a turning point."
        severity = "positive"
    elif actual_result == "loss" and prev_form.count("L") <= 1:
        title = "Form blip: loss after strong recent results"
        description = f"Suffered a loss after a strong run: {prev_form}. May be an isolated setback."
        severity = "negative"
    elif actual_result == "win" and prev_form.count("W") >= 3:
        title = "Form continuation: extended winning run"
        description = f"Extended strong form with another win. Recent run: {prev_form}."
        severity = "positive"
    elif actual_result == "loss" and prev_form.count("L") >= 3:
        title = "Form concern: extended losing run"
        description = f"Extended poor form with another loss. Recent run: {prev_form}."
        severity = "negative"
    else:
        return None

    context = {
        "recent_form": prev_form,
        "actual_result": actual_result,
        "sample_size": len(rows),
    }
    return {"note_type": "trend", "title": title, "description": description,
            "context": context, "severity": severity}


def _was_prediction_correct(tw_signal: dict[str, Any], actual_result: str, gf: int, ga: int) -> bool:
    """Determine if a TW signal prediction was correct."""
    pick_type = tw_signal.get("pick_type", "")
    selection = tw_signal.get("selection", "")

    if pick_type == "match_result":
        sel_norm = selection.lower().replace(" ", "_")
        mapping = {"home": "home_win", "1": "home_win", "away": "away_win", "2": "away_win", "x": "draw"}
        normalized = mapping.get(sel_norm, sel_norm)
        return normalized == actual_result

    if pick_type == "goals":
        total = gf + ga
        if selection == "over_25":
            return total > 2
        if selection == "under_25":
            return total < 3
        return False

    if pick_type == "btts":
        both_scored = gf > 0 and ga > 0
        if selection == "yes":
            return both_scored
        if selection == "no":
            return not both_scored
        return False

    return False


# ---------------------------------------------------------------------------
# Backfill: regrade predictions that were incorrectly graded as void
# ---------------------------------------------------------------------------

def regrade_void_predictions(limit: int = 2000) -> dict[str, Any]:
    """Regrade all ``team_watcher_predictions`` rows where ``result = 'void'``.

    These rows were produced by the bug in ``observe_match`` that passed an
    empty result dict to ``grade_tw_predictions``, causing every prediction to
    resolve as ``"void"`` instead of ``"win"`` / ``"loss"``.

    For each void row the function:
    1. Fetches the corresponding ``ai_team_watcher_matches`` row (same
       ``team_key`` + ``match_id``) which already stores the correct
       ``goals_for`` / ``goals_against`` values.
    2. Reconstructs a proper result dict and calls ``_grade_tw_selection``.
    3. Updates the ``result`` in-place; resets ``graded_at`` to now so weight
       updates see fresh data.

    Rows whose match has no score data remain ``"void"`` — they are skipped
    to avoid making things worse.

    Returns::

        {
            "status": "ok",
            "inspected": <total void rows found>,
            "regraded": <rows updated to win/loss>,
            "still_void": <rows with no score data>,
            "skipped_no_match": <rows with no ai_team_watcher_matches row>,
        }
    """
    try:
        _init_db()
        with db_conn() as conn:
            conn.row_factory = sqlite3.Row
            init_tw_tables(conn)

            # Fetch all void rows (up to limit)
            void_rows = conn.execute(
                """
                SELECT id, team_key, match_id, pick_type, selection
                FROM team_watcher_predictions
                WHERE result = 'void'
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            if not void_rows:
                return {"status": "ok", "inspected": 0, "regraded": 0, "still_void": 0, "skipped_no_match": 0}

            now = datetime.now(timezone.utc).isoformat()
            regraded = 0
            still_void = 0
            skipped_no_match = 0

            for row in void_rows:
                # Look up the stored match scores
                match_row = conn.execute(
                    """
                    SELECT goals_for, goals_against
                    FROM ai_team_watcher_matches
                    WHERE team_key = ? AND match_id = ?
                    LIMIT 1
                    """,
                    (row["team_key"], row["match_id"]),
                ).fetchone()

                if match_row is None:
                    skipped_no_match += 1
                    continue

                goals_for = match_row["goals_for"]
                goals_against = match_row["goals_against"]

                if goals_for is None or goals_against is None:
                    still_void += 1
                    continue

                # Reconstruct a result dict that _grade_tw_selection can use
                result_dict = {
                    "home_score": goals_for,   # from the team's perspective; outcome
                    "away_score": goals_against,  # derived below via full outcome calc
                }

                # Determine canonical outcome from raw scores stored in the match row
                # goals_for / goals_against are always from the tracked team's POV,
                # so we need the raw home/away scores.  Retrieve them from the
                # raw_match_json field which stores both sides.
                raw_row = conn.execute(
                    """
                    SELECT raw_match_json, team_side
                    FROM ai_team_watcher_matches
                    WHERE team_key = ? AND match_id = ?
                    LIMIT 1
                    """,
                    (row["team_key"], row["match_id"]),
                ).fetchone()

                raw_match = {}
                if raw_row and raw_row["raw_match_json"]:
                    try:
                        raw_match = json.loads(raw_row["raw_match_json"]) if isinstance(raw_row["raw_match_json"], str) else raw_row["raw_match_json"]
                    except (ValueError, TypeError):
                        raw_match = {}

                team_side = (raw_row["team_side"] if raw_row else None) or "home"

                # Resolve actual home/away scores
                if team_side == "home":
                    h_score = int(goals_for)
                    a_score = int(goals_against)
                else:
                    h_score = int(goals_against)
                    a_score = int(goals_for)

                result_dict = {"home_score": h_score, "away_score": a_score}

                new_grade = _grade_tw_selection(
                    selection=row["selection"],
                    pick_type=row["pick_type"],
                    actual_outcome=_resolve_outcome_from_result(result_dict),
                    result=result_dict,
                )

                if new_grade == "void":
                    still_void += 1
                    continue

                conn.execute(
                    """
                    UPDATE team_watcher_predictions
                    SET result = ?, graded_at = ?
                    WHERE id = ?
                    """,
                    (new_grade, now, row["id"]),
                )
                regraded += 1

            conn.commit()

        logger.info(
            "regrade_void_predictions: inspected=%d regraded=%d still_void=%d skipped_no_match=%d",
            len(void_rows), regraded, still_void, skipped_no_match,
        )
        return {
            "status": "ok",
            "inspected": len(void_rows),
            "regraded": regraded,
            "still_void": still_void,
            "skipped_no_match": skipped_no_match,
        }

    except Exception as exc:
        logger.error("regrade_void_predictions error: %s", exc)
        return {"status": "error", "reason": str(exc)}
