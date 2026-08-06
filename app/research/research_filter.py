"""
Research-driven predictor filter — data-driven rule engine.

All BLOCK/CAUTION/TRUST sets are computed entirely from the live
research_stats table, regenerated daily by a scheduler job.
No country or league names are hardcoded in this module.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.db import db_conn

logger = logging.getLogger(__name__)

# ── Threshold constants (no name lists) ──────────────────────

BLOCK_LEAGUE_LOSS_RATE = 0.75
BLOCK_COUNTRY_LOSS_RATE = 0.50
CAUTION_COUNTRY_LOSS_RATE = 0.40
CAUTION_LEAGUE_LOSS_RATE = 0.55
TRUST_COUNTRY_WIN_RATE = 0.80

MIN_LEAGUE_SAMPLES = 5
MIN_COUNTRY_SAMPLES = 10
MIN_PICK_TYPE_SAMPLES = 10
MIN_SELECTION_SAMPLES = 10
MIN_CONFIDENCE_BAND_SAMPLES = 10
MIN_SOURCE_SAMPLES = 10
MIN_ODDS_BUCKET_SAMPLES = 10
MIN_FAVORITE_SIDE_SAMPLES = 10

CAUTION_THRESHOLD = 0.66
TRUST_BOOST_CAP = 8
PUBLISHED_CONFIDENCE_CAP = 88

DYNAMIC_CACHE_TTL = 600  # seconds


# ── Lazy singleton: ensure research_stats table exists on import ──────

_research_stats_ensured = False
_research_stats_lock = threading.Lock()


def _ensure_research_stats_table(conn: Any) -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS for research_stats."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS research_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dimension TEXT NOT NULL,
            key TEXT NOT NULL,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            win_rate REAL NOT NULL DEFAULT 0.0,
            loss_rate REAL NOT NULL DEFAULT 0.0,
            min_samples INTEGER NOT NULL DEFAULT 10,
            updated_at TEXT NOT NULL DEFAULT current_timestamp,
            UNIQUE(dimension, key)
        )
        """
    )
    conn.commit()


def _ensure_table_on_import() -> None:
    global _research_stats_ensured
    if _research_stats_ensured:
        return
    with _research_stats_lock:
        if _research_stats_ensured:
            return
        try:
            with db_conn() as conn:
                _ensure_research_stats_table(conn)
            _research_stats_ensured = True
        except Exception:
            pass


_ensure_table_on_import()


def _normalise_league_key(raw: str) -> str:
    """Lowercase, spaces→hyphens, strip whitespace."""
    return raw.lower().strip().replace(" ", "-")


# ── Dynamic rule cache ──────────────────────────────────────────

_dynamic_cache: dict[str, frozenset[str]] = {
    "block_leagues": frozenset(),
    "block_countries": frozenset(),
    "caution_leagues": frozenset(),
    "caution_countries": frozenset(),
    "trust_countries": frozenset(),
}
_dynamic_cache_time: float = 0.0
_dynamic_lock = threading.Lock()


def _load_dynamic_rules() -> None:
    """Read research_stats and populate dynamic block/caution/trust frozensets.

    Refreshes every DYNAMIC_CACHE_TTL seconds.  On failure, log WARNING
    and keep dynamic sets as empty frozensets.
    """
    global _dynamic_cache, _dynamic_cache_time
    now = time.time()
    if now - _dynamic_cache_time < DYNAMIC_CACHE_TTL:
        return
    with _dynamic_lock:
        if now - _dynamic_cache_time < DYNAMIC_CACHE_TTL:
            return
        try:
            with db_conn() as conn:
                rows = conn.execute(
                    "SELECT dimension, key, loss_rate, win_rate, total FROM research_stats"
                ).fetchall()
            block_leagues = set()
            block_countries = set()
            caution_leagues = set()
            caution_countries = set()
            trust_countries = set()
            for row in rows:
                dim = row["dimension"]
                key = row["key"]
                loss_rate = float(row["loss_rate"] or 0)
                win_rate = float(row["win_rate"] or 0)
                total = int(row["total"] or 0)
                if dim == "league" and loss_rate >= BLOCK_LEAGUE_LOSS_RATE and total >= MIN_LEAGUE_SAMPLES:
                    block_leagues.add(key)
                if dim == "country" and loss_rate >= BLOCK_COUNTRY_LOSS_RATE and total >= MIN_COUNTRY_SAMPLES:
                    block_countries.add(key)
                if dim == "country" and CAUTION_COUNTRY_LOSS_RATE <= loss_rate < BLOCK_COUNTRY_LOSS_RATE and total >= MIN_COUNTRY_SAMPLES:
                    caution_countries.add(key)
                if dim == "league" and CAUTION_LEAGUE_LOSS_RATE <= loss_rate < BLOCK_LEAGUE_LOSS_RATE and total >= MIN_LEAGUE_SAMPLES:
                    caution_leagues.add(key)
                if dim == "country" and win_rate >= TRUST_COUNTRY_WIN_RATE and total >= MIN_COUNTRY_SAMPLES:
                    trust_countries.add(key)
            _dynamic_cache = {
                "block_leagues": frozenset(block_leagues),
                "block_countries": frozenset(block_countries),
                "caution_leagues": frozenset(caution_leagues),
                "caution_countries": frozenset(caution_countries),
                "trust_countries": frozenset(trust_countries),
            }
            _dynamic_cache_time = now
        except Exception as exc:
            logger.warning("research_filter: dynamic rule load failed: %s", exc)


def _get_dynamic_rules() -> dict[str, frozenset[str]]:
    """Return current dynamic rules, refreshing cache if stale."""
    _load_dynamic_rules()
    return _dynamic_cache


# ── evaluate_pick() ────────────────────────────────────────────

def evaluate_pick(pick: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a single pick against research-driven rules.

    Returns a dict with:
      - blocked: bool
      - reason: str | None
      - trust_boost: int
      - evidence: dict
    """
    evidence: dict[str, Any] = {}
    pick_type = str(pick.get("type") or "")
    selection = str(pick.get("selection") or "")
    confidence = int(pick.get("confidence") or 0)
    draw_odds = float(pick.get("draw_odds") or 0)
    favorite_odds = float(pick.get("favorite_odds") or 0)
    home_odds = float(pick.get("home_odds") or 0)
    country = str(pick.get("country") or "").lower().strip()
    league_key = str(pick.get("league_key") or "").lower().strip()
    if not league_key and pick.get("league_name"):
        league_key = _normalise_league_key(str(pick["league_name"]))

    # ── Hard block checks (return on first match) ────────────────────
    if pick_type == "match_result":
        return {"blocked": True, "reason": "research_block:match_result_61pct_loss", "trust_boost": 0, "evidence": {"matched_value": "match_result"}}
    if confidence < 60:
        return {"blocked": True, "reason": "research_block:confidence_below_60", "trust_boost": 0, "evidence": {"matched_value": confidence}}
    if 0 < draw_odds < 2.00:
        return {"blocked": True, "reason": "research_block:draw_odds_below_2", "trust_boost": 0, "evidence": {"matched_value": draw_odds}}
    if favorite_odds >= 2.50:
        return {"blocked": True, "reason": "research_block:favorite_odds_2_50_plus", "trust_boost": 0, "evidence": {"matched_value": favorite_odds}}
    if 2.50 <= home_odds <= 2.99:
        return {"blocked": True, "reason": "research_block:home_odds_danger_zone", "trust_boost": 0, "evidence": {"matched_value": home_odds}}

    dynamic = _get_dynamic_rules()

    if league_key and league_key in dynamic["block_leagues"]:
        return {"blocked": True, "reason": "research_block:dynamic_league", "trust_boost": 0, "evidence": {"matched_value": league_key}}

    if country and country in dynamic["block_countries"]:
        return {"blocked": True, "reason": "research_block:dynamic_country", "trust_boost": 0, "evidence": {"matched_value": country}}

    # ── Caution checks ────────────────────────────────────────────────
    caution_conditions: list[str] = []
    matched_value: Any = None
    is_noisy_band = False

    # 2.1 Away or Draw with confidence 60-71 → block
    if selection == "Away or Draw" and 60 <= confidence <= 71:
        caution_conditions.append("away_or_draw_low_conf")
        matched_value = selection
        return {"blocked": True, "reason": "research_caution:away_or_draw_low_conf", "trust_boost": 0, "evidence": {"caution_conditions": caution_conditions, "matched_value": matched_value}}

    # 2.2 Draw as market favorite → caution
    if str(pick.get("favorite_side") or "").lower() == "draw":
        caution_conditions.append("draw_favorite")
        if matched_value is None:
            matched_value = "draw"

    # 2.3 Country in dynamic caution list → caution
    if country and country in dynamic["caution_countries"]:
        caution_conditions.append("dynamic_caution_country")
        if matched_value is None:
            matched_value = country

    # 2.4 League in dynamic caution list → caution
    if league_key and league_key in dynamic["caution_leagues"]:
        caution_conditions.append("dynamic_caution_league")
        if matched_value is None:
            matched_value = league_key

    # 2.5 Confidence 60-66 band → noisy band
    if 60 <= confidence <= 66:
        caution_conditions.append("noisy_band")
        is_noisy_band = True
        if matched_value is None:
            matched_value = confidence

    # 2.6 competition_special:europa-league → caution
    source = str(pick.get("source") or "")
    if "europa-league" in source.lower():
        caution_conditions.append("europa_league_source")
        if matched_value is None:
            matched_value = source

    # Apply noisy_band + caution_context escalation
    if is_noisy_band and len(caution_conditions) > 1:
        return {"blocked": True, "reason": "research_caution:noisy_band_plus_caution_context", "trust_boost": 0, "evidence": {"caution_conditions": caution_conditions, "matched_value": matched_value, "noisy_band": True}}

    # If only noisy_band fired (no other caution), cap confidence but don't block
    if is_noisy_band and len(caution_conditions) == 1:
        evidence["noisy_band"] = True
        evidence["caution_conditions"] = caution_conditions
        evidence["matched_value"] = matched_value

    # ── Trust boost accumulation ──────────────────────────────────────
    trust_boost = 0
    research_trust_boosts: list[dict[str, Any]] = []

    # 3.1 Home or Away selection → +4
    if selection == "Home or Away":
        boost = 4
        trust_boost += boost
        research_trust_boosts.append({"rule": "home_or_away", "contribution": boost})

    # 3.2 live_total_goals pick type → +3
    if pick_type == "live_total_goals":
        boost = 3
        trust_boost += boost
        research_trust_boosts.append({"rule": "live_total_goals", "contribution": boost})

    # 3.3 sportybet_market_signal source → +5
    if source == "sportybet_market_signal":
        boost = 5
        trust_boost += boost
        research_trust_boosts.append({"rule": "sportybet_market_signal", "contribution": boost})

    # 3.4 Home odds 1.30-1.69 → +2
    if 1.30 <= home_odds <= 1.69 and home_odds > 0:
        boost = 2
        trust_boost += boost
        research_trust_boosts.append({"rule": "home_odds_130_169", "contribution": boost})

    # 3.5 Draw odds 3.00+ → +2
    if draw_odds >= 3.00:
        boost = 2
        trust_boost += boost
        research_trust_boosts.append({"rule": "draw_odds_3_plus", "contribution": boost})

    # 3.6 Favorite odds 1.50-1.69 → +2
    if 1.50 <= favorite_odds <= 1.69 and favorite_odds > 0:
        boost = 2
        trust_boost += boost
        research_trust_boosts.append({"rule": "favorite_odds_150_169", "contribution": boost})

    # 3.7 Away odds 1.70-1.99 → +2
    away_odds = float(pick.get("away_odds") or 0)
    if 1.70 <= away_odds <= 1.99 and away_odds > 0:
        boost = 2
        trust_boost += boost
        research_trust_boosts.append({"rule": "away_odds_170_199", "contribution": boost})

    # 3.8 Country in dynamic trust countries → +3
    if country and country in dynamic["trust_countries"]:
        boost = 3
        trust_boost += boost
        research_trust_boosts.append({"rule": "trust_country", "contribution": boost})

    # 3.9 Confidence 74 → +1
    if confidence == 74:
        boost = 1
        trust_boost += boost
        research_trust_boosts.append({"rule": "confidence_74", "contribution": boost})

    # Cap trust boost
    trust_boost = min(trust_boost, TRUST_BOOST_CAP)

    # Cap published confidence
    published_confidence = min(confidence + trust_boost, PUBLISHED_CONFIDENCE_CAP)

    evidence["research_trust_boosts"] = research_trust_boosts
    evidence["noisy_band"] = is_noisy_band
    if caution_conditions:
        evidence["caution_conditions"] = caution_conditions
        evidence["matched_value"] = matched_value

    return {
        "blocked": False,
        "reason": None,
        "trust_boost": trust_boost,
        "published_confidence": published_confidence,
        "evidence": evidence,
    }


# ── _research_filter_candidate() ────────────────────────────────

def _research_filter_candidate(
    pick: dict[str, Any],
    odds_profile: dict[str, float] | None = None,
    country: str = "",
    league_key: str = "",
) -> bool:
    """Inline stripped-down block + caution checks for betbuilder candidates.

    Returns True when candidate is safe to include, False to exclude.
    Must match the block/pass decision of evaluate_pick() for the same inputs.
    No evidence dict, no trust boost.
    """
    pick_type = str(pick.get("type") or "")
    selection = str(pick.get("selection") or "")
    confidence = int(pick.get("confidence") or 0)

    if odds_profile is None:
        odds_profile = {}

    draw_odds = float(odds_profile.get("draw_odds") or 0)
    favorite_odds = float(odds_profile.get("favorite_odds") or 0)
    home_odds = float(odds_profile.get("home_odds") or 0)

    # Hard block checks (same order as evaluate_pick)
    if pick_type == "match_result":
        return False
    if confidence < 60:
        return False
    if 0 < draw_odds < 2.00:
        return False
    if favorite_odds >= 2.50:
        return False
    if 2.50 <= home_odds <= 2.99:
        return False

    dynamic = _get_dynamic_rules()

    if league_key and league_key in dynamic["block_leagues"]:
        return False
    if country and country in dynamic["block_countries"]:
        return False

    # Caution checks (block only for first matched condition that results in block)
    if selection == "Away or Draw" and 60 <= confidence <= 71:
        return False

    # noisy_band + caution_context escalation
    is_noisy_band = 60 <= confidence <= 66
    caution_count = 0
    if str(pick.get("favorite_side") or "").lower() == "draw":
        caution_count += 1
    if country and country in dynamic["caution_countries"]:
        caution_count += 1
    if league_key and league_key in dynamic["caution_leagues"]:
        caution_count += 1
    if is_noisy_band:
        caution_count += 1
    source = str(pick.get("source") or "")
    if "europa-league" in source.lower():
        caution_count += 1

    if is_noisy_band and caution_count > 1:
        return False

    return True


# ── get_research_context_for_prompt() ──────────────────────────

_RESEARCH_CONTEXT_CACHE: dict[str, tuple[str, float]] = {}
_RESEARCH_CONTEXT_CACHE_TTL = 600  # 10 minutes


def get_research_context_for_prompt() -> str:
    """Build a compact RESEARCH_STATS text block for LLM prompts.

    Queries research_stats for rows meeting minimum sample thresholds,
    surfaces key insights in priority order, truncates to ≤ 1000 chars.
    Caches result in-process for 10 minutes; refresh also triggers
    _load_dynamic_rules() so dynamic rules stay current.
    """
    now = time.time()
    cache_key = "research_context"
    cached = _RESEARCH_CONTEXT_CACHE.get(cache_key)
    if cached and now - cached[1] < _RESEARCH_CONTEXT_CACHE_TTL:
        return cached[0]

    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT dimension, key, wins, losses, total, win_rate, loss_rate FROM research_stats WHERE total >= ? ORDER BY dimension, key",
                (5,),
            ).fetchall()
    except Exception:
        rows = []

    if not rows:
        result = _get_static_fallback()
        _RESEARCH_CONTEXT_CACHE[cache_key] = (result, now)
        return result

    # Refresh dynamic rules cache too
    _load_dynamic_rules()

    # Build compact context
    best_selection = ""
    best_country_pool: list[str] = []
    worst_warnings: list[str] = []
    optimal_conf_zone = ""
    top_odds_insight = ""

    by_dim: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        dim = row["dimension"]
        by_dim.setdefault(dim, []).append({
            "key": row["key"],
            "wins": int(row["wins"] or 0),
            "losses": int(row["losses"] or 0),
            "total": int(row["total"] or 0),
            "win_rate": float(row["win_rate"] or 0),
            "loss_rate": float(row["loss_rate"] or 0),
        })

    # Best selection type (highest win_rate, min 10 samples)
    sel_rows = by_dim.get("selection", [])
    if sel_rows:
        sel_rows.sort(key=lambda r: r["win_rate"], reverse=True)
        best = sel_rows[0]
        best_selection = f"best_selection:{best['key']}({best['win_rate']:.0%} win)"

    # Best country pool (top 3 by win_rate, min 10 samples)
    country_rows = by_dim.get("country", [])
    if country_rows:
        country_rows.sort(key=lambda r: r["win_rate"], reverse=True)
        top3 = country_rows[:3]
        best_country_pool = [f"{c['key']}({c['win_rate']:.0%})" for c in top3]

    # Worst league/country warnings (loss_rate >= 0.40, min samples)
    for dim_name in ("league", "country"):
        dim_rows = by_dim.get(dim_name, [])
        for r in dim_rows:
            if r["loss_rate"] >= 0.40 and r["total"] >= 5:
                worst_warnings.append(f"{dim_name}:{r['key']}({r['loss_rate']:.0%} loss)")

    # Optimal confidence zone
    conf_rows = by_dim.get("confidence_band", [])
    if conf_rows:
        conf_rows.sort(key=lambda r: r["win_rate"], reverse=True)
        best_conf = conf_rows[0]
        optimal_conf_zone = f"optimal_conf:{best_conf['key']}({best_conf['win_rate']:.0%} win)"

    # Top odds-profile insight
    odds_rows = by_dim.get("odds_profile", [])
    if odds_rows:
        odds_rows.sort(key=lambda r: r["win_rate"], reverse=True)
        best_odds = odds_rows[0]
        top_odds_insight = f"best_odds:{best_odds['key']}({best_odds['win_rate']:.0%} win)"

    parts = [p for p in [best_selection, optimal_conf_zone, top_odds_insight] if p]
    if best_country_pool:
        parts.append(f"top_countries:{','.join(best_country_pool)}")
    if worst_warnings:
        parts.append(f"warnings:{';'.join(worst_warnings[:5])}")

    result = "RESEARCH_STATS " + " | ".join(parts)
    # Truncate to ≤ 1000 chars
    if len(result) > 1000:
        result = result[:1000]

    _RESEARCH_CONTEXT_CACHE[cache_key] = (result, now)
    return result


def _get_static_fallback() -> str:
    """Read RESEARCH_FINDINGS.md and condense the Consolidated Rules section."""
    try:
        import pathlib
        base = pathlib.Path(__file__).resolve().parent.parent.parent
        md_path = base / "RESEARCH_FINDINGS.md"
        if not md_path.exists():
            return ""
        text = md_path.read_text(encoding="utf-8")
        # Extract Consolidated Rules section
        start = text.find("## 10. CONSOLIDATED RULES")
        if start == -1:
            start = text.find("Consolidated Rules")
        if start == -1:
            return ""
        section = text[start:]
        # Condense: take first ~800 chars
        condensed = section[:800].replace("\n", " ").strip()
        return f"[static snapshot — live stats not yet available] {condensed}"
    except Exception:
        return ""
