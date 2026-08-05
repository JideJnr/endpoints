# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/competition/competition_analyser.py
# This shim will be removed in v2.0.

from app.competition.competition_analyser import *  # noqa: F401,F403
from app.competition.competition_analyser import (  # noqa: F401
    CompletedRound,
    StandingRow,
    RoundResult,
    TeamOddsMovement,
    AnalysisContext,
    init_competition_analysis_table,
    persist_competition_analysis,
    get_latest_analysis,
    get_analysis_history,
    is_round_complete,
    should_skip_small_round,
    should_generate_analysis,
    detect_newly_completed_rounds,
    classify_odds_movement_direction,
    assemble_analysis_context,
    build_analysis_prompt,
    run_competition_analysis,
    catchup_competition_scores,
    job_competition_analysis,
)
