"""
Pipeline Control Router
-----------------------
REST endpoints for listing, toggling, and applying presets to all
toggleable scheduler pipelines.

  GET  /pipelines                      — list all pipeline states
  POST /pipelines/{engine_id}/enable   — enable one pipeline
  POST /pipelines/{engine_id}/disable  — disable one pipeline
  POST /pipelines/preset/{name}        — apply a preset (cloud / local / off)
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipelines", tags=["pipelines"])


# ── List all pipelines ────────────────────────────────────────────────────────

@router.get("")
def list_pipelines():
    """Return the full pipeline state list with live engine states and job-run data."""
    from app.pipeline_registry import get_all_pipeline_states
    try:
        pipelines = get_all_pipeline_states()
        return {
            "status": "success",
            "count": len(pipelines),
            "pipelines": pipelines,
        }
    except Exception as exc:
        _logger.exception("list_pipelines failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Enable / disable single pipeline ─────────────────────────────────────────

@router.post("/{engine_id}/enable")
def enable_pipeline(engine_id: str):
    """Enable a toggleable pipeline by engine_id and trigger an immediate run if applicable."""
    from app.pipeline_registry import TOGGLEABLE_IDS, PIPELINE_MAP
    from app.league_memory import set_engine_status
    from app.activity_log import record_activity
    import threading

    if engine_id not in TOGGLEABLE_IDS:
        raise HTTPException(
            status_code=404,
            detail=f"Pipeline '{engine_id}' not found. Valid IDs: {sorted(TOGGLEABLE_IDS)}",
        )

    set_engine_status(engine_id, "active")
    record_activity(
        f"Pipeline '{PIPELINE_MAP[engine_id].label}' enabled",
        job="pipeline_control",
        status="ok",
        details={"engine_id": engine_id, "action": "enable"},
    )

    # Immediate run for pipelines that have a dedicated job function
    _IMMEDIATE_RUN_MAP = {
        "unified_upcoming": "app.scheduler.job_unified_upcoming",
        "unified_live":     "app.scheduler.job_unified_live",
        "sofa_pipeline":    "app.scheduler.job_sofa_pipeline",
        "competition_special": "app.scheduler.job_competition_special",
    }
    if engine_id in _IMMEDIATE_RUN_MAP:
        def _run():
            try:
                module_path, fn_name = _IMMEDIATE_RUN_MAP[engine_id].rsplit(".", 1)
                import importlib
                mod = importlib.import_module(module_path)
                fn = getattr(mod, fn_name)
                fn()
            except Exception as exc:
                _logger.warning("Immediate run failed for %s: %s", engine_id, exc)
        threading.Thread(target=_run, daemon=True).start()

    return {
        "status": "success",
        "engine_id": engine_id,
        "enabled": True,
        "label": PIPELINE_MAP[engine_id].label,
        "immediate_run": engine_id in _IMMEDIATE_RUN_MAP,
    }


@router.post("/{engine_id}/disable")
def disable_pipeline(engine_id: str):
    """Disable a toggleable pipeline by engine_id."""
    from app.pipeline_registry import TOGGLEABLE_IDS, PIPELINE_MAP
    from app.league_memory import set_engine_status
    from app.activity_log import record_activity

    if engine_id not in TOGGLEABLE_IDS:
        raise HTTPException(
            status_code=404,
            detail=f"Pipeline '{engine_id}' not found. Valid IDs: {sorted(TOGGLEABLE_IDS)}",
        )

    set_engine_status(engine_id, "paused")
    record_activity(
        f"Pipeline '{PIPELINE_MAP[engine_id].label}' disabled",
        job="pipeline_control",
        status="ok",
        details={"engine_id": engine_id, "action": "disable"},
    )
    return {
        "status": "success",
        "engine_id": engine_id,
        "enabled": False,
        "label": PIPELINE_MAP[engine_id].label,
    }


# ── Apply preset ──────────────────────────────────────────────────────────────

@router.post("/preset/{preset_name}")
def apply_preset(preset_name: str):
    """
    Apply a configuration preset atomically.

    Available presets:
    - **cloud** — disable SportyBet ingest (blocked on cloud), enable SofaScore-only pipeline
    - **local** — enable all toggleable pipelines
    - **off**   — disable all toggleable pipelines
    """
    from app.pipeline_registry import PRESETS, PIPELINE_MAP
    from app.league_memory import set_engine_status
    from app.activity_log import record_activity

    if preset_name not in PRESETS:
        raise HTTPException(
            status_code=404,
            detail=f"Preset '{preset_name}' not found. Available: {sorted(PRESETS.keys())}",
        )

    changes = PRESETS[preset_name]
    applied: list[dict[str, Any]] = []

    for engine_id, status in changes.items():
        set_engine_status(engine_id, status)
        pipeline = PIPELINE_MAP.get(engine_id)
        applied.append({
            "engine_id": engine_id,
            "label": pipeline.label if pipeline else engine_id,
            "status": status,
            "enabled": status == "active",
        })

    record_activity(
        f"Pipeline preset '{preset_name}' applied ({len(applied)} pipelines changed)",
        job="pipeline_control",
        status="ok",
        details={"preset": preset_name, "changes": applied},
    )

    return {
        "status": "success",
        "preset": preset_name,
        "applied_count": len(applied),
        "changes": applied,
    }
