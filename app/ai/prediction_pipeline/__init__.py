"""
prediction_pipeline — the evidence-first AI prediction pipeline, split by concern.

This package is the implementation behind the public, stable import path
``app.ai.ai_prediction_pipeline`` (kept as a thin re-export facade because
several modules — scheduler.py, the dynamic pipeline registry in
routers/pipelines.py, frontend.py, storage/league_memory/queries.py,
monitoring/self_learner.py — import directly from that path, and one of
them (pipelines.py) resolves ``job_ai_prediction_queue`` by dotted-string
path at runtime).

Sub-modules, in dependency order:
    weights.py         Specialist accuracy tracking (SQLite-backed).
                        get_specialist_weights, record_specialist_outcome,
                        grade_specialist_contributions, get_specialist_summary

    evidence.py         The six independent evidence-gathering steps plus
                        shared match/odds/tier helpers.
                        _step_h2h, _step_common_opponent, _step_form,
                        _step_odds, _step_similar_matches, _step_team_history,
                        _name, _teams, _tournament, _best_odds,
                        classify_tournament_tier, sort_gate, apply_tier_filter

    teams.py            Team behaviour profiling (depends on evidence.py's
                        _history_for_team).
                        TeamBehaviourProfile, derive_team_profile, persist_team_profile

    markets.py          Market candidate shortlisting.
                        MarketCandidate, shortlist_markets

    orchestration.py    Ties everything together: the decider call, the
                        rules-engine fallback, the top-level entry point,
                        and the scheduled job queue.
                        run_ai_prediction_with_fallback, job_ai_prediction_queue
"""
