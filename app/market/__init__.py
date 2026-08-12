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

_LAZY_EXPORTS = {
    # market_intent.py
    "normalise_market_text": "app.market.market_intent",
    "classify_market_intent": "app.market.market_intent",
    "parse_total_line": "app.market.market_intent",
    "grade_market_intent": "app.market.market_intent",
    "selection_key": "app.market.market_intent",
    # regime.py
    "Regime": "app.market.regime",
    "TIER_1": "app.market.regime",
    "TIER_2": "app.market.regime",
    "TIER_3": "app.market.regime",
    "TIER_4": "app.market.regime",
    "get_regime": "app.market.regime",
    "get_regime_for_doc": "app.market.regime",
    "passes_regime_gate": "app.market.regime",
    "apply_regime_stake_cap": "app.market.regime",
    "regime_summary_for_predictions": "app.market.regime",
    # season_stage.py
    "SMALL_LEAGUE_MAX": "app.market.season_stage",
    "MEDIUM_LEAGUE_MAX": "app.market.season_stage",
    "classify_table_size": "app.market.season_stage",
    "detect_season_stage": "app.market.season_stage",
    "season_aware_table_weight": "app.market.season_stage",
}


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if not module_name:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value

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

