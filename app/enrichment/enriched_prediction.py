from __future__ import annotations

import math
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.storage.db import DB_PATH, _conn, db_conn
from app.storage.league_memory import _init_db

from app.models.dixon_coles import run_dixon_coles
from app.enrichment.contextual_intelligence import apply_contextual_adjustment, build_contextual_intelligence
from app.models.elo import elo_prediction
from app.models.ensemble import ensemble_prediction
from app.risk.kelly import kelly_fraction
from app.market.market_intent import classify_market_intent, selection_key as market_selection_key
from app.utils.match_state import classify_match_state
from app.models.poisson import run_poisson
from app.ai.prediction_agent import predict_sofascore_event, predict_sporty_match
from app.risk.risk_manager import apply_risk_controls
from app.market.season_stage import detect_season_stage
from app.signal_combinations import live_context_from_doc
from app.utils.time_context import match_time_context
from app.config.config import get_settings
from app.storage.league_memory._helpers import build_pick, build_sporty_doc

# ── Imports from extracted sub-modules ───────────────────────────────────────
from app.enrichment.signal_aggregator import (  # noqa: F401
    calculate_win_probabilities as _calculate_signal_probabilities,
    global_signal_stats as _global_signal_stats,
    prefetch_signal_stats as _prefetch_signal_stats,
    reset_signal_stats_cache as _reset_signal_stats_batch_cache,
    signal_value as _signal_value_helper,
    signal_metric as _signal_metric,
)
from app.enrichment.confidence_calibrator import (  # noqa: F401
    cap_market_confidence as _cap_market_confidence,
)
from app.utils.primitives import _to_float, _to_int
from app.storage.buffer import _is_not_started_period
from app.utils.match_helpers import _team_name, _played_seconds


LONGSHOT_MIN_DECIMAL_ODDS = 2.0

# ── Feature Importance Tracking ───────────────────────────────────────────────
# Tracks which signals/features actually drive correct predictions.
# Used by the learning system to weight signals more accurately.

_FEATURE_IMPORTANCE_CACHE: dict[str, dict[str, float]] = {}
_FEATURE_IMPORTANCE_CACHE_HITS = 0
_FEATURE_IMPORTANCE_CACHE_REFRESH = 100


def get_feature_importance(league: str = "") -> dict[str, float]:
    """Return per-signal win rates used as feature importance weights."""
    global _FEATURE_IMPORTANCE_CACHE, _FEATURE_IMPORTANCE_CACHE_HITS
    _FEATURE_IMPORTANCE_CACHE_HITS += 1
    cache_key = league or "__global__"
    if (
        not _FEATURE_IMPORTANCE_CACHE
        or _FEATURE_IMPORTANCE_CACHE_HITS % _FEATURE_IMPORTANCE_CACHE_REFRESH == 0
    ):
        try:
            _FEATURE_IMPORTANCE_CACHE = _load_feature_importance()
        except Exception:
            _FEATURE_IMPORTANCE_CACHE = {}
    return _FEATURE_IMPORTANCE_CACHE.get(cache_key, _FEATURE_IMPORTANCE_CACHE.get("__global__", {}))


def _load_feature_importance() -> dict[str, dict[str, float]]:
    """Load signal win rates from signal_outcomes table."""
    try:
        _init_db()
        with _conn(timeout=10) as conn:
            rows = conn.execute("""
                select signal_name,
                       count(*) as samples,
                       sum(case when result = 'win' then 1 else 0 end) as wins,
                       sum(case when result = 'loss' then 1 else 0 end) as losses
                from signal_outcomes
                where result in ('win', 'loss')
                  and signal_name is not null
                  and signal_name != ''
                group by signal_name
                having samples >= 5
                order by samples desc
            """).fetchall()
            global_weights: dict[str, float] = {}
            for row in rows:
                name = str(row["signal_name"] or "")
                samples = int(row["samples"] or 0)
                wins = int(row["wins"] or 0)
                if samples >= 5:
                    global_weights[name] = round(wins / samples, 4)
            return {"__global__": global_weights}
    except Exception:
        return {}


def _apply_feature_importance(signals: list[dict[str, Any]], league: str = "") -> list[dict[str, Any]]:
    """Attach feature importance scores to signals based on historical win rates."""
    importance = get_feature_importance(league)
    if not importance:
        return signals
    for signal in signals or []:
        name = str(signal.get("name") or "")
        if name and name in importance:
            signal["feature_importance"] = importance[name]
            signal["historical_win_rate"] = importance[name]
    return signals


def _aggregator_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal signal shape into SignalAggregator input shape."""
    converted: list[dict[str, Any]] = []
    for signal in signals or []:
        name = str(signal.get("name") or signal.get("signal_name") or "")
        if not name or name == "signal_aggregator":
            continue
        value = signal.get("impact")
        if value is None:
            value = signal.get("value") or signal.get("signal_value") or 0
        converted.append({
            "name": name,
            "value": value,
            "source": signal.get("source") or "enriched_prediction",
        })
    return converted


def _signal_aggregator_overall_impact(context: dict[str, Any]) -> int:
    if not context:
        return 0
    confidence = float(context.get("confidence") or 0)
    home = float(context.get("home_prob") or 0)
    away = float(context.get("away_prob") or 0)
    draw = float(context.get("draw_prob") or 0)
    spread = max(home, away, draw) - min(home, away, draw)
    return max(-4, min(4, round(spread * confidence * 10)))


def _signal_aggregator_pick_adjustment(pick: dict[str, Any], context: dict[str, Any]) -> int:
    if not context:
        return 0
    selection = str(pick.get("selection") or "").lower()
    probability = None
    if "home" in selection and "away" not in selection:
        probability = context.get("home_prob")
    elif "away" in selection:
        probability = context.get("away_prob")
    elif "draw" in selection:
        probability = context.get("draw_prob")
    if probability is None:
        return 0
    confidence = float(context.get("confidence") or 0)
    edge = float(probability or 0) - 0.45
    return max(-4, min(4, round(edge * confidence * 20)))


NOISY_SUPPORT_SIGNALS = {
    "goal_pressure",
    "dixon_coles_model",
    "poisson_model",
    "elo_model",
    "prediction_memory",
    "odds_edge",
}

MODIFIER_ONLY_SIGNALS = {"learned_signal_adjustment"}

BACKGROUND_CONTEXT_SIGNALS = {
    "sportybet_detail_available",
    "sportybet_markets_available",
    "sofascore_detail_available",
    "source_blend_full_signal_plus_sporty",
    "source_blend_full_signal",
    "source_blend_sportybet_market_signal",
    "source_blend_sportybet_prematch_minimum",
    "source_blend_sportybet_live_signal",
    "web_context",
}

RISK_SIGNALS = {"risk_management", "red_card_state"}


def _is_live_period(period: Any) -> bool:
    if not period:
        return False
    text = str(period).lower().strip()
    if _is_not_started_period(text):
        return False
    return text not in {"ft", "finished", "ended", "aet", "ap", "full time", "after penalties", "after extra time"}

def _readiness_reasons(
    missing: list[str],
    *,
    doc: dict[str, Any],
    home_sample: int,
    away_sample: int,
    has_sofascore_id: bool,
    is_live: bool,
) -> list[dict[str, str]]:
    descriptions = {
        "sofascore_detail": ("SofaScore detail is missing.", "call fetch_event_detail(sofascore_id)"),
        "home_recent_history": ("Home team has fewer than 3 finished recent matches.", "call fetch_team_history(home_team_id)"),
        "away_recent_history": ("Away team has fewer than 3 finished recent matches.", "call fetch_team_history(away_team_id)"),
        "sportybet_markets": ("SportyBet markets are missing.", "fetch SportyBet match markets"),
        "sportybet_detail": ("SportyBet detail is missing.", "fetch SportyBet match detail"),
        "time_context": ("Match time context is missing.", "compute match_time_context(match)"),
        "live_statistics": ("Live statistics are not available yet.", "call fetch_event_statistics(sofascore_id)"),
        "live_clock_mature": ("Live match clock is too early for fallback prediction.", "wait until at least 5 minutes are played"),
    }
    reasons: list[dict[str, str]] = []
    for item in missing:
        code = "sofascore_detail_missing" if item == "sofascore_detail" and has_sofascore_id else item
        description, resolution = descriptions.get(item, (f"Missing required field: {item}.", f"populate {item}"))
        if item == "home_recent_history":
            description = f"Home team history sample is {home_sample}, below the required 3."
        elif item == "away_recent_history":
            description = f"Away team history sample is {away_sample}, below the required 3."
        reasons.append({"code": code, "description": description, "resolution": resolution})
    if is_live and has_sofascore_id and "sofascore" not in _live_data_sources(doc, doc.get("sofascore_detail") or {}) and "live_statistics" not in missing:
        reasons.append({
            "code": "live_stats_not_fetched",
            "description": "Live match has a SofaScore ID but no live statistics were fetched.",
            "resolution": "call fetch_event_statistics(sofascore_id)",
        })
    return reasons


def _readiness_score(missing: list[str]) -> int:
    total_required = 8
    present = max(0, total_required - len(set(missing)))
    return round(100 * present / total_required)



def _provider_state(doc: dict[str, Any]) -> str:
    source = str(doc.get("data_source") or "").strip().lower()
    if source in {"sportybet", "sofascore", "both"}:
        return source
    sources = doc.get("data_sources") or {}
    sporty = sources.get("sportybet") if isinstance(sources, dict) else {}
    sofa = sources.get("sofascore") if isinstance(sources, dict) else {}
    sporty_available = bool((sporty or {}).get("available") or doc.get("sportybet_detail") or doc.get("raw_sporty"))
    sofa_available = bool((sofa or {}).get("available") or doc.get("sofascore_detail") or doc.get("sofascore_id"))
    if sporty_available and sofa_available:
        return "both"
    if sofa_available:
        return "sofascore"
    return "sportybet" if sporty_available else "both"


def _live_data_sources(doc: dict[str, Any], detail: dict[str, Any] | None = None) -> list[str]:
    sources: list[str] = []
    sporty_live = doc.get("live_data_sportybet") or {}
    sofa_live = doc.get("live_data_sofascore") or {}
    detail = detail or doc.get("sofascore_detail") or {}
    from app.utils.match_helpers import _played_seconds as _ps
    if sporty_live or (doc.get("sportybet_markets") and _ps(doc.get("played_seconds")) >= 300):
        sources.append("sportybet")
    if sofa_live or detail.get("statistics") or detail.get("match_statistics"):
        sources.append("sofascore")
    return sources


def _live_stats_source(doc: dict[str, Any], live_sources: list[str]) -> str:
    if "sofascore" in live_sources:
        return "sofascore_api"
    if "sportybet" in live_sources:
        return "sportybet_api"
    return "prematch_fallback" if doc.get("sofascore_id") else "none"


def prediction_readiness(doc: dict[str, Any]) -> dict[str, Any]:
    detail = doc.get("sofascore_detail") or {}
    home_history = detail.get("home_last_matches") or doc.get("home_recent_history") or []
    away_history = detail.get("away_last_matches") or doc.get("away_recent_history") or []
    home_sample = len([m for m in home_history if (m.get("status") or {}).get("type") == "finished"])
    away_sample = len([m for m in away_history if (m.get("status") or {}).get("type") == "finished"])
    has_sofascore_id = bool(doc.get("sofascore_id") or detail.get("id"))
    markets = doc.get("sportybet_markets") or doc.get("markets") or []
    period = doc.get("period") or (doc.get("sportybet_detail") or {}).get("period")
    is_live = bool(doc.get("is_live")) or _is_live_period(period)

    missing: list[str] = []
    if not detail:
        missing.append("sofascore_detail")
    if home_sample < 3:
        missing.append("home_recent_history")
    if away_sample < 3:
        missing.append("away_recent_history")
    if not markets:
        missing.append("sportybet_markets")
    if not (doc.get("sportybet_detail") or doc.get("raw_sporty") or markets):
        missing.append("sportybet_detail")
    if is_live and has_sofascore_id and "sofascore" not in _live_data_sources(doc, detail):
        missing.append("live_statistics")

    # The full enriched model requires SofaScore detail and both team histories.
    hard_missing = {"sofascore_detail", "home_recent_history", "away_recent_history"}
    ready = not any(item in hard_missing for item in missing)
    live_sources = _live_data_sources(doc, detail)
    return {
        "ready": ready,
        "missing": missing,
        "score": _readiness_score(missing),
        "assurance": "full" if ready and markets else "partial" if ready else "deferred",
        "has_sofascore_id": has_sofascore_id,
        "home_finished_history": home_sample,
        "away_finished_history": away_sample,
        "live_data_sources": live_sources,
        "live_stats_source": _live_stats_source(doc, live_sources) if is_live else None,
        "reasons": _readiness_reasons(
            missing,
            doc=doc,
            home_sample=home_sample,
            away_sample=away_sample,
            has_sofascore_id=has_sofascore_id,
            is_live=is_live,
        ),
    }


def predict_enriched_match(doc: dict[str, Any]) -> dict[str, Any]:
    """Run every available model against the richest document we have for a match."""
    _reset_signal_stats_batch_cache()  # reset per-prediction batch cache
    readiness = doc.get("prediction_readiness") or prediction_readiness(doc)
    doc["prediction_readiness"] = readiness
    if not readiness["ready"]:
        raise ValueError(f"Prediction deferred until full signal is ready: {', '.join(readiness['missing'])}")
    detail = doc.get("sofascore_detail") or {}
    home = detail.get("home_team") or detail.get("homeTeam") or {}
    away = detail.get("away_team") or detail.get("awayTeam") or {}
    home_id = home.get("id")
    away_id = away.get("id")

    # ── Data quality gate ─────────────────────────────────────────────────────
    # Don't waste a prediction record on matches with no real data.
    # Require at minimum a SofaScore detail block OR team history with sample_size >= 3.
    home_history = detail.get("home_last_matches") or []
    away_history = detail.get("away_last_matches") or []
    home_sample  = len([m for m in home_history if (m.get("status") or {}).get("type") == "finished"])
    away_sample  = len([m for m in away_history if (m.get("status") or {}).get("type") == "finished"])
    has_real_data = bool(detail) and home_sample >= 3 and away_sample >= 3
    if not has_real_data:
        if doc.get("sportybet_markets") or doc.get("markets"):
            return _fallback_market_prediction(doc, detail, home_sample, away_sample)
        raise ValueError(
            f"Insufficient data for prediction: "
            f"has_detail={bool(detail)} home_sample={home_sample} away_sample={away_sample}"
        )

    rules = _rules_prediction(doc, detail)
    venue_signal = _venue_form_signal(doc, detail)
    if venue_signal:
        rules.setdefault("signals", []).append(venue_signal)
    poisson = dixon = elo = None
    if home_id and away_id:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                "poisson": pool.submit(run_poisson, int(home_id), int(away_id), doc=doc),
                "dixon": pool.submit(run_dixon_coles, int(home_id), int(away_id), doc=doc),
                "elo": pool.submit(elo_prediction, str(home_id), str(away_id), doc=doc),
            }
            for model_name, future in futures.items():
                try:
                    result = future.result()
                    if model_name == "poisson":
                        poisson = result
                    elif model_name == "dixon":
                        dixon = result
                    else:
                        elo = _contextual_elo_prediction(doc, result)
                except Exception as exc:
                    if model_name == "poisson":
                        poisson = {"error": str(exc)}
                    elif model_name == "dixon":
                        dixon = {"error": str(exc)}
                    else:
                        elo = {"error": str(exc)}

    best_pick = (rules.get("picks") or [{}])[0]
    ensemble = ensemble_prediction(
        dixon if dixon and not dixon.get("error") else None,
        elo if elo and not elo.get("error") else None,
        poisson if poisson and not poisson.get("error") else None,
        int(best_pick.get("confidence") or 50),
        str(best_pick.get("selection") or best_pick.get("pick") or ""),
    )

    # ── Team prediction history signals ───────────────────────────────────────
    # Emit risk/boost signals based on each team's historical prediction
    # accuracy in this competition (queried from team_competitions).
    # These are appended to the signals list later, after it is initialised.
    _team_hist_signals = _team_history_signals(doc, detail)

    value_bets = _value_bets(
        doc,
        dixon if dixon and not dixon.get("error") else poisson,
        ensemble,
    )
    signals = list(rules.get("signals") or [])
    signals.extend(_team_hist_signals)
    signals.extend(_source_quality_signals(doc, readiness))
    if _provider_state(doc) == "sofascore" and not (doc.get("sportybet_markets") or doc.get("markets")):
        signals.append({"name": "no_odds_data", "value": True, "impact": -2})

    # Season stage awareness: when the season hasn't started or is just
    # beginning, standings are unreliable.  Add a signal so the prediction
    # engine and contextual intelligence can adjust confidence accordingly.
    standings = detail.get("standings") or doc.get("standings") or []
    season_stage = doc.get("season_stage") or detect_season_stage(standings)
    if season_stage:
        stage = season_stage.get("stage")
        if stage == "not_started":
            signals.append({
                "name": "season_not_started",
                "value": season_stage,
                "impact": -4,
            })
        elif stage == "beginning":
            signals.append({
                "name": "season_beginning",
                "value": season_stage,
                "impact": -2,
            })

    signals.extend(_model_signals(poisson, dixon, elo, ensemble, doc))
    finished_memory: dict[str, Any] = {}
    close_strength_context: dict[str, Any] = {}
    database_adj = 0
    try:
        from app.storage.league_memory import close_match_strength_context, weighted_finished_match_memory

        finished_memory = weighted_finished_match_memory(doc)
        close_strength_context = close_match_strength_context(doc)
        database_adj = _finished_memory_adjustment(ensemble, finished_memory)
        signals.append({
            "name": "finished_database_memory",
            "value": finished_memory,
            "impact": database_adj,
        })
        signals.append({
            "name": "close_match_strength_memory",
            "value": close_strength_context,
            "impact": _close_strength_adjustment(ensemble, close_strength_context),
        })
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "finished_memory_failed", exc)

    # ── User pick signal ──────────────────────────────────────────────────────
    # If the user has submitted a pick for this match, inject it as a signal
    # that nudges confidence toward their selection.
    try:
        from app.storage.league_memory import get_behavior_weighted_picks
        match_id_str = str(doc.get("sportybet_id") or doc.get("id") or "")
        if match_id_str:
            user_picks = [p for p in get_behavior_weighted_picks(match_id_str) if p.get("user_action") == "user_pick"]
            if user_picks:
                latest = user_picks[0]
                user_selection = str(latest.get("selection") or "")
                user_pick_type = str(latest.get("pick_type") or "match_result")
                # Determine alignment with ensemble prediction
                ensemble_pred = str((ensemble or {}).get("prediction") or "").lower()
                user_sel_lower = user_selection.lower()
                agrees = (
                    ("home" in user_sel_lower and "home" in ensemble_pred) or
                    ("away" in user_sel_lower and "away" in ensemble_pred) or
                    ("draw" in user_sel_lower and "draw" in ensemble_pred) or
                    ("over" in user_sel_lower and "over" in ensemble_pred) or
                    ("under" in user_sel_lower and "under" in ensemble_pred) or
                    ("btts" in user_sel_lower and "btts" in ensemble_pred)
                )
                impact = 4 if agrees else -2
                signals.append({
                    "name": "user_pick_signal",
                    "value": {
                        "selection": user_selection,
                        "pick_type": user_pick_type,
                        "agrees_with_model": agrees,
                        "recorded_at": latest.get("created_at"),
                    },
                    "impact": impact,
                    "role": "decision_driver" if agrees else "risk_signal",
                })
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "user_pick_signal_failed", exc)

    # ── Odds pattern signal ───────────────────────────────────────────────────
    odds_pattern_signal: dict[str, Any] = {}
    pattern_adj = 0
    odds_movement: dict[str, Any] = {}
    market_adj = 0
    try:
        from app.market.odds_pattern import pattern_signal
        match_id_str = str(doc.get("sportybet_id") or doc.get("id") or "")
        if match_id_str:
            odds_pattern_signal = pattern_signal(match_id_str)
            pattern_adj = int(odds_pattern_signal.get("confidence_adjustment") or 0)
            signals.append({
                "name":   "odds_pattern",
                "value":  odds_pattern_signal,
                "impact": pattern_adj,
            })
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "odds_pattern_failed", exc)

    try:
        from app.market.market import get_movement
        match_id_str = str(doc.get("sportybet_id") or doc.get("id") or "")
        if match_id_str:
            odds_movement = get_movement(match_id_str)
            market_adj = _market_adjustment(ensemble, odds_movement)
            signals.append({
                "name": "odds_progression",
                "value": {
                    "sharp_signal": odds_movement.get("sharp_signal"),
                    "strongest_pull": odds_movement.get("strongest_pull"),
                    "market_snapshots": odds_movement.get("market_snapshots"),
                },
                "impact": market_adj,
            })
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "market_movement_failed", exc)

    # ── Team Watcher Engine signal ────────────────────────────────────────────
    tw_signal = None
    has_tw_pick = False
    try:
        from app.team_watcher.team_watcher_engine import team_watcher_signal, record_tw_prediction
        tw_signal = team_watcher_signal(doc)
        signals.append(tw_signal)
        has_tw_pick = tw_signal.get("pick_type") not in (None, "no_bet")
        if has_tw_pick:
            match_id_str = str(doc.get("sportybet_id") or doc.get("id") or "")
            raw_sporty = doc.get("raw_sporty") if isinstance(doc.get("raw_sporty"), dict) else doc
            home_name_raw = raw_sporty.get("home_team") or doc.get("home_team") or ""
            home_key = "-".join("".join(ch.lower() if ch.isalnum() else " " for ch in str(home_name_raw)).split())
            record_tw_prediction(home_key, match_id_str, tw_signal)
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "team_watcher_engine_error", exc)
        tw_signal = None
        has_tw_pick = False

    # Team Watch signal (opponent tier, goal timing, signal combo history)
    try:
        from app.team_watcher.team_watcher import team_watch_signal as _tw_watch_signal
        tw_watch = _tw_watch_signal(doc)
        if tw_watch and (tw_watch.get("value") or {}).get("available"):
            signals.append(tw_watch)
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "team_watch_signal_error", exc)

    # ── SofaScore grade signal ────────────────────────────────────────────────
    longshot_signal = _consensus_longshot_value_signal(doc, ensemble, poisson, dixon, elo, rules, odds_movement)
    if longshot_signal:
        signals.append(longshot_signal)

    grade_adj = 0
    try:
        from app.data_clients.sofascore_grades import grade_signal_for_match
        match_id_str = str(doc.get("sportybet_id") or doc.get("id") or "")
        match_date_str = doc.get("match_date")
        grade_sig = grade_signal_for_match(detail, match_id=match_id_str, match_date=match_date_str)
        if grade_sig.get("available"):
            grade_adj = int(grade_sig.get("impact") or 0)
            signals.append(grade_sig)
    except Exception:
        pass

    # ── Learned signal weight adjustments ────────────────────────────────────
    # Apply self-learner knowledge: boost signals that historically work,
    # suppress signals that historically mislead — per league.
    learned_signal_adj = 0
    try:
        from app.monitoring.self_learner import get_signal_weights
        league = doc.get("tournament") or doc.get("category") or ""
        if isinstance(league, dict):
            league = league.get("name") or ""
        learned_weights = get_signal_weights(league)
        if learned_weights:
            for sig in signals:
                name = sig.get("name") or ""
                weight_adj = learned_weights.get(name)
                if weight_adj is not None and abs(weight_adj) > 0.1:
                    # Scale down positive learned boosts while reset-era
                    # duplicate pollution is still washing out of history.
                    contribution = _stabilized_learned_adjustment(weight_adj)
                    learned_signal_adj += contribution
            learned_signal_adj = _cap_learned_signal_adjustment(signals, learned_signal_adj)
            if learned_signal_adj != 0:
                signals.append({
                    "name": "learned_signal_adjustment",
                    "value": {
                        "league": league,
                        "adjustment": learned_signal_adj,
                        "signals_weighted": len(learned_weights),
                    },
                    "impact": learned_signal_adj,
                })
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "learned_signal_weights_failed", exc)

    # Time-decay for live matches
    minute = rules.get("minute") or 0
    match_state = classify_match_state(doc)
    is_live = bool(match_state.get("is_live"))
    live_data_sources = _live_data_sources(doc, detail) if is_live else []
    live_stats_available = bool(live_data_sources or _live_match_statistics(detail).get("has_stats")) if is_live else False
    from app.ai.prediction_agent import _apply_time_decay, _is_high_late_goal_league, _time_decay_multiplier
    late_goal_league = _is_high_late_goal_league(
        (doc.get("tournament") or "") + " " + (doc.get("category") or "")
    )

    market_picks = _market_selector_picks(doc, ensemble, poisson, dixon, finished_memory, rules)
    market_longshot_signal = _consensus_longshot_market_pick_signal(doc, market_picks, odds_movement)
    if market_longshot_signal and not (
        longshot_signal
        and ((longshot_signal.get("value") or {}).get("selection") == (market_longshot_signal.get("value") or {}).get("selection"))
    ):
        signals.append(market_longshot_signal)
    goal_selector_context = _goal_selector_context(ensemble, poisson, dixon, finished_memory, rules)
    if goal_selector_context:
        signals.append({
            "name": "goal_environment",
            "value": goal_selector_context,
            "impact": -6 if goal_selector_context.get("profile") == "hot" else -2 if goal_selector_context.get("profile") == "warm" else 2,
        })
    live_prior = {**ensemble, "probabilities": _blended_model_probabilities(ensemble, poisson, dixon)}
    live_picks = _live_inplay_picks(doc, detail, rules, live_prior, finished_memory, odds_movement) if is_live else []
    if live_picks:
        signals.append({
            "name": "live_inplay_state",
            "value": {
                "minute": minute,
                "score": _live_score(doc, detail),
                "markets": [pick.get("type") for pick in live_picks],
            },
            "impact": max(int(pick.get("confidence") or 0) for pick in live_picks) - 55,
        })
    picks = _combined_picks(rules, ensemble, value_bets, market_picks, doc)
    picks.extend(live_picks)
    if is_live and live_stats_available:
        _apply_live_stat_result_boosts(picks, detail, signals, minute)
    picks = _apply_time_decay(picks, minute, is_live, late_goal_league)

    # ── Confidence floor: ensure no non-no_bet pick is below 55 when ensemble ─
    # probability for the pick's outcome is at least 45 %.
    _DOMINANT_OUTCOME_THRESHOLD = 45.0
    _CONFIDENCE_FLOOR = 55
    probs = ensemble.get("probabilities") or {}
    _outcome_prob_map = {
        "home win": _to_float(probs.get("home_win")) or 0.0,
        "away win": _to_float(probs.get("away_win")) or 0.0,
        "draw": _to_float(probs.get("draw")) or 0.0,
    }
    for pick in picks:
        if pick.get("type") == "no_bet":
            continue
        raw = int(pick.get("confidence") or 50)
        sel_lower = str(pick.get("selection") or "").lower()
        ensemble_p = 0.0
        for outcome_key, op in _outcome_prob_map.items():
            if outcome_key in sel_lower:
                ensemble_p = op
                break
        # Only apply floor when pick has not yet been through calibration
        # (calibration may intentionally reduce confidence for poor bands)
        already_calibrated = isinstance(pick.get("calibration"), dict) and pick["calibration"].get("calibrated")
        floor_applied = ensemble_p >= _DOMINANT_OUTCOME_THRESHOLD and raw < _CONFIDENCE_FLOOR and not already_calibrated
        if floor_applied:
            pick["confidence"] = _CONFIDENCE_FLOOR
        pick["confidence_floor_applied"] = floor_applied
    candidate_pool = [dict(pick) for pick in picks]

    # ── Feature importance: attach historical win rates to signals ─────────────
    try:
        _fi_league = doc.get("tournament") or doc.get("category") or ""
        if isinstance(_fi_league, dict):
            _fi_league = _fi_league.get("name") or ""
        _apply_feature_importance(signals, str(_fi_league))
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("feature_importance_error", str(exc))

    signal_aggregator_context: dict[str, Any] = {}
    try:
        _agg_league = doc.get("tournament") or doc.get("category") or "__global__"
        if isinstance(_agg_league, dict):
            _agg_league = _agg_league.get("name") or "__global__"
        signal_aggregator_context = _calculate_signal_probabilities(
            _aggregator_signals(signals),
            league_key=str(_agg_league or "__global__"),
        )
        signals.append({
            "name": "signal_aggregator",
            "value": signal_aggregator_context,
            "impact": _signal_aggregator_overall_impact(signal_aggregator_context),
        })
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "signal_aggregator_failed", exc)

    # ── Calibration: adjust confidence based on historical win rates ──────────
    try:
        from app.enrichment.confidence_calibrator import calibrate_confidence, stake_multiplier
        from app.storage.league_memory import weighted_prediction_memory
        from app.market.regime import get_regime_for_doc, apply_regime_stake_cap

        regime = get_regime_for_doc(doc)

        for pick in picks:
            raw_conf = int(pick.get("confidence") or 50)
            cal = calibrate_confidence(pick.get("type") or "match_result", raw_conf)
            memory = weighted_prediction_memory(doc, pick.get("type"), pick.get("selection"))
            memory_adj = _stabilized_memory_adjustment(int(memory.get("confidence_adjustment") or 0))
            pick_learned_adj = _learned_signal_adjustment_for_pick(doc, signals, pick.get("type"))
            aggregator_adj = _signal_aggregator_pick_adjustment(pick, signal_aggregator_context)
            combo_memory = {}
            combo_adj = 0
            try:
                from app.storage.league_memory import weighted_signal_combination_memory
                combo_memory = weighted_signal_combination_memory(
                    doc,
                    signals,
                    pick.get("type"),
                    pick.get("selection"),
                    prediction_mode="live" if is_live else "prematch",
                    live_context=live_context_from_doc(doc, minute),
                )
                combo_adj = int(combo_memory.get("adjustment") or 0)
            except Exception:
                combo_memory = {}
            # Apply pattern and market movement adjustment on top.
            _raw_adj = pattern_adj + market_adj + memory_adj + database_adj + grade_adj + pick_learned_adj + combo_adj + aggregator_adj
            _raw_adj = max(-15, min(15, _raw_adj))  # cumulative cap prevents additive drift
            cal_conf = min(99, max(1, cal["adjusted_confidence"] + _raw_adj))
            # Apply regime tier penalty (Tier 4 gets -5)
            tier_penalty = {1: 0, 2: 0, 3: 0, 4: -5}.get(regime.tier, 0)
            cal_conf = min(99, max(1, cal_conf + tier_penalty))
            # Apply league accuracy adjustment — penalise leagues where the
            # system underperforms (win_rate < 50%), boost leagues where it
            # consistently outperforms (win_rate > 65%).  Requires at least 8
            # graded samples to trust the rate.  The team watcher and league
            # memory already carry this signal; this makes it explicit and
            # bidirectional at pick confidence level.
            #
            #  win_rate < 50%  →  penalty:  -(50 - wr) / 5   (max -10)
            #  win_rate > 65%  →  boost:    +(wr - 65) / 5   (max +10)
            #  50% ≤ wr ≤ 65%  →  no adjustment (neutral band)
            MAX_LEAGUE_ADJUSTMENT = 10
            try:
                from app.monitoring.self_learner import get_league_accuracy
                _league_key = doc.get("tournament") or doc.get("category") or ""
                if isinstance(_league_key, dict):
                    _league_key = _league_key.get("name") or ""
                _lacc = get_league_accuracy(str(_league_key))
                if _lacc.get("known"):
                    for _lt in (_lacc.get("by_pick_type") or []):
                        if _lt.get("pick_type") in (pick.get("type"), "__all__"):
                            _samples = int(_lt.get("samples") or 0)
                            _wr = float(_lt.get("win_rate") or 0)  # already in %
                            _league_adj = 0
                            if _samples >= 8:
                                if _wr < 50.0:
                                    # Underperforming league — reduce confidence
                                    _league_adj = -round((50.0 - _wr) / 5)
                                    _league_adj = max(-MAX_LEAGUE_ADJUSTMENT, _league_adj)
                                elif _wr > 65.0:
                                    # Outperforming league — boost confidence
                                    _league_adj = round((_wr - 65.0) / 5)
                                    _league_adj = min(MAX_LEAGUE_ADJUSTMENT, _league_adj)
                            if _league_adj != 0:
                                cal_conf = min(99, max(1, cal_conf + _league_adj))
                                pick.setdefault("calibration", {})
                                pick["calibration"]["league_accuracy_adj"] = _league_adj
                                pick["calibration"]["league_win_rate"] = _wr
                                pick["calibration"]["league_samples"] = _samples
                            break
            except Exception:
                pass
            # Apply TW_Signal confidence impact (bounded ±8)
            if tw_signal and has_tw_pick:
                tw_impact = max(-8, min(8, tw_signal.get("confidence_impact", 0)))
                cal_conf = max(1, min(99, cal_conf + tw_impact))
            pick["confidence"] = cal_conf

            raw_stake = stake_multiplier(pick.get("type") or "match_result", raw_conf)
            capped_stake = apply_regime_stake_cap(raw_stake, doc.get("tournament"), doc.get("category"))

            pick["calibration"] = {
                "raw_confidence":    raw_conf,
                "win_rate":          cal.get("win_rate"),
                "calibrated_probability": cal.get("calibrated_probability"),
                "probability_source": cal.get("probability_source"),
                "samples":           cal.get("samples"),
                "double_down":       cal.get("double_down", False),
                "stake_multiplier":  capped_stake,
                "calibrated":        cal.get("calibrated", False),
                "regime_tier":       regime.tier,
                "regime_name":       regime.name,
                "regime_stake_cap":  regime.stake_cap,
                "memory_weighting":   memory,
                "learned_signal_adjustment": pick_learned_adj,
                "signal_aggregator": signal_aggregator_context,
                "signal_aggregator_adjustment": aggregator_adj,
                "learned_signal_combination": combo_memory,
                "learned_signal_combination_adjustment": combo_adj,
                "confidence_floor_applied": bool(pick.get("confidence_floor_applied")),
                # Preserve league accuracy adj computed above (may have been set before this dict)
                "league_accuracy_adj": (pick.get("calibration") or {}).get("league_accuracy_adj"),
                "league_win_rate":     (pick.get("calibration") or {}).get("league_win_rate"),
                "league_samples":      (pick.get("calibration") or {}).get("league_samples"),
            }
            # ── Calibration gap monitoring ─────────────────────────────────────
            try:
                from app.enrichment.confidence_calibrator import compute_calibration_gap
                gap_info = compute_calibration_gap(pick.get("type") or "match_result", raw_conf)
                pick["calibration"]["gap"] = gap_info
                if gap_info.get("gap_severity") == "severe":
                    signals.append({
                        "name": "calibration_gap_severe",
                        "value": gap_info,
                        "impact": -8,
                        "reason": f"Confidence {raw_conf}% exceeds historical win rate by {gap_info['gap']} points",
                    })
                elif gap_info.get("gap_severity") == "moderate":
                    signals.append({
                        "name": "calibration_gap_moderate",
                        "value": gap_info,
                        "impact": -4,
                        "reason": f"Confidence {raw_conf}% exceeds historical win rate by {gap_info['gap']} points",
                    })
            except Exception as exc:
                from app.utils.health_counters import record_health_event
                record_health_event("calibration_gap_error", str(exc))
            if memory.get("blended_win_rate") is not None:
                signals.append({
                    "name": "prediction_memory",
                    "value": memory,
                    "impact": memory_adj,
                })
            if combo_memory.get("samples"):
                signals.append({
                    "name": "learned_signal_combination",
                    "value": combo_memory,
                    "impact": combo_adj,
                })
            # Prefer CLV-based stake sizing if enough data exists
            try:
                from app.risk.clv import clv_stake_multiplier
                clv_mult = clv_stake_multiplier(pick.get("type") or "match_result", raw_conf)
                if clv_mult != 1.0:
                    capped_clv = apply_regime_stake_cap(clv_mult, doc.get("tournament"), doc.get("category"))
                    pick["calibration"]["stake_multiplier"] = capped_clv
                    pick["calibration"]["stake_source"] = "clv"
                else:
                    pick["calibration"]["stake_source"] = "win_rate"
            except Exception as exc:
                from app.utils.health_counters import record_health_event
                record_health_event("enriched_prediction", "clv_stake_multiplier_failed", exc)
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "calibration_block_failed", exc)

    contextual_intelligence = build_contextual_intelligence(
        doc,
        signals=signals,
        picks=picks,
        odds_movement=odds_movement,
    )
    signals.append({
        "name": "contextual_intelligence",
        "value": contextual_intelligence,
        "impact": int(contextual_intelligence.get("confidence_adjustment") or 0),
    })
    picks = apply_contextual_adjustment(picks, contextual_intelligence)
    if contextual_intelligence.get("no_prediction_recommended") and max([int(pick.get("confidence") or 0) for pick in picks] or [0]) < 78:
        picks = [_pick("no_bet", "Avoid game", 50, "Contextual intelligence recommends no bet: " + "; ".join((contextual_intelligence.get("risk") or {}).get("reasons") or []))]

    _attach_goal_environment_evidence(picks, goal_selector_context if isinstance(goal_selector_context, dict) else None)
    picks = _curate_picks(picks, doc, signals=signals, odds_movement=odds_movement)
    rejected_picks = _rejected_pick_trace(candidate_pool, picks)
    _attach_stake_sizing(doc, picks)
    risk_management = apply_risk_controls(
        doc,
        picks,
        signals=signals,
        contextual_intelligence=contextual_intelligence,
        odds_movement=odds_movement,
        models={
            "poisson": poisson,
            "dixon_coles": dixon,
            "elo": elo,
            "ensemble": ensemble,
        },
        readiness=readiness,
    )
    signals.append({
        "name": "risk_management",
        "value": risk_management,
        "impact": -8 if risk_management.get("risk_level") == "high" else -3 if risk_management.get("risk_level") == "medium" else 1,
        "role": "risk_signal",
    })
    _signal_roles(signals)
    if picks and picks[0].get("type") == "no_bet" and picks[0].get("suppressed_picks"):
        rejected_picks.extend(_rejected_pick_trace(picks[0].get("suppressed_picks") or [], []))

    prediction = {
        "match_id": doc.get("sportybet_id") or doc.get("id") or detail.get("id"),
        "name": doc.get("sportybet_name") or doc.get("name") or detail.get("name"),
        "source": "enriched_ensemble",
        "match_date": doc.get("match_date"),
        "tournament": doc.get("tournament") or ((detail.get("tournament") or {}).get("name") if isinstance(detail.get("tournament"), dict) else None),
        "category": doc.get("category") or _detail_country(detail),
        "country": doc.get("category") or _detail_country(detail),
        "time_context": doc.get("time_context") or match_time_context(doc),
        "match_state": match_state,
        "is_live": is_live,
        "prediction_mode": "live" if (is_live and live_stats_available) else "prematch",
        "live_data_sources": live_data_sources,
        "live_stats_source": readiness.get("live_stats_source") or _live_stats_source(doc, live_data_sources),
        "live_statistics_summary": _live_match_statistics(detail).get("box_score") if live_stats_available else {},
        "data_source": _provider_state(doc),
        "teams": {
            "home": home or {"name": _team_name(doc, "home")},
            "away": away or {"name": _team_name(doc, "away")},
        },
        "rules": _evidence_rules(rules),
        "models": {
            "poisson": poisson,
            "dixon_coles": dixon,
            "elo": elo,
            "ensemble": ensemble,
            "finished_database_memory": finished_memory,
            "close_match_strength_memory": close_strength_context,
        },
        "web_context": doc.get("web_context") or {},
        "data_sources": doc.get("data_sources") or {},
        "sportybet_detail": doc.get("sportybet_detail") or {},
        "odds_movement": odds_movement,
        "value_bets": value_bets,
        "market_selector": market_picks,
        "live_inplay": live_picks,
        "signals": sorted(signals, key=lambda item: abs(item.get("impact") or 0), reverse=True),
        "contextual_intelligence": contextual_intelligence,
        "risk_management": risk_management,
        "learned_role_decision": (picks[0].get("learned_role_decision") if picks else None),
        "picks": picks,
        "candidate_pool_count": len(candidate_pool),
        "rejected_picks": rejected_picks,
        "time_decay_applied": is_live and minute >= 46,
        "time_decay_multiplier": _time_decay_multiplier(minute) if is_live else 1.0,
        "regime": _regime_info(doc),
        "data_quality": {
            "prediction_readiness": readiness,
            "has_sofascore_detail": bool(detail),
            "has_sportybet_detail": bool(doc.get("sportybet_detail") or doc.get("raw_sporty")),
            "has_sportybet_markets": bool(doc.get("sportybet_markets") or doc.get("markets")),
            "has_web_context": bool((doc.get("web_context") or {}).get("snippets")),
            "has_raw_sporty": bool(doc.get("raw_sporty")),
            "has_raw_sofascore": bool(doc.get("raw_sofascore_event") or doc.get("sofascore_event")),
            "manual_match": bool(doc.get("manual_match")),
            "live_stats_available": live_stats_available,
            "live_data_sources": live_data_sources,
        },
        "has_team_watcher_pick": has_tw_pick,
        **({
            "team_watcher_pick": {
                "selection": tw_signal.get("selection"),
                "confidence": tw_signal.get("confidence"),
                "models_agree": tw_signal.get("models_agree", False),
            },
        } if has_tw_pick and tw_signal else {}),
    }
    _finalize_prediction_output(prediction, ensemble)
    return prediction


def _compute_h2h_signals(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive H2H dominance signals from SofaScore ``last_meetings`` data.

    Takes up to 10 most recent entries from ``detail["last_meetings"]`` and
    computes home/away/draw dominance based on actual scorelines, correctly
    accounting for which team was the home side in each historical meeting.

    Returns one signal dict or an empty list.  Returns ``[]`` on any error so
    the caller is never interrupted.

    Signal names: ``h2h_home``, ``h2h_away``, ``h2h_draw``
    Source tag:   ``"sofascore_last_meetings"``

    Requirements: R13.1, R13.2, R13.3, R13.4, R13.5, R13.7
    """
    try:
        meetings = (detail.get("last_meetings") or [])[:10]
        if len(meetings) < 3:
            return []

        home_id = str((detail.get("home_team") or detail.get("homeTeam") or {}).get("id") or "")

        home_wins = 0
        away_wins = 0
        draws = 0

        for m in meetings:
            h_score = (m.get("homeScore") or {}).get("current")
            a_score = (m.get("awayScore") or {}).get("current")
            if h_score is None or a_score is None:
                continue
            try:
                h_score = int(h_score)
                a_score = int(a_score)
            except (TypeError, ValueError):
                continue
            # Determine whether the meeting's home team is the current home team
            meeting_home_id = str((m.get("homeTeam") or {}).get("id") or "")
            current_home_is_meeting_home = (meeting_home_id == home_id)

            if h_score > a_score:
                # Meeting home team won
                if current_home_is_meeting_home:
                    home_wins += 1
                else:
                    away_wins += 1
            elif h_score < a_score:
                # Meeting away team won
                if current_home_is_meeting_home:
                    away_wins += 1
                else:
                    home_wins += 1
            else:
                draws += 1

        total = home_wins + draws + away_wins
        if total < 3:
            return []

        home_ratio = home_wins / total
        away_ratio = away_wins / total

        if home_wins > away_wins and home_ratio >= 0.5:
            return [{
                "name": "h2h_home",
                "value": round(home_ratio, 2),
                "impact": round(home_ratio * 4),
                "source": "sofascore_last_meetings",
            }]
        elif away_wins > home_wins and away_ratio >= 0.5:
            return [{
                "name": "h2h_away",
                "value": round(away_ratio, 2),
                "impact": round(away_ratio * 4),
                "source": "sofascore_last_meetings",
            }]
        else:
            return [{
                "name": "h2h_draw",
                "value": round(draws / total, 2),
                "impact": 1,
                "source": "sofascore_last_meetings",
            }]
    except Exception:
        return []


def _rules_prediction(doc: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    if detail:
        try:
            official_state = classify_match_state(doc)
            rules_detail = detail
            status = detail.get("status") if isinstance(detail.get("status"), dict) else {}
            if not official_state.get("is_live") and status.get("type") == "inprogress":
                rules_detail = {
                    **detail,
                    "status": {**status, "type": "notstarted", "description": "Not started"},
                    "played_seconds": None,
                }
            result = predict_sofascore_event(
                rules_detail,
                rules_detail.get("home_last_matches") or [],
                rules_detail.get("away_last_matches") or [],
            )
            # Inject H2H signals derived from SofaScore last_meetings (R13).
            # Before appending each computed signal, check whether an existing
            # signal with the same name and equal or higher abs(impact) already
            # exists — if so, discard the computed one (R13.6).
            h2h_computed = _compute_h2h_signals(rules_detail)
            if h2h_computed:
                existing_signals = result.get("signals") or []
                for computed_sig in h2h_computed:
                    sig_name = computed_sig.get("name")
                    computed_impact = abs(computed_sig.get("impact") or 0)
                    already_covered = any(
                        s.get("name") == sig_name and abs(s.get("impact") or 0) >= computed_impact
                        for s in existing_signals
                    )
                    if not already_covered:
                        existing_signals.append(computed_sig)
                result["signals"] = existing_signals

            # Competition round analysis injection (R14.4–R14.7).
            # If competition_round_analysis is present on the doc, extract form
            # insight and optionally emit a competition_momentum signal.
            # audit["competition_context_applied"] = True whenever the key is
            # present, regardless of whether a momentum signal is emitted.
            try:
                import json as _json
                cra = doc.get("competition_round_analysis")
                if cra:
                    audit = result.setdefault("audit", {})
                    audit["competition_context_applied"] = True
                    analysis_text = cra.get("analysis_text") or "{}"
                    try:
                        cra_data = _json.loads(analysis_text)
                    except Exception:
                        cra_data = {}
                    top_table = cra_data.get("top_table") or []
                    # The top_table entries may be dicts with a "team" key or
                    # plain strings — normalise both.
                    top_teams = []
                    for entry in top_table[:2]:
                        if isinstance(entry, dict):
                            top_teams.append(str(entry.get("team") or "").lower())
                        else:
                            top_teams.append(str(entry).lower())
                    if top_teams:
                        home_name = str(
                            (rules_detail.get("home_team") or {}).get("name") or ""
                        ).lower()
                        away_name = str(
                            (rules_detail.get("away_team") or {}).get("name") or ""
                        ).lower()
                        home_in_top = home_name and any(
                            home_name in t or t in home_name for t in top_teams
                        )
                        away_in_top = away_name and any(
                            away_name in t or t in away_name for t in top_teams
                        )
                        # Emit a momentum signal only when exactly one team has a
                        # clear form advantage in the top 2.
                        if home_in_top and not away_in_top:
                            signals_list = result.setdefault("signals", [])
                            signals_list.append({
                                "name": "competition_momentum",
                                "value": {"direction": "home", "source": "competition_round_analysis"},
                                "impact": 1,
                            })
                        elif away_in_top and not home_in_top:
                            signals_list = result.setdefault("signals", [])
                            signals_list.append({
                                "name": "competition_momentum",
                                "value": {"direction": "away", "source": "competition_round_analysis"},
                                "impact": 1,
                            })
            except Exception:
                pass

            return result
        except Exception as exc:
            from app.utils.health_counters import record_health_event
            record_health_event("enriched_prediction", "rules_prediction_failed", exc)
    sporty_doc = build_sporty_doc(doc)
    return predict_sporty_match(sporty_doc)


def _evidence_rules(rules: dict[str, Any]) -> dict[str, Any]:
    """Expose rules as evidence only; final picks come from the unified selector."""
    if not rules:
        return {}
    return {
        **rules,
        "evidence_picks_suppressed": len([p for p in rules.get("picks") or [] if p.get("type") != "no_bet"]),
        "picks": [],
        "decision_role": "evidence_only",
    }


def _fallback_market_prediction(
    doc: dict[str, Any],
    detail: dict[str, Any],
    home_sample: int,
    away_sample: int,
) -> dict[str, Any]:
    """Prematch fallback when SofaScore matched but historical detail is thin."""
    sporty_doc = build_sporty_doc(doc)
    rules = predict_sporty_match(sporty_doc)
    raw_picks = [dict(pick) for pick in (rules.get("picks") or [])]
    raw_picks = _upgrade_over_1_5_to_over_2_5(raw_picks, rules.get("signals") or [])
    picks = _curate_picks(raw_picks, doc, signals=list(rules.get("signals") or []), odds_movement={})
    match_state = classify_match_state(doc)
    is_live = bool(match_state.get("is_live"))
    live_data_sources = _live_data_sources(doc, detail) if is_live else []
    live_stats_available = bool(live_data_sources or _live_match_statistics(detail).get("has_stats")) if is_live else False
    odds_movement: dict[str, Any] = {}
    if is_live:
        try:
            from app.market.market import get_movement
            match_id_str = str(doc.get("sportybet_id") or doc.get("id") or "")
            odds_movement = get_movement(match_id_str) if match_id_str else {}
        except Exception as exc:
            from app.utils.health_counters import record_health_event
            record_health_event("enriched_prediction", "fallback_market_movement_failed", exc)
    live_picks = _live_inplay_picks(doc, detail, rules, {}, {}, odds_movement) if is_live else []
    picks.extend(live_picks)
    # Allow prematch market-favorite picks when markets are available,
    # even with sofascore/both provider state. Markets alone provide
    # a valid degraded signal for prematch when history is thin.
    if not is_live and _provider_state(doc) != "sportybet" and not (doc.get("sportybet_markets") or doc.get("markets")):
        picks = [
            _pick(
                "no_bet",
                "No strong bet",
                50,
                "Thin history fallback is not allowed to publish prematch market-favorite picks without market data",
            )
        ]
    contextual_intelligence = build_contextual_intelligence(
        doc,
        signals=list(rules.get("signals") or []),
        picks=picks,
        odds_movement=odds_movement,
    )
    picks = apply_contextual_adjustment(picks, contextual_intelligence)
    rejected_picks = _rejected_pick_trace(raw_picks, picks)
    _attach_stake_sizing(doc, picks)
    signals = list(rules.get("signals") or [])
    signals.extend(_source_quality_signals(doc, prediction_readiness(doc)))
    signals.append({
        "name": "contextual_intelligence",
        "value": contextual_intelligence,
        "impact": int(contextual_intelligence.get("confidence_adjustment") or 0),
    })
    signals.append({
        "name": "data_depth",
        "value": {
            "has_sofascore_detail": bool(detail),
            "home_finished_history": home_sample,
            "away_finished_history": away_sample,
            "fallback": "sportybet_market_rules",
        },
        "impact": -5,
    })
    if _provider_state(doc) == "sportybet" and not detail:
        signals.append({"name": "no_form_data", "value": True, "impact": -2})
    risk_management = apply_risk_controls(
        doc,
        picks,
        signals=signals,
        contextual_intelligence=contextual_intelligence,
        odds_movement=odds_movement,
        models={},
        readiness=prediction_readiness(doc),
    )
    signals.append({
        "name": "risk_management",
        "value": risk_management,
        "impact": -8 if risk_management.get("risk_level") == "high" else -3 if risk_management.get("risk_level") == "medium" else 1,
    })
    if picks and picks[0].get("type") == "no_bet" and picks[0].get("suppressed_picks"):
        rejected_picks.extend(_rejected_pick_trace(picks[0].get("suppressed_picks") or [], []))
    prediction = {
        "match_id": doc.get("sportybet_id") or doc.get("id") or detail.get("id"),
        "name": doc.get("sportybet_name") or doc.get("name") or detail.get("name"),
        "source": "sportybet_market_signal" if _provider_state(doc) == "sportybet" else "sportybet_market_fallback",
        "match_date": doc.get("match_date"),
        "tournament": doc.get("tournament") or ((detail.get("tournament") or {}).get("name") if isinstance(detail.get("tournament"), dict) else None),
        "category": doc.get("category") or _detail_country(detail),
        "country": doc.get("category") or _detail_country(detail),
        "time_context": doc.get("time_context") or match_time_context(doc),
        "match_state": match_state,
        "is_live": is_live,
        "prediction_mode": "live" if (is_live and live_stats_available) else "prematch",
        "live_data_sources": live_data_sources,
        "live_stats_source": _live_stats_source(doc, live_data_sources),
        "live_statistics_summary": _live_match_statistics(detail).get("box_score") if live_stats_available else {},
        "data_source": _provider_state(doc),
        "teams": {
            "home": detail.get("home_team") or {"name": _team_name(doc, "home")},
            "away": detail.get("away_team") or {"name": _team_name(doc, "away")},
        },
        "rules": _evidence_rules(rules),
        "models": {},
        "web_context": doc.get("web_context") or {},
        "data_sources": doc.get("data_sources") or {},
        "sportybet_detail": doc.get("sportybet_detail") or {},
        "odds_movement": odds_movement,
        "value_bets": [],
        "market_selector": [],
        "live_inplay": live_picks,
        "signals": sorted(signals, key=lambda item: abs(item.get("impact") or 0), reverse=True),
        "contextual_intelligence": contextual_intelligence,
        "risk_management": risk_management,
        "learned_role_decision": (picks[0].get("learned_role_decision") if picks else None),
        "picks": picks,
        "candidate_pool_count": len(raw_picks),
        "rejected_picks": rejected_picks,
        "fallback_reason": "SofaScore detail exists but finished team history is below model threshold.",
        "data_quality": {
            "has_sofascore_detail": bool(detail),
            "has_sportybet_detail": bool(doc.get("sportybet_detail") or doc.get("raw_sporty")),
            "has_sportybet_markets": bool(doc.get("sportybet_markets") or doc.get("markets")),
            "has_web_context": bool((doc.get("web_context") or {}).get("snippets")),
            "has_raw_sporty": bool(doc.get("raw_sporty")),
            "has_raw_sofascore": bool(doc.get("raw_sofascore_event") or doc.get("sofascore_event")),
            "manual_match": bool(doc.get("manual_match")),
            "live_stats_available": live_stats_available,
            "live_data_sources": live_data_sources,
            "thin_history_fallback": True,
        },
    }
    _finalize_prediction_output(prediction, {})
    return prediction


def _upgrade_over_1_5_to_over_2_5(
    picks: list[dict[str, Any]],
    signals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Conservative rule:
    - If the model wants `Over 1.5 goals`, only upgrade to `Over 2.5 goals`
      when goal pressure is strong enough (and optionally when market steam exists).

    Rationale: our Over 1.5 history shows higher goal_pressure correlates with
    total>=3 outcomes; Over 2.5 is a better expression of that stronger state.
    """
    if not picks:
        return picks
    settings = get_settings()
    threshold = float(settings.over25_upgrade_goal_pressure or 0)
    require_steam = bool(settings.over25_upgrade_requires_market_steam)

    goal_pressure = 0.0
    has_market_steam = False
    for sig in signals or []:
        if not isinstance(sig, dict):
            continue
        if sig.get("name") == "goal_pressure":
            goal_pressure = _to_float(sig.get("impact")) or _to_float(sig.get("value")) or 0.0
        if sig.get("name") == "market_steam":
            has_market_steam = True

    if goal_pressure < threshold:
        return picks
    if require_steam and not has_market_steam:
        return picks

    upgraded: list[dict[str, Any]] = []
    for pick in picks:
        if str(pick.get("type") or "").lower() == "goals" and str(pick.get("selection") or "").lower() == "over 1.5 goals":
            confidence = int(pick.get("confidence") or 0)
            # Over 2.5 is a higher bar; keep it conservative by shaving confidence.
            upgraded.append({
                **pick,
                "selection": "Over 2.5 goals",
                "confidence": max(55, min(95, confidence - 6)),
                "reason": f"{pick.get('reason') or ''}; upgraded to over 2.5 due to goal_pressure {round(goal_pressure, 1)}",
            })
        else:
            upgraded.append(pick)
    return upgraded


def _value_bets(
    doc: dict[str, Any],
    model: dict[str, Any] | None,
    ensemble: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not model or model.get("error") or not model.get("probabilities"):
        return []
    calibrated_probs = model.get("calibrated_probabilities") or (ensemble or {}).get("calibrated_probabilities")
    if not calibrated_probs:
        return []
    probs = calibrated_probs
    markets = doc.get("sportybet_markets") or doc.get("markets") or []
    bets = []
    for market in markets:
        name = str(market.get("name") or "").lower()
        if not (market.get("id") == "1" or "1x2" in name or name == "match result"):
            continue
        for selection in market.get("selections") or []:
            decimal = _to_float(selection.get("odds"))
            if not decimal or decimal <= 1:
                continue
            selection_name = str(selection.get("name") or "")
            if selection_name in {"Home", "1"}:
                probability = _probability_unit(probs.get("home_win") or probs.get("home"))
            elif selection_name in {"Draw", "X"}:
                probability = _probability_unit(probs.get("draw"))
            elif selection_name in {"Away", "2"}:
                probability = _probability_unit(probs.get("away_win") or probs.get("away"))
            else:
                continue
            if probability is None:
                continue
            kelly = kelly_fraction(probability, decimal)
            edge = float(kelly.get("edge_percent") or 0)
            min_probability = 0.34 if selection_name in {"Draw", "X"} else 0.52
            max_odds = 4.0 if selection_name in {"Home", "1", "Away", "2"} else 5.5
            if (
                kelly.get("recommendation") == "bet"
                and edge >= 5
                and probability >= min_probability
                and decimal <= max_odds
                and _ensemble_agrees_with_value(selection_name, ensemble)
            ):
                memory = _candidate_memory(doc, "value_bet", selection_name)
                if not _candidate_allowed(memory, min_win_rate=52, min_samples=8, require_samples=True):
                    continue
                bets.append({"selection": selection_name, "decimal_odds": decimal, "kelly": kelly, "memory": memory})
    return sorted(bets, key=lambda item: item["kelly"]["edge_percent"], reverse=True)


def _ensemble_agrees_with_value(selection_name: str, ensemble: dict[str, Any] | None) -> bool:
    prediction = str((ensemble or {}).get("prediction") or "").lower()
    if not prediction:
        return True
    selection = str(selection_name or "").lower()
    if selection in {"home", "1"}:
        return "home" in prediction
    if selection in {"away", "2"}:
        return "away" in prediction
    if selection in {"draw", "x"}:
        return "draw" in prediction
    return False


def _candidate_memory(doc: dict[str, Any], pick_type: str, selection: str | None = None) -> dict[str, Any]:
    try:
        from app.storage.league_memory import weighted_candidate_memory

        return weighted_candidate_memory(doc, pick_type, selection)
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "candidate_memory_failed", exc)
        return {"allow": True, "blended_win_rate": None, "scopes": {}}


def _candidate_allowed(
    memory: dict[str, Any],
    min_win_rate: float = 50,
    min_samples: int = 10,
    require_samples: bool = False,
) -> bool:
    samples = 0
    for stats in (memory.get("scopes") or {}).values():
        samples = max(samples, int((stats or {}).get("samples") or 0))
    win_rate = memory.get("blended_win_rate")
    if require_samples and samples < min_samples:
        return False
    if win_rate is None or samples < min_samples:
        return True
    return float(win_rate) >= min_win_rate


def _consensus_longshot_value_signal(
    doc: dict[str, Any],
    ensemble: dict[str, Any] | None,
    poisson: dict[str, Any] | None,
    dixon: dict[str, Any] | None,
    elo: dict[str, Any] | None,
    rules: dict[str, Any] | None,
    odds_movement: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Signals a model-market disagreement: our stack likes a side priced 2.00+."""
    odds_index = _market_odds_index(doc)
    model_probs = _blended_model_probabilities(ensemble or {}, poisson, dixon)
    sides = {
        "home": {
            "selection": "Home",
            "prob_key": "home_win",
            "odds": odds_index.get("home") or odds_index.get("home win"),
        },
        "away": {
            "selection": "Away",
            "prob_key": "away_win",
            "odds": odds_index.get("away") or odds_index.get("away win"),
        },
    }
    best_side = None
    best_score = -999.0
    for side, info in sides.items():
        odds = _to_float(info.get("odds"))
        probability = _to_float(model_probs.get(info["prob_key"])) or 0.0
        # This signal is not for speculative outsiders. It is for a side our
        # stack rates as genuinely strong, yet still priced at useful odds.
        if not odds or odds < LONGSHOT_MIN_DECIMAL_ODDS or probability < 55:
            continue
        implied = 100 / odds
        edge = probability - implied
        if edge > best_score:
            best_score = edge
            best_side = side
    if not best_side:
        return None

    info = sides[best_side]
    odds = float(info["odds"])
    probability = float(model_probs.get(info["prob_key"]) or 0.0)
    implied = 100 / odds
    support: list[str] = []
    opposition: list[str] = []

    def _side_from_probs(probs: dict[str, Any] | None) -> str | None:
        if not probs:
            return None
        home_prob = _to_float(probs.get("home_win")) or 0.0
        away_prob = _to_float(probs.get("away_win")) or 0.0
        draw_prob = _to_float(probs.get("draw")) or 0.0
        if max(home_prob, away_prob, draw_prob) < 38:
            return None
        if home_prob > away_prob and home_prob > draw_prob:
            return "home"
        if away_prob > home_prob and away_prob > draw_prob:
            return "away"
        return "draw"

    model_votes = {
        "ensemble": _side_from_probs((ensemble or {}).get("probabilities") or {}),
        "poisson": _side_from_probs((poisson or {}).get("probabilities") or {}),
        "dixon_coles": _side_from_probs((dixon or {}).get("probabilities") or {}),
    }
    if elo and not elo.get("error"):
        home_elo = _to_float(elo.get("home_win_probability")) or 50.0
        away_elo = _to_float(elo.get("away_win_probability")) or (100 - home_elo)
        model_votes["elo"] = "home" if home_elo > away_elo + 4 else "away" if away_elo > home_elo + 4 else None
    rules_edge = _rules_side_edge(rules or {})
    if rules_edge >= 2:
        model_votes["rules_evidence"] = "home"
    elif rules_edge <= -2:
        model_votes["rules_evidence"] = "away"

    for name, vote in model_votes.items():
        if vote == best_side:
            support.append(name)
        elif vote in {"home", "away"}:
            opposition.append(name)

    movement_detail = _longshot_market_movement(best_side, odds_movement or {})
    if movement_detail.get("supports"):
        support.append("market_movement")
    market_risk: list[str] = []
    if movement_detail.get("opposes"):
        market_risk.append("market_movement")

    edge = probability - implied
    selection = str(info["selection"])
    market_intent = classify_market_intent("consensus_longshot_value", selection, {
        "market_intent": {
            "family": "outcome",
            "market": "1x2",
            "intent": f"{best_side}_win",
            "direction": best_side,
            "line": None,
            "raw_selection": selection,
        }
    })
    quality = _longshot_quality_context(doc, best_side, movement_detail)
    quality_penalty = float(quality.get("penalty") or 0.0)
    adjusted_edge = edge - quality_penalty
    for flag in quality.get("risk_flags") or []:
        if flag not in market_risk:
            market_risk.append(flag)

    # Longshot value must survive context, not only model agreement. Cross-league
    # matches are especially noisy: if the selected side is from/weighs as a
    # weaker recent league profile and the market is fading it, keep it out of
    # promoted longshot signals until memory proves otherwise.
    severe_quality_risk = bool(quality.get("severe"))
    if severe_quality_risk and not _longshot_memory_proven(doc, selection):
        return None
    if (
        severe_quality_risk
        and movement_detail.get("opposes")
        and not movement_detail.get("supports")
        and not _longshot_memory_proven(doc, selection)
    ):
        return None

    min_support = 4 if movement_detail.get("opposes") else 3
    min_adjusted_edge = 18 if movement_detail.get("opposes") else 14
    if len(support) < min_support or opposition or adjusted_edge < min_adjusted_edge:
        return None

    memory = _candidate_memory(doc, "consensus_longshot_value", selection)
    if not _candidate_allowed(memory, min_win_rate=55, min_samples=15, require_samples=True):
        return None
    return {
        "name": "consensus_longshot_value",
        "value": {
            "selection": selection,
            "side": best_side,
            "market_family": market_intent.get("family"),
            "market": market_intent.get("market"),
            "intent": market_intent.get("intent"),
            "market_intent": market_intent,
            "decimal_odds": round(odds, 3),
            "model_probability": round(probability, 1),
            "implied_probability": round(implied, 1),
            "edge_percent": round(edge, 1),
            "adjusted_edge_percent": round(adjusted_edge, 1),
            "supporting_models": support,
            "opposing_models": opposition,
            "risk_flags": market_risk,
            "market_movement": movement_detail,
            "quality_context": quality,
            "memory": memory,
            "label": "Consensus longshot value",
        },
        "impact": round(max(3.0, min(18.0, adjusted_edge / 1.5 + len(support) * 1.5)), 2),
    }


def _longshot_memory_proven(doc: dict[str, Any], selection: str) -> bool:
    memory = _candidate_memory(doc, "consensus_longshot_value", selection)
    scopes = memory.get("scopes") or {}
    samples = max((int((stats or {}).get("samples") or 0) for stats in scopes.values()), default=0)
    win_rate = memory.get("blended_win_rate")
    return samples >= 8 and win_rate is not None and float(win_rate) >= 58


def _consensus_longshot_market_pick_signal(
    doc: dict[str, Any],
    market_picks: list[dict[str, Any]],
    odds_movement: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Promote high-confidence non-1X2 market value without changing its intent."""
    odds_index = _market_odds_index(doc)
    best: dict[str, Any] | None = None
    best_edge = -999.0
    for pick in market_picks or []:
        selection = str(pick.get("selection") or pick.get("pick") or "")
        confidence = _to_float(pick.get("confidence")) or 0.0
        intent = classify_market_intent(str(pick.get("type") or ""), selection, pick)
        if intent.get("market") in {"1x2", "double_chance", "live_match_winner"}:
            continue
        odds = _odds_for_pick(selection, odds_index)
        if not odds or odds < LONGSHOT_MIN_DECIMAL_ODDS or confidence < 64:
            continue
        implied = 100 / odds
        edge = confidence - implied
        if edge < 10:
            continue
        if edge > best_edge:
            best_edge = edge
            best = {
                "pick": pick,
                "selection": selection,
                "confidence": confidence,
                "odds": odds,
                "implied": implied,
                "edge": edge,
                "intent": intent,
            }
    if not best:
        return None

    selection = best["selection"]
    memory = _candidate_memory(doc, "consensus_longshot_value", selection)
    if not _candidate_allowed(memory, min_win_rate=55, min_samples=15, require_samples=True):
        return None
    intent = best["intent"]
    movement = (odds_movement or {}).get("strongest_pull") or {}
    support = ["market_selector", "model_memory"]
    if memory.get("blended_win_rate") is not None:
        support.append("graded_memory")
    return {
        "name": "consensus_longshot_value",
        "value": {
            "selection": selection,
            "market_family": intent.get("family"),
            "market": intent.get("market"),
            "intent": intent.get("intent"),
            "market_intent": intent,
            "decimal_odds": round(float(best["odds"]), 3),
            "model_probability": round(float(best["confidence"]), 1),
            "implied_probability": round(float(best["implied"]), 1),
            "edge_percent": round(float(best["edge"]), 1),
            "adjusted_edge_percent": round(float(best["edge"]), 1),
            "supporting_models": support,
            "opposing_models": [],
            "risk_flags": [],
            "market_movement": movement,
            "memory": memory,
            "label": f"Consensus {intent.get('family')} value",
        },
        "impact": round(max(3.0, min(16.0, float(best["edge"]) / 1.6 + len(support))), 2),
    }


def _longshot_quality_context(
    doc: dict[str, Any],
    side: str,
    movement_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail = doc.get("sofascore_detail") or {}
    home_history = detail.get("home_last_matches") or []
    away_history = detail.get("away_last_matches") or []
    try:
        from app.competition.league_strength import history_league_strength, league_strength_score

        home_strength = history_league_strength(home_history)
        away_strength = history_league_strength(away_history)
        tournament = detail.get("tournament") or doc.get("tournament") or {}
        tournament_name = tournament.get("name") if isinstance(tournament, dict) else str(tournament or "")
        match_strength = league_strength_score(tournament_name)
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "league_strength_failed", exc)
        home_strength = {"sample_size": 0, "avg_score": 55}
        away_strength = {"sample_size": 0, "avg_score": 55}
        match_strength = {"score": 55, "basis": "unknown"}

    home_avg = float(home_strength.get("avg_score") or 55)
    away_avg = float(away_strength.get("avg_score") or 55)
    home_sample = int(home_strength.get("sample_size") or 0)
    away_sample = int(away_strength.get("sample_size") or 0)
    selected_edge = (home_avg - away_avg) if side == "home" else (away_avg - home_avg)

    home_country = _team_country(detail, "home")
    away_country = _team_country(detail, "away")
    cross_country = bool(home_country and away_country and home_country != away_country)
    risk_flags: list[str] = []
    penalty = 0.0

    if min(home_sample, away_sample) < 4:
        risk_flags.append("thin_cross_league_history")
        penalty += 2.0
    if cross_country:
        risk_flags.append("cross_country_matchup")
        penalty += 1.0
    if selected_edge < -2:
        risk_flags.append("selected_side_weaker_league_context")
        penalty += min(8.0, abs(selected_edge) * 1.2)
    elif selected_edge > 3:
        penalty -= min(3.0, selected_edge * 0.35)

    movement = movement_detail or {}
    if movement.get("opposes"):
        odds_change = abs(float(movement.get("odds_change_percent") or 0))
        if odds_change >= 12:
            risk_flags.append("market_fading_longshot")
            penalty += min(6.0, odds_change / 4.0)
    if movement.get("supports"):
        penalty -= 2.0

    severe = selected_edge <= -4 or ("market_fading_longshot" in risk_flags and "selected_side_weaker_league_context" in risk_flags)
    return {
        "home_recent_strength": home_strength,
        "away_recent_strength": away_strength,
        "match_league_strength": match_strength,
        "selected_side": side,
        "selected_strength_edge": round(selected_edge, 1),
        "home_country": home_country,
        "away_country": away_country,
        "cross_country": cross_country,
        "risk_flags": risk_flags,
        "penalty": round(max(-3.0, penalty), 2),
        "severe": severe,
    }


def _team_country(detail: dict[str, Any], side: str) -> str | None:
    team = detail.get(f"{side}_team") or detail.get(f"{side}Team") or {}
    country = team.get("country") if isinstance(team, dict) else None
    if isinstance(country, dict):
        return country.get("name") or country.get("alpha2") or country.get("slug")
    if country:
        return str(country)
    return None


def _contextual_elo_prediction(doc: dict[str, Any], elo: dict[str, Any]) -> dict[str, Any]:
    """Remove fake home advantage for neutral/cross-country cup contexts.

    Sporty/Sofa often list one team as "home" even when the real-world edge is
    not a league home fixture. Equal Elo + automatic home boost was making
    matches like Freiburg vs Aston Villa look home-favored despite market and
    common-opponent evidence pointing away.
    """
    if not elo or elo.get("error"):
        return elo
    neutral = _neutral_or_cross_country_cup(doc)
    venue = _venue_form_context(doc, doc.get("sofascore_detail") or {})
    home_elo = _to_float(elo.get("home_elo"))
    away_elo = _to_float(elo.get("away_elo"))
    if home_elo is None or away_elo is None:
        return elo
    adjusted = dict(elo)
    if neutral:
        home_expected = 1 / (1 + 10 ** ((away_elo - home_elo) / 400))
        adjusted["home_win_probability"] = round(home_expected * 100, 1)
        adjusted["away_win_probability"] = round((1 - home_expected) * 100, 1)
        adjusted["home_advantage_removed"] = True
        adjusted["context_note"] = "neutral/cross-country cup context; listed home team is not treated as a full home edge"
    elif venue.get("sample_ok"):
        venue_edge = float(venue.get("edge") or 0)
        home_prob = float(adjusted.get("home_win_probability") or 50)
        home_prob = max(5.0, min(95.0, home_prob + max(-8.0, min(8.0, venue_edge * 0.75))))
        adjusted["home_win_probability"] = round(home_prob, 1)
        adjusted["away_win_probability"] = round(100 - home_prob, 1)
        adjusted["venue_adjustment"] = round(venue_edge, 2)
        adjusted["venue_context"] = venue
    return adjusted


def _neutral_or_cross_country_cup(doc: dict[str, Any]) -> bool:
    detail = doc.get("sofascore_detail") or {}
    tournament = detail.get("tournament") or doc.get("tournament") or {}
    tournament_name = tournament.get("name") if isinstance(tournament, dict) else str(tournament or "")
    category = doc.get("category") or doc.get("country") or ""
    text = " ".join([str(tournament_name or ""), str(category or "")]).lower()
    if "international clubs" in text:
        return True
    if any(key in text for key in ("final", "knockout", "cup")):
        home_country = _team_country(detail, "home")
        away_country = _team_country(detail, "away")
        return bool(home_country and away_country and home_country != away_country)
    return False


def _venue_form_signal(doc: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any] | None:
    context = _venue_form_context(doc, detail)
    if not context.get("sample_ok"):
        return None
    edge = float(context.get("edge") or 0)
    return {
        "name": "venue_form_edge",
        "value": context,
        "impact": round(max(-12.0, min(12.0, edge)), 2),
    }


def _venue_form_context(doc: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    home_id = _detail_team_id(detail, "home")
    away_id = _detail_team_id(detail, "away")
    home_history = detail.get("home_last_matches") or []
    away_history = detail.get("away_last_matches") or []
    home_venue = _venue_record(home_history, home_id, "home")
    away_venue = _venue_record(away_history, away_id, "away")
    sample_ok = home_venue["sample"] >= 3 and away_venue["sample"] >= 3
    ppg_edge = home_venue["points_per_game"] - away_venue["points_per_game"]
    gd_edge = home_venue["goal_diff_per_game"] - away_venue["goal_diff_per_game"]
    edge = ppg_edge * 7.0 + gd_edge * 3.5
    if home_venue["loss_rate"] >= 0.5:
        edge -= 3.0
    if away_venue["loss_rate"] <= 0.25 and away_venue["sample"] >= 4:
        edge -= 3.0
    if home_venue["win_rate"] >= 0.6 and home_venue["sample"] >= 4:
        edge += 2.0
    if away_venue["win_rate"] >= 0.5 and away_venue["sample"] >= 4:
        edge -= 2.0
    edge = max(-12.0, min(12.0, edge))
    return {
        "edge": round(edge, 2),
        "sample_ok": sample_ok,
        "home_at_home": home_venue,
        "away_on_road": away_venue,
        "interpretation": (
            "home venue advantage" if edge > 2
            else "away travel advantage" if edge < -2
            else "venue neutral"
        ),
        "neutral_context": _neutral_or_cross_country_cup(doc),
    }


def _venue_record(history: list[dict[str, Any]], team_id: Any, venue: str) -> dict[str, Any]:
    if not team_id:
        return _empty_venue_record()
    rows = []
    for match in history or []:
        if (match.get("status") or {}).get("type") != "finished":
            continue
        is_home = str(((match.get("home_team") or match.get("homeTeam") or {}).get("id") or "")) == str(team_id)
        if (venue == "home" and not is_home) or (venue == "away" and is_home):
            continue
        score = match.get("score") or {}
        home_goals = _to_int(score.get("home"), 0)
        away_goals = _to_int(score.get("away"), 0)
        gf = home_goals if is_home else away_goals
        ga = away_goals if is_home else home_goals
        rows.append((gf, ga))
        if len(rows) >= 8:
            break
    if not rows:
        return _empty_venue_record()
    wins = sum(1 for gf, ga in rows if gf > ga)
    draws = sum(1 for gf, ga in rows if gf == ga)
    losses = sum(1 for gf, ga in rows if gf < ga)
    sample = len(rows)
    points = wins * 3 + draws
    return {
        "sample": sample,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "points_per_game": round(points / sample, 2),
        "goal_diff_per_game": round(sum(gf - ga for gf, ga in rows) / sample, 2),
        "goals_for_per_game": round(sum(gf for gf, _ in rows) / sample, 2),
        "goals_against_per_game": round(sum(ga for _, ga in rows) / sample, 2),
        "win_rate": round(wins / sample, 2),
        "loss_rate": round(losses / sample, 2),
    }


def _empty_venue_record() -> dict[str, Any]:
    return {
        "sample": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "points_per_game": 0.0,
        "goal_diff_per_game": 0.0,
        "goals_for_per_game": 0.0,
        "goals_against_per_game": 0.0,
        "win_rate": 0.0,
        "loss_rate": 0.0,
    }


def _detail_team_id(detail: dict[str, Any], side: str) -> Any:
    team = detail.get(f"{side}_team") or detail.get(f"{side}Team") or {}
    return team.get("id") if isinstance(team, dict) else None


def _longshot_market_movement(side: str, odds_movement: dict[str, Any]) -> dict[str, Any]:
    pull = (odds_movement or {}).get("market_pull") or {}
    side_pull = pull.get(side) if isinstance(pull, dict) else None
    strongest = (odds_movement or {}).get("strongest_pull") or {}
    if not side_pull and strongest.get("selection") == side:
        side_pull = strongest
    if not isinstance(side_pull, dict):
        return {"available": False, "supports": False, "opposes": False}

    direction = str(side_pull.get("direction") or "stable").lower()
    odds_change = _to_float(side_pull.get("odds_change_percent"))
    implied_change = _to_float(side_pull.get("implied_change_percent"))
    supported = direction in {"backed", "strong_backed"}
    opposed = direction in {"faded", "strong_faded"}
    return {
        "available": True,
        "selection": side,
        "direction": direction,
        "supports": supported,
        "opposes": opposed,
        "odds_change_percent": odds_change,
        "implied_change_percent": implied_change,
        "opening_odds": side_pull.get("opening_odds"),
        "current_odds": side_pull.get("current_odds"),
        "market_belief": side_pull.get("market_belief"),
        "snapshots": odds_movement.get("snapshots"),
    }


def _attach_stake_sizing(doc: dict[str, Any], picks: list[dict[str, Any]]) -> None:
    """Attach conservative Kelly stake metadata to every pick with a matching price."""
    odds_index = _market_odds_index(doc)
    for pick in picks:
        selection = str(pick.get("selection") or pick.get("pick") or "")
        decimal = _odds_for_pick(selection, odds_index)
        calibration = pick.get("calibration") if isinstance(pick.get("calibration"), dict) else {}
        calibrated_probability = _to_float(calibration.get("calibrated_probability"))
        probability = (
            max(0.01, min(0.99, calibrated_probability))
            if calibrated_probability is not None
            else None
        )
        stake_cap = float(((pick.get("calibration") or {}).get("stake_multiplier")) or 1.0)
        stake: dict[str, Any] = {
            "probability": round(probability, 3) if probability is not None else None,
            "confidence": pick.get("confidence"),
            "decimal_odds": decimal,
            "stake_source": "calibrated_kelly" if decimal and probability is not None else "no_calibrated_probability",
            "stake_cap_multiplier": stake_cap,
            "recommended": False,
            "stake_per_100": 0,
        }
        if decimal and decimal > 1 and probability is not None:
            kelly = kelly_fraction(probability, decimal)
            capped_per_100 = round(min(float(kelly.get("stake_per_100") or 0), stake_cap * 2.0), 2)
            stake.update({
                **kelly,
                "stake_per_100": capped_per_100,
                "raw_stake_per_100": kelly.get("stake_per_100"),
                "recommended": bool(kelly.get("value_bet")) and capped_per_100 > 0,
            })
        pick["stake"] = stake


def _market_odds_index(doc: dict[str, Any]) -> dict[str, float]:
    markets = doc.get("sportybet_markets") or doc.get("markets") or []
    index: dict[str, float] = {}
    for market in markets or []:
        market_name = str(market.get("name") or "").lower()
        specifier = str(market.get("specifier") or market.get("desc") or market.get("line") or "").lower()
        for selection in market.get("selections") or market.get("choices") or []:
            name = str(selection.get("name") or selection.get("label") or selection.get("outcome") or "")
            odds = _to_float(selection.get("odds") or selection.get("decimalOdds") or selection.get("decimal_odds"))
            if not odds or odds <= 1:
                continue
            keys = {_norm_market_key(name)}
            if market.get("id") == "1" or "1x2" in market_name or market_name == "match result":
                if name in {"Home", "1"}:
                    keys.update({"home", "home win"})
                elif name in {"Away", "2"}:
                    keys.update({"away", "away win"})
                elif name in {"Draw", "X"}:
                    keys.update({"draw", "x"})
            if "double chance" in market_name:
                if name.upper() == "1X" or "home" in name.lower():
                    keys.update({"home or draw", "1x"})
                elif name.upper() == "X2" or "away" in name.lower():
                    keys.update({"away or draw", "x2"})
                elif name == "12":
                    keys.update({"home or away", "12"})
            if "total" in market_name or "over/under" in market_name:
                line = specifier or market_name
                keys.add(_norm_market_key(f"{name} {line}"))
                if "2.5" in line:
                    keys.add(_norm_market_key(f"{name} 2.5"))
                if "1.5" in line:
                    keys.add(_norm_market_key(f"{name} 1.5"))
                if "3.5" in line:
                    keys.add(_norm_market_key(f"{name} 3.5"))
            if "both teams" in market_name or "btts" in market_name:
                if name.lower() in {"yes", "y"}:
                    keys.update({"both teams to score", "btts yes"})
                elif name.lower() in {"no", "n"}:
                    keys.update({"both teams to score - no", "btts no"})
            for key in keys:
                if key:
                    index.setdefault(key, odds)
    return index


def _odds_for_pick(selection: str, odds_index: dict[str, float]) -> float | None:
    key = _norm_market_key(selection)
    if key in odds_index:
        return odds_index[key]
    text = key
    aliases = []
    if "or draw" in text or "double chance" in text:
        if "home or draw" in text or "draw or home" in text or text == "1x":
            aliases = ["home or draw", "1x"]
        elif "away or draw" in text or "draw or away" in text or text == "x2":
            aliases = ["away or draw", "x2"]
        else:
            return None
    elif "home or draw" in text or text == "1x":
        aliases = ["home or draw", "1x"]
    elif "away or draw" in text or "draw or away" in text or text == "x2":
        aliases = ["away or draw", "x2"]
    elif "home or away" in text or text == "12":
        aliases = ["home or away", "12"]
    elif "home" in text:
        aliases = ["home", "home win"]
    elif "away" in text:
        aliases = ["away", "away win"]
    elif "draw" in text:
        aliases = ["draw", "x"]
    elif "under" in text:
        aliases = [text, "under 3.5" if "3.5" in text else "under 2.5" if "2.5" in text else "under 1.5" if "1.5" in text else "under"]
    elif "over" in text:
        aliases = [text, "over 2.5" if "2.5" in text else "over 1.5" if "1.5" in text else "over 0.5" if "0.5" in text else "over"]
    elif "btts" in text or "both teams" in text:
        aliases = ["both teams to score - no", "btts no"] if "no" in text else ["both teams to score", "btts yes"]
    for alias in aliases:
        odds = odds_index.get(_norm_market_key(alias))
        if odds:
            return odds
    return None


def _norm_market_key(value: str) -> str:
    return " ".join(str(value or "").lower().replace("-", " ").split())


def _value_confirms_pick(value_selection: str, pick_selection: str) -> bool:
    value = _norm_market_key(value_selection)
    pick = _norm_market_key(pick_selection)
    if value in {"home", "1"}:
        return pick in {"home", "home win"} or "home or draw" in pick
    if value in {"away", "2"}:
        return pick in {"away", "away win"} or "away or draw" in pick
    if value in {"draw", "x"}:
        return pick == "draw"
    return value == pick


def _combined_picks(
    rules: dict[str, Any],
    ensemble: dict[str, Any],
    value_bets: list[dict[str, Any]],
    market_picks: list[dict[str, Any]] | None = None,
    doc: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build final prematch picks from one selector."""
    picks = []
    picks.extend(market_picks or [])
    if value_bets and picks:
        for value in value_bets:
            if _value_confirms_pick(str(value.get("selection") or ""), str(picks[0].get("selection") or "")):
                picks[0]["value_overlay"] = {
                    "selection": value["selection"],
                    "edge_percent": value["kelly"]["edge_percent"],
                    "stake_per_100": value["kelly"]["stake_per_100"],
                }
                break
    if not picks:
        draw_exclusion = _draw_exclusion_fallback(ensemble)
        if draw_exclusion:
            picks.append(draw_exclusion)

    if not picks:
        picks.append({
            "type": "no_bet",
            "selection": "No strong bet",
            "confidence": 50,
            "reason": "Low confidence ensemble — insufficient rule signals",
        })
    return _curate_picks(picks, doc)


def _draw_exclusion_fallback(ensemble: dict[str, Any]) -> dict[str, Any] | None:
    probs = (ensemble or {}).get("probabilities") or {}
    home = _to_float(probs.get("home_win")) or 0.0
    draw = _to_float(probs.get("draw")) or 0.0
    away = _to_float(probs.get("away_win")) or 0.0
    if home + away < 72 or draw > 24:
        return None
    pick = _selector_pick(
        "double_chance",
        "Home or Away",
        min(84, home + away),
        "draw probability is low; protect against choosing the wrong winner",
    )
    pick["draw_exclusion"] = True
    pick["source"] = "draw_exclusion_fallback"
    return pick


def _live_inplay_picks(
    doc: dict[str, Any],
    detail: dict[str, Any],
    rules: dict[str, Any],
    ensemble: dict[str, Any],
    finished_memory: dict[str, Any],
    odds_movement: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Live-only markets. This deliberately does not run for prematch predictions."""
    minute = _live_minute(doc, detail, rules)
    if minute <= 0 or minute >= 90:
        return []
    home_goals, away_goals = _live_score(doc, detail)
    total_goals = home_goals + away_goals
    score_diff = home_goals - away_goals
    home_name = _team_name(doc, "home") or "Home"
    away_name = _team_name(doc, "away") or "Away"
    odds = _live_1x2_probabilities(doc)
    favorite = max(odds, key=odds.get) if odds else None
    prematch = _live_prematch_prior(ensemble)
    stats = _live_match_statistics(detail)
    # Policy: never publish "live_*" picks without live statistics.
    # A live match may still produce a prematch-style prediction via other paths.
    if not stats.get("has_stats"):
        return []
    market_live = _live_market_pressure(odds_movement or {})
    goal_pressure = _signal_value(rules.get("signals") or [], "goal_pressure")
    if goal_pressure <= 0:
        goal_pressure = _memory_goal_pressure(finished_memory)
    goal_pressure += stats["goal_pressure"]
    goal_pressure += max(-4.0, min(4.0, (prematch.get("over_2_5", 50.0) - 50.0) / 5.0))
    goal_pressure += market_live["goal_pressure"]

    home_pressure = stats["home_pressure"] + max(-6.0, min(6.0, (prematch.get("home_win", 33.3) - prematch.get("away_win", 33.3)) / 5.0))
    away_pressure = stats["away_pressure"] + max(-6.0, min(6.0, (prematch.get("away_win", 33.3) - prematch.get("home_win", 33.3)) / 5.0))
    home_pressure += market_live["home_pressure"]
    away_pressure += market_live["away_pressure"]

    picks: list[dict[str, Any]] = []

    # Next goal / no next goal
    next_goal_conf = 47 + min(18, max(0, goal_pressure - 12) * 0.9)
    if minute >= 55 and total_goals <= 2:
        next_goal_conf += 4
    if minute >= 75:
        next_goal_conf -= 4
    if abs(score_diff) <= 1:
        next_goal_conf += 4
    if stats["has_stats"]:
        next_goal_conf += min(8, max(0, stats["goal_pressure"] * 0.7))
    if next_goal_conf >= 55 and minute < 84:
        reason = "score clock, prematch goal prior, and live pressure still leave enough goal window"
        if stats["has_stats"]:
            reason += f"; stats pressure {stats['summary']}"
        if market_live["summary"]:
            reason += f"; odds movement {market_live['summary']}"
        picks.append(_pick("live_next_goal", "Next goal likely", next_goal_conf, reason))
        picks.append(_pick("live_total_goals", f"Over {total_goals + 0.5:g} live", max(55, next_goal_conf - 2), "same live goal window maps to the current over line"))

    no_goal_conf = 42 + max(0, minute - 65) * 0.85 - max(0, goal_pressure - 16) * 0.55
    if total_goals >= 3:
        no_goal_conf += 5
    if abs(score_diff) >= 2:
        no_goal_conf += 4
    if prematch.get("over_2_5", 50) >= 58:
        no_goal_conf -= 4
    if stats["has_stats"]:
        no_goal_conf -= min(8, max(0, stats["goal_pressure"] * 0.65))
    if market_live["goal_pressure"] > 0:
        no_goal_conf -= min(7, market_live["goal_pressure"])
    if no_goal_conf >= 55:
        picks.append(_pick("live_total_goals", f"Under {total_goals + 0.5:g} live", no_goal_conf, "late match state makes another goal less necessary"))

    # Next team to score
    team, team_reason, team_conf = _live_next_team(
        home_name,
        away_name,
        score_diff,
        favorite,
        odds,
        minute,
        home_pressure,
        away_pressure,
        stats["summary"],
        bool(stats["has_stats"]),
        stats["home_pressure"],
        stats["away_pressure"],
    )
    if team and team_conf >= 55 and minute < 86:
        selection = f"{team} next team to score"
        memory = _candidate_memory(doc, "live_team_to_score", selection)
        if _candidate_allowed(memory, min_win_rate=54, min_samples=8):
            pick = _pick("live_team_to_score", selection, team_conf + int(memory.get("confidence_adjustment") or 0), team_reason)
            pick["candidate_memory"] = memory
            picks.append(pick)

    # Live winner / protection
    winner_selection, winner_conf, winner_reason = _live_winner_pick(
        home_name, away_name, score_diff, favorite, odds, minute, prematch, home_pressure, away_pressure
    )
    if winner_selection and winner_conf >= 55:
        picks.append(_pick("live_match_winner", winner_selection, winner_conf, winner_reason))

    return _dedupe_picks(picks)


def _live_minute(doc: dict[str, Any], detail: dict[str, Any], rules: dict[str, Any]) -> int:
    if rules.get("minute"):
        return int(rules.get("minute") or 0)
    played = doc.get("played_seconds")
    if isinstance(played, str) and ":" in played:
        return _to_int(played.split(":", 1)[0]) or 0
    if played:
        return int((_to_int(played) or 0) / 60)
    status = detail.get("status") or {}
    desc = str(status.get("description") or doc.get("period") or "")
    digits = "".join(ch for ch in desc if ch.isdigit())
    return _to_int(digits) or 0


def _live_score(doc: dict[str, Any], detail: dict[str, Any]) -> tuple[int, int]:
    score = doc.get("score") or detail.get("score") or {}
    return _to_int(score.get("home")), _to_int(score.get("away"))


def _live_1x2_probabilities(doc: dict[str, Any]) -> dict[str, float]:
    odds = {}
    for market in doc.get("sportybet_markets") or doc.get("markets") or []:
        name = str(market.get("name") or "").lower()
        if not (market.get("id") == "1" or "1x2" in name or name == "match result"):
            continue
        for selection in market.get("selections") or []:
            decimal = _to_float(selection.get("odds"))
            if not decimal or decimal <= 1:
                continue
            label = str(selection.get("name") or "").lower()
            if label in {"home", "1"}:
                odds["home"] = 1 / decimal
            elif label in {"draw", "x"}:
                odds["draw"] = 1 / decimal
            elif label in {"away", "2"}:
                odds["away"] = 1 / decimal
    return odds


def _signal_value(signals: list[dict[str, Any]], name: str) -> float:
    # Delegated to signal_aggregator.signal_value; kept here as a local shim
    # for the many internal callers that reference the private name.
    return _signal_value_helper(signals, name)


def _memory_goal_pressure(memory: dict[str, Any]) -> float:
    blended = memory.get("blended") or {}
    if _finished_memory_only_global(memory):
        return 0.0
    avg_goals = _to_float(blended.get("avg_goals")) or 0
    over_25 = _to_float(blended.get("over_2_5_rate")) or 0
    return avg_goals * 5 + over_25 * 10


def _finished_memory_local_samples(memory: dict[str, Any] | None) -> int:
    scopes = (memory or {}).get("scopes") or {}
    evidence_scopes = (
        "tournament_odds",
        "country_odds",
        "global_odds",
        "tournament",
        "country",
    )
    return sum(int((scopes.get(key) or {}).get("samples") or 0) for key in evidence_scopes)


def _finished_memory_only_global(memory: dict[str, Any] | None) -> bool:
    scopes = (memory or {}).get("scopes") or {}
    return _finished_memory_local_samples(memory) <= 0 and int((scopes.get("global") or {}).get("samples") or 0) > 0


def _finished_memory_sample_factor(memory: dict[str, Any] | None, divisor: int = 120) -> float:
    samples = _finished_memory_local_samples(memory)
    return min(1.0, samples / divisor) if samples > 0 else 0.0


def _source_quality_signals(doc: dict[str, Any], readiness: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    sources = doc.get("data_sources") or {}
    sporty = sources.get("sportybet") or {}
    sofa = sources.get("sofascore") or {}
    sportradar = sources.get("sportradar") or {}
    sportradar_detail = doc.get("sportradar_detail") or {}
    signals: list[dict[str, Any]] = []
    if sporty.get("available"):
        signals.append({
            "name": "sportybet_detail_available",
            "value": {"market_count": sporty.get("market_count"), "live_clock": sporty.get("live_clock")},
            "impact": 0,
            "role": "readiness",
        })
    if sporty.get("markets"):
        signals.append({
            "name": "sportybet_markets_available",
            "value": {"market_count": sporty.get("market_count")},
            "impact": 0,
            "role": "readiness",
        })
    if sofa.get("detail"):
        signals.append({
            "name": "sofascore_detail_available",
            "value": {"statistics": sofa.get("statistics"), "history": sofa.get("history")},
            "impact": 0,
            "role": "readiness",
        })
    if sofa.get("statistics"):
        signals.append({
            "name": "sofascore_statistics_available",
            "value": {"statistics": True},
            "impact": 0,
            "role": "readiness",
        })
    if sportradar.get("available"):
        summary = sportradar_detail.get("summary") or {}
        evidence_count = sum(
            1 for key in ("has_h2h", "has_form", "has_lineups", "has_statistics", "has_standings", "has_probability")
            if summary.get(key)
        )
        signals.append({
            "name": "sportradar_widget_available",
            "value": {
                "match_id": sportradar_detail.get("match_id"),
                "summary": summary,
                "standings": bool(sportradar.get("standings")),
            },
            "impact": 0,
            "role": "readiness",
        })
    elif sportradar.get("error"):
        signals.append({
            "name": "sportradar_widget_unavailable",
            "value": {"error": sportradar.get("error")},
            "impact": 0,
        })
    assurance = (readiness or {}).get("assurance")
    if assurance:
        signals.append({
            "name": f"source_blend_{assurance}",
            "value": {"assurance": assurance, "sources": sources},
            "impact": 0,
            "role": "readiness",
        })
    return signals


def _close_strength_adjustment(ensemble: dict[str, Any], context: dict[str, Any] | None) -> int:
    if not context or int(context.get("samples") or 0) < 10:
        return 0
    prediction = str((ensemble or {}).get("prediction") or "").lower()
    odds_stats = context.get("similar_odds_outcomes") or {}
    league_stats = context.get("league_outcomes") or {}
    stats = odds_stats if int(odds_stats.get("samples") or 0) >= 12 else league_stats
    if not stats:
        return 0
    key = "home_win_rate" if "home" in prediction else "away_win_rate" if "away" in prediction else "draw_rate" if "draw" in prediction else ""
    rate = _to_float(stats.get(key)) if key else None
    impact = 0
    if rate is not None:
        if rate >= 0.55:
            impact += 3
        elif rate <= 0.30:
            impact -= 3
    strength_delta = _to_float(context.get("strength_delta_ppg")) or 0.0
    if "home" in prediction and strength_delta > 0.35:
        impact += 2
    elif "away" in prediction and strength_delta < -0.35:
        impact += 2
    elif ("home" in prediction and strength_delta < -0.35) or ("away" in prediction and strength_delta > 0.35):
        impact -= 2
    return max(-5, min(5, impact))


def _learned_signal_adjustment_for_pick(
    doc: dict[str, Any],
    signals: list[dict[str, Any]],
    pick_type: str | None,
) -> int:
    """Apply self-learner feedback scoped by league and market type."""
    try:
        from app.monitoring.self_learner import get_signal_weights

        league = doc.get("tournament") or doc.get("category") or ""
        if isinstance(league, dict):
            league = league.get("name") or ""
        weights = get_signal_weights(league, pick_type or "unknown")
        adjustment = 0
        for signal in signals:
            weight_adj = weights.get(signal.get("name") or "")
            if weight_adj is not None and abs(weight_adj) > 0.1:
                impact = _to_float(signal.get("impact")) or 1.0
                direction = 1 if impact >= 0 else -1
                adjustment += _stabilized_learned_adjustment(float(weight_adj) * direction)
        return max(-8, min(2, adjustment))
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "learned_signal_adjustment_failed", exc)
        return 0

def _cap_learned_signal_adjustment(signals: list[dict[str, Any]], adjustment: int) -> int:
    if adjustment <= 0:
        return max(-8, adjustment)
    weak_support = 0
    checked = 0
    # Batch-fetch all signal stats in one query instead of one per signal.
    signal_names = [
        str(s.get("name") or "")
        for s in (signals or [])
        if s.get("name") and s.get("name") not in BACKGROUND_CONTEXT_SIGNALS
    ]
    _prefetch_signal_stats(signal_names)
    for signal in signals or []:
        name = str(signal.get("name") or "")
        if not name or name in BACKGROUND_CONTEXT_SIGNALS:
            continue
        stats = _global_signal_stats(name)
        samples = int(stats.get("samples") or 0)
        win_rate = _rate_percent(stats.get("win_rate"))
        if samples >= 100 and win_rate is not None:
            checked += 1
            if win_rate < 63:
                weak_support += 1
    if checked and weak_support >= max(1, checked // 2):
        return min(2, adjustment)
    return max(-8, min(2, adjustment))


def _stabilized_learned_adjustment(weight_adj: float) -> int:
    contribution = round(float(weight_adj) * 3)
    return max(-3, min(1, contribution)) if contribution > 0 else max(-3, contribution)


def _stabilized_memory_adjustment(adjustment: int) -> int:
    if adjustment > 0:
        return min(2, adjustment)
    return max(-8, adjustment)



def _live_prematch_prior(ensemble: dict[str, Any]) -> dict[str, float]:
    probs = (ensemble or {}).get("probabilities") or {}
    return {
        "home_win": _to_float(probs.get("home_win")) or 33.3,
        "draw": _to_float(probs.get("draw")) or 33.3,
        "away_win": _to_float(probs.get("away_win")) or 33.3,
        "over_2_5": _to_float(probs.get("over_2_5")) or 50.0,
        "btts": _to_float(probs.get("btts")) or 50.0,
    }


def _live_match_statistics(detail: dict[str, Any]) -> dict[str, Any]:
    stats = detail.get("statistics") or detail.get("match_statistics") or []
    home: dict[str, float] = {}
    away: dict[str, float] = {}

    def add(name: str, home_value: Any, away_value: Any) -> None:
        key = _stat_key(name)
        if not key:
            return
        home[key] = max(home.get(key, 0.0), _stat_number(home_value))
        away[key] = max(away.get(key, 0.0), _stat_number(away_value))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            name = node.get("name") or node.get("key") or node.get("groupName")
            home_value = node.get("home") if "home" in node else node.get("homeValue")
            away_value = node.get("away") if "away" in node else node.get("awayValue")
            if name and (home_value is not None or away_value is not None):
                add(str(name), home_value, away_value)
            for child_key in ("groups", "statisticsItems", "items", "statistics"):
                for child in node.get(child_key) or []:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(stats)

    home_pressure = _side_pressure(home)
    away_pressure = _side_pressure(away)
    total_pressure = home_pressure + away_pressure
    pressure_gap = home_pressure - away_pressure
    summary = (
        f"H {round(home_pressure, 1)} / A {round(away_pressure, 1)}"
        if home or away else "not available"
    )
    return {
        "has_stats": bool(home or away),
        "home_pressure": home_pressure,
        "away_pressure": away_pressure,
        "goal_pressure": min(16.0, total_pressure / 6.0 + abs(pressure_gap) / 12.0),
        "summary": summary,
        "home": home,
        "away": away,
        "box_score": {
            "home": {
                "possession": home.get("possession"),
                "shots_on_target": home.get("shots_on_target"),
                "dangerous_attacks": home.get("dangerous_attacks"),
                "corners": home.get("corners"),
            },
            "away": {
                "possession": away.get("possession"),
                "shots_on_target": away.get("shots_on_target"),
                "dangerous_attacks": away.get("dangerous_attacks"),
                "corners": away.get("corners"),
            },
        },
    }


def _apply_live_stat_result_boosts(
    picks: list[dict[str, Any]],
    detail: dict[str, Any],
    signals: list[dict[str, Any]],
    minute: int,
) -> None:
    stats = _live_match_statistics(detail)
    if not stats.get("has_stats"):
        return
    home = stats.get("home") or {}
    away = stats.get("away") or {}
    home_boost = away_boost = 0
    if (home.get("possession") or 0) >= 60:
        home_boost += 3
    if (away.get("possession") or 0) >= 60:
        away_boost += 3
    if (home.get("shots_on_target") or 0) - (away.get("shots_on_target") or 0) >= 3:
        home_boost += 4
    if (away.get("shots_on_target") or 0) - (home.get("shots_on_target") or 0) >= 3:
        away_boost += 4
    for pick in picks:
        if pick.get("type") not in {"match_result", "live_match_winner"}:
            continue
        selection = str(pick.get("selection") or "").lower()
        boost = home_boost if "home" in selection or "home win" in selection else away_boost if "away" in selection or "away win" in selection else 0
        if boost:
            pick["confidence"] = min(95, int(pick.get("confidence") or 50) + boost)
            pick.setdefault("evidence", {})["live_statistics_boost"] = boost
    impact = max(home_boost, away_boost)
    if minute >= 46 and impact:
        impact = max(impact, 6)
    if impact:
        signals.append({
            "name": "live_statistics_result_edge",
            "value": stats.get("box_score"),
            "impact": impact,
            "role": "decision_driver",
        })


def _stat_key(name: str) -> str | None:
    normalized = str(name or "").lower().replace("%", "").replace("-", " ").replace("_", " ")
    if "expected goals" in normalized or normalized.strip() == "xg":
        return "xg"
    if "shot on target" in normalized or "shots on target" in normalized:
        return "shots_on_target"
    if "total shots" in normalized or normalized == "shots":
        return "shots"
    if "dangerous attack" in normalized:
        return "dangerous_attacks"
    if normalized.strip() == "attacks":
        return "attacks"
    if "ball possession" in normalized or normalized.strip() == "possession":
        return "possession"
    if "corner" in normalized:
        return "corners"
    if "big chance" in normalized:
        return "big_chances"
    return None


def _stat_number(value: Any) -> float:
    if isinstance(value, dict):
        value = value.get("value") or value.get("displayValue") or value.get("current")
    text = str(value or "").replace("%", "").strip()
    return _to_float(text) or 0.0


def _side_pressure(values: dict[str, float]) -> float:
    return (
        values.get("xg", 0) * 12
        + values.get("shots_on_target", 0) * 3.2
        + values.get("shots", 0) * 1.1
        + values.get("big_chances", 0) * 4.0
        + values.get("dangerous_attacks", 0) * 0.35
        + values.get("attacks", 0) * 0.08
        + values.get("corners", 0) * 0.8
        + max(0, values.get("possession", 50) - 50) * 0.08
    )


def _live_market_pressure(odds_movement: dict[str, Any]) -> dict[str, Any]:
    pull = odds_movement.get("strongest_pull") or {}
    selection = str(pull.get("selection") or "").lower()
    direction = str(pull.get("direction") or "").lower()
    strength = str(pull.get("strength") or "").lower()
    points = abs(_to_float(pull.get("implied_probability_points")) or _to_float(pull.get("implied_change_percent")) or 0)
    base = min(10.0, points / 2.0)
    if strength == "strong":
        base += 3.0
    elif strength == "moderate":
        base += 1.5

    home_pressure = away_pressure = goal_pressure = 0.0
    summary_parts: list[str] = []
    if selection in {"home", "away"} and direction in {"backed", "faded"}:
        sign = 1.0 if direction == "backed" else -1.0
        if selection == "home":
            home_pressure += sign * base
            away_pressure -= sign * base * 0.45
        else:
            away_pressure += sign * base
            home_pressure -= sign * base * 0.45
        summary_parts.append(f"{selection} {direction}")
    elif selection == "draw" and direction == "backed":
        goal_pressure -= min(5.0, base * 0.6)
        summary_parts.append("draw backed")

    for market in odds_movement.get("markets") or []:
        market_name = str(market.get("market") or market.get("market_name") or market.get("name") or "").lower()
        selection_name = str(market.get("selection") or market.get("selection_name") or "").lower()
        pull_info = market.get("market_pull") or {}
        market_pull = str((pull_info if isinstance(pull_info, dict) else {}).get("direction") or market.get("direction") or "").lower()
        change = abs(
            _to_float((pull_info if isinstance(pull_info, dict) else {}).get("implied_change_percent"))
            or _to_float((market.get("movement") if isinstance(market.get("movement"), dict) else {}).get("percent"))
            or _to_float(market.get("odds_change_percent"))
            or 0
        )
        move_strength = min(6.0, change / 3.0)
        if not market_pull or market_pull == "stable":
            continue
        backed = market_pull in {"backed", "strong_backed"} or market_pull.endswith("backed")
        faded = market_pull in {"faded", "strong_faded"} or market_pull.endswith("faded")
        text = f"{market_name} {selection_name}"
        if ("over" in text or "goal" in text) and backed:
            goal_pressure += move_strength
        if ("under" in text or "no goal" in text) and backed:
            goal_pressure -= move_strength
        if ("over" in text or "goal" in text) and faded:
            goal_pressure -= move_strength * 0.7
        if selection_name in {"home", "1"} and backed:
            home_pressure += move_strength
        elif selection_name in {"away", "2"} and backed:
            away_pressure += move_strength

    return {
        "home_pressure": home_pressure,
        "away_pressure": away_pressure,
        "goal_pressure": goal_pressure,
        "summary": ", ".join(summary_parts),
    }


def _live_next_team(
    home_name: str,
    away_name: str,
    score_diff: int,
    favorite: str | None,
    odds: dict[str, float],
    minute: int,
    home_pressure: float,
    away_pressure: float,
    stats_summary: str,
    has_stats: bool = False,
    stats_home_pressure: float = 0.0,
    stats_away_pressure: float = 0.0,
) -> tuple[str | None, str, float]:
    if not has_stats:
        return None, "next-team-to-score requires live match statistics", 0
    if minute >= 78:
        return None, "next-team-to-score disabled late unless pressure is extreme", 0
    stats_gap = stats_home_pressure - stats_away_pressure
    if abs(stats_gap) < 8:
        return None, "next-team-to-score requires a clear live-stat pressure gap", 0
    pressure_gap = home_pressure - away_pressure
    if abs(pressure_gap) >= 9 and (pressure_gap * stats_gap) > 0:
        team = home_name if pressure_gap > 0 else away_name
        return team, f"live stats and prematch prior point to this side; pressure {stats_summary}", 58 + min(16, abs(pressure_gap) * 0.8)
    if favorite in {"home", "away"} and abs(pressure_gap) >= 5:
        prob = odds.get(favorite, 0)
        team = home_name if favorite == "home" else away_name
        if (favorite == "home" and stats_gap <= 0) or (favorite == "away" and stats_gap >= 0):
            return None, "market favorite does not match live-stat pressure", 0
        pressure_boost = max(0, pressure_gap if favorite == "home" else -pressure_gap) * 0.35
        if pressure_boost > 0:
            return team, "live market agrees with real attacking pressure", 54 + min(16, prob * 24 + pressure_boost)
    return None, "no clear live-stat edge for next team to score", 0


def _live_winner_pick(
    home_name: str,
    away_name: str,
    score_diff: int,
    favorite: str | None,
    odds: dict[str, float],
    minute: int,
    prematch: dict[str, float],
    home_pressure: float,
    away_pressure: float,
) -> tuple[str | None, float, str]:
    time_left_factor = max(0, 90 - minute) / 90
    prematch_gap = (prematch.get("home_win", 33.3) - prematch.get("away_win", 33.3)) / 5.0
    pressure_gap = (home_pressure - away_pressure) / 8.0
    if score_diff > 0:
        conf = 60 + min(24, score_diff * 10 + minute * 0.18 + max(-6, min(8, prematch_gap + pressure_gap)))
        return f"{home_name} live winner", conf, "home side leads; prematch model and live pressure are included"
    if score_diff < 0:
        conf = 60 + min(24, abs(score_diff) * 10 + minute * 0.18 + max(-6, min(8, -prematch_gap - pressure_gap)))
        return f"{away_name} live winner", conf, "away side leads; prematch model and live pressure are included"
    if favorite in {"home", "away"}:
        prob = odds.get(favorite, 0)
        team = home_name if favorite == "home" else away_name
        prior_boost = prematch_gap + pressure_gap if favorite == "home" else -prematch_gap - pressure_gap
        conf = 52 + prob * 30 * time_left_factor + max(-5, min(8, prior_boost))
        return f"{team} live winner lean", conf, "score is level but market, prematch model, and live pressure have a clear side"
    if minute >= 70:
        return "Live draw protection", 56, "late level score makes draw protection relevant"
    return None, 0, ""


def _pick_family(pick: dict[str, Any]) -> str:
    kind = str(pick.get("type") or "").lower()
    selection = str(pick.get("selection") or pick.get("pick") or "")
    intent = pick.get("market_intent") if isinstance(pick.get("market_intent"), dict) else classify_market_intent(kind, selection, pick)
    family = str(intent.get("family") or kind or "other")
    market = str(intent.get("market") or family)
    if kind == "consensus_longshot_value":
        return f"longshot:{market}"
    if kind in {"value_bet", "market_value"}:
        return f"value:{market}"
    if kind.startswith("live_"):
        return f"live:{market}"
    return market or family or "other"


def _candidate_role_memory(doc: dict[str, Any], pick_type: str, selection: str) -> dict[str, Any]:
    """Learn whether this market works better as primary or secondary in similar context."""
    try:
        from app.storage.db import DB_PATH
        from app.storage.db import _conn
        from app.storage.league_memory import _init_db

        league = str(doc.get("tournament") or doc.get("league_name") or "")
        country = str(doc.get("category") or doc.get("country") or "")
        odds_profile = _current_1x2_odds_profile(doc)
        movement_signature = _current_movement_signature(doc)
        selection_key = _selection_key(selection)
        _init_db()
        with _conn(timeout=20) as conn:
            params: list[Any] = [pick_type]
            clauses = ["c.pick_type = ?"]
            scope_sql = ["1 = 1"]
            if league:
                scope_sql.append("c.league_name = ?")
                params.append(league)
            if country:
                scope_sql.append("c.country_name = ?")
                params.append(country)
            rows = conn.execute(
                f"""
                with opening as (
                    select os.match_id, os.home_odds, os.draw_odds, os.away_odds
                    from odds_snapshots os
                    join (
                        select match_id, min(snapshot_time) as snapshot_time
                        from odds_snapshots
                        group by match_id
                    ) first on first.match_id = os.match_id and first.snapshot_time = os.snapshot_time
                ),
                latest as (
                    select os.match_id, os.home_odds, os.draw_odds, os.away_odds
                    from odds_snapshots os
                    join (
                        select match_id, max(snapshot_time) as snapshot_time
                        from odds_snapshots
                        group by match_id
                    ) last on last.match_id = os.match_id and last.snapshot_time = os.snapshot_time
                )
                select c.role, c.selection, c.result, c.league_name, c.country_name, c.created_at,
                       opening.home_odds as open_home, opening.draw_odds as open_draw, opening.away_odds as open_away,
                       latest.home_odds as current_home, latest.draw_odds as current_draw, latest.away_odds as current_away
                from prediction_candidate_history c
                left join opening on opening.match_id = c.match_id
                left join latest on latest.match_id = c.match_id
                where c.graded_at is not null
                  and c.result in ('win', 'loss')
                  and {" and ".join(clauses)}
                  and ({" or ".join(scope_sql)})
                order by c.created_at desc
                limit 1200
                """,
                tuple(params),
            ).fetchall()

        roles: dict[str, dict[str, Any]] = {}
        weighted_roles: dict[str, dict[str, float]] = {}
        for row in rows:
            if _selection_key(row["selection"] or "") != selection_key:
                continue
            role = str(row["role"] or "candidate")
            scope_weight = _role_scope_weight(row, league, country)
            odds_weight = _role_odds_weight(odds_profile, row)
            movement_weight = _role_movement_weight(movement_signature, row)
            weight = max(0.05, min(1.4, scope_weight * odds_weight * movement_weight))
            bucket = weighted_roles.setdefault(role, {"samples": 0.0, "wins": 0.0, "losses": 0.0, "raw": 0.0, "local": 0.0})
            bucket["samples"] += weight
            bucket["raw"] += 1
            if scope_weight >= 0.72:
                bucket["local"] += weight
            if row["result"] == "win":
                bucket["wins"] += weight
            elif row["result"] == "loss":
                bucket["losses"] += weight

        for role, bucket in weighted_roles.items():
            samples = bucket["wins"] + bucket["losses"]
            roles[role] = {
                "samples": round(samples, 1),
                "raw_samples": int(bucket["raw"]),
                "wins": round(bucket["wins"], 1),
                "losses": round(bucket["losses"], 1),
                "local_samples": round(bucket.get("local", 0.0), 1),
                "win_rate": round(bucket["wins"] / samples, 3) if samples else 0.0,
            }

        primary = roles.get("primary") or {}
        secondary = roles.get("secondary") or roles.get("alternative") or {}
        primary_rate = float(primary.get("win_rate") or 0)
        primary_samples = float(primary.get("samples") or 0)
        secondary_rate = float(secondary.get("win_rate") or 0)
        secondary_samples = float(secondary.get("samples") or 0)
        adjustment = 0
        if primary_samples >= 6:
            adjustment += round((primary_rate - 0.52) * 14)
        if secondary_samples >= 8 and secondary_rate < 0.45:
            adjustment += round((secondary_rate - 0.45) * 8)
        return {
            "primary": primary,
            "secondary": secondary,
            "primary_adjustment": max(-6, min(6, adjustment)),
            "odds_profile_used": bool(odds_profile),
            "movement_profile_used": bool(movement_signature),
            "context_quality": _role_context_quality(primary_samples + secondary_samples, bool(odds_profile), bool(movement_signature)),
        }
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "candidate_role_memory_failed", exc)
        return {"primary_adjustment": 0}


def _selection_key(selection: str) -> str:
    return market_selection_key(selection)


def _role_scope_weight(row: Any, league: str, country: str) -> float:
    if league and str(row["league_name"] or "") == league:
        return 1.0
    if country and str(row["country_name"] or "") == country:
        return 0.72
    return 0.35


def _role_odds_weight(current: dict[str, float] | None, row: Any) -> float:
    if not current:
        return 1.0
    hist_current = {
        "home_odds": _to_float(row["current_home"]),
        "draw_odds": _to_float(row["current_draw"]),
        "away_odds": _to_float(row["current_away"]),
    }
    hist_open = {
        "home_odds": _to_float(row["open_home"]),
        "draw_odds": _to_float(row["open_draw"]),
        "away_odds": _to_float(row["open_away"]),
    }
    if not all(hist_current.values()):
        return 0.75
    diff = sum(abs(float(current[key]) - float(hist_current[key] or 0)) for key in current) / 3.0
    weight = max(0.25, 1.0 - min(diff, 1.5) / 1.5)
    if _favorite_side(current) == _favorite_side(hist_current):
        weight += 0.15
    if all(hist_open.values()):
        open_diff = sum(abs(float(hist_open[key] or 0) - float(hist_current[key] or 0)) for key in current) / 3.0
        if open_diff >= 0.18:
            weight += 0.05
    return min(1.25, weight)


def _role_movement_weight(current: dict[str, str] | None, row: Any) -> float:
    if not current:
        return 1.0
    historical = _movement_signature_from_odds(
        _to_float(row["open_home"]),
        _to_float(row["open_draw"]),
        _to_float(row["open_away"]),
        _to_float(row["current_home"]),
        _to_float(row["current_draw"]),
        _to_float(row["current_away"]),
    )
    if not historical:
        return 0.9
    if historical == current:
        return 1.22
    if historical.get("selection") == current.get("selection") and historical.get("direction") != current.get("direction"):
        return 0.7
    if historical.get("direction") == current.get("direction"):
        return 1.05
    return 0.9


def _role_context_quality(samples: float, odds_used: bool, movement_used: bool) -> str:
    if samples >= 16 and odds_used and movement_used:
        return "strong"
    if samples >= 8 and (odds_used or movement_used):
        return "usable"
    if samples >= 4:
        return "thin"
    return "building"


def _current_1x2_odds_profile(doc: dict[str, Any]) -> dict[str, float] | None:
    odds: dict[str, float] = {}
    for market in doc.get("sportybet_markets") or doc.get("markets") or []:
        name = str(market.get("name") or "").lower()
        if not (market.get("id") == "1" or "1x2" in name or "match result" in name):
            continue
        for selection in market.get("selections") or []:
            decimal = _to_float(selection.get("odds"))
            if not decimal or decimal <= 1:
                continue
            label = str(selection.get("name") or "").lower()
            if label in {"home", "1"}:
                odds["home_odds"] = decimal
            elif label in {"draw", "x"}:
                odds["draw_odds"] = decimal
            elif label in {"away", "2"}:
                odds["away_odds"] = decimal
    return odds if {"home_odds", "draw_odds", "away_odds"} <= set(odds) else None


def _current_movement_signature(doc: dict[str, Any]) -> dict[str, str] | None:
    try:
        from app.market.market import get_movement

        match_id = str(doc.get("sportybet_id") or doc.get("id") or "")
        if not match_id:
            return None
        pull = (get_movement(match_id) or {}).get("strongest_pull") or {}
        selection = str(pull.get("selection") or "").lower()
        direction = str(pull.get("direction") or "").lower()
        if selection in {"home", "draw", "away"} and direction in {"backed", "faded"}:
            return {"selection": selection, "direction": direction}
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "current_movement_signature_failed", exc)
        return None
    return None


def _movement_signature_from_odds(
    open_home: float | None,
    open_draw: float | None,
    open_away: float | None,
    current_home: float | None,
    current_draw: float | None,
    current_away: float | None,
) -> dict[str, str] | None:
    pairs = {
        "home": (open_home, current_home),
        "draw": (open_draw, current_draw),
        "away": (open_away, current_away),
    }
    best: tuple[str, str, float] | None = None
    for selection, (opening, current) in pairs.items():
        if not opening or not current or opening <= 1 or current <= 1:
            continue
        pct_change = (current - opening) / opening * 100
        implied_change = (1 / current - 1 / opening) * 100
        magnitude = abs(implied_change)
        if magnitude < 2.5:
            continue
        direction = "backed" if pct_change < 0 else "faded"
        if best is None or magnitude > best[2]:
            best = (selection, direction, magnitude)
    if not best:
        return None
    return {"selection": best[0], "direction": best[1]}


def _favorite_side(odds: dict[str, float | None]) -> str | None:
    values = {
        "home": _to_float(odds.get("home_odds")),
        "draw": _to_float(odds.get("draw_odds")),
        "away": _to_float(odds.get("away_odds")),
    }
    values = {key: value for key, value in values.items() if value and value > 1}
    if not values:
        return None
    return min(values, key=lambda key: values[key] or 999)


def _signal_promotion_policy(
    doc: dict[str, Any],
    pick: dict[str, Any],
    signals: list[dict[str, Any]],
    *,
    score_memory: dict[str, Any],
    role_memory: dict[str, Any],
    odds_movement: dict[str, Any],
) -> dict[str, Any]:
    pick_type = str(pick.get("type") or "")
    selection = str(pick.get("selection") or pick.get("pick") or "")
    signal_roles = _signal_roles(signals)
    noisy_support = [
        name for name, role in signal_roles.items()
        if role == "supporting_evidence" and name in NOISY_SUPPORT_SIGNALS
    ]
    pick_signal_proof = _pick_specific_signal_proof(pick_type, selection, noisy_support)
    score_driver = _score_memory_driver(score_memory)
    calibration_driver = _calibration_driver(pick)
    role_driver = _role_learning_driver(role_memory)
    market_driver = _market_agreement_driver(selection, odds_movement)
    positive_clv = _positive_clv_driver(pick)
    risk_clean = _promotion_risk_clean(odds_movement)

    decision_drivers: list[str] = []
    if score_driver.get("strong"):
        decision_drivers.append("score_shape_memory")
    if calibration_driver.get("strong"):
        decision_drivers.append("pick_specific_calibration")
    if role_driver.get("strong"):
        decision_drivers.append("role_learning_primary_edge")
    if market_driver.get("strong"):
        decision_drivers.append("strong_odds_movement_agreement")
    for item in pick_signal_proof:
        if item.get("strong"):
            decision_drivers.append(f"pick_specific_signal:{item.get('signal')}")

    good_history = any(
        item.get("good")
        for item in (score_driver, calibration_driver, role_driver)
    )
    strong_history = any(
        item.get("strong")
        for item in (score_driver, calibration_driver, role_driver)
    )
    mostly_noisy = bool(noisy_support) and not decision_drivers
    cap = 95
    reasons: list[str] = []
    if mostly_noisy:
        cap = 68
        reasons.append("mostly_noisy_support_signals")
    elif not decision_drivers:
        cap = 68
        reasons.append("no_decision_driver")
    elif good_history and not (market_driver.get("strong") and strong_history):
        cap = min(cap, 74)
        reasons.append("good_history_without_market_confirmation")
    elif market_driver.get("strong") and strong_history:
        reasons.append("strong_history_and_market_agreement")

    high_confidence_allowed = (
        strong_history
        and risk_clean
        and int(_odds_snapshot_count(odds_movement)) >= 2
        and (positive_clv.get("strong") or market_driver.get("strong"))
    )
    if not high_confidence_allowed:
        cap = min(cap, 82)
        reasons.append("missing_85_plus_requirements")

    if _is_over_1_5_pick(pick):
        goal_proof = _over_1_5_goal_proof(score_memory, odds_movement, signals)
        if not goal_proof.get("allow_goal_pressure"):
            cap = min(cap, 68)
            reasons.append("over_1_5_goal_pressure_not_proven")
    return {
        "version": "signal_promotion_policy_v1",
        "signal_roles": signal_roles,
        "noisy_support_signals": noisy_support,
        "decision_drivers": decision_drivers,
        "pick_signal_proof": pick_signal_proof,
        "score_memory": score_driver,
        "calibration": calibration_driver,
        "role_learning": role_driver,
        "market_agreement": market_driver,
        "positive_clv": positive_clv,
        "risk_clean": risk_clean,
        "high_confidence_allowed": high_confidence_allowed,
        "confidence_cap": cap,
        "cap_reasons": sorted(set(reasons)),
    }


def _signal_roles(signals: list[dict[str, Any]]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for signal in signals or []:
        name = str(signal.get("name") or "")
        if not name:
            continue
        explicit = signal.get("role")
        if explicit:
            roles[name] = str(explicit)
        elif name in MODIFIER_ONLY_SIGNALS:
            roles[name] = "supporting_evidence"
            signal["role"] = "supporting_evidence"
            signal["modifier_only"] = True
        elif name in NOISY_SUPPORT_SIGNALS:
            roles[name] = "supporting_evidence"
            signal["role"] = "supporting_evidence"
        elif name in BACKGROUND_CONTEXT_SIGNALS:
            roles[name] = "background_context"
            signal["role"] = "background_context"
        elif name in RISK_SIGNALS or _to_float(signal.get("impact")) is not None and (_to_float(signal.get("impact")) or 0) < 0:
            roles[name] = "risk_signal"
            signal["role"] = "risk_signal"
        else:
            roles[name] = "decision_driver"
            signal["role"] = "decision_driver"
    return roles


def _pick_specific_signal_proof(pick_type: str, selection: str, signal_names: list[str]) -> list[dict[str, Any]]:
    proof = []
    for signal_name in signal_names:
        stats = _pick_signal_stats(pick_type, selection, signal_name)
        samples = int(stats.get("samples") or 0)
        win_rate = _rate_percent(stats.get("win_rate"))
        proof.append({
            "signal": signal_name,
            **stats,
            "good": samples >= 50 and win_rate is not None and win_rate >= 65,
            "strong": samples >= 100 and win_rate is not None and win_rate >= 72,
        })
    return proof


def _pick_signal_stats(pick_type: str, selection: str, signal_name: str) -> dict[str, Any]:
    if not pick_type or not selection or not signal_name:
        return {"samples": 0, "wins": 0, "losses": 0, "win_rate": None}
    try:
        from app.storage.db import DB_PATH
        from app.storage.db import _conn
        from app.storage.league_memory import _init_db

        _init_db()
        with _conn(timeout=5) as conn:
            row = conn.execute(
                """
                select count(*) as samples,
                       sum(case when result = 'win' then 1 else 0 end) as wins,
                       sum(case when result = 'loss' then 1 else 0 end) as losses
                from signal_outcomes
                where pick_type = ?
                  and lower(selection) = lower(?)
                  and signal_name = ?
                  and result in ('win', 'loss')
                """,
                (pick_type, selection, signal_name),
            ).fetchone()
    except Exception as exc:
        from app.utils.health_counters import record_health_event
        record_health_event("enriched_prediction", "global_signal_stats_failed", exc)
        return {"samples": 0, "wins": 0, "losses": 0, "win_rate": None}
    samples = int((row or [0])[0] or 0)
    wins = int((row or [0, 0])[1] or 0)
    losses = int((row or [0, 0, 0])[2] or 0)
    return {
        "samples": samples,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / samples * 100, 1) if samples else None,
    }


def _score_memory_driver(score_memory: dict[str, Any]) -> dict[str, Any]:
    samples = int(score_memory.get("samples") or 0)
    win_rate = _rate_percent(score_memory.get("win_rate"))
    return {
        "samples": samples,
        "win_rate": win_rate,
        "good": samples >= 30 and win_rate is not None and win_rate >= 65,
        "strong": samples >= 100 and win_rate is not None and win_rate >= 72,
        "classification": score_memory.get("classification"),
    }


def _calibration_driver(pick: dict[str, Any]) -> dict[str, Any]:
    calibration = pick.get("calibration") if isinstance(pick.get("calibration"), dict) else {}
    samples = int(calibration.get("samples") or 0)
    win_rate = _rate_percent(calibration.get("win_rate"))
    return {
        "samples": samples,
        "win_rate": win_rate,
        "good": samples >= 50 and win_rate is not None and win_rate >= 68,
        "strong": samples >= 100 and win_rate is not None and win_rate >= 72,
    }


def _role_learning_driver(role_memory: dict[str, Any]) -> dict[str, Any]:
    primary = role_memory.get("primary") or {}
    samples = int(primary.get("raw_samples") or primary.get("samples") or 0)
    win_rate = _rate_percent(primary.get("win_rate"))
    return {
        "samples": samples,
        "win_rate": win_rate,
        "good": samples >= 20 and win_rate is not None and win_rate >= 68,
        "strong": samples >= 60 and win_rate is not None and win_rate >= 72,
        "context_quality": role_memory.get("context_quality"),
    }


def _market_agreement_driver(selection: str, odds_movement: dict[str, Any]) -> dict[str, Any]:
    pull = odds_movement.get("strongest_pull") if isinstance(odds_movement, dict) else {}
    if not isinstance(pull, dict) or not pull:
        return {"available": False, "strong": False, "snapshots": _odds_snapshot_count(odds_movement)}
    pull_selection = str(pull.get("selection") or "").lower()
    direction = str(pull.get("direction") or "").lower()
    strength = str(pull.get("strength") or "").lower()
    supports = _movement_supports_selection(selection, pull_selection, direction)
    snapshots = _odds_snapshot_count(odds_movement)
    return {
        "available": True,
        "selection": pull_selection,
        "direction": direction,
        "strength": strength,
        "snapshots": snapshots,
        "supports": supports,
        "strong": supports and snapshots >= 2 and strength in {"moderate", "strong"},
    }


def _positive_clv_driver(pick: dict[str, Any]) -> dict[str, Any]:
    calibration = pick.get("calibration") if isinstance(pick.get("calibration"), dict) else {}
    stake_source = str(calibration.get("stake_source") or "")
    stake_multiplier = _to_float(calibration.get("stake_multiplier")) or 0.0
    return {
        "stake_source": stake_source,
        "stake_multiplier": stake_multiplier,
        "strong": stake_source == "clv" and stake_multiplier > 1.0,
    }


def _promotion_risk_clean(odds_movement: dict[str, Any]) -> bool:
    snapshots = _odds_snapshot_count(odds_movement)
    pull = odds_movement.get("strongest_pull") if isinstance(odds_movement, dict) else {}
    if snapshots < 2:
        return False
    if not isinstance(pull, dict):
        return True
    volatility = abs(
        _to_float(pull.get("odds_change_percent"))
        or _to_float(pull.get("implied_change_percent"))
        or _to_float(pull.get("change_percent"))
        or 0
    )
    return volatility < 10


def _movement_supports_selection(selection: str, pull_selection: str, direction: str) -> bool:
    norm = _norm_market_key(selection)
    if direction not in {"backed", "strong_backed"}:
        return False
    if pull_selection == "home":
        return norm in {"home", "home win"} or "home or draw" in norm
    if pull_selection == "away":
        return norm in {"away", "away win"} or "away or draw" in norm
    if pull_selection == "draw":
        return norm == "draw" or "home or draw" in norm or "away or draw" in norm
    return False


def _odds_snapshot_count(odds_movement: dict[str, Any] | None) -> int:
    if not isinstance(odds_movement, dict):
        return 0
    value = odds_movement.get("snapshots") or odds_movement.get("market_snapshots") or odds_movement.get("history")
    if isinstance(value, list):
        return len(value)
    try:
        return int(value or 0)
    except Exception:
        return 0


def _is_over_1_5_pick(pick: dict[str, Any]) -> bool:
    return str(pick.get("type") or "").lower() in {"goals", "live_goals"} and "over 1.5" in str(pick.get("selection") or "").lower()


def _over_1_5_goal_proof(score_memory: dict[str, Any], odds_movement: dict[str, Any], signals: list[dict[str, Any]]) -> dict[str, Any]:
    low_total_rate = _rate_percent((score_memory.get("score_profile") or {}).get("low_total_rate"))
    nil_nil_rate = _rate_percent((score_memory.get("score_profile") or {}).get("nil_nil_rate"))
    market = _market_agreement_driver("Over 1.5 goals", odds_movement)
    goal_pressure = max([
        _to_float(signal.get("impact")) or _to_float(signal.get("value")) or 0
        for signal in signals
        if signal.get("name") == "goal_pressure"
    ] or [0])
    allow = (
        goal_pressure >= 24
        and (low_total_rate is None or low_total_rate <= 22)
        and (nil_nil_rate is None or nil_nil_rate <= 8)
        and market.get("strong")
    )
    return {
        "allow_goal_pressure": bool(allow),
        "goal_pressure": round(goal_pressure, 2),
        "low_total_rate": low_total_rate,
        "nil_nil_rate": nil_nil_rate,
        "market_agreement": market,
    }


def _rate_percent(value: Any) -> float | None:
    rate = _to_float(value)
    if rate is None:
        return None
    return round(rate * 100, 1) if 0 <= rate <= 1 else round(rate, 1)


def _curate_picks(
    picks: list[dict[str, Any]],
    doc: dict[str, Any] | None = None,
    *,
    signals: list[dict[str, Any]] | None = None,
    odds_movement: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return one primary pick plus a small set of non-conflicting alternatives."""
    candidates = []
    no_bet = next((pick for pick in picks if pick.get("type") == "no_bet"), None)
    for pick in _dedupe_picks(picks):
        if pick.get("type") == "no_bet":
            continue
        confidence = int(pick.get("confidence") or 0)
        selection = pick.get("selection") or pick.get("pick")
        if not selection or confidence < 55:
            continue
        if pick.get("type") == "value_bet" and confidence < 65:
            continue
        if pick.get("type") == "market_value" and confidence < 65:
            continue
        if pick.get("type") == "ensemble_1x2" and confidence < 58:
            continue
        market_intent = classify_market_intent(str(pick.get("type") or ""), str(selection), pick)
        role_memory = _candidate_role_memory(doc or {}, pick_type=str(pick.get("type") or ""), selection=str(selection))
        score_memory = _selection_score_memory(str(pick.get("type") or ""), str(selection))
        if pick.get("type") in {"value_bet", "market_value", "consensus_longshot_value"}:
            candidate_memory = _candidate_memory(doc or {}, str(pick.get("type") or ""), str(selection))
            if not _candidate_allowed(candidate_memory, min_win_rate=55, min_samples=15, require_samples=True):
                continue
        role_adjustment = int(role_memory.get("primary_adjustment") or 0)
        score_adjustment = int(score_memory.get("confidence_adjustment") or 0)
        adjusted_confidence = _cap_market_confidence(pick, max(50, min(96, confidence + role_adjustment + score_adjustment)))
        score_cap = score_memory.get("confidence_cap")
        if score_cap is not None:
            adjusted_confidence = min(adjusted_confidence, int(score_cap))
        signal_policy = _signal_promotion_policy(
            doc or {},
            {**pick, "selection": selection, "confidence": adjusted_confidence},
            signals or [],
            score_memory=score_memory,
            role_memory=role_memory,
            odds_movement=odds_movement or {},
        )
        adjusted_confidence = min(adjusted_confidence, int(signal_policy.get("confidence_cap") or adjusted_confidence))
        if adjusted_confidence < 55:
            continue

        ranking_confidence = adjusted_confidence

        evidence = pick.get("evidence") if isinstance(pick.get("evidence"), dict) else {}
        evidence["score_result_memory"] = score_memory
        evidence["signal_policy"] = signal_policy
        candidates.append({
            **pick,
            "evidence": evidence,
            "selection": selection,
            "market_intent": market_intent,
            "confidence": adjusted_confidence,
            "raw_confidence": confidence,
            "family": _pick_family({**pick, "market_intent": market_intent}),
            "role_learning": role_memory,
            "score_result_memory": score_memory,
            "signal_policy": signal_policy,
            "ranking_confidence": ranking_confidence,
        })

    if not candidates and no_bet:
        return [{**no_bet, "selection": no_bet.get("selection") or no_bet.get("pick") or "No strong bet", "role": "primary"}]
    if not candidates:
        return [{
            "type": "no_bet",
            "selection": "Avoid game",
            "confidence": 50,
            "reason": "No pick cleared the data and confidence gate",
            "family": "avoid",
            "role": "primary",
            "ranking_confidence": 50,
        }]

    candidates.sort(
        key=lambda item: (
            100 if item.get("clear_winner") else 0,
            item.get("confidence") or 0,
            item.get("ranking_confidence") or 0,
            8 if str(item.get("family") or "").startswith("value:") else 4 if item.get("family") == "1x2" else 0,
        ),
        reverse=True,
    )

    curated: list[dict[str, Any]] = []
    used_families: set[str] = set()
    for pick in candidates:
        family = pick["family"]
        if family in used_families:
            continue
        role_memory = pick.get("role_learning") or {}
        secondary = role_memory.get("secondary") or {}
        if curated and int(secondary.get("samples") or 0) >= 8 and float(secondary.get("win_rate") or 0) < 0.45:
            continue
        curated.append({**pick, "role": "primary" if not curated else "alternative"})
        used_families.add(family)
        if len(curated) >= 3:
            break
    _attach_learned_role_decision(curated)
    return curated


def _rejected_pick_trace(raw_picks: list[dict[str, Any]], curated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Explain which generated picks did not become part of the final slate."""
    curated_keys = {
        (str(pick.get("type") or ""), str(pick.get("selection") or pick.get("pick") or ""))
        for pick in curated
    }
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for pick in raw_picks:
        pick_type = str(pick.get("type") or "")
        selection = str(pick.get("selection") or pick.get("pick") or "")
        key = (pick_type, selection)
        if key in curated_keys or key in seen:
            continue
        seen.add(key)
        confidence = int(pick.get("confidence") or 0)
        reason = "lower_ranked_same_family_or_conflict"
        if not selection:
            reason = "missing_selection"
        elif pick_type == "no_bet":
            reason = "no_bet_evidence_only"
        elif confidence < 55:
            reason = "below_minimum_confidence"
        elif pick_type in {"value_bet", "market_value"} and confidence < 65:
            reason = "value_pick_below_value_gate"
        elif pick_type == "ensemble_1x2" and confidence < 58:
            reason = "ensemble_1x2_below_gate"
        rejected.append({
            "type": pick_type,
            "selection": selection,
            "confidence": confidence,
            "reason": reason,
            "market_intent": classify_market_intent(pick_type, selection, pick),
        })
        if len(rejected) >= 20:
            break
    return rejected


def _attach_learned_role_decision(picks: list[dict[str, Any]]) -> None:
    real_picks = [pick for pick in picks if pick.get("type") != "no_bet"]
    if not real_picks:
        return
    primary = real_picks[0]
    alternatives = real_picks[1:]
    scored = [("primary", primary, _learned_role_score(primary, "primary"))]
    for pick in alternatives:
        scored.append(("secondary", pick, _learned_role_score(pick, "secondary")))
    role, best, score = max(scored, key=lambda item: item[2])
    primary_score = scored[0][2]
    if role != "primary":
        secondary_stats = (best.get("role_learning") or {}).get("secondary") or (best.get("role_learning") or {}).get("alternative") or {}
        local_samples = float(secondary_stats.get("local_samples") or 0)
        primary_conf = float(primary.get("confidence") or 0)
        secondary_conf = float(best.get("confidence") or 0)
        if (local_samples < 2 and secondary_conf < primary_conf + 8) or (local_samples < 5 and secondary_conf < primary_conf + 5):
            role, best, score = scored[0]
    edge = round(score - primary_score, 2) if role != "primary" else round(score - max([item[2] for item in scored[1:]] or [score]), 2)
    decision = {
        "role": role,
        "selection": best.get("selection"),
        "type": best.get("type"),
        "score": round(score, 2),
        "edge": edge,
        "reason": (
            "secondary_outscores_primary_in_context"
            if role != "primary"
            else "primary_remains_best_in_context"
        ),
        "context_quality": (best.get("role_learning") or {}).get("context_quality") or "building",
    }
    for pick in real_picks:
        pick["learned_best"] = pick is best
        pick["learned_role_decision"] = decision


def _selection_score_memory(pick_type: str, selection: str) -> dict[str, Any]:
    """Score-result memory for every selection.

    This is the generalized version of the 0-0/Over 1.5 lesson: look at
    settled final scores for the same selection and expose which result shapes
    have historically produced wins/losses.
    """
    if not pick_type or not selection:
        return {"available": False, "reason": "missing_pick"}
    try:
        from app.storage.db import DB_PATH
        from app.storage.db import _conn
        from app.storage.league_memory import _init_db

        _init_db()
        with _conn(timeout=5) as conn:
            exact = _selection_score_scope(
                conn,
                "exact_selection",
                "pick_type = ? and lower(selection) = lower(?)",
                (pick_type, selection),
            )
            type_scope = _selection_score_scope(
                conn,
                "pick_type",
                "pick_type = ?",
                (pick_type,),
            )
    except Exception as exc:
        return {"available": False, "reason": str(exc)}

    best = exact if int(exact.get("samples") or 0) >= 12 else type_scope
    samples = int(best.get("samples") or 0)
    win_rate = _to_float(best.get("win_rate"))
    adjustment = 0
    cap = None
    classification = "unproven"
    if samples >= 20 and win_rate is not None and win_rate < 0.50:
        classification = "learned_high_risk"
        adjustment = -8
        cap = 58
    elif samples >= 20 and win_rate is not None and win_rate < 0.58:
        classification = "caution"
        adjustment = -4
        cap = 70
    elif samples >= 20 and win_rate is not None and win_rate >= 0.72:
        classification = "proven_selection"
        adjustment = 3
    elif samples >= 12 and win_rate is not None and win_rate >= 0.65:
        classification = "promising_selection"
        adjustment = 1

    failure_shape = _dominant_failure_shape(best)
    if failure_shape.get("share", 0) >= 0.35 and classification not in {"proven_selection", "promising_selection"}:
        adjustment = min(adjustment, -3)

    return {
        "available": samples > 0,
        "version": "selection_score_memory_v1",
        "classification": classification,
        "confidence_adjustment": adjustment,
        "confidence_cap": cap,
        "best_scope": best.get("scope"),
        "samples": samples,
        "wins": best.get("wins"),
        "losses": best.get("losses"),
        "win_rate": round((win_rate or 0) * 100, 1) if win_rate is not None else None,
        "score_profile": {
            "nil_nil_rate": best.get("nil_nil_rate"),
            "low_total_rate": best.get("low_total_rate"),
            "high_total_rate": best.get("high_total_rate"),
            "draw_rate": best.get("draw_rate"),
            "home_win_rate": best.get("home_win_rate"),
            "away_win_rate": best.get("away_win_rate"),
            "avg_total_goals": best.get("avg_total_goals"),
            "top_loss_scores": best.get("top_loss_scores"),
            "top_win_scores": best.get("top_win_scores"),
            "dominant_failure_shape": failure_shape,
        },
        "scopes": [exact, type_scope],
    }


def _selection_score_scope(
    conn: sqlite3.Connection,
    scope: str,
    where_sql: str,
    params: tuple[Any, ...],
) -> dict[str, Any]:
    base_sql = f"""
        from (
            select match_id, pick_type, selection, result, final_home, final_away
            from prediction_history
            where graded_at is not null and result in ('win', 'loss')
              and final_home is not null and final_away is not null
              and pick_type != 'no_bet'
            union all
            select match_id, pick_type, selection, result, final_home, final_away
            from prediction_candidate_history
            where graded_at is not null and result in ('win', 'loss')
              and final_home is not null and final_away is not null
              and pick_type != 'no_bet'
        )
        where {where_sql}
    """
    row = conn.execute(f"""
        select
            count(*) as samples,
            sum(case when result = 'win' then 1 else 0 end) as wins,
            sum(case when result = 'loss' then 1 else 0 end) as losses,
            avg(final_home + final_away) as avg_total_goals,
            sum(case when final_home = 0 and final_away = 0 then 1 else 0 end) as nil_nil,
            sum(case when final_home + final_away <= 1 then 1 else 0 end) as low_total,
            sum(case when final_home + final_away >= 4 then 1 else 0 end) as high_total,
            sum(case when final_home = final_away then 1 else 0 end) as draws,
            sum(case when final_home > final_away then 1 else 0 end) as home_wins,
            sum(case when final_away > final_home then 1 else 0 end) as away_wins
        {base_sql}
    """, params).fetchone()
    samples = int(row["samples"] or 0)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    top_loss_scores = _top_score_shapes(conn, base_sql, params, result="loss")
    top_win_scores = _top_score_shapes(conn, base_sql, params, result="win")
    return {
        "scope": scope,
        "samples": samples,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / samples, 4) if samples else None,
        "avg_total_goals": round(float(row["avg_total_goals"] or 0), 2) if samples else None,
        "nil_nil_rate": _percent(row["nil_nil"], samples),
        "low_total_rate": _percent(row["low_total"], samples),
        "high_total_rate": _percent(row["high_total"], samples),
        "draw_rate": _percent(row["draws"], samples),
        "home_win_rate": _percent(row["home_wins"], samples),
        "away_win_rate": _percent(row["away_wins"], samples),
        "top_loss_scores": top_loss_scores,
        "top_win_scores": top_win_scores,
    }


def _top_score_shapes(
    conn: sqlite3.Connection,
    base_sql: str,
    params: tuple[Any, ...],
    *,
    result: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(f"""
        select final_home, final_away, count(*) as count
        {base_sql}
          and result = ?
        group by final_home, final_away
        order by count desc, final_home, final_away
        limit 5
    """, (*params, result)).fetchall()
    return [
        {
            "score": f"{row['final_home']}-{row['final_away']}",
            "count": int(row["count"] or 0),
        }
        for row in rows
    ]


def _dominant_failure_shape(scope: dict[str, Any]) -> dict[str, Any]:
    losses = int(scope.get("losses") or 0)
    if losses <= 0:
        return {}
    top = (scope.get("top_loss_scores") or [{}])[0]
    top_count = int(top.get("count") or 0)
    return {
        "score": top.get("score"),
        "count": top_count,
        "share": round(top_count / losses, 3) if losses else 0,
    }


def _percent(value: Any, total: int) -> float | None:
    if not total:
        return None
    return round(float(value or 0) / total * 100, 1)


def _attach_goal_environment_evidence(picks: list[dict[str, Any]], goal_env: dict[str, Any] | None) -> None:
    """Ensure under-goals picks carry the computed goal environment context.

    This is used to keep downstream confidence capping and audits stable even
    when upstream pick sources omit the evidence block.
    """
    if not goal_env or not isinstance(goal_env, dict):
        return
    profile = goal_env.get("profile")
    if not profile:
        return
    for pick in picks or []:
        if not isinstance(pick, dict) or pick.get("type") == "no_bet":
            continue
        selection = str(pick.get("selection") or pick.get("pick") or "").lower()
        if "under" not in selection and "over" not in selection:
            continue
        if "goals" not in selection and "goal" not in selection:
            continue
        evidence = pick.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        current = evidence.get("goal_environment")
        if isinstance(current, dict) and current.get("profile"):
            continue
        evidence["goal_environment"] = goal_env
        pick["evidence"] = evidence


def _learned_role_score(pick: dict[str, Any], role: str) -> float:
    confidence = float(pick.get("confidence") or 0)
    ranking = float(pick.get("ranking_confidence") or confidence)
    raw = float(pick.get("raw_confidence") or confidence)
    memory = pick.get("role_learning") or {}
    stats = memory.get("primary") if role == "primary" else (memory.get("secondary") or memory.get("alternative") or {})
    samples = float(stats.get("samples") or 0)
    local_samples = float(stats.get("local_samples") or 0)
    win_rate = float(stats.get("win_rate") or 0)
    role_lift = (win_rate - 0.52) * 20 if samples >= 5 else 0
    sample_trust = min(4.0, samples / 4.0)
    if role != "primary":
        if local_samples < 2:
            role_lift *= 0.25
            sample_trust *= 0.25
            ranking -= 4
        elif local_samples < 5:
            role_lift *= 0.6
            sample_trust *= 0.6
    adjustment = float(memory.get("primary_adjustment") or 0) * (0.35 if role == "primary" else 0.15)
    return ranking + role_lift + sample_trust + adjustment + (confidence - raw) * 0.25


def _market_selector_picks(
    doc: dict[str, Any],
    ensemble: dict[str, Any],
    poisson: dict[str, Any] | None,
    dixon: dict[str, Any] | None,
    finished_memory: dict[str, Any] | None,
    rules: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Let data choose the best market instead of forcing one bet shape.

    Inputs are blended 1X2 model probabilities, goal distributions from
    Poisson/Dixon-Coles, and finished-score memory from tournament, country,
    then whole database. The output can be straight 1X2, double chance, BTTS,
    over, or under.
    """
    model_probs = _blended_model_probabilities(ensemble, poisson, dixon)
    memory = {} if _finished_memory_only_global(finished_memory) else ((finished_memory or {}).get("blended") or {})
    samples = _finished_memory_local_samples(finished_memory)
    sample_factor = _finished_memory_sample_factor(finished_memory)
    picks: list[dict[str, Any]] = []

    home = float(model_probs.get("home_win") or 0)
    draw = float(model_probs.get("draw") or 0)
    away = float(model_probs.get("away_win") or 0)
    rule_edge = _rules_side_edge(rules or {})
    if rule_edge:
        home = max(1.0, min(96.0, home + rule_edge))
        away = max(1.0, min(96.0, away - rule_edge))
        total_1x2 = home + draw + away
        if total_1x2 > 0:
            home = round(home / total_1x2 * 100, 1)
            draw = round(draw / total_1x2 * 100, 1)
            away = round(away / total_1x2 * 100, 1)
    model_over_2_5 = float(model_probs.get("over_2_5") or 0)
    model_btts = float(model_probs.get("btts") or 0)
    over_2_5 = _blend_rate(model_over_2_5 if model_over_2_5 > 0 else 50.0, float(memory.get("over_2_5_rate") or 0) * 100, sample_factor)
    btts = _blend_rate(model_btts if model_btts > 0 else 50.0, float(memory.get("btts_rate") or 0) * 100, sample_factor)
    under_2_5 = 100 - over_2_5
    under_3_5 = _under_probability(poisson, dixon, 3.5)
    under_1_5 = _under_probability(poisson, dixon, 1.5)
    avg_goals = float(memory.get("avg_goals") or 0)
    goal_env = _goal_environment(rules or {}, memory, over_2_5, btts)

    home_mem = float(memory.get("home_win_rate") or 0) * 100
    draw_mem = float(memory.get("draw_rate") or 0) * 100
    away_mem = float(memory.get("away_win_rate") or 0) * 100
    if sample_factor:
        home = _blend_rate(home, home_mem, sample_factor)
        draw = _blend_rate(draw, draw_mem, sample_factor)
        away = _blend_rate(away, away_mem, sample_factor)

    best_side = max({"Home Win": home, "Draw": draw, "Away Win": away}, key={"Home Win": home, "Draw": draw, "Away Win": away}.get)
    side_conf = {"Home Win": home, "Draw": draw, "Away Win": away}[best_side]
    second_side = sorted([home, draw, away], reverse=True)[1]
    clear_gap = float(get_settings().clear_winner_probability_gap)
    if home >= 55 and home - away >= clear_gap and home > draw:
        pick = _selector_pick("match_result", "Home Win", home, "ensemble home-win probability clears draw and away by threshold")
        pick["clear_winner"] = True
        picks.append(pick)
    elif away >= 55 and away - home >= clear_gap and away > draw:
        pick = _selector_pick("match_result", "Away Win", away, "ensemble away-win probability clears draw and home by threshold")
        pick["clear_winner"] = True
        picks.append(pick)
    # Straight 1X2 needs a real separation. A 45-50% side can be useful for
    # double chance, but it is too fragile as a primary match-winner pick.
    if side_conf >= 55 and side_conf - second_side >= 12 and samples >= 8:
        picks.append(_selector_pick("match_result", best_side, side_conf, "1X2 model and local finished-score memory agree with separation"))

    # When 1X2 lacks clear separation, do not manufacture a side pick from
    # synthetic fallback signals.  The safer protection market is draw-only
    # exclusion ("Home or Away") when draw probability is genuinely low.

    # Double chance as last resort — only when directional picks are unavailable
    dc_options = [
        ("Home or Draw", home + draw, away, "home avoids defeat profile"),
        ("Away or Draw", away + draw, home, "away avoids defeat profile"),
        ("Home or Away", home + away, draw, "draw risk is low from model and database"),
    ]
    side_signal_total = _rules_side_signal_total(rules or {})
    for selection, probability, excluded, reason in dc_options:
        if selection == "Home or Away":
            max_draw = 18 if goal_env.get("profile") == "hot" else 24
            if probability >= 72 and draw <= max_draw:
                pick = _selector_pick("double_chance", selection, min(probability + 3, 84), reason)
                pick["draw_exclusion"] = True
                picks.append(pick)
        elif probability >= 66 and excluded <= 34:
            penalty = _double_chance_conflict_penalty(selection, side_signal_total)
            adjusted_probability = min(probability, 86) - penalty
            if adjusted_probability >= 66:
                pick = _selector_pick("double_chance", selection, adjusted_probability, reason)
                if penalty:
                    pick["reason"] = f"{reason}; reduced for side-signal conflict"
                    pick["evidence"] = {
                        **(pick.get("evidence") or {}),
                        "side_signal_total": round(side_signal_total, 2),
                        "conflict_penalty": round(penalty, 2),
                    }
                picks.append(pick)

    nil_nil = goal_env.get("nil_nil_risk") or {}
    if under_1_5 >= 52 and avg_goals and avg_goals <= 1.8:
        picks.append({
            **_selector_pick("goals", "Under 1.5 goals", under_1_5, "previous final scores point to a very low total"),
            "evidence": {"goal_environment": goal_env},
        })
    elif nil_nil.get("level") in {"medium", "high"} and under_1_5 >= 45 and (not avg_goals or avg_goals <= 2.15):
        confidence = min(78, under_1_5 + float(nil_nil.get("score") or 0) * 3)
        picks.append({
            **_selector_pick("goals", "Under 1.5 goals", confidence, "learned 0-0 profile warns against early over 1.5"),
            "evidence": {"goal_environment": goal_env},
        })
    # Guardrail: Under 2.5 collapses in hot environments (our recent misses are
    # dominated by 2-2 / 3+ totals even when "under" looked priced in).
    under_2_5_allowed = False
    if under_2_5 >= 56 and (not avg_goals or avg_goals <= 2.55):
        if goal_env.get("profile") == "calm":
            under_2_5_allowed = True
        elif goal_env.get("profile") == "warm":
            # Warm environments require a stronger under edge to publish.
            under_2_5_allowed = under_2_5 >= 60 and (not avg_goals or avg_goals <= 2.45) and float(goal_env.get("goal_pressure") or 0) <= 21
        else:
            under_2_5_allowed = False
    if under_2_5_allowed:
        picks.append({
            **_selector_pick("goals", "Under 2.5 goals", under_2_5, "goal model and finished database lean under"),
            "evidence": {"goal_environment": goal_env},
        })
    if _allow_under_3_5(under_3_5, avg_goals, goal_env):
        picks.append({
            **_selector_pick(
                "goals",
                "Under 3.5 goals",
                min(under_3_5, goal_env["confidence_cap"]),
                goal_env["under_reason"],
            ),
            "evidence": {"goal_environment": goal_env},
        })
    if over_2_5 >= 58 and avg_goals >= 2.65:
        picks.append(_selector_pick("goals", "Over 2.5 goals", over_2_5, "model goals and historical finals both run high"))
    if btts >= 58:
        picks.append(_selector_pick("goals", "Both teams to score", btts, "both-side scoring probability is above market threshold"))
    elif btts <= 43 and samples >= 20:
        picks.append(_selector_pick("goals", "Both teams to score - No", 100 - btts, "finished-score memory shows weak both-team scoring"))

    return sorted(
        _dedupe_picks(picks),
        key=lambda item: (1 if item.get("clear_winner") else 0, item.get("confidence") or 0),
        reverse=True,
    )[:5]


def _blended_model_probabilities(
    ensemble: dict[str, Any],
    poisson: dict[str, Any] | None,
    dixon: dict[str, Any] | None,
) -> dict[str, float]:
    totals = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0, "over_2_5": 0.0, "btts": 0.0}
    goal_weight = 0.0

    def add_goal(probs: dict[str, Any], w: float) -> None:
        nonlocal goal_weight
        if not probs:
            return
        for key in ("over_2_5", "btts"):
            totals[key] += float(probs.get(key) or 0) * w
        goal_weight += w

    # Poisson/Dixon-Coles are goal-shape models.  They can support totals/BTTS,
    # but they must not push Home/Draw/Away side picks.
    ensemble_probs = (ensemble or {}).get("probabilities") or {}
    for key in ("home_win", "draw", "away_win"):
        totals[key] = float(ensemble_probs.get(key) or 0)

    add_goal((dixon or {}).get("probabilities") or {}, 0.55)
    add_goal((poisson or {}).get("probabilities") or {}, 0.45)
    for key in ("over_2_5", "btts"):
        totals[key] = round(totals[key] / goal_weight, 2) if goal_weight else float(ensemble_probs.get(key) or 0)
    return {key: round(value, 2) for key, value in totals.items()}


def _rules_side_edge(rules: dict[str, Any]) -> float:
    """Convert evidence-only rules signals into a small 1X2 probability nudge."""
    return max(-8.0, min(8.0, round(_rules_side_signal_total(rules) / 5.0, 2)))


def _rules_side_signal_total(rules: dict[str, Any]) -> float:
    """Aggregate side-specific signals: positive means home, negative means away."""
    side_signal_names = {
        "recent_history_edge",
        "avg_rating_edge",
        "league_strength_edge",
        "h2h_edge",
        "league_position_edge",
        "odds_edge",
        "common_opponent_edge",
        "market_steam",
        "venue_form_edge",
    }
    edge = 0.0
    for signal in rules.get("signals") or []:
        if signal.get("name") not in side_signal_names:
            continue
        impact = _to_float(signal.get("impact"))
        if impact is not None:
            edge += impact
    return edge


def _double_chance_conflict_penalty(selection: str, side_signal_total: float) -> float:
    """Lower DC confidence when the excluded side has meaningful evidence.

    A double-chance pick can survive mild disagreement, but it should not look
    elite when venue, form, standings, and market-side signals lean into the
    side we are excluding.
    """
    conflict = 0.0
    if selection == "Away or Draw" and side_signal_total > 5:
        conflict = side_signal_total - 5
    elif selection == "Home or Draw" and side_signal_total < -5:
        conflict = abs(side_signal_total) - 5
    if conflict <= 0:
        return 0.0
    return min(14.0, round(6.0 + conflict * 0.75, 2))


def _under_probability(poisson: dict[str, Any] | None, dixon: dict[str, Any] | None, line: float) -> float:
    values = []
    for model in (dixon, poisson):
        if not model or model.get("error"):
            continue
        home_lam = _to_float(model.get("home_lambda"))
        away_lam = _to_float(model.get("away_lambda"))
        if home_lam is None or away_lam is None:
            continue
        max_total = int(math.floor(line))
        probability = 0.0
        for home_goals in range(max_total + 1):
            for away_goals in range(max_total + 1):
                if home_goals + away_goals < line:
                    probability += _poisson_probability(home_lam, home_goals) * _poisson_probability(away_lam, away_goals)
        values.append(probability * 100)
    if values:
        return round(sum(values) / len(values), 1)
    over_2_5 = float(((dixon or {}).get("probabilities") or (poisson or {}).get("probabilities") or {}).get("over_2_5") or 0)
    if line == 2.5:
        return round(100 - over_2_5, 1)
    return 0.0


def _goal_selector_context(
    ensemble: dict[str, Any],
    poisson: dict[str, Any] | None,
    dixon: dict[str, Any] | None,
    finished_memory: dict[str, Any] | None,
    rules: dict[str, Any] | None,
) -> dict[str, Any]:
    model_probs = _blended_model_probabilities(ensemble, poisson, dixon)
    memory = {} if _finished_memory_only_global(finished_memory) else ((finished_memory or {}).get("blended") or {})
    sample_factor = _finished_memory_sample_factor(finished_memory)
    over_2_5 = _blend_rate(float(model_probs.get("over_2_5") or 0), float(memory.get("over_2_5_rate") or 0) * 100, sample_factor)
    btts = _blend_rate(float(model_probs.get("btts") or 0), float(memory.get("btts_rate") or 0) * 100, sample_factor)
    context = _goal_environment(rules or {}, memory, over_2_5, btts)
    context["under_3_5_probability"] = _under_probability(poisson, dixon, 3.5)
    context["under_3_5_allowed"] = _allow_under_3_5(
        float(context["under_3_5_probability"]),
        float(memory.get("avg_goals") or 0),
        context,
    )
    return context


def _goal_environment(
    rules: dict[str, Any],
    memory: dict[str, Any],
    over_2_5: float,
    btts: float,
) -> dict[str, Any]:
    goal_pressure = 0.0
    for signal in rules.get("signals") or []:
        if signal.get("name") == "goal_pressure":
            goal_pressure = _to_float(signal.get("value")) or 0.0
            break

    avg_goals = float(memory.get("avg_goals") or 0)
    memory_over = float(memory.get("over_2_5_rate") or 0) * 100
    memory_btts = float(memory.get("btts_rate") or 0) * 100
    blended_over = max(over_2_5, memory_over)
    blended_btts = max(btts, memory_btts)

    hot_reasons: list[str] = []
    if goal_pressure >= 24:
        hot_reasons.append("recent team goal pressure is high")
    if avg_goals >= 3.05:
        hot_reasons.append("finished database average is above three goals")
    if blended_over >= 58:
        hot_reasons.append("over 2.5 environment is active")
    if blended_btts >= 63:
        hot_reasons.append("both-team scoring rate is elevated")

    warm_reasons: list[str] = []
    if 19 <= goal_pressure < 24:
        warm_reasons.append("recent team goal pressure is not fully calm")
    if 2.75 <= avg_goals < 3.05:
        warm_reasons.append("database average is near the danger zone")

    if hot_reasons:
        confidence_cap = 68
        profile = "hot"
    elif warm_reasons:
        confidence_cap = 78
        profile = "warm"
    else:
        confidence_cap = 90
        profile = "calm"

    nil_nil = _nil_nil_risk_profile(rules, memory, goal_pressure, blended_over, blended_btts)

    return {
        "profile": profile,
        "goal_pressure": goal_pressure,
        "avg_goals": avg_goals,
        "over_2_5": blended_over,
        "btts": blended_btts,
        "nil_nil_risk": nil_nil,
        "hot_reasons": hot_reasons,
        "warm_reasons": warm_reasons,
        "confidence_cap": confidence_cap,
        "under_reason": (
            "low goal model is backed by calm recent totals and database memory"
            if profile == "calm"
            else "under 3.5 only passes with reduced confidence because goal environment is mixed"
        ),
    }


def _nil_nil_risk_profile(
    rules: dict[str, Any],
    memory: dict[str, Any],
    goal_pressure: float,
    over_2_5: float,
    btts: float,
) -> dict[str, Any]:
    """Learned 0-0 warning profile from graded history.

    Local graded 0-0s commonly share: lower-than-normal goal pressure, negative
    recent-history/common-opponent edges, weak/negative market steam, and a low
    finished-score goal environment. Use that as a brake on Over 1.5 and a
    selective boost for Under 1.5.
    """
    recent_edge = _signal_metric(rules, "recent_history_edge", default=0.0)
    common_edge = _signal_metric(rules, "common_opponent_edge", default=0.0)
    market_steam = _signal_metric(rules, "market_steam", default=0.0)
    odds_edge = _signal_metric(rules, "odds_edge", default=0.0)
    avg_goals = float(memory.get("avg_goals") or 0)
    draw_rate = float(memory.get("draw_rate") or 0) * 100

    score = 0
    reasons: list[str] = []
    if 0 < goal_pressure <= 31:
        score += 2
        reasons.append("goal_pressure_matches_0_0_band")
    elif 31 < goal_pressure <= 35.5:
        score += 1
        reasons.append("goal_pressure_near_0_0_band")
    if avg_goals and avg_goals <= 2.05:
        score += 2
        reasons.append("finished_memory_low_average_goals")
    elif avg_goals and avg_goals <= 2.30:
        score += 1
        reasons.append("finished_memory_not_goal_heavy")
    if recent_edge <= -1:
        score += 1
        reasons.append("recent_history_edge_negative")
    if common_edge <= -2:
        score += 1
        reasons.append("common_opponent_edge_negative")
    if market_steam <= -4:
        score += 1
        reasons.append("market_steam_cold")
    if over_2_5 <= 48:
        score += 1
        reasons.append("over_2_5_environment_soft")
    if btts <= 52:
        score += 1
        reasons.append("btts_environment_soft")
    if draw_rate >= 30:
        score += 1
        reasons.append("finished_memory_draw_rate_high")
    if odds_edge >= 8 and goal_pressure >= 36:
        score -= 2
        reasons.append("odds_and_goal_pressure_push_against_0_0")

    level = "high" if score >= 6 else "medium" if score >= 4 else "low"
    return {
        "version": "nil_nil_profile_v1",
        "level": level,
        "score": max(0, score),
        "reasons": reasons[:8],
        "learned_from": {
            "zero_zero_matches": 263,
            "over_1_5_zero_zero_losses": 44,
            "zero_zero_goal_pressure_avg": 30.5,
            "scoring_goal_pressure_avg": 34.2,
        },
        "inputs": {
            "goal_pressure": round(goal_pressure, 2),
            "avg_goals": round(avg_goals, 2),
            "recent_history_edge": round(recent_edge, 2),
            "common_opponent_edge": round(common_edge, 2),
            "market_steam": round(market_steam, 2),
            "over_2_5": round(over_2_5, 2),
            "btts": round(btts, 2),
            "draw_rate": round(draw_rate, 2),
        },
    }


def _signal_metric_local(rules: dict[str, Any], name: str, *, default: float = 0.0) -> float:
    # Delegated to signal_aggregator.signal_metric; kept here under a local
    # alias because the many internal callers reference _signal_metric by name.
    return _signal_metric(rules, name, default=default)


def _allow_under_3_5(under_3_5: float, avg_goals: float, goal_env: dict[str, Any]) -> bool:
    profile = goal_env.get("profile")
    over_2_5 = float(goal_env.get("over_2_5") or 0)
    btts = float(goal_env.get("btts") or 0)

    if profile == "hot":
        return under_3_5 >= 92 and (not avg_goals or avg_goals <= 2.45) and over_2_5 <= 45 and btts <= 52
    if profile == "warm":
        return under_3_5 >= 84 and (not avg_goals or avg_goals <= 2.65) and over_2_5 <= 52 and btts <= 58
    return under_3_5 >= 76 and (not avg_goals or avg_goals <= 2.85) and over_2_5 <= 55 and btts <= 60


def _poisson_probability(lam: float, goals: int) -> float:
    return (math.exp(-lam) * (lam ** goals)) / math.factorial(goals)


def _blend_rate(model_rate: float, memory_rate: float, sample_factor: float) -> float:
    memory_weight = 0.35 * sample_factor
    return round(model_rate * (1 - memory_weight) + memory_rate * memory_weight, 1)


def _selector_pick(kind: str, selection: str, confidence: float, reason: str) -> dict[str, Any]:
    return build_pick(kind, selection, confidence, reason, source="market_selector", include_market_intent=True)


def _dedupe_picks(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for pick in picks:
        key = (str(pick.get("type") or ""), str(pick.get("selection") or "").lower())
        current = deduped.get(key)
        if not current or int(pick.get("confidence") or 0) > int(current.get("confidence") or 0):
            deduped[key] = pick
    return list(deduped.values())


def _pick(kind: str, selection: str, confidence: float, reason: str) -> dict[str, Any]:
    return build_pick(kind, selection, confidence, reason, include_market_intent=True)


def _finalize_prediction_output(prediction: dict[str, Any], ensemble: dict[str, Any]) -> None:
    picks = prediction.get("picks") or []
    if picks:
        picks.sort(key=lambda pick: int(pick.get("confidence") or 0), reverse=True)
        _apply_publication_filters(prediction, picks)
        picks.sort(key=lambda pick: int(pick.get("confidence") or 0), reverse=True)
        for index, pick in enumerate(picks):
            pick["role"] = "primary" if index == 0 else "alternative"
            selection = str(pick.get("selection") or "")
            if "or draw protection" in selection.lower():
                pick["selection"] = selection.replace(" or draw protection", " Win")
    primary = picks[0] if picks else {"type": "no_bet", "selection": "No Bet", "confidence": 50}
    probabilities = (ensemble or {}).get("probabilities") or (((prediction.get("models") or {}).get("ensemble") or {}).get("probabilities") or {})
    home_prob = _to_float(probabilities.get("home_win")) or 0.0
    away_prob = _to_float(probabilities.get("away_win")) or 0.0
    draw_prob = _to_float(probabilities.get("draw")) or 0.0
    clear_gap = float(get_settings().clear_winner_probability_gap)
    clear_winner = (
        (home_prob - away_prob >= clear_gap and home_prob > draw_prob)
        or (away_prob - home_prob >= clear_gap and away_prob > draw_prob)
        or bool(primary.get("clear_winner"))
    )
    prediction["clear_winner"] = bool(clear_winner)
    prediction["prediction_label"] = _prediction_label(primary)
    if prediction["prediction_label"] in {"Home Win", "Away Win"}:
        prediction["winner_confidence"] = round(home_prob if prediction["prediction_label"] == "Home Win" else away_prob)
    if primary.get("type") == "no_bet":
        suppressed = primary.get("suppressed_picks") or []
        prediction["secondary_picks"] = [pick for pick in suppressed if int(pick.get("confidence") or 0) >= 60]
    live_summary = _live_match_statistics((prediction.get("data_quality") or {}).get("sofascore_detail") or {})
    if not live_summary.get("has_stats"):
        detail = ((prediction.get("data_sources") or {}).get("sofascore") or {}) if isinstance(prediction.get("data_sources"), dict) else {}
        live_summary = _live_match_statistics(detail if isinstance(detail, dict) else {})
    if (prediction.get("data_quality") or {}).get("live_stats_available"):
        detail_stats = prediction.get("live_statistics_summary")
        prediction["live_statistics_summary"] = detail_stats or live_summary.get("box_score") or {}


def _apply_publication_filters(prediction: dict[str, Any], picks: list[dict[str, Any]]) -> None:
    signals = prediction.get("signals") or []
    direction = _directional_signal_map(signals)
    conflict = all(key in direction for key in ("h2h_edge", "odds_edge", "common_opponent_edge")) and len({
        direction["h2h_edge"],
        direction["odds_edge"],
        direction["common_opponent_edge"],
    }) > 1
    if conflict:
        _append_publication_signal(prediction, "directional_signal_conflict", {
            "h2h": direction["h2h_edge"],
            "market": direction["odds_edge"],
            "common_opponents": direction["common_opponent_edge"],
            "action": "cap_high_confidence",
        })

    blocked: list[dict[str, Any]] = []
    for pick in picks:
        selection = str(pick.get("selection") or "")
        normalized = selection.lower()
        reasons: list[str] = []
        if conflict:
            reasons.append("h2h_market_common_opponent_conflict")
        if "away or draw" in normalized and not _all_directional_support(direction, "away", ("odds_edge", "recent_history_edge", "common_opponent_edge")):
            reasons.append("away_or_draw_requires_market_recent_common_support")
        if ("home win" in normalized or normalized == "home") and not _home_win_has_enough_support(direction):
            reasons.append("home_win_requires_market_plus_recent_or_common_support")
        excluded_side = _double_chance_excluded_side(normalized)
        if excluded_side:
            exclusion = _excluded_side_strength(signals, excluded_side)
            if exclusion["block"]:
                reasons.append("double_chance_excludes_clear_stronger_side")
                pick.setdefault("publication_filter", {})
                pick["publication_filter"]["blocked"] = True
                pick["publication_filter"]["reasons"] = reasons
                pick["publication_filter"]["excluded_side_strength"] = exclusion
                pick["reason"] = f"{pick.get('reason') or ''}; blocked by publication filter: {', '.join(reasons)}"
                blocked.append(pick)
                continue

        if reasons and int(pick.get("confidence") or 0) >= 65:
            pick["confidence"] = 64
            pick.setdefault("publication_filter", {})
            pick["publication_filter"]["capped"] = True
            pick["publication_filter"]["reasons"] = reasons
            pick["reason"] = f"{pick.get('reason') or ''}; capped by publication filter: {', '.join(reasons)}"
    if blocked:
        picks[:] = [pick for pick in picks if not (pick.get("publication_filter") or {}).get("blocked")]
        if not picks:
            picks.append({
                "type": "no_bet",
                "selection": "No Bet",
                "confidence": 50,
                "reason": "blocked side pick because double chance excluded the clearer stronger side",
                "suppressed_picks": blocked,
            })


def _directional_signal_map(signals: list[dict[str, Any]]) -> dict[str, str]:
    direction: dict[str, str] = {}
    for signal in signals:
        name = str(signal.get("name") or "")
        impact = _to_float(signal.get("impact"))
        if impact is None or abs(impact) < 1.5:
            continue
        if name in {"h2h_edge", "odds_edge", "common_opponent_edge", "recent_history_edge", "league_position_edge", "venue_form_edge", "avg_rating_edge", "market_steam"}:
            direction[name] = "home" if impact > 0 else "away"
    return direction


def _double_chance_excluded_side(normalized_selection: str) -> str | None:
    if "home or draw" in normalized_selection:
        return "away"
    if "away or draw" in normalized_selection:
        return "home"
    return None


def _excluded_side_strength(signals: list[dict[str, Any]], side: str) -> dict[str, Any]:
    """Return whether a double-chance pick is excluding a clearly stronger team.

    Positive impacts mean home edge; negative impacts mean away edge.  We only
    block when several independent side signals point toward the excluded team,
    so a single noisy venue/form metric cannot veto a pick by itself.
    """
    if side not in {"home", "away"}:
        return {"block": False, "side": side, "support_count": 0, "total": 0.0, "signals": []}
    side_names = {
        "avg_rating_edge",
        "odds_edge",
        "market_steam",
        "league_position_edge",
        "common_opponent_edge",
        "recent_history_edge",
        "venue_form_edge",
        "h2h_edge",
    }
    strong_names = {"avg_rating_edge", "odds_edge", "market_steam", "league_position_edge", "common_opponent_edge"}
    support: list[dict[str, Any]] = []
    opposing_total = 0.0
    for signal in signals:
        name = str(signal.get("name") or "")
        if name not in side_names:
            continue
        impact = _to_float(signal.get("impact"))
        if impact is None:
            continue
        side_impact = impact if side == "home" else -impact
        if side_impact >= 3:
            support.append({"name": name, "impact": round(side_impact, 2), "strong": name in strong_names})
        elif side_impact <= -3:
            opposing_total += abs(side_impact)
    total = round(sum(float(item["impact"]) for item in support), 2)
    strong_count = sum(1 for item in support if item.get("strong"))
    support_count = len(support)
    block = (strong_count >= 2 and total >= 14 and total - opposing_total >= 6) or (strong_count >= 3 and total >= 11)
    return {
        "block": bool(block),
        "side": side,
        "support_count": support_count,
        "strong_count": strong_count,
        "total": total,
        "opposing_total": round(opposing_total, 2),
        "signals": support,
    }


def _all_directional_support(direction: dict[str, str], side: str, names: tuple[str, ...]) -> bool:
    return all(direction.get(name) == side for name in names)


def _home_win_has_enough_support(direction: dict[str, str]) -> bool:
    if direction.get("odds_edge") != "home":
        return False
    return direction.get("recent_history_edge") == "home" or direction.get("common_opponent_edge") == "home"


def _append_publication_signal(prediction: dict[str, Any], name: str, value: dict[str, Any]) -> None:
    signals = prediction.setdefault("signals", [])
    if any(signal.get("name") == name for signal in signals):
        return
    signals.append({"name": name, "value": value, "impact": -6, "role": "risk_signal"})


def _prediction_label(pick: dict[str, Any]) -> str:
    if pick.get("type") == "no_bet":
        return "No Bet"
    selection = str(pick.get("selection") or "")
    normalized = selection.lower()
    if normalized in {"home win", "away win", "draw"}:
        return selection.title() if normalized == "draw" else selection
    if "home win" in normalized or normalized == "home":
        return "Home Win"
    if "away win" in normalized or normalized == "away":
        return "Away Win"
    if "home or draw" in normalized:
        return "Home or Draw"
    if "away or draw" in normalized:
        return "Away or Draw"
    if "over 2.5" in normalized:
        return "Over 2.5 Goals"
    if "under 2.5" in normalized:
        return "Under 2.5 Goals"
    if "both teams" in normalized and "no" in normalized:
        return "Both Teams Not to Score"
    if "both teams" in normalized or "btts" in normalized:
        return "Both Teams to Score"
    return "No Bet" if not selection else selection


def _model_signals(
    poisson: dict[str, Any] | None,
    dixon: dict[str, Any] | None,
    elo: dict[str, Any] | None,
    ensemble: dict[str, Any],
    doc: dict[str, Any],
) -> list[dict[str, Any]]:
    signals = []
    has_poisson = bool(poisson and not poisson.get("error"))
    has_dixon = bool(dixon and not dixon.get("error"))
    if has_poisson and has_dixon:
        signals.append({
            "name": "goal_model_family",
            "value": {
                "poisson": poisson.get("probabilities"),
                "dixon_coles": dixon.get("probabilities"),
                "note": "poisson and dixon-coles are correlated goal models and count as one family",
                "applies_to": ["goals", "btts"],
            },
            "impact": 0,
            "role": "goal_market_evidence",
        })
    elif has_poisson:
        signals.append({"name": "poisson_model", "value": poisson.get("probabilities"), "impact": _prob_impact(poisson), "role": "supporting_evidence"})
    elif has_dixon:
        signals.append({"name": "dixon_coles_model", "value": dixon.get("probabilities"), "impact": _prob_impact(dixon), "role": "supporting_evidence"})
    if elo and not elo.get("error"):
        signals.append({"name": "elo_model", "value": elo, "impact": round((elo.get("home_win_probability", 50) - 50) / 3, 2), "role": "supporting_evidence"})
    if ensemble and not ensemble.get("error"):
        signals.append({"name": "ensemble_model", "value": ensemble, "impact": round((ensemble.get("confidence", 50) - 50) / 2, 2)})
    consensus = _model_consensus_signal(poisson, dixon, elo, ensemble)
    if consensus:
        signals.append(consensus)
    web = doc.get("web_context") or {}
    snippets = web.get("snippets") or []
    scraped = web.get("scraped") or []
    open_router = web.get("open_router_analysis") or {}
    attempts = web.get("attempts") or []
    web_impact = 0
    if web.get("error") or web.get("disabled"):
        web_impact = -1
    elif snippets or scraped:
        web_impact = min(4, len(snippets) + (2 if scraped else 0))
    signals.append(
        {
            "name": "web_context",
            "value": {
                "query": web.get("query"),
                "snippets": len(snippets),
                "scraped": len(scraped),
                "open_router_status": open_router.get("status"),
                "open_router_evidence": len(open_router.get("evidence") or []),
                "attempts": attempts,
                "source_titles": [
                    item.get("title")
                    for item in snippets[:3]
                    if isinstance(item, dict) and item.get("title")
                ],
                "error": web.get("error"),
                "disabled": web.get("disabled"),
            },
            "impact": web_impact,
        }
    )
    # Add sentiment signal from Open Router analysis when available
    sentiment = open_router.get("sentiment") or {}
    if isinstance(sentiment, dict) and sentiment.get("home_sentiment"):
        sentiment_conf = sentiment.get("confidence", 50)
        home_sent = sentiment.get("home_sentiment", "neutral")
        away_sent = sentiment.get("away_sentiment", "neutral")
        overall_sent = sentiment.get("overall_sentiment", "neutral")
        # Derive a directional impact: positive sentiment for home = positive impact
        sentiment_direction = {"positive": 1, "negative": -1, "neutral": 0, "mixed": 0}
        home_dir = sentiment_direction.get(home_sent, 0)
        away_dir = sentiment_direction.get(away_sent, 0)
        net_direction = home_dir - away_dir
        signals.append(
            {
                "name": "web_sentiment",
                "value": {
                    "home_sentiment": home_sent,
                    "away_sentiment": away_sent,
                    "overall_sentiment": overall_sent,
                    "confidence": sentiment_conf,
                    "net_direction": net_direction,
                },
                "impact": round(net_direction * sentiment_conf / 25, 2),
                "role": "supporting_evidence",
            }
        )
    # Add probability signal from Open Router analysis when available
    probability = open_router.get("probability") or {}
    if isinstance(probability, dict) and any(
        probability.get(k) is not None for k in ("implied_home_win", "implied_draw", "implied_away_win")
    ):
        prob_home = probability.get("implied_home_win")
        prob_draw = probability.get("implied_draw")
        prob_away = probability.get("implied_away_win")
        signals.append(
            {
                "name": "web_probability",
                "value": {
                    "implied_home_win": prob_home,
                    "implied_draw": prob_draw,
                    "implied_away_win": prob_away,
                    "source": "web_research",
                },
                "impact": round((prob_home - 50) / 10 if prob_home is not None else 0, 2),
                "role": "supporting_evidence",
            }
        )
    return signals


def _model_consensus_signal(
    poisson: dict[str, Any] | None,
    dixon: dict[str, Any] | None,
    elo: dict[str, Any] | None,
    ensemble: dict[str, Any] | None,
) -> dict[str, Any] | None:
    votes: list[str] = []
    for model in (poisson, dixon):
        if not model or model.get("error"):
            continue
        probs = model.get("probabilities") or {}
        side = max(
            ("home_win", "draw", "away_win"),
            key=lambda key: _to_float(probs.get(key)) or 0,
        )
        votes.append(side)
    if elo and not elo.get("error"):
        home = _to_float(elo.get("home_win_probability")) or 0
        away = _to_float(elo.get("away_win_probability")) or 0
        draw = _to_float(elo.get("draw_probability")) or 0
        votes.append(max({"home_win": home, "draw": draw, "away_win": away}, key={"home_win": home, "draw": draw, "away_win": away}.get))
    if len(votes) < 2:
        return None
    top = Counter(votes).most_common(1)[0]
    agreement = top[1] / len(votes)
    if agreement < 0.67:
        return {
            "name": "model_consensus",
            "value": {"votes": votes, "agreement": round(agreement, 2), "selection": None},
            "impact": 0,
            "role": "background_context",
        }
    confidence = _to_float((ensemble or {}).get("confidence")) or 50
    return {
        "name": "model_consensus",
        "value": {"votes": votes, "agreement": round(agreement, 2), "selection": top[0]},
        "impact": min(6, round((confidence - 50) / 6, 2)),
        "role": "supporting_evidence",
    }


def _prob_impact(model: dict[str, Any]) -> float:
    probs = model.get("probabilities") or {}
    return round((max(float(probs.get("home_win") or 0), float(probs.get("away_win") or 0), float(probs.get("draw") or 0)) - 33.3) / 3, 2)


def _correlated_model_family_impact(poisson: dict[str, Any], dixon: dict[str, Any]) -> float:
    return round((_prob_impact(poisson) + _prob_impact(dixon)) / 2, 2)


def _market_adjustment(ensemble: dict[str, Any], odds_movement: dict[str, Any]) -> int:
    pull = odds_movement.get("strongest_pull") or {}
    direction = pull.get("direction")
    selection = str(pull.get("selection") or "").lower()
    if not direction or direction == "stable" or not selection:
        return 0

    prediction = str((ensemble or {}).get("prediction") or "").lower()
    if "home" in prediction:
        predicted_side = "home"
    elif "away" in prediction:
        predicted_side = "away"
    elif "draw" in prediction:
        predicted_side = "draw"
    else:
        return 0

    strength = pull.get("strength")
    base = 5 if strength == "strong" else 3 if strength == "moderate" else 1
    if selection == predicted_side and direction == "backed":
        return base
    if selection == predicted_side and direction == "faded":
        return -base
    if selection != predicted_side and direction == "backed":
        return -max(1, base - 1)
    return 0


def _finished_memory_adjustment(ensemble: dict[str, Any], memory: dict[str, Any]) -> int:
    blended = memory.get("blended") or {}
    if not blended or _finished_memory_only_global(memory):
        return 0
    prediction = str((ensemble or {}).get("prediction") or "").lower()
    if "home" in prediction:
        rate = float(blended.get("home_win_rate") or 0)
    elif "draw" in prediction:
        rate = float(blended.get("draw_rate") or 0)
    elif "away" in prediction:
        rate = float(blended.get("away_win_rate") or 0)
    else:
        return 0
    baseline = 0.333
    sample_factor = _finished_memory_sample_factor(memory, divisor=100)
    return max(-6, min(6, round((rate - baseline) * 18 * sample_factor)))


# based entry point for callers that prefer object-oriented composition.


def _regime_info(doc: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.market.regime import get_regime_for_doc
        r = get_regime_for_doc(doc)
        return {
            "tier":           r.tier,
            "name":           r.name,
            "min_confidence": r.min_confidence,
            "edge_threshold": r.edge_threshold,
            "stake_cap":      r.stake_cap,
            "description":    r.description,
        }
    except Exception:
        return {}


def _normalise_team_key(name: str) -> str:
    """Normalise a team name to a stable dash-separated lowercase key.

    Matches the convention used when recording team_competitions rows:
    non-alphanumeric characters are replaced with spaces, then joined
    with dashes after stripping.
    """
    return "-".join("".join(ch.lower() if ch.isalnum() else " " for ch in str(name or "")).split())


def _team_history_signals(doc: dict[str, Any], detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Return team prediction history signals derived from the ``team_competitions`` table.

    For each team (home and away) the function queries ``team_competitions`` for
    the current competition and emits one of the following signals when the team
    has at least 10 recorded predictions:

    * ``team_prediction_history_risk``  (impact = -3) — accuracy < 40 %
    * ``team_prediction_history_boost`` (impact = +2) — accuracy >= 60 %

    No signal is emitted when fewer than 10 predictions are available or when
    accuracy falls in the neutral 40–60 % range.

    Any DB error (including the table not existing) is swallowed and an empty
    list is returned so that the calling code is never interrupted.

    Requirements: R11.1, R11.2, R11.3, R11.4, R11.5, R11.6
    """
    signals: list[dict[str, Any]] = []
    try:
        home_team = detail.get("home_team") or detail.get("homeTeam") or {}
        away_team = detail.get("away_team") or detail.get("awayTeam") or {}
        home_name = (home_team.get("name") or "") if isinstance(home_team, dict) else ""
        away_name = (away_team.get("name") or "") if isinstance(away_team, dict) else ""
        home_key = _normalise_team_key(home_name)
        away_key = _normalise_team_key(away_name)

        # Derive competition key using the same normaliser used when the rows
        # were written — normalize_league from league_memory.
        from app.storage.league_memory import normalize_league  # avoid circular imports
        tournament_raw = doc.get("tournament") or ""
        if isinstance(tournament_raw, dict):
            tournament_raw = tournament_raw.get("name") or ""
        comp_key = normalize_league(str(tournament_raw))

        _init_db()
        with db_conn(timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            for team_key, side, team_name in [
                (home_key, "home", home_name),
                (away_key, "away", away_name),
            ]:
                if not team_key or not comp_key:
                    continue
                row = conn.execute(
                    "SELECT prediction_total, prediction_correct "
                    "FROM team_competitions "
                    "WHERE team_key = ? AND competition_key = ? LIMIT 1",
                    (team_key, comp_key),
                ).fetchone()
                if not row:
                    continue
                total = int(row["prediction_total"] or 0)
                if total < 10:
                    continue
                correct = int(row["prediction_correct"] or 0)
                accuracy = correct / total
                if accuracy < 0.40:
                    signals.append({
                        "name": "team_prediction_history_risk",
                        "value": {
                            "team": team_name,
                            "team_key": team_key,
                            "side": side,
                            "accuracy": round(accuracy, 4),
                            "prediction_total": total,
                            "prediction_correct": correct,
                        },
                        "impact": -3,
                        "role": "risk_signal",
                    })
                elif accuracy >= 0.60:
                    signals.append({
                        "name": "team_prediction_history_boost",
                        "value": {
                            "team": team_name,
                            "team_key": team_key,
                            "side": side,
                            "accuracy": round(accuracy, 4),
                            "prediction_total": total,
                            "prediction_correct": correct,
                        },
                        "impact": 2,
                        "role": "decision_driver",
                    })
    except Exception:
        pass
    return signals


def _detail_country(detail: dict[str, Any]) -> str | None:
    tournament = detail.get("tournament") or {}
    if isinstance(tournament, dict):
        category = tournament.get("category") or {}
        if isinstance(category, dict) and category.get("name"):
            return str(category.get("name"))
    return None


def _probability_unit(value: Any) -> float | None:
    number = _to_float(value)
    if number is None:
        return None
    probability = number / 100 if number > 1 else number
    return max(0.0, min(1.0, probability))


class EnrichedPrediction:
    """Orchestration entry point for the enriched prediction pipeline.

    Wraps :func:`predict_enriched_match` and :func:`prediction_readiness` in a
    class interface for callers that prefer object composition.

    Usage::

        ep = EnrichedPrediction(doc)
        if ep.is_ready():
            result = ep.predict()

    Signal helpers and confidence calibration are fully delegated to
    :mod:`app.enrichment.signal_aggregator` and
    :mod:`app.enrichment.confidence_calibrator` respectively.
    """

    def __init__(self, doc: dict[str, Any]) -> None:
        self._doc = doc
        self._readiness: dict[str, Any] | None = None

    # ── Readiness ─────────────────────────────────────────────────────────────

    def readiness(self) -> dict[str, Any]:
        """Return the data-readiness report for the wrapped match document."""
        if self._readiness is None:
            self._readiness = prediction_readiness(self._doc)
        return self._readiness

    def is_ready(self) -> bool:
        """Return True when the match document has sufficient data to predict."""
        return bool(self.readiness().get("ready"))

    # ── Prediction ────────────────────────────────────────────────────────────

    def predict(self) -> dict[str, Any]:
        """Run the full enriched prediction pipeline and return the result dict.

        Raises:
            ValueError: when the document is not yet ready (missing data fields).
        """
        return predict_enriched_match(self._doc)





