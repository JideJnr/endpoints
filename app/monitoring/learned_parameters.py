from __future__ import annotations

import json
import sqlite3
import time
from functools import lru_cache
from statistics import median
from typing import Any

from app.storage.db import db_conn
from app.storage.league_memory import _init_db


_GRADED_SQL = """
    select *
    from (
        select
            ph.*,
            row_number() over (
                partition by match_id, pick_type, selection
                order by datetime(coalesce(graded_at, created_at)) desc, id desc
            ) as rn
        from (
            select id, match_id, league_name, country_name, pick_type,
                   selection, confidence, result, signals_json, picks_json,
                   audit_json, models_json, created_at, graded_at
            from prediction_history
            where graded_at is not null
              and result in ('win', 'loss')
              and pick_type != 'no_bet'
            union all
            select id, match_id, league_name, country_name, pick_type,
                   selection, confidence, result, signals_json, '[]' as picks_json,
                   audit_json, '{}' as models_json, created_at, graded_at
            from prediction_candidate_history
            where graded_at is not null
              and result in ('win', 'loss')
              and pick_type != 'no_bet'
        ) ph
    )
    where rn = 1
"""

_GRADED_ROWS_CACHE: tuple[dict[str, Any], ...] | None = None
_GRADED_ROWS_FETCHED_AT: float = 0.0
_GRADED_ROWS_TTL: int = 3600


def clear_learned_parameter_cache() -> None:
    global _GRADED_ROWS_CACHE, _GRADED_ROWS_FETCHED_AT
    get_learned_ensemble_weights.cache_clear()
    get_market_regime_params.cache_clear()
    get_pick_generator_thresholds.cache_clear()
    get_prediction_agent_params.cache_clear()
    get_calibration_gap_thresholds.cache_clear()
    get_league_goal_average.cache_clear()
    get_frontend_engine_params.cache_clear()
    get_frontend_api_limits.cache_clear()
    get_engine_learning_limits.cache_clear()
    _GRADED_ROWS_CACHE = None
    _GRADED_ROWS_FETCHED_AT = 0.0


def _graded_rows() -> tuple[dict[str, Any], ...]:
    global _GRADED_ROWS_CACHE, _GRADED_ROWS_FETCHED_AT
    now = time.monotonic()
    if _GRADED_ROWS_CACHE is not None and now - _GRADED_ROWS_FETCHED_AT < _GRADED_ROWS_TTL:
        return _GRADED_ROWS_CACHE
    _init_db()
    try:
        with db_conn(timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            _GRADED_ROWS_CACHE = tuple(dict(row) for row in conn.execute(_GRADED_SQL).fetchall())
    except Exception:
        _GRADED_ROWS_CACHE = ()
    _GRADED_ROWS_FETCHED_AT = now
    return _GRADED_ROWS_CACHE


@lru_cache(maxsize=1)
def get_learned_ensemble_weights(min_samples: int = 15) -> dict[str, float]:
    model_names = ("dixon_coles", "elo", "poisson", "rules", "llm")
    stats = {name: {"samples": 0, "wins": 0} for name in model_names}
    signal_map = {
        "dixon_coles": {"dixon_coles_model"},
        "elo": {"elo_model"},
        "poisson": {"poisson_model"},
        "rules": {"goal_pressure", "h2h_edge", "league_position_edge", "recent_history_edge", "common_opponent_edge", "avg_rating_edge", "market_steam", "odds_edge"},
        "llm": {"openrouter_agent", "ai_brain_review"},
    }
    for row in _graded_rows():
        result = row.get("result")
        model_hits = _models_for_row(row)
        if not model_hits:
            signal_names = {str(sig.get("name") or "") for sig in _json_list(row.get("signals_json"))}
            model_hits = {model for model, names in signal_map.items() if signal_names & names}
        for model in model_hits:
            if model not in stats:
                continue
            stats[model]["samples"] += 1
            if result == "win":
                stats[model]["wins"] += 1

    usable = {
        model: values
        for model, values in stats.items()
        if values["samples"] >= min_samples
    }
    if not usable:
        return {}

    raw = {
        model: max(0.01, values["wins"] / values["samples"])
        for model, values in usable.items()
    }
    total = sum(raw.values())
    return {model: round(weight / total, 4) for model, weight in raw.items() if total > 0}


@lru_cache(maxsize=1)
def get_market_regime_params(min_samples: int = 8) -> dict[int, dict[str, Any]]:
    leagues: list[dict[str, Any]] = []
    by_league: dict[str, dict[str, Any]] = {}
    for row in _graded_rows():
        league = _norm(row.get("league_name"))
        if not league:
            continue
        item = by_league.setdefault(league, {"samples": 0, "wins": 0, "winning_conf": [], "losing_conf": []})
        item["samples"] += 1
        conf = _num(row.get("confidence"))
        if row.get("result") == "win":
            item["wins"] += 1
            if conf is not None:
                item["winning_conf"].append(conf)
        elif conf is not None:
            item["losing_conf"].append(conf)
    for league, item in by_league.items():
        if item["samples"] >= min_samples:
            leagues.append({**item, "league": league, "win_rate": item["wins"] / item["samples"]})
    if not leagues:
        return {}
    leagues.sort(key=lambda item: item["win_rate"], reverse=True)
    buckets = {
        1: leagues[: max(1, len(leagues) // 4)],
        2: leagues[max(1, len(leagues) // 4): max(2, len(leagues) // 2)],
        3: leagues[max(2, len(leagues) // 2): max(3, len(leagues) * 3 // 4)],
        4: leagues[max(3, len(leagues) * 3 // 4):] or leagues[-1:],
    }
    params: dict[int, dict[str, Any]] = {}
    for tier, rows in buckets.items():
        wins = [conf for item in rows for conf in item["winning_conf"]]
        losses = [conf for item in rows for conf in item["losing_conf"]]
        win_rate = sum(item["wins"] for item in rows) / max(1, sum(item["samples"] for item in rows))
        threshold = _percentile(wins, 35 if tier < 4 else 65)
        loss_med = median(losses) if losses else threshold
        edge_threshold = max(0.0, min(0.20, (threshold - loss_med) / 100 if threshold and loss_med else 0.0))
        params[tier] = {
            "min_confidence": int(round(threshold)) if threshold is not None else None,
            "edge_threshold": round(edge_threshold, 4),
            "clv_min_samples": max(1, min(item["samples"] for item in rows)),
            "stake_cap": round(max(0.25, min(2.5, win_rate * 2.5)), 2),
            "samples": sum(item["samples"] for item in rows),
            "win_rate": round(win_rate, 4),
        }
    return params


@lru_cache(maxsize=1)
def get_pick_generator_thresholds(min_samples: int = 10) -> dict[str, float]:
    wins_by_side: dict[str, list[float]] = {"home": [], "away": [], "draw": []}
    value_edges: list[float] = []
    for row in _graded_rows():
        if row.get("result") != "win":
            continue
        side = _selection_side(row.get("selection"))
        conf = _num(row.get("confidence"))
        if side and conf is not None:
            wins_by_side[side].append(conf / 100 if conf > 1 else conf)
        for pick in _json_list(row.get("picks_json")):
            edge = _num(pick.get("value_edge"))
            if edge is not None and edge > 0:
                value_edges.append(edge)
    result: dict[str, float] = {}
    for side, values in wins_by_side.items():
        if len(values) >= min_samples:
            result[f"{side}_baseline"] = round(max(0.01, min(0.99, _percentile(values, 25) or 0)), 4)
    all_wins = [value for values in wins_by_side.values() for value in values]
    if len(all_wins) >= min_samples:
        result["minimum"] = round(max(0.01, min(0.99, _percentile(all_wins, 10) or 0)), 4)
        result["medium"] = round(max(0.01, min(0.99, _percentile(all_wins, 35) or 0)), 4)
        result["high"] = round(max(0.01, min(0.99, _percentile(all_wins, 70) or 0)), 4)
    if len(value_edges) >= min_samples:
        result["value_edge"] = round(max(0.0, _percentile(value_edges, 25) or 0), 4)
    return result


@lru_cache(maxsize=1)
def get_prediction_agent_params(min_samples: int = 10) -> dict[str, float]:
    rows = list(_graded_rows())
    if len(rows) < min_samples:
        return {}
    wins = [_num(row.get("confidence")) for row in rows if row.get("result") == "win"]
    losses = [_num(row.get("confidence")) for row in rows if row.get("result") == "loss"]
    wins = [v for v in wins if v is not None]
    losses = [v for v in losses if v is not None]
    if len(wins) < min_samples:
        return {}
    win_floor = _percentile(wins, 20) or 0
    loss_mid = median(losses) if losses else win_floor
    return {
        "directional_base_confidence": round(win_floor, 2),
        "no_bet_confidence": round(max(1.0, min(win_floor, loss_mid)), 2),
        "common_opponent_goal_diff_weight": _learn_signal_scale("common_opponent_edge", 0.35),
        "common_opponent_edge_scale": _learn_signal_scale("common_opponent_edge", 1.0),
        "form_edge_scale": _learn_signal_scale("recent_history_edge", 1.0),
        "opponent_top_weight": _learn_side_weight("top"),
        "opponent_bottom_weight": _learn_side_weight("bottom"),
    }


@lru_cache(maxsize=1)
def get_calibration_gap_thresholds(min_samples: int = 3) -> dict[str, float]:
    try:
        _init_db()
        with db_conn(timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                select band_low, win_rate
                from confidence_calibration
                where pick_type = '__global__'
                  and samples >= ?
                  and win_rate is not null
            """, (min_samples,)).fetchall()
    except Exception:
        rows = []
    gaps = sorted(abs((int(row["band_low"]) + 5) - float(row["win_rate"]) * 100) for row in rows)
    if len(gaps) < min_samples:
        return {}
    return {
        "moderate": round(_percentile(gaps, 50) or 0, 2),
        "severe": round(_percentile(gaps, 75) or 0, 2),
    }


@lru_cache(maxsize=1)
def get_league_goal_average(min_matches: int = 20) -> dict[str, Any]:
    totals: list[float] = []
    try:
        _init_db()
        with db_conn(timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'").fetchall()}
            if "finished_matches" in tables:
                rows = conn.execute("select raw_json from finished_matches order by rowid desc limit 5000").fetchall()
                for row in rows:
                    doc = json.loads(row["raw_json"] or "{}")
                    score = doc.get("score") or {}
                    home = _num(score.get("home"))
                    away = _num(score.get("away"))
                    if home is not None and away is not None:
                        totals.append(home + away)
    except Exception:
        pass
    if len(totals) < min_matches:
        return {"available": False, "samples": len(totals)}
    avg_total = sum(totals) / len(totals)
    return {"available": True, "samples": len(totals), "team_goal_average": round(avg_total / 2, 4), "match_goal_average": round(avg_total, 4)}


@lru_cache(maxsize=1)
def get_frontend_engine_params() -> dict[str, Any]:
    goal_avg = get_league_goal_average(min_matches=5)
    weights = get_learned_ensemble_weights(min_samples=5)
    params: dict[str, Any] = {"source": "historical"}
    if goal_avg.get("available"):
        params["leagueAvgGoals"] = goal_avg["team_goal_average"]
        params["maxGoals"] = max(4, min(14, int(round(goal_avg["match_goal_average"] * 3))))
    if weights:
        params["ELO_K"] = round(16 + 48 * weights.get("elo", 0), 2)
        params["ELO_HOME_ADVANTAGE"] = round(50 + 150 * _home_win_rate(), 2)
        params["DEFAULT_ELO"] = round(1000 + 1000 * max(weights.values()), 2)
    return params


@lru_cache(maxsize=1)
def get_frontend_api_limits() -> dict[str, int]:
    samples = len(_graded_rows())
    if samples <= 0:
        return {}
    return {
        "similarMatches": max(5, min(25, samples // 20)),
        "systemActivity": max(10, min(100, samples // 10)),
        "competitionBuffer": max(50, min(500, samples)),
        "teamWatchers": max(20, min(200, samples // 5)),
        "predictionHistory": max(50, min(1000, samples)),
        "predictionCheckData": max(100, min(2000, samples * 2)),
        "engineDashboard": max(500, min(20000, samples * 10)),
        "modelExplorer": max(100, min(5000, samples * 2)),
        "sofaIngest": max(50, min(500, samples // 2)),
    }


@lru_cache(maxsize=1)
def get_engine_learning_limits() -> dict[str, int]:
    samples = len(_graded_rows())
    if samples <= 0:
        return {}
    return {
        "matchHistory": max(10, min(500, samples // 4)),
        "topEngines": max(3, min(50, int(max(3, samples ** 0.5)))),
        "topRules": max(5, min(100, int(max(5, samples ** 0.5) * 2))),
    }


def _json_list(raw: Any) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    return [item for item in value or [] if isinstance(item, dict)] if isinstance(value, list) else []


def _models_for_row(row: dict[str, Any]) -> set[str]:
    try:
        models = json.loads(row.get("models_json") or "{}")
    except Exception:
        models = {}
    result = set()
    if not isinstance(models, dict):
        return result
    for name in ("dixon_coles", "elo", "poisson", "rules", "llm", "openrouter"):
        if models.get(name):
            result.add("llm" if name == "openrouter" else name)
    return result


def _learn_signal_scale(signal_name: str, neutral: float) -> float:
    wins: list[float] = []
    losses: list[float] = []
    for row in _graded_rows():
        for sig in _json_list(row.get("signals_json")):
            if str(sig.get("name") or "") != signal_name:
                continue
            impact = abs(_num(sig.get("impact")) or _num(sig.get("value")) or 0)
            (wins if row.get("result") == "win" else losses).append(impact)
    if len(wins) < 5:
        return neutral
    win_med = median(wins)
    loss_med = median(losses) if losses else win_med
    return round(max(0.1, min(3.0, neutral * (1 + (win_med - loss_med) / 10))), 3)


def _learn_side_weight(bucket: str) -> float:
    params = get_pick_generator_thresholds(min_samples=5)
    baseline = params.get("home_baseline" if bucket == "top" else "away_baseline")
    if baseline is None:
        return 1.0
    return round(max(0.2, min(2.0, baseline * 2)), 3)


def _home_win_rate() -> float:
    rows = [row for row in _graded_rows() if _selection_side(row.get("selection")) == "home"]
    if not rows:
        return 0.5
    return sum(1 for row in rows if row.get("result") == "win") / len(rows)


def _selection_side(selection: Any) -> str | None:
    text = str(selection or "").lower()
    if text in {"1", "home"} or "home" in text:
        return "home"
    if text in {"2", "away"} or "away" in text:
        return "away"
    if text in {"x", "draw"} or "draw" in text:
        return "draw"
    return None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    idx = (len(ordered) - 1) * pct / 100
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    frac = idx - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _num(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("current") if value.get("current") is not None else value.get("display")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().replace("-", " ").replace("_", " ").split())
