# predictx/app/prediction_flow.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.utils.prediction_flow import *  # re-export full public API
from app.utils.prediction_flow import (
    PredictionDeferred,
    predict_and_record_enriched,
    apply_prediction_state,
    prediction_deferred_message,
)
