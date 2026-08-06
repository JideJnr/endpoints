from __future__ import annotations

import sqlite3
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db


def learned_best_pick(picks: list[dict[str, Any]]) -> dict[str, Any]:
    learned = [pick for pick in picks if pick.get("learned_best")]
    if learned:
        return max(
            learned,
            key=lambda pk: float(((pk.get("learned_role_decision") or {}).get("score") or pk.get("confidence") or 0)),
        )
    decision_picks = [
        pick for pick in picks
        if (pick.get("learned_role_decision") or {}).get("selection") == pick.get("selection")
        and (pick.get("learned_role_decision") or {}).get("type") == pick.get("type")
    ]
    if decision_picks:
        return max(
            decision_picks,
            key=lambda pk: float(((pk.get("learned_role_decision") or {}).get("score") or pk.get("confidence") or 0)),
        )
    return max(picks, key=lambda pk: int(pk.get("confidence") or 0))


def load_role_memory_rows() -> dict[tuple[str, str], list[dict[str, Any]]]:
    try:
        _init_db()
        with db_conn(timeout=20) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select role, league_name, country_name, pick_type, selection, result
                from prediction_candidate_history
                where graded_at is not null
                  and result in ('win', 'loss')
                order by created_at desc
                limit 5000
                """
            ).fetchall()
        index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            data = dict(row)
            key = (str(data.get("pick_type") or ""), str(data.get("selection") or "").lower())
            index.setdefault(key, []).append(data)
        return index
    except Exception:
        return {}


def backfill_role_learning(
    prediction: dict[str, Any],
    picks: list[dict[str, Any]],
    match_id: str,
    role_rows: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
) -> None:
    if not picks or all(pick.get("role_learning") for pick in picks):
        return
    try:
        context = {
            "tournament": prediction.get("league_name") or prediction.get("tournament"),
            "category": prediction.get("country_name") or prediction.get("country"),
        }
        for pick in picks:
            if pick.get("role_learning"):
                continue
            memory = fast_role_memory(
                str(context.get("tournament") or ""),
                str(context.get("category") or ""),
                str(pick.get("type") or ""),
                str(pick.get("selection") or pick.get("pick") or ""),
                role_rows or {},
            )
            confidence = int(pick.get("confidence") or 0)
            adjustment = int(memory.get("primary_adjustment") or 0)
            pick["role_learning"] = memory
            pick["raw_confidence"] = pick.get("raw_confidence") or confidence
            pick["ranking_confidence"] = pick.get("ranking_confidence") or confidence + adjustment
        attach_fast_learned_decision(picks)
        prediction["learned_role_decision"] = (picks[0].get("learned_role_decision") if picks else None)
    except Exception:
        return


def fast_role_memory(
    league: str,
    country: str,
    pick_type: str,
    selection: str,
    role_rows: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    if not pick_type or not selection:
        return {"primary_adjustment": 0, "context_quality": "building"}
    buckets: dict[str, dict[str, float]] = {}
    selection_l = selection.lower()
    for row in role_rows.get((pick_type, selection_l), []):
        role = str(row["role"] or "candidate")
        if league and str(row.get("league_name") or "") == league:
            weight = 1.0
        elif country and str(row.get("country_name") or "") == country:
            weight = 0.7
        else:
            weight = 0.3
        bucket = buckets.setdefault(role, {"samples": 0.0, "wins": 0.0, "losses": 0.0, "raw": 0.0, "local": 0.0})
        bucket["samples"] += weight
        bucket["raw"] += 1
        if weight >= 0.7:
            bucket["local"] += weight
        if row.get("result") == "win":
            bucket["wins"] += weight
        else:
            bucket["losses"] += weight

    roles: dict[str, Any] = {}
    for role, bucket in buckets.items():
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
    primary_samples = float(primary.get("samples") or 0)
    secondary_samples = float(secondary.get("samples") or 0)
    primary_rate = float(primary.get("win_rate") or 0)
    secondary_rate = float(secondary.get("win_rate") or 0)
    adjustment = round((primary_rate - 0.52) * 12) if primary_samples >= 5 else 0
    if secondary_samples >= 8 and secondary_rate < 0.45:
        adjustment += round((secondary_rate - 0.45) * 6)
    return {
        "primary": primary,
        "secondary": secondary,
        "primary_adjustment": max(-5, min(5, adjustment)),
        "odds_profile_used": False,
        "movement_profile_used": False,
        "context_quality": "usable" if primary_samples + secondary_samples >= 8 else "building",
    }


def attach_fast_learned_decision(picks: list[dict[str, Any]]) -> None:
    if not picks:
        return

    def score(pick: dict[str, Any], role: str) -> float:
        confidence = float(pick.get("confidence") or 0)
        ranking = float(pick.get("ranking_confidence") or confidence)
        memory = pick.get("role_learning") or {}
        stats = memory.get("primary") if role == "primary" else (memory.get("secondary") or memory.get("alternative") or {})
        samples = float(stats.get("samples") or 0)
        local_samples = float(stats.get("local_samples") or 0)
        win_rate = float(stats.get("win_rate") or 0)
        lift = (win_rate - 0.52) * 16 if samples >= 5 else 0
        trust = min(3.0, samples / 8.0)
        if role != "primary":
            if local_samples < 2:
                lift *= 0.25
                trust *= 0.25
                ranking -= 4
            elif local_samples < 5:
                lift *= 0.6
                trust *= 0.6
        return ranking + lift + trust

    scored = []
    for index, pick in enumerate(picks):
        role = "primary" if index == 0 or pick.get("role") == "primary" else "secondary"
        scored.append((role, pick, score(pick, role)))
    best_role, best_pick, best_score = max(scored, key=lambda item: item[2])
    primary_score = scored[0][2]
    if best_role != "primary":
        primary_pick = scored[0][1]
        secondary_stats = (best_pick.get("role_learning") or {}).get("secondary") or (best_pick.get("role_learning") or {}).get("alternative") or {}
        local_samples = float(secondary_stats.get("local_samples") or 0)
        primary_conf = float(primary_pick.get("confidence") or 0)
        secondary_conf = float(best_pick.get("confidence") or 0)
        if (local_samples < 2 and secondary_conf < primary_conf + 8) or (local_samples < 5 and secondary_conf < primary_conf + 5):
            best_role, best_pick, best_score = scored[0]
    edge = round(best_score - primary_score, 2) if best_role != "primary" else round(best_score - max([item[2] for item in scored[1:]] or [best_score]), 2)
    decision = {
        "role": best_role,
        "selection": best_pick.get("selection"),
        "type": best_pick.get("type"),
        "score": round(best_score, 2),
        "edge": edge,
        "reason": "secondary_outscores_primary_in_context" if best_role != "primary" else "primary_remains_best_in_context",
        "context_quality": (best_pick.get("role_learning") or {}).get("context_quality") or "building",
    }
    for pick in picks:
        pick["learned_best"] = pick is best_pick
        pick["learned_role_decision"] = decision
