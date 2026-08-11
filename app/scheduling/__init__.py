"""
Scheduling domain package.

APScheduler job runner, job state machine, pipeline toggle registry, loop authority.

Contains:
  - scheduler.py       — All APScheduler job functions and scheduler lifecycle management
  - job_state.py       — Cross-process SQLite job guard, heartbeat, and state ledger
  - loop_authority.py  — Correction authority lease manager (prevents self-healing loop conflicts)
  - pipeline_registry.py — Single source of truth for all toggleable scheduler pipelines

Dependency direction: scheduling → storage → config
"""
# noqa: F401

from app.scheduling.scheduler import (  # noqa: F401
    LIVE_PRIORITY_ENGINE_ID,
    UNIFIED_UPCOMING_BATCH_SIZE,
    SCHEDULER_INTERVAL_DEFAULTS,
    job_ingest_upcoming,
    job_ingest_live,
    job_enrich_worker,
    job_live_priority,
    get_live_priority_mode,
    set_live_priority_mode,
    job_live_priority_toggle,
    scheduler_intervals,
    patch_scheduler_intervals,
    reset_scheduler_intervals,
    job_sofa_pipeline,
    job_enrich_future,
    job_unified_upcoming,
    reset_deferred_predictions_and_repredict,
    job_unified_live,
    job_competition_special,
    job_archive_finished,
    job_flush_to_mongo,
    job_grade_predictions,
    job_grade_overdue_predictions,
    job_prune_mongo,
    job_keep_alive,
    job_system_supervisor,
    job_prediction_monitor,
    job_autopilot_guardian,
    start_scheduler,
    stop_scheduler,
    scheduler_status,
    is_shutting_down,
    run_job_with_guard,
)

from app.scheduling.job_state import (  # noqa: F401
    JobBusy,
    OWNER,
    job_guard,
    finish_job,
    heartbeat,
    list_job_states,
    recover_abandoned_jobs,
)

from app.scheduling.loop_authority import (  # noqa: F401
    CorrectionAuthorityBusy,
    OWNER as LOOP_AUTHORITY_OWNER,
    correction_authority,
    authority_snapshot,
)

from app.scheduling.pipeline_registry import (  # noqa: F401
    SourceType,
    StatusType,
    PipelineDef,
    PIPELINES,
    PIPELINE_MAP,
    TOGGLEABLE_IDS,
    PRESETS,
    ensure_default_states,
    is_pipeline_enabled,
    get_enrich_worker_mode,
    get_all_pipeline_states,
)
