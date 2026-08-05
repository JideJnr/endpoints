# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.sofa_pipeline — redirects to app.data_clients.sofa_pipeline.
This file will be removed in v2.0. Update imports to: from app.data_clients.sofa_pipeline import ...
"""
from app.data_clients.sofa_pipeline import *  # noqa: F401, F403
from app.data_clients.sofa_pipeline import (  # noqa: F401
    SOFA_ID_PREFIX,
    ENRICH_WORKERS,
    ENGINE_STATE_ID,
    get_sofa_pipeline_mode,
    set_sofa_pipeline_mode,
    _sofa_event_to_buffer_doc,
    ingest_from_sofascore,
    _get_sofa_buffer_matches,
    enrich_sofa_pipeline,
    run_sofa_pipeline_cycle,
)
