# predictx/app/llm.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.llm import *  # re-export full public API
from app.ai.llm import (
    get_llm,
    get_fast_llm,
    is_groq_available,
)
