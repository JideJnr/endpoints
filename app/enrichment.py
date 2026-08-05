# predictx/app/enrichment.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.enrichment.enrichment import *  # noqa: F401
from app.enrichment.enrichment import (
    FUZZY_THRESHOLD,
    LLM_FALLBACK_THRESHOLD,
    DETAIL_WORKERS,
    WEB_WORKERS,
    JUNK_MARKERS,
    run_enrichment,
)  # noqa: F401
