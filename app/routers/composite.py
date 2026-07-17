"""
Composite Endpoints
-------------------
Single-call endpoints that aggregate data from multiple sources so the
frontend never needs to fan out parallel requests on page load.

  GET /composite/prediction-dashboard   — predictions + upcoming + performance + roi + clv
  GET /composite/analytics-dashboard   — performance + roi + clv + longshots
  GET /agent/value-bets-full            — value bets (cleanup runs in background, not blocking)
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from fastapi import APIRouter, Query

_logger = logging.getLogger(__name__)
router = APIRouter(tags=["composite"])


# ── Prediction Dashboard — 5 → 1 ──────────────────────────────────────────────

@router.get("/composite/prediction-dashboard")
def get_prediction_dashboard():
    """
    Single endpoint that replaces the 5 parallel calls on the prediction dashboard:
      - GET /predictions/today
      - GET /matches/upcoming-enriched-predicted
      - GET /agent/analytics/performance
      - GET /agent/analytics/roi
      - GET /analytics/clv?days=30

    Each section is fetched concurrently. A section that fails returns null
    so the page still renders with partial data.
    """
    from app.routers.frontend import get_predictions_today, get_upcoming_enriched_predicted, get_clv_analytics
    from app.routers.agent import get_performance_analytics, get_roi_analysis

    def _safe(fn, label: str) -> Any:
        try:
            return fn()
        except Exception as exc:
            _logger.warning("composite/prediction-dashboard: %s failed: %s", label, exc)
            return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_safe, get_predictions_today,          "predictions"): "predictions",
            pool.submit(_safe, get_upcoming_enriched_predicted, "upcoming"):   "upcoming",
            pool.submit(_safe, get_performance_analytics,       "performance"):"performance",
            pool.submit(_safe, get_roi_analysis,                "roi"):        "roi",
            pool.submit(_safe, lambda: get_clv_analytics(days=30), "clv"):    "clv",
        }
        results: dict[str, Any] = {}
        for future in as_completed(futures):
            key = futures[future]
            results[key] = future.result()

    return {
        "status": "success",
        "predictions": results.get("predictions"),
        "upcoming":    results.get("upcoming"),
        "performance": results.get("performance"),
        "roi":         results.get("roi"),
        "clv":         results.get("clv"),
    }


# ── Analytics Dashboard — 4 → 1 ───────────────────────────────────────────────

@router.get("/composite/analytics-dashboard")
def get_analytics_dashboard(days: int = Query(default=30, ge=1, le=365)):
    """
    Single endpoint that replaces the 4 parallel calls on the analytics page:
      - GET /agent/analytics/performance
      - GET /agent/analytics/roi
      - GET /analytics/clv?days=30
      - GET /analytics/signal-matches?signal_name=consensus_longshot_value&limit=80

    Each section is fetched concurrently. A section that fails returns null.
    """
    from app.routers.frontend import get_clv_analytics, get_signal_matches
    from app.routers.agent import get_performance_analytics, get_roi_analysis

    def _safe(fn, label: str) -> Any:
        try:
            return fn()
        except Exception as exc:
            _logger.warning("composite/analytics-dashboard: %s failed: %s", label, exc)
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_safe, get_performance_analytics,                        "performance"): "performance",
            pool.submit(_safe, get_roi_analysis,                                 "roi"):         "roi",
            pool.submit(_safe, lambda: get_clv_analytics(days=days),            "clv"):         "clv",
            pool.submit(_safe, lambda: get_signal_matches(
                signal_name="consensus_longshot_value",
                result="",
                limit=80,
            ),                                                                    "longshots"):  "longshots",
        }
        results: dict[str, Any] = {}
        for future in as_completed(futures):
            key = futures[future]
            results[key] = future.result()

    return {
        "status": "success",
        "performance": results.get("performance"),
        "roi":         results.get("roi"),
        "clv":         results.get("clv"),
        "longshots":   results.get("longshots"),
    }
