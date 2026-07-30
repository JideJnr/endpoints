from __future__ import annotations

from typing import Any

from app.contextual_intelligence import build_contextual_intelligence
from app.match_state import classify_match_state
from app.prediction_audit import build_prediction_audit, build_deferred_prediction_audit
from app.time_context import match_time_context


def build_match_intelligence(doc: dict[str, Any]) -> dict[str, Any]:
    """Canonical match object for operations, UI, and debugging."""
    prediction = doc.get("prediction") if isinstance(doc.get("prediction"), dict) else None
    readiness = doc.get("prediction_readiness") or {}
    stored_contextual = (
        prediction.get("contextual_intelligence")
        if prediction and isinstance(prediction.get("contextual_intelligence"), dict)
        else {}
    )
    contextual = (
        stored_contextual
        if stored_contextual.get("learned_performance")
        else build_contextual_intelligence(doc, prediction or {})
    )
    try:
        from app.team_watcher import team_context_for_match
        team_watchers = team_context_for_match(doc)
    except Exception as exc:
        team_watchers = {"available": False, "error": str(exc)}
    return {
        "version": "match_intelligence_v1",
        "provider_ids": {
            "sportybet_id": str(doc.get("sportybet_id") or doc.get("id") or ""),
            "sofascore_id": doc.get("sofascore_id") or ((doc.get("sofascore_detail") or {}).get("id")),
        },
        "state": classify_match_state(doc),
        "time_context": doc.get("time_context") or match_time_context(doc),
        "enrichment": {
            "enriched_at": doc.get("enriched_at"),
            "data_sources": doc.get("data_sources") or {},
            "sportybet": {
                "available": bool(doc.get("sportybet_detail") or doc.get("raw_sporty")),
                "markets": len(doc.get("sportybet_markets") or doc.get("markets") or []),
                "status": doc.get("sportybet_data_status"),
            },
            "sofascore": {
                "matched": bool(doc.get("sofascore_id") or doc.get("sofascore_event")),
                "detail": bool(doc.get("sofascore_detail")),
                "status": doc.get("sofascore_match_status"),
            },
        },
        "prediction": {
            "ready": bool(readiness.get("ready")),
            "missing": readiness.get("missing") or [],
            "error": doc.get("prediction_error"),
            "current_pick": (prediction.get("picks") or [{}])[0] if prediction else None,
            "contextual": contextual,
            "audit": (
                prediction.get("audit")
                if prediction and isinstance(prediction.get("audit"), dict)
                else build_prediction_audit(prediction, doc)
                if prediction
                else build_deferred_prediction_audit(doc, readiness)
            ),
        },
        "learning": {
            "role_decision": (prediction or {}).get("learned_role_decision"),
            "regime": (prediction or {}).get("regime"),
            "risk_management": (prediction or {}).get("risk_management"),
            "team_watchers": team_watchers,
        },
        "lifecycle": doc.get("lifecycle") or {},
    }
