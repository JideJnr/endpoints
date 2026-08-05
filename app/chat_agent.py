# predictx/app/chat_agent.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.ai.chat_agent import *  # re-export full public API
from app.ai.chat_agent import (
    ChatIntent,
    run_chat_prediction,
    parse_chat_intent,
    predict_no_next_goal,
    predict_next_goal,
    predict_next_team_to_score,
    format_chat_answer,
)
