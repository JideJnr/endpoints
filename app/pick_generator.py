# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/risk/pick_generator.py
# This shim will be removed in v2.0.

from app.risk.pick_generator import *  # noqa: F401,F403
from app.risk.pick_generator import (  # noqa: F401
    CONFIDENCE_THRESHOLDS,
    PickGenerator,
    generate_picks,
    generate_optimized_slip,
)
