# noqa: F401  # DEPRECATED shim — see migration_checklist.md
# This file is a compatibility shim. The real implementation lives in:
#   predictx/app/risk/kelly.py
# This shim will be removed in v2.0.

from app.risk.kelly import *  # noqa: F401,F403
from app.risk.kelly import (  # noqa: F401
    kelly_fraction,
    kelly_for_prediction,
)
