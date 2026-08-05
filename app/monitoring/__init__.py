"""
Monitoring domain package.

System and prediction auditing, supervision, and self-learning feedback loop.

All modules have been migrated from predictx/app/ into this domain package.
See docs/migration_checklist.md for the full list of moves.

Submodules:
  - system_audit       prediction_system_audit
  - prediction_audit   build_prediction_audit, build_pick_audit, build_deferred_prediction_audit, grading_reason
  - system_supervisor  run_system_supervisor, latest_supervisor_snapshots
  - prediction_monitor run_prediction_monitor, latest_prediction_monitor_snapshots
  - self_learner       run_learning_cycle, get_signal_weights, get_learned_weights,
                       get_league_accuracy, get_top_signals, get_learning_summary

Public API is re-exported lazily to avoid circular import chains at package init.
Import directly from submodules for use in other domain packages:

    from app.monitoring.system_audit import prediction_system_audit
    from app.monitoring.prediction_audit import build_prediction_audit, grading_reason
    from app.monitoring.system_supervisor import run_system_supervisor
    from app.monitoring.prediction_monitor import run_prediction_monitor
    from app.monitoring.self_learner import run_learning_cycle, get_signal_weights
"""
# Exports are available via direct submodule imports to avoid circular import chains.
# The monitoring sub-packages participate in cross-domain cycles (storage ↔ monitoring,
# ai ↔ monitoring) that would deadlock if resolved eagerly at package init time.
