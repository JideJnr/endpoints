"""
Market domain package.

Market classification — market intent, odds patterns, regime detection, season stage.

NOTE: app.market.market (odds snapshotting) is NOT eagerly imported here because it
depends on app.league_memory which depends on app.market_intent — creating a circular
import chain. Callers that need snapshot_odds / get_movement / get_all_movements
should import directly:
    from app.market.market import snapshot_odds, get_movement, get_all_movements
"""
# noqa: F401  # DEPRECATED shim — see migration_checklist.md

# ── market_intent.py — pure logic, no circular deps ──────────────────────────
from app.market.market_intent import (  # noqa: F401
    normalise_market_text,
    classify_market_intent,
    parse_total_line,
    grade_market_intent,
    selection_key,
)

# ── regime.py — pure logic, no circular deps ─────────────────────────────────
from app.market.regime import (  # noqa: F401
    Regime,
    TIER_1,
    TIER_2,
    TIER_3,
    TIER_4,
    get_regime,
    get_regime_for_doc,
    passes_regime_gate,
    apply_regime_stake_cap,
    regime_summary_for_predictions,
)

# ── season_stage.py — pure logic, no circular deps ───────────────────────────
from app.market.season_stage import (  # noqa: F401
    SMALL_LEAGUE_MAX,
    MEDIUM_LEAGUE_MAX,
    classify_table_size,
    detect_season_stage,
    season_aware_table_weight,
)

# ── odds_pattern.py — depends on app.db and app.league_memory (via _init_db) ─
# Deferred to avoid circular: league_memory → market_intent → market.__init__
# Import directly: from app.market.market.odds_pattern import extract_pattern, pattern_signal
# ─────────────────────────────────────────────────────────────────────────────

# ── market.py — depends on app.db, app.league_memory, app.config ─────────────
# Deferred to avoid circular: league_memory → market_intent → market.__init__
# Import directly: from app.market.market import snapshot_odds, get_movement
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    # market_intent
    "normalise_market_text",
    "classify_market_intent",
    "parse_total_line",
    "grade_market_intent",
    "selection_key",
    # regime
    "Regime",
    "TIER_1",
    "TIER_2",
    "TIER_3",
    "TIER_4",
    "get_regime",
    "get_regime_for_doc",
    "passes_regime_gate",
    "apply_regime_stake_cap",
    "regime_summary_for_predictions",
    # season_stage
    "SMALL_LEAGUE_MAX",
    "MEDIUM_LEAGUE_MAX",
    "classify_table_size",
    "detect_season_stage",
    "season_aware_table_weight",
]

