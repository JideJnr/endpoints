"""
Competition domain package.

Competition analysis — registry, special logic, league strength, SOS.

NOTE: competition_analyser and competition_special are NOT eagerly imported here
because they depend on app.league_memory which depends on app.competition_registry,
creating a circular import chain at package initialisation time.

Callers should import those modules directly:
    from app.competition.competition_analyser import run_competition_analysis, ...
    from app.competition.competition_special import get_competition_settings, ...

Safe eager imports:
    competition_registry — imported by league_memory directly, not via this __init__
    league_strength      — no circular deps, pure logic
    sos                  — no circular deps, pure logic (lazy fetches via sofascore_client)

See docs/migration_checklist.md for the full list of moves.
"""

# ── competition_registry — depended on by league_memory; safe to import here ─
# (league_memory imports the shim/registry directly, not via this __init__)
from app.competition.competition_registry import (  # noqa: F401
    init_competition_registry_tables,
    ensure_competition,
    get_competition,
    list_competitions,
    ensure_team_competition,
    update_team_competition_stats,
    record_team_prediction_outcome,
    add_performance_note,
    get_team_performance_notes,
    get_team_competition_stats,
    get_team_competition_history,
)

# ── league_strength — pure logic, no circular deps ────────────────────────────
from app.competition.league_strength import (  # noqa: F401
    league_strength_score,
    history_league_strength,
    league_strength_edge,
)

# ── sos — deferred ────────────────────────────────────────────────────────────
# sos.py imports app.sofascore_client at module level, which triggers
# sofascore_grades → league_memory → competition_registry → competition/__init__ cycle.
# Import directly:
#   from app.competition.sos import analyse_schedule, compare_schedules

# ── competition_analyser and competition_special ──────────────────────────────
# Deferred — both depend on app.league_memory which creates a circular import
# when loaded at package init time.
# Import directly:
#   from app.competition.competition_analyser import run_competition_analysis
#   from app.competition.competition_special import get_competition_settings
