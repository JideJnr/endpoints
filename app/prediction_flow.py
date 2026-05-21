from __future__ import annotations

from typing import Any

from app.ai_brain import oversee_prediction
from app.enriched_prediction import predict_enriched_match, prediction_readiness
from app.league_memory import record_prediction
from app.prediction_audit import build_deferred_prediction_audit, build_prediction_audit


class PredictionDeferred(Exception):
    """Raised when a match does not meet the prediction data contract."""

    def __init__(self, readiness: dict[str, Any]):
        self.readiness = readiness
        missing = ", ".join(readiness.get("missing") or [])
        super().__init__(missing or "prediction data is not ready")


def predict_and_record_enriched(
    doc: dict[str, Any],
    *,
    match_id: str | None = None,
    match_date: str | None = None,
    source: str = "enriched_ensemble",
    attach_brain: bool = False,
) -> dict[str, Any]:
    """
    Single prediction write path for enriched buffer documents.

    History is append-only. This helper only decides whether the current document
    is ready, builds the prediction, optionally applies the AI brain adjustment,
    and records the immutable prediction row for future grading/learning.
    """
    readiness = prediction_readiness(doc)
    doc["prediction_readiness"] = readiness
    if not readiness.get("ready"):
        raise PredictionDeferred(readiness)

    prediction = predict_enriched_match(doc)
    if attach_brain:
        _attach_brain_review(prediction, doc)
    prediction["audit"] = build_prediction_audit(prediction, doc)

    resolved_match_id = str(
        match_id
        or prediction.get("match_id")
        or doc.get("sportybet_id")
        or doc.get("id")
        or ""
    )
    record_prediction({
        **prediction,
        "match_id": resolved_match_id,
        "sportybet_id": prediction.get("sportybet_id") or resolved_match_id,
        "sofascore_id": prediction.get("sofascore_id") or doc.get("sofascore_id") or ((doc.get("sofascore_detail") or {}).get("id")),
        "match_date": prediction.get("match_date") or match_date or doc.get("match_date"),
        "source": source,
    })
    return prediction


def apply_prediction_state(
    doc: dict[str, Any],
    *,
    match_id: str | None = None,
    match_date: str | None = None,
    source: str = "enriched_ensemble",
    attach_brain: bool = False,
) -> dict[str, Any]:
    """
    Apply the single prediction state transition used by workers and endpoints.

    Prediction history remains append-only. The mutable buffer document only
    carries the current state: predicted, deferred, or error.
    """
    try:
        prediction = predict_and_record_enriched(
            doc,
            match_id=match_id,
            match_date=match_date,
            source=source,
            attach_brain=attach_brain,
        )
        readiness = doc.get("prediction_readiness") or prediction.get("prediction_readiness") or prediction_readiness(doc)
        doc["prediction"] = prediction
        doc["prediction_error"] = None
        doc["prediction_readiness"] = readiness
        return {
            "status": "predicted",
            "prediction": prediction,
            "readiness": readiness,
            "message": "Prediction completed",
        }
    except PredictionDeferred as exc:
        readiness = exc.readiness
        message = prediction_deferred_message(readiness)
        doc["prediction"] = None
        doc["prediction_error"] = message
        doc["prediction_readiness"] = readiness
        doc["prediction_audit"] = build_deferred_prediction_audit(doc, readiness)
        return {
            "status": "deferred",
            "prediction": None,
            "readiness": readiness,
            "audit": doc["prediction_audit"],
            "message": message,
        }
    except Exception as exc:
        readiness = doc.get("prediction_readiness") or prediction_readiness(doc)
        message = f"Prediction failed: {exc}"
        doc["prediction"] = None
        doc["prediction_error"] = message
        doc["prediction_readiness"] = readiness
        return {
            "status": "error",
            "prediction": None,
            "readiness": readiness,
            "message": message,
            "error": str(exc),
        }


def prediction_deferred_message(readiness: dict[str, Any]) -> str:
    missing = ", ".join(readiness.get("missing") or [])
    return "Prediction deferred until full signal is ready" + (f": {missing}" if missing else "")


def _attach_brain_review(prediction: dict[str, Any], detail: dict[str, Any]) -> None:
    brain = oversee_prediction(prediction, detail)
    prediction["ai_brain"] = brain
    adjustment = int(brain.get("confidence_adjustment") or 0)
    if adjustment:
        for pick in prediction.get("picks") or []:
            pick["confidence"] = max(1, min(95, int(pick.get("confidence", 50)) + adjustment))
    prediction.setdefault("signals", []).append({
        "name": "ai_brain_review",
        "value": brain,
        "impact": adjustment,
    })
