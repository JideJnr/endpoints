from __future__ import annotations

from typing import Any

from app.storage.db import db_conn
from app.ai.ai_brain import oversee_prediction
from app.enrichment.enriched_prediction import predict_enriched_match, prediction_readiness
from app.storage.db import DB_PATH
from app.storage.league_memory import record_deferred_prediction_decision, record_prediction
from app.monitoring.prediction_audit import build_deferred_prediction_audit, build_prediction_audit
from app.utils.match_state import classify_match_state
from app.config.config import get_settings

import sqlite3


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
    use_llm_pipeline: bool | None = False,
    prebuilt_prediction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Single prediction write path for enriched buffer documents.

    History is append-only. This helper only decides whether the current document
    is ready, builds the prediction, optionally applies the AI brain adjustment,
    and records the immutable prediction row for future grading/learning.

    prebuilt_prediction: pass an already-built prediction dict (with a "picks"
    list, as produced by build_pick()) to record it as-is, instead of deriving
    one here via the LLM pipeline or the deterministic ensemble. Used by
    run_ai_prediction_with_fallback (app/ai/ai_prediction_pipeline.py), which
    runs its own evidence-specialist + decider pipeline and must not trigger a
    second, independent LLM pipeline run in here.
    """
    readiness = prediction_readiness(doc)
    doc["prediction_readiness"] = readiness
    if not readiness.get("ready"):
        raise PredictionDeferred(readiness)

    if prebuilt_prediction is not None:
        prediction = prebuilt_prediction
        if attach_brain and "ai_brain" not in prediction:
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
            "engine": "ai_llm" if prediction.get("prediction_source") == "llm_pipeline" else "deterministic",
        })
        return prediction

    # Use LLM pipeline if requested and available
    if use_llm_pipeline:
        from app.ai.llm_pipeline import run_llm_pipeline
        from app.config.config import get_settings as _gs
        if _gs().openrouter_api_key:
            prediction = run_llm_pipeline(doc, attach_brain=attach_brain)
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
                "engine": "ai_llm",
            })
            return prediction

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
        "engine": "deterministic",
    })
    return prediction


def apply_prediction_state(
    doc: dict[str, Any],
    *,
    match_id: str | None = None,
    match_date: str | None = None,
    source: str = "enriched_ensemble",
    attach_brain: bool = False,
    allow_repeat: bool = False,
    use_llm_pipeline: bool | None = False,
    prebuilt_prediction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Apply the single prediction state transition used by workers and endpoints.

    Prediction history remains append-only. The mutable buffer document only
    carries the current state: predicted, deferred, or error.
    """
    try:
        resolved_match_id = str(match_id or doc.get("sportybet_id") or doc.get("id") or doc.get("match_id") or "")
        readiness = doc.get("prediction_readiness") or prediction_readiness(doc)
        doc["prediction_readiness"] = readiness
        prediction_mode = _prediction_mode_from_readiness(readiness)
        cooldown_engine = (
            "ai_llm"
            if (prebuilt_prediction or {}).get("prediction_source") == "llm_pipeline" or use_llm_pipeline
            else "deterministic"
        )
        if resolved_match_id and not allow_repeat:
            cooldown_minutes = 3 if prediction_mode == "live" else 180
            if (
                prediction_mode == "live"
                and (doc.get("sofascore_id") or (doc.get("sofascore_detail") or {}).get("id"))
                and "sofascore" in (readiness.get("live_data_sources") or [])
            ):
                cooldown_minutes = 2
            recent = _recent_ungraded_prediction(
                resolved_match_id, minutes=cooldown_minutes, prediction_mode=prediction_mode, engine=cooldown_engine
            )
            if recent:
                return {
                    "status": "skipped",
                    "prediction": None,
                    "readiness": readiness,
                    "message": f"Skipped prediction (cooldown {cooldown_minutes}m): already predicted recently",
                    "skip_reason": "already_predicted_recently",
                    "existing": recent,
                }
        prediction = predict_and_record_enriched(
            doc,
            match_id=match_id,
            match_date=match_date,
            source=_prediction_source(source, readiness),
            attach_brain=attach_brain,
            use_llm_pipeline=use_llm_pipeline,
            prebuilt_prediction=prebuilt_prediction,
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
        record_deferred_prediction_decision(
            doc=doc,
            readiness=readiness,
            audit=doc["prediction_audit"],
            source=source,
            reason=message,
        )
        return {
            "status": "deferred",
            "prediction": None,
            "readiness": readiness,
            "audit": doc["prediction_audit"],
            "message": message,
        }
    except Exception as exc:
        readiness = doc.get("prediction_readiness") or prediction_readiness(doc)
        doc["prediction_readiness"] = readiness
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


def _recent_ungraded_prediction(
    match_id: str, *, minutes: int, prediction_mode: str = "prematch", engine: str = "deterministic"
) -> dict[str, Any] | None:
    """Return the most recent ungraded prediction for match_id within a time window.

    Scoped to `engine`: this cooldown exists to stop one engine from
    re-predicting the same match over and over, not to stop a SECOND engine
    from also having a say. Without the engine filter, the deterministic
    engine (which fires immediately on every enrichment pass) would always
    win this check and permanently block the AI pipeline's attempt at the
    same match -- the AI would burn a real LLM call and then have its result
    silently discarded by this gate. Each engine gets its own independent
    cooldown clock against itself.
    """
    if not match_id or minutes <= 0:
        return None
    with db_conn(timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            select id, created_at, pick_type, selection, confidence, source, prediction_mode
            from prediction_history
            where match_id = ?
              and coalesce(prediction_mode, 'prematch') = ?
              and coalesce(engine, 'deterministic') = ?
              and graded_at is null
              and pick_type != 'no_bet'
              and datetime(created_at) >= datetime('now', ?)
            order by datetime(created_at) desc, id desc
            limit 1
            """,
            (match_id, prediction_mode, engine, f"-{int(minutes)} minutes"),
        ).fetchone()
        if not row:
            return None
        return dict(row)


def _prediction_mode_from_readiness(readiness: dict[str, Any]) -> str:
    mode = readiness.get("prediction_mode")
    if mode in {"prematch", "live"}:
        return str(mode)
    if readiness.get("is_live") and readiness.get("live_data_sources"):
        return "live"
    return "prematch"


def _prediction_source(default: str, readiness: dict[str, Any]) -> str:
    if default != "enriched_ensemble":
        return default
    data_source = readiness.get("data_source")
    if data_source == "sportybet":
        return "sportybet_market_signal"
    if data_source == "sofascore":
        return "sofascore_form_signal"
    return default


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



