# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/risk/validation_gate.py
# This shim will be removed in v2.0.

from app.risk.validation_gate import *  # noqa: F401,F403
from app.risk.validation_gate import (  # noqa: F401
    evaluate_promotion_gate,
    MIN_CALIBRATION_SAMPLES,
    MIN_CLV_SAMPLES,
    MAX_CALIBRATION_GAP_POINTS,
    MAX_RECENT_LOSS_RATE,
    MAX_RECENT_LOSS_STREAK,
)
