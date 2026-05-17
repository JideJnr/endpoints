from __future__ import annotations

from typing import Any

from app.dixon_coles import run_dixon_coles
from app.elo import elo_prediction
from app.ensemble import ensemble_prediction
from app.kelly import kelly_fraction
from app.poisson import run_poisson
from app.prediction_agent import predict_sofascore_event, predict_sporty_match
from app.time_context import match_time_context


def predict_enriched_match(doc: dict[str, Any]) -> dict[str, Any]:
    """Run every available model against the richest document we have for a match."""
    detail = doc.get("sofascore_detail") or {}
    home = detail.get("home_team") or detail.get("homeTeam") or {}
    away = detail.get("away_team") or detail.get("awayTeam") or {}
    home_id = home.get("id")
    away_id = away.get("id")

    rules = _rules_prediction(doc, detail)
    poisson = dixon = elo = None
    if home_id and away_id:
        try:
            poisson = run_poisson(int(home_id), int(away_id))
        except Exception as exc:
            poisson = {"error": str(exc)}
        try:
            dixon = run_dixon_coles(int(home_id), int(away_id))
        except Exception as exc:
            dixon = {"error": str(exc)}
        try:
            elo = elo_prediction(str(home_id), str(away_id))
        except Exception as exc:
            elo = {"error": str(exc)}

    best_pick = (rules.get("picks") or [{}])[0]
    ensemble = ensemble_prediction(
        dixon if dixon and not dixon.get("error") else None,
        elo if elo and not elo.get("error") else None,
        poisson if poisson and not poisson.get("error") else None,
        int(best_pick.get("confidence") or 50),
        str(best_pick.get("selection") or best_pick.get("pick") or ""),
    )

    value_bets = _value_bets(doc, dixon if dixon and not dixon.get("error") else poisson)
    signals = list(rules.get("signals") or [])
    signals.extend(_model_signals(poisson, dixon, elo, ensemble, doc))

    prediction = {
        "match_id": doc.get("sportybet_id") or doc.get("id") or detail.get("id"),
        "name": doc.get("sportybet_name") or doc.get("name") or detail.get("name"),
        "source": "enriched_ensemble",
        "match_date": doc.get("match_date"),
        "time_context": doc.get("time_context") or match_time_context(doc),
        "teams": {
            "home": home or {"name": _team_name(doc, "home")},
            "away": away or {"name": _team_name(doc, "away")},
        },
        "rules": rules,
        "models": {
            "poisson": poisson,
            "dixon_coles": dixon,
            "elo": elo,
            "ensemble": ensemble,
        },
        "web_context": doc.get("web_context") or {},
        "value_bets": value_bets,
        "signals": sorted(signals, key=lambda item: abs(item.get("impact") or 0), reverse=True),
        "picks": _combined_picks(rules, ensemble, value_bets),
        "data_quality": {
            "has_sofascore_detail": bool(detail),
            "has_sportybet_markets": bool(doc.get("sportybet_markets") or doc.get("markets")),
            "has_web_context": bool((doc.get("web_context") or {}).get("snippets")),
            "has_raw_sporty": bool(doc.get("raw_sporty")),
            "has_raw_sofascore": bool(doc.get("raw_sofascore_event") or doc.get("sofascore_event")),
            "manual_match": bool(doc.get("manual_match")),
        },
    }
    return prediction


def _rules_prediction(doc: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    if detail:
        try:
            return predict_sofascore_event(
                detail,
                detail.get("home_last_matches") or [],
                detail.get("away_last_matches") or [],
            )
        except Exception:
            pass
    sporty_doc = {
        **doc,
        "id": doc.get("id") or doc.get("sportybet_id"),
        "name": doc.get("name") or doc.get("sportybet_name"),
        "markets": doc.get("markets") or doc.get("sportybet_markets") or [],
    }
    return predict_sporty_match(sporty_doc)


def _value_bets(doc: dict[str, Any], model: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not model or model.get("error") or not model.get("probabilities"):
        return []
    probs = model["probabilities"]
    markets = doc.get("sportybet_markets") or doc.get("markets") or []
    bets = []
    for market in markets:
        name = (market.get("name") or "").lower()
        if not (market.get("id") == "1" or "1x2" in name or name == "match result"):
            continue
        for selection in market.get("selections") or []:
            decimal = _to_float(selection.get("odds"))
            if not decimal or decimal <= 1:
                continue
            selection_name = str(selection.get("name") or "")
            if selection_name in {"Home", "1"}:
                probability = float(probs.get("home_win") or 0) / 100
            elif selection_name in {"Draw", "X"}:
                probability = float(probs.get("draw") or 0) / 100
            elif selection_name in {"Away", "2"}:
                probability = float(probs.get("away_win") or 0) / 100
            else:
                continue
            kelly = kelly_fraction(probability, decimal)
            if kelly["value_bet"]:
                bets.append({"selection": selection_name, "decimal_odds": decimal, "kelly": kelly})
    return sorted(bets, key=lambda item: item["kelly"]["edge_percent"], reverse=True)


def _combined_picks(rules: dict[str, Any], ensemble: dict[str, Any], value_bets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    picks = []
    if ensemble and not ensemble.get("error"):
        picks.append(
            {
                "type": "ensemble_1x2",
                "selection": ensemble["prediction"],
                "confidence": round(float(ensemble["confidence"])),
                "reason": f"Weighted model blend using {', '.join(ensemble.get('models_used') or [])}",
            }
        )
    picks.extend(rules.get("picks") or [])
    if value_bets:
        top = value_bets[0]
        picks.insert(
            0,
            {
                "type": "value_bet",
                "selection": top["selection"],
                "confidence": round(top["kelly"]["probability"] * 100),
                "reason": f"{top['kelly']['edge_percent']}% model edge, stake {top['kelly']['stake_per_100']} per 100",
            },
        )
    return sorted(picks, key=lambda pick: pick.get("confidence") or 0, reverse=True)


def _model_signals(
    poisson: dict[str, Any] | None,
    dixon: dict[str, Any] | None,
    elo: dict[str, Any] | None,
    ensemble: dict[str, Any],
    doc: dict[str, Any],
) -> list[dict[str, Any]]:
    signals = []
    if poisson and not poisson.get("error"):
        signals.append({"name": "poisson_model", "value": poisson.get("probabilities"), "impact": _prob_impact(poisson)})
    if dixon and not dixon.get("error"):
        signals.append({"name": "dixon_coles_model", "value": dixon.get("probabilities"), "impact": _prob_impact(dixon)})
    if elo and not elo.get("error"):
        signals.append({"name": "elo_model", "value": elo, "impact": round((elo.get("home_win_probability", 50) - 50) / 3, 2)})
    if ensemble and not ensemble.get("error"):
        signals.append({"name": "ensemble_model", "value": ensemble, "impact": round((ensemble.get("confidence", 50) - 50) / 2, 2)})
    web = doc.get("web_context") or {}
    signals.append(
        {
            "name": "web_context",
            "value": {
                "query": web.get("query"),
                "snippets": len(web.get("snippets") or []),
                "scraped": len(web.get("scraped") or []),
                "error": web.get("error"),
                "disabled": web.get("disabled"),
            },
            "impact": 0,
        }
    )
    return signals


def _prob_impact(model: dict[str, Any]) -> float:
    probs = model.get("probabilities") or {}
    return round((max(float(probs.get("home_win") or 0), float(probs.get("away_win") or 0), float(probs.get("draw") or 0)) - 33.3) / 3, 2)


def _team_name(doc: dict[str, Any], side: str) -> str:
    team = doc.get(f"{side}_team")
    if isinstance(team, dict):
        return team.get("name") or ""
    if team:
        return str(team)
    name = doc.get("sportybet_name") or doc.get("name") or ""
    parts = [part.strip() for part in str(name).split(" vs ", 1)]
    index = 0 if side == "home" else 1
    return parts[index] if len(parts) > index else ""


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
