from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from typing import Any, Callable
from urllib import request as urllib_request

from app.activity_log import record_activity
from app.buffer import get_buffered_match, refresh_sporty_match_state, store_enriched
from app.enriched_prediction import prediction_readiness
from app.match_intelligence import build_match_intelligence
from app.match_state import classify_match_state
from app.prediction_flow import apply_prediction_state


MAX_AGENT_ITERATIONS = 10


@dataclass
class AgentAction:
    key: str
    purpose: str
    expected_result: str
    completion_condition: str
    retry_limit: int = 1
    failure_behavior: str = "stop"
    required: bool = True
    status: str = "pending"
    attempts: int = 0
    result: dict[str, Any] = field(default_factory=dict)


class AgentExecutionError(Exception):
    def __init__(self, message: str, trace: dict[str, Any]):
        self.trace = trace
        super().__init__(message)


def run_agentic_match_prediction(
    sportybet_id: str,
    *,
    attach_brain: bool = True,
    allow_repeat: bool = False,
    force_enrich: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Plan-first prediction execution for one buffered match.

    The agent chooses the minimum useful sequence, executes one action at a
    time, reevaluates after state changes, and uses bounded retries/deduping so
    prediction cannot spin forever.
    """
    objective = {
        "type": "predict_match",
        "match_id": str(sportybet_id),
        "final_objective": "Produce a fresh, auditable prediction only when the match data is ready.",
    }
    state: dict[str, Any] = {
        "match_id": str(sportybet_id),
        "doc": None,
        "refresh": None,
        "readiness": None,
        "prediction_state": None,
        "match_intelligence": None,
        "match_mode": None,
        "data_audit": None,
        "historical_intelligence": None,
        "reasoning_advice": None,
        "last_signature": None,
        "dry_run": bool(dry_run),
        "force_enrich": bool(force_enrich),
        "allow_repeat": bool(allow_repeat),
    }
    trace: dict[str, Any] = {
        "objective": objective,
        "reasoning_layer": _local_reasoning_layer(),
        "limits": {
            "max_iterations": MAX_AGENT_ITERATIONS,
            "duplicate_action_detection": True,
            "state_comparison_checks": True,
            "retry_thresholds": True,
            "cooldowns": "delegated to prediction_flow by match state",
        },
        "plan": [],
        "completed": [],
        "skipped": [],
        "failures": [],
        "started_at": _now(),
    }

    queue = _initial_plan(state)
    trace["local_llm_advice"] = _local_llm_plan_advice(objective, queue, state)
    seen_actions: set[str] = set()

    for iteration in range(1, MAX_AGENT_ITERATIONS + 1):
        _sync_plan(trace, queue)
        if not queue:
            break
        action = queue.pop(0)
        action_identity = f"{action.key}:{_state_signature(state)}"
        if action_identity in seen_actions:
            action.status = "skipped"
            action.result = {"reason": "duplicate_action_state"}
            trace["skipped"].append(asdict(action))
            continue
        seen_actions.add(action_identity)

        action.attempts += 1
        before = _state_signature(state)
        try:
            if dry_run:
                action.status = "planned"
                action.result = {"dry_run": True}
            else:
                _execute_action(action, state)
            after = _state_signature(state)
            action.result.setdefault("state_changed", before != after)
            action.status = action.status if action.status == "planned" else "completed"
            trace["completed"].append(asdict(action))
        except Exception as exc:
            action.status = "failed"
            action.result = {"error": str(exc)}
            trace["failures"].append(asdict(action))
            if action.attempts < action.retry_limit:
                queue.insert(0, action)
                continue
            if action.required or action.failure_behavior == "stop":
                trace["finished_at"] = _now()
                trace["status"] = "failed"
                raise AgentExecutionError(str(exc), trace) from exc

        if dry_run:
            continue
        queue = _reevaluate_queue(state, queue, action)
        if _is_objective_complete(state):
            break

    trace["finished_at"] = _now()
    if queue:
        trace["skipped"].extend(asdict(action) for action in queue)
    if len(trace["completed"]) >= MAX_AGENT_ITERATIONS:
        trace["status"] = "iteration_limit_reached"
    else:
        trace["status"] = _final_status(state)

    doc = state.get("doc") if isinstance(state.get("doc"), dict) else None
    if doc is not None:
        doc["agentic_execution"] = _compact_trace(trace)
        store_enriched(str(sportybet_id), doc)

    prediction_state = state.get("prediction_state") or {}
    return {
        "status": trace["status"],
        "sportybet_id": str(sportybet_id),
        "prediction": prediction_state.get("prediction") or (doc or {}).get("prediction"),
        "prediction_state": prediction_state,
        "readiness": state.get("readiness"),
        "sporty_refresh": state.get("refresh"),
        "agent": trace,
    }


def _initial_plan(state: dict[str, Any]) -> list[AgentAction]:
    return [
        AgentAction(
            key="initialize_match_state",
            purpose="Load the match endpoint-equivalent detail and establish current status, time, phase, and provider IDs.",
            expected_result="Canonical match intelligence is available before prediction logic starts.",
            completion_condition="state.doc and state.match_intelligence are present.",
            retry_limit=1,
            failure_behavior="stop",
            required=True,
        ),
        AgentAction(
            key="decide_match_type",
            purpose="Branch execution between pre-match and live intelligence workflows.",
            expected_result="Match mode is classified as prematch or live.",
            completion_condition="state.match_mode is present.",
            retry_limit=1,
            failure_behavior="stop",
            required=True,
        ),
        AgentAction(
            key="data_availability_audit",
            purpose="Inspect available datasets, classify missing information by importance, and decide what can be skipped.",
            expected_result="Required and optional missing datasets are known.",
            completion_condition="state.data_audit is present.",
            retry_limit=1,
            failure_behavior="continue",
            required=True,
        ),
    ]


def _reevaluate_queue(state: dict[str, Any], queue: list[AgentAction], completed: AgentAction) -> list[AgentAction]:
    doc = state.get("doc") if isinstance(state.get("doc"), dict) else {}
    readiness = state.get("readiness") if isinstance(state.get("readiness"), dict) else {}
    planned = {item.key for item in queue}

    if completed.key == "data_availability_audit":
        mode = state.get("match_mode")
        if mode == "live" and "refresh_live_context" not in planned:
            queue.insert(0, AgentAction(
                key="refresh_live_context",
                purpose="Refresh live match details, odds, events, lineups, statistics, and momentum before prediction.",
                expected_result="Live context replaces stale pre-match assumptions.",
                completion_condition="fresh live doc is available and can be audited.",
                retry_limit=1,
                failure_behavior="stop",
                required=True,
            ))
        elif mode == "prematch" and "check_freshness" not in planned:
            queue.insert(0, AgentAction(
                key="check_freshness",
                purpose="Validate freshness of stored pre-match status/time/odds before querying more data.",
                expected_result="Fresh activity state or a safe fallback decision.",
                completion_condition="state.refresh is present.",
                retry_limit=1,
                failure_behavior="stop",
                required=True,
            ))

    if completed.key in {"check_freshness", "refresh_live_context", "evaluate_readiness", "enrich_context"}:
        if _needs_sofascore_reconciliation(doc, state) and "reconcile_sofascore" not in planned:
            queue.insert(0, AgentAction(
                key="reconcile_sofascore",
                purpose="Locate the corresponding SofaScore match, compare provider consistency, and enrich missing context.",
                expected_result="SofaScore detail/history/statistics are attached when available.",
                completion_condition="provider reconciliation has run or been safely skipped.",
                retry_limit=1,
                failure_behavior="continue",
                required=False,
            ))
        elif _needs_enrichment(doc, readiness, state) and "enrich_context" not in planned:
            queue.insert(0, AgentAction(
                key="enrich_context",
                purpose="Fetch missing or stale SofaScore/Sporty/web context required for high-assurance prediction.",
                expected_result="Stored enriched document contains usable detail, odds, state, and readiness inputs.",
                completion_condition="fresh doc is available and readiness can be reevaluated.",
                retry_limit=1,
                failure_behavior="stop",
                required=True,
            ))
        elif "historical_match_intelligence" not in planned:
            queue.insert(0, AgentAction(
                key="historical_match_intelligence",
                purpose="Extract reusable tactical, scoring, comeback, manager, lineup, and home/away patterns from previous SofaScore records.",
                expected_result="Compact historical intelligence is available or marked unavailable.",
                completion_condition="state.historical_intelligence is present.",
                retry_limit=1,
                failure_behavior="continue",
                required=False,
            ))

    if completed.key == "reconcile_sofascore":
        if "historical_match_intelligence" not in planned:
            queue.insert(0, AgentAction(
                key="historical_match_intelligence",
                purpose="Extract reusable tactical, scoring, comeback, manager, lineup, and home/away patterns from previous SofaScore records.",
                expected_result="Compact historical intelligence is available or marked unavailable.",
                completion_condition="state.historical_intelligence is present.",
                retry_limit=1,
                failure_behavior="continue",
                required=False,
            ))

    if completed.key == "historical_match_intelligence":
        if "local_llm_reasoning" not in planned:
            queue.insert(0, AgentAction(
                key="local_llm_reasoning",
                purpose="Ask the optional local LLM to summarize large context, detect anomalies, and recommend next planner action.",
                expected_result="Advisory reasoning is captured without overriding deterministic safety rules.",
                completion_condition="state.reasoning_advice is present.",
                retry_limit=1,
                failure_behavior="continue",
                required=False,
            ))

    if completed.key in {"local_llm_reasoning", "historical_match_intelligence"}:
        if "evaluate_readiness" not in planned:
            queue.insert(0, AgentAction(
                key="evaluate_readiness",
                purpose="Check whether enriched and historical information is sufficient for final prediction.",
                expected_result="Prediction readiness is known.",
                completion_condition="state.readiness is present.",
                retry_limit=1,
                failure_behavior="continue",
                required=True,
            ))

    if completed.key == "evaluate_readiness":
        readiness = state.get("readiness") if isinstance(state.get("readiness"), dict) else {}
        if not _needs_enrichment(doc, readiness, state) and "run_prediction" not in planned and not state.get("prediction_state"):
            queue.insert(0, AgentAction(
                key="run_prediction",
                purpose="Execute the existing prediction engine only after readiness gates pass.",
                expected_result="Prediction is produced, skipped by cooldown, or deferred with audit.",
                completion_condition="state.prediction_state is present.",
                retry_limit=1,
                failure_behavior="stop",
                required=True,
            ))

    if completed.key == "enrich_context" and "evaluate_readiness_after_enrichment" not in planned:
        queue.insert(0, AgentAction(
            key="evaluate_readiness_after_enrichment",
            purpose="Recheck state after new enrichment before prediction.",
            expected_result="Updated readiness reflects the fresh enriched document.",
            completion_condition="state.readiness is refreshed.",
            retry_limit=1,
            failure_behavior="stop",
            required=True,
        ))

    return queue


def _execute_action(action: AgentAction, state: dict[str, Any]) -> None:
    handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "initialize_match_state": _action_initialize_match_state,
        "decide_match_type": _action_decide_match_type,
        "data_availability_audit": _action_data_availability_audit,
        "check_freshness": _action_check_freshness,
        "refresh_live_context": _action_refresh_live_context,
        "reconcile_sofascore": _action_reconcile_sofascore,
        "historical_match_intelligence": _action_historical_match_intelligence,
        "local_llm_reasoning": _action_local_llm_reasoning,
        "evaluate_readiness": _action_evaluate_readiness,
        "evaluate_readiness_after_enrichment": _action_evaluate_readiness,
        "enrich_context": _action_enrich_context,
        "run_prediction": _action_run_prediction,
    }
    handler = handlers[action.key]
    action.result = handler(state)


def _action_initialize_match_state(state: dict[str, Any]) -> dict[str, Any]:
    doc = get_buffered_match(state["match_id"])
    if not doc:
        raise AgentExecutionError(f"Match {state['match_id']} not found in buffer", {"status": "not_found"})
    state["doc"] = doc
    intelligence = build_match_intelligence(doc)
    state["match_intelligence"] = intelligence
    provider_ids = intelligence.get("provider_ids") or {}
    match_state = intelligence.get("state") or {}
    return {
        "found": True,
        "match_date": doc.get("match_date"),
        "provider_ids": provider_ids,
        "state": {
            "mode": match_state.get("mode"),
            "is_live": bool(match_state.get("is_live")),
            "is_prematch": bool(match_state.get("is_prematch")),
            "is_finished": bool(match_state.get("is_finished")),
            "minute": match_state.get("minute") or match_state.get("played_minutes"),
            "period": match_state.get("period"),
        },
        "has_enrichment": bool(doc.get("enriched_at")),
    }


def _action_decide_match_type(state: dict[str, Any]) -> dict[str, Any]:
    doc = state.get("doc") if isinstance(state.get("doc"), dict) else {}
    match_state = classify_match_state(doc)
    if match_state.get("is_live"):
        mode = "live"
    elif match_state.get("is_finished") or match_state.get("is_terminal"):
        mode = "finished"
    else:
        mode = "prematch"
    state["match_mode"] = mode
    if mode == "finished":
        raise RuntimeError("Prediction blocked because match is already terminal")
    return {
        "match_mode": mode,
        "priority": "live_refresh_first" if mode == "live" else "freshness_then_selective_enrichment",
        "state_reason": match_state.get("reason"),
    }


def _action_data_availability_audit(state: dict[str, Any]) -> dict[str, Any]:
    doc = state.get("doc") if isinstance(state.get("doc"), dict) else {}
    audit = _data_availability_audit(doc, str(state.get("match_mode") or "prematch"))
    state["data_audit"] = audit
    return audit


def _action_check_freshness(state: dict[str, Any]) -> dict[str, Any]:
    refresh = refresh_sporty_match_state(state["match_id"])
    state["refresh"] = refresh
    if not refresh.get("active"):
        doc = state.get("doc") if isinstance(state.get("doc"), dict) else {}
        if _can_use_buffer_fallback(refresh, doc):
            refresh = {
                **refresh,
                "active": True,
                "fallback_used": True,
                "fallback_reason": "fresh_sporty_lookup_failed_used_buffered_sporty",
            }
            state["refresh"] = refresh
        else:
            raise RuntimeError("Prediction blocked because fresh SportyBet check says match is inactive")
    refreshed_doc = get_buffered_match(state["match_id"])
    if refreshed_doc:
        state["doc"] = refreshed_doc
    return {
        "active": bool(refresh.get("active")),
        "fallback_used": bool(refresh.get("fallback_used")),
        "reason": refresh.get("reason") or refresh.get("fallback_reason"),
    }


def _action_refresh_live_context(state: dict[str, Any]) -> dict[str, Any]:
    refresh = refresh_sporty_match_state(state["match_id"])
    state["refresh"] = refresh
    if not refresh.get("active"):
        raise RuntimeError("Live prediction blocked because fresh provider state is inactive")
    return _action_reconcile_sofascore(state)


def _action_reconcile_sofascore(state: dict[str, Any]) -> dict[str, Any]:
    from app.match_enrichment import enrich_buffered_match

    before = state.get("doc") if isinstance(state.get("doc"), dict) else {}
    result = enrich_buffered_match(state["match_id"], auto_predict=False)
    doc = get_buffered_match(state["match_id"]) or before
    state["doc"] = doc
    state["match_intelligence"] = build_match_intelligence(doc)
    state["data_audit"] = _data_availability_audit(doc, str(state.get("match_mode") or "prematch"))
    consistency = _provider_consistency(before, doc)
    return {
        "matched_sofascore": bool(result.get("matched_sofascore") or doc.get("sofascore_id")),
        "sofascore_id": doc.get("sofascore_id"),
        "match_source": result.get("match_source"),
        "provider_consistency": consistency,
        "data_audit": state["data_audit"],
    }


def _action_evaluate_readiness(state: dict[str, Any]) -> dict[str, Any]:
    doc = state.get("doc") if isinstance(state.get("doc"), dict) else {}
    readiness = prediction_readiness(doc)
    doc["prediction_readiness"] = readiness
    state["readiness"] = readiness
    return {
        "ready": bool(readiness.get("ready")),
        "assurance": readiness.get("assurance"),
        "missing": readiness.get("missing") or [],
        "needs_enrichment": _needs_enrichment(doc, readiness, state),
    }


def _action_enrich_context(state: dict[str, Any]) -> dict[str, Any]:
    from app.match_enrichment import enrich_buffered_match

    result = enrich_buffered_match(state["match_id"], auto_predict=False)
    doc = get_buffered_match(state["match_id"])
    if doc:
        state["doc"] = doc
        state["match_intelligence"] = build_match_intelligence(doc)
        state["data_audit"] = _data_availability_audit(doc, str(state.get("match_mode") or "prematch"))
    state["readiness"] = None
    return {
        "matched_sofascore": bool(result.get("matched_sofascore")),
        "minimum_enrichment_status": result.get("minimum_enrichment_status"),
        "has_detail": bool(result.get("has_detail")),
        "has_web_context": bool(result.get("has_web_context")),
    }


def _action_historical_match_intelligence(state: dict[str, Any]) -> dict[str, Any]:
    doc = state.get("doc") if isinstance(state.get("doc"), dict) else {}
    intelligence = _historical_intelligence(doc)
    state["historical_intelligence"] = intelligence
    doc["historical_match_intelligence"] = intelligence
    return intelligence


def _action_local_llm_reasoning(state: dict[str, Any]) -> dict[str, Any]:
    advice = _local_llm_context_advice(state)
    state["reasoning_advice"] = advice
    doc = state.get("doc") if isinstance(state.get("doc"), dict) else None
    if doc is not None:
        doc["local_reasoning_advice"] = advice
    return advice


def _action_run_prediction(state: dict[str, Any]) -> dict[str, Any]:
    doc = state.get("doc") if isinstance(state.get("doc"), dict) else {}
    result = apply_prediction_state(
        doc,
        match_id=state["match_id"],
        match_date=doc.get("match_date"),
        source="agentic_enriched_ensemble",
        attach_brain=True,
        allow_repeat=bool(state.get("allow_repeat")),
    )
    state["prediction_state"] = result
    state["readiness"] = result.get("readiness") or state.get("readiness")
    if result.get("prediction"):
        doc["prediction"] = result["prediction"]
        doc["prediction_error"] = None
    return {
        "status": result.get("status"),
        "message": result.get("message"),
        "skip_reason": result.get("skip_reason"),
        "has_prediction": bool(result.get("prediction")),
    }


def _needs_enrichment(doc: dict[str, Any], readiness: dict[str, Any], state: dict[str, Any]) -> bool:
    if state.get("force_enrich"):
        return True
    if readiness.get("ready"):
        return False
    missing = set(readiness.get("missing") or [])
    if missing.intersection({"sofascore_detail", "home_last_matches", "away_last_matches", "standings", "markets"}):
        return True
    if not doc.get("enriched_at"):
        return True
    return False


def _needs_sofascore_reconciliation(doc: dict[str, Any], state: dict[str, Any]) -> bool:
    audit = state.get("data_audit") if isinstance(state.get("data_audit"), dict) else {}
    missing_required = set(audit.get("missing_required") or [])
    if "sofascore_match" in missing_required or "sofascore_detail" in missing_required:
        return True
    if state.get("match_mode") == "live":
        return bool(missing_required.intersection({"live_statistics", "incidents", "lineups"}))
    return False


def _is_objective_complete(state: dict[str, Any]) -> bool:
    prediction_state = state.get("prediction_state")
    if not isinstance(prediction_state, dict):
        return False
    return prediction_state.get("status") in {"predicted", "skipped", "deferred", "error"}


def _final_status(state: dict[str, Any]) -> str:
    prediction_state = state.get("prediction_state") or {}
    status = prediction_state.get("status")
    if status == "predicted":
        return "success"
    if status == "skipped":
        return "skipped"
    if status == "deferred":
        return "deferred"
    if status == "error":
        return "error"
    return "planned" if state.get("dry_run") else "incomplete"


def _state_signature(state: dict[str, Any]) -> str:
    doc = state.get("doc") if isinstance(state.get("doc"), dict) else {}
    readiness = state.get("readiness") if isinstance(state.get("readiness"), dict) else {}
    payload = {
        "match_id": state.get("match_id"),
        "enriched_at": doc.get("enriched_at"),
        "predicted_at": doc.get("predicted_at"),
        "prediction_error": doc.get("prediction_error"),
        "sofascore_id": doc.get("sofascore_id"),
        "match_mode": state.get("match_mode"),
        "data_audit": state.get("data_audit"),
        "historical_ready": bool(state.get("historical_intelligence")),
        "readiness_ready": readiness.get("ready"),
        "readiness_missing": readiness.get("missing") or [],
        "refresh_active": (state.get("refresh") or {}).get("active") if isinstance(state.get("refresh"), dict) else None,
        "prediction_status": (state.get("prediction_state") or {}).get("status") if isinstance(state.get("prediction_state"), dict) else None,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _can_use_buffer_fallback(refresh: dict[str, Any], doc: dict[str, Any]) -> bool:
    if refresh.get("reason") != "not_found_in_fresh_sporty":
        return False
    if not (doc.get("raw_sporty") or doc.get("sportybet_id") or doc.get("id")):
        return False
    state = classify_match_state(doc)
    return not state.get("is_finished") and not state.get("is_terminal")


def _sync_plan(trace: dict[str, Any], queue: list[AgentAction]) -> None:
    trace["plan"] = [asdict(action) for action in queue]


def _compact_trace(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": trace.get("status"),
        "objective": trace.get("objective"),
        "limits": trace.get("limits"),
        "completed": trace.get("completed", [])[-8:],
        "skipped": trace.get("skipped", [])[-8:],
        "failures": trace.get("failures", [])[-5:],
        "finished_at": trace.get("finished_at"),
    }


def _data_availability_audit(doc: dict[str, Any], mode: str) -> dict[str, Any]:
    detail = doc.get("sofascore_detail") if isinstance(doc.get("sofascore_detail"), dict) else {}
    raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else {}
    markets = doc.get("sportybet_markets") or doc.get("markets") or raw_sporty.get("markets") or []
    datasets = {
        "sportybet_match": bool(doc.get("sportybet_detail") or raw_sporty or doc.get("sportybet_id") or doc.get("id")),
        "odds": bool(markets),
        "sofascore_match": bool(doc.get("sofascore_id") or doc.get("sofascore_event")),
        "sofascore_detail": bool(detail),
        "h2h": bool(detail.get("h2h")),
        "team_history": bool(detail.get("home_last_matches") and detail.get("away_last_matches")),
        "standings": bool(detail.get("standings") or doc.get("standings") or doc.get("league_table")),
        "web_context": bool((doc.get("web_context") or {}).get("snippets")),
        "live_statistics": bool(detail.get("statistics") or detail.get("match_statistics")),
        "incidents": bool(detail.get("incidents")),
        "lineups": bool(detail.get("lineups")),
    }
    required = ["sportybet_match", "odds", "sofascore_match", "sofascore_detail", "team_history"]
    optional = ["h2h", "standings", "web_context"]
    if mode == "live":
        required.extend(["live_statistics", "incidents"])
        optional.append("lineups")
    else:
        optional.extend(["lineups", "incidents", "live_statistics"])
    missing_required = [key for key in required if not datasets.get(key)]
    missing_optional = [key for key in optional if not datasets.get(key)]
    return {
        "mode": mode,
        "datasets": datasets,
        "required": required,
        "optional": optional,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "can_skip": [key for key in optional if key in missing_optional],
        "sufficiency": "sufficient" if not missing_required else "needs_context",
    }


def _provider_consistency(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    sporty_name = str(after.get("sportybet_name") or after.get("name") or before.get("name") or "")
    sofa_name = str(after.get("sofascore_name") or ((after.get("sofascore_event") or {}).get("name")) or "")
    sporty_start = after.get("start_time") or before.get("start_time")
    sofa_start = (after.get("sofascore_event") or {}).get("start_timestamp")
    return {
        "sporty_name": sporty_name,
        "sofascore_name": sofa_name,
        "name_consistent": bool(sofa_name and _simple_team_overlap(sporty_name, sofa_name) >= 0.5),
        "sporty_start_time": sporty_start,
        "sofascore_start_timestamp": sofa_start,
    }


def _simple_team_overlap(left: str, right: str) -> float:
    left_tokens = {token.lower() for token in left.replace("vs", " ").split() if len(token) > 2}
    right_tokens = {token.lower() for token in right.replace("vs", " ").split() if len(token) > 2}
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))


def _historical_intelligence(doc: dict[str, Any]) -> dict[str, Any]:
    detail = doc.get("sofascore_detail") if isinstance(doc.get("sofascore_detail"), dict) else {}
    home_history = list(detail.get("home_last_matches") or doc.get("home_last_matches") or [])
    away_history = list(detail.get("away_last_matches") or doc.get("away_last_matches") or [])
    h2h = detail.get("h2h") or {}
    return {
        "available": bool(home_history or away_history or h2h),
        "home": _team_history_summary(home_history),
        "away": _team_history_summary(away_history),
        "h2h": _h2h_summary(h2h),
        "patterns": _pattern_summary(home_history, away_history, h2h),
        "source": "sofascore_team_history_and_h2h",
    }


def _team_history_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    finished = [event for event in events if _score_pair(event) is not None]
    totals = []
    btts = 0
    comeback_markers = 0
    for event in finished[:12]:
        score = _score_pair(event)
        if score is None:
            continue
        home, away = score
        totals.append(home + away)
        if home > 0 and away > 0:
            btts += 1
        ht_home = ((event.get("score") or {}).get("home_ht"))
        ht_away = ((event.get("score") or {}).get("away_ht"))
        if ht_home is not None and ht_away is not None and (home - away) * (int(ht_home or 0) - int(ht_away or 0)) < 0:
            comeback_markers += 1
    sample = len(totals)
    return {
        "sample": sample,
        "avg_total_goals": round(sum(totals) / sample, 2) if sample else None,
        "over_1_5_rate": round(sum(1 for total in totals if total > 1.5) / sample, 2) if sample else None,
        "over_2_5_rate": round(sum(1 for total in totals if total > 2.5) / sample, 2) if sample else None,
        "btts_rate": round(btts / sample, 2) if sample else None,
        "comeback_markers": comeback_markers,
    }


def _h2h_summary(h2h: dict[str, Any]) -> dict[str, Any]:
    duel = h2h.get("team_duel") or h2h.get("teamDuel") or {}
    return {
        "available": bool(duel),
        "home_wins": duel.get("homeWins"),
        "away_wins": duel.get("awayWins"),
        "draws": duel.get("draws"),
    }


def _pattern_summary(home_history: list[dict[str, Any]], away_history: list[dict[str, Any]], h2h: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    home = _team_history_summary(home_history)
    away = _team_history_summary(away_history)
    for label, summary in (("home", home), ("away", away)):
        if (summary.get("over_2_5_rate") or 0) >= 0.65:
            patterns.append(f"{label}_recent_matches_high_total_goals")
        if (summary.get("btts_rate") or 0) >= 0.65:
            patterns.append(f"{label}_recent_btts_tendency")
        if (summary.get("avg_total_goals") or 0) <= 1.7 and summary.get("sample"):
            patterns.append(f"{label}_recent_low_scoring_tendency")
        if (summary.get("comeback_markers") or 0) >= 2:
            patterns.append(f"{label}_comeback_or_ht_ft_volatility")
    if _h2h_summary(h2h).get("available"):
        patterns.append("h2h_available")
    return patterns


def _score_pair(event: dict[str, Any]) -> tuple[int, int] | None:
    score = event.get("score") or {}
    try:
        home = score.get("home")
        away = score.get("away")
        if home is None or away is None:
            return None
        return int(home), int(away)
    except Exception:
        return None


def _local_reasoning_layer() -> dict[str, Any]:
    local_url = os.getenv("PREDICTX_LOCAL_LLM_URL", "").strip()
    return {
        "provider": "deterministic_local_reasoner",
        "role": "planning, sufficiency checks, loop prevention, and action queue optimization",
        "llm_optional": "A local/free LLM can advise the planner when PREDICTX_LOCAL_LLM_URL is configured.",
        "local_llm_configured": bool(local_url),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_llm_plan_advice(
    objective: dict[str, Any],
    queue: list[AgentAction],
    state: dict[str, Any],
) -> dict[str, Any]:
    """
    Optional local/free LLM advisory layer.

    The deterministic action planner remains authoritative. This hook is only
    allowed to summarize, flag missing context, or suggest a next action; it
    cannot execute tools or mutate the queue.
    """
    url = os.getenv("PREDICTX_LOCAL_LLM_URL", "").strip()
    if not url:
        return {"enabled": False, "reason": "PREDICTX_LOCAL_LLM_URL not configured"}
    model = os.getenv("PREDICTX_LOCAL_LLM_MODEL", "llama3.2:3b")
    prompt = {
        "objective": objective,
        "candidate_actions": [
            {
                "key": action.key,
                "purpose": action.purpose,
                "required": action.required,
                "failure_behavior": action.failure_behavior,
            }
            for action in queue
        ],
        "instruction": (
            "Return compact JSON with keys: sufficiency, missing_information, "
            "next_best_action, loop_risk. Do not ask to call endpoints repeatedly."
        ),
    }
    try:
        payload = json.dumps({
            "model": model,
            "prompt": json.dumps(prompt, default=str),
            "stream": False,
            "options": {"temperature": 0},
        }).encode("utf-8")
        req = urllib_request.Request(
            url.rstrip("/") + "/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=4) as resp:
            raw = json.loads(resp.read().decode("utf-8") or "{}")
        return {
            "enabled": True,
            "provider": "ollama_compatible",
            "model": model,
            "advice": raw.get("response"),
        }
    except Exception as exc:
        return {"enabled": False, "provider": "ollama_compatible", "error": str(exc)}


def _local_llm_context_advice(state: dict[str, Any]) -> dict[str, Any]:
    url = os.getenv("PREDICTX_LOCAL_LLM_URL", "").strip()
    compact = {
        "match_id": state.get("match_id"),
        "match_mode": state.get("match_mode"),
        "data_audit": state.get("data_audit"),
        "historical_intelligence": state.get("historical_intelligence"),
        "readiness": state.get("readiness"),
    }
    if not url:
        return {
            "enabled": False,
            "reason": "PREDICTX_LOCAL_LLM_URL not configured",
            "deterministic_summary": _deterministic_context_summary(compact),
        }
    model = os.getenv("PREDICTX_LOCAL_LLM_MODEL", "llama3.2:3b")
    prompt = {
        "context": compact,
        "instruction": (
            "Return compact JSON with keys: missing_context, anomalies, "
            "historical_vs_current_summary, momentum_summary, optimized_prediction_context, "
            "next_action. Safety rules are deterministic and cannot be overridden."
        ),
    }
    try:
        payload = json.dumps({
            "model": model,
            "prompt": json.dumps(prompt, default=str),
            "stream": False,
            "options": {"temperature": 0},
        }).encode("utf-8")
        req = urllib_request.Request(
            url.rstrip("/") + "/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=5) as resp:
            raw = json.loads(resp.read().decode("utf-8") or "{}")
        return {
            "enabled": True,
            "provider": "ollama_compatible",
            "model": model,
            "advice": raw.get("response"),
        }
    except Exception as exc:
        return {
            "enabled": False,
            "provider": "ollama_compatible",
            "error": str(exc),
            "deterministic_summary": _deterministic_context_summary(compact),
        }


def _deterministic_context_summary(compact: dict[str, Any]) -> dict[str, Any]:
    audit = compact.get("data_audit") if isinstance(compact.get("data_audit"), dict) else {}
    historical = compact.get("historical_intelligence") if isinstance(compact.get("historical_intelligence"), dict) else {}
    return {
        "sufficiency": audit.get("sufficiency"),
        "missing_required": audit.get("missing_required") or [],
        "missing_optional": audit.get("missing_optional") or [],
        "historical_patterns": historical.get("patterns") or [],
        "next_action_hint": "predict_if_ready_else_defer",
    }
