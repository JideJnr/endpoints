from __future__ import annotations

import math
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
    finished_memory: dict[str, Any] = {}
    database_adj = 0
    try:
        from app.league_memory import weighted_finished_match_memory

        finished_memory = weighted_finished_match_memory(doc)
        database_adj = _finished_memory_adjustment(ensemble, finished_memory)
        signals.append({
            "name": "finished_database_memory",
            "value": finished_memory,
            "impact": database_adj,
        })
    except Exception:
        pass

    # ── Odds pattern signal ───────────────────────────────────────────────────
    odds_pattern_signal: dict[str, Any] = {}
    pattern_adj = 0
    odds_movement: dict[str, Any] = {}
    market_adj = 0
    try:
        from app.odds_pattern import pattern_signal
        match_id_str = str(doc.get("sportybet_id") or doc.get("id") or "")
        if match_id_str:
            odds_pattern_signal = pattern_signal(match_id_str)
            pattern_adj = int(odds_pattern_signal.get("confidence_adjustment") or 0)
            signals.append({
                "name":   "odds_pattern",
                "value":  odds_pattern_signal,
                "impact": pattern_adj,
            })
    except Exception:
        pass

    try:
        from app.market import get_movement
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
    except Exception:
        pass

    # ── SofaScore grade signal ────────────────────────────────────────────────
    grade_adj = 0
    try:
        from app.sofascore_grades import grade_signal_for_match
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
        from app.self_learner import get_signal_weights
        league = doc.get("tournament") or doc.get("category") or ""
        if isinstance(league, dict):
            league = league.get("name") or ""
        learned_weights = get_signal_weights(league)
        if learned_weights:
            for sig in signals:
                name = sig.get("name") or ""
                weight_adj = learned_weights.get(name)
                if weight_adj is not None and abs(weight_adj) > 0.1:
                    # Scale: weight_adj of +1.0 → +3 confidence, -1.0 → -3
                    contribution = round(weight_adj * 3)
                    learned_signal_adj += contribution
            learned_signal_adj = max(-8, min(8, learned_signal_adj))
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
    except Exception:
        pass

    # Time-decay for live matches
    minute = rules.get("minute") or 0
    is_live = bool(detail.get("status", {}).get("type") == "inprogress") or bool(
        doc.get("period") and doc.get("period") not in ("Not start", "Not started", "", None)
    )
    from app.prediction_agent import _apply_time_decay, _is_high_late_goal_league, _time_decay_multiplier
    late_goal_league = _is_high_late_goal_league(
        (doc.get("tournament") or "") + " " + (doc.get("category") or "")
    )

    market_picks = _market_selector_picks(doc, ensemble, poisson, dixon, finished_memory)
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
    picks = _combined_picks(rules, ensemble, value_bets, market_picks)
    picks.extend(live_picks)
    picks = _apply_time_decay(picks, minute, is_live, late_goal_league)

    # ── Calibration: adjust confidence based on historical win rates ──────────
    try:
        from app.confidence_calibrator import calibrate_confidence, stake_multiplier
        from app.league_memory import weighted_prediction_memory
        from app.regime import get_regime_for_doc, apply_regime_stake_cap

        regime = get_regime_for_doc(doc)

        for pick in picks:
            raw_conf = int(pick.get("confidence") or 50)
            cal = calibrate_confidence(pick.get("type") or "match_result", raw_conf)
            memory = weighted_prediction_memory(doc, pick.get("type"), pick.get("selection"))
            memory_adj = int(memory.get("confidence_adjustment") or 0)
            pick_learned_adj = _learned_signal_adjustment_for_pick(doc, signals, pick.get("type"))
            # Apply pattern and market movement adjustment on top.
            cal_conf = min(99, max(1, cal["adjusted_confidence"] + pattern_adj + market_adj + memory_adj + database_adj + grade_adj + pick_learned_adj))
            # Apply regime tier penalty (Tier 4 gets -5)
            tier_penalty = {1: 0, 2: 0, 3: 0, 4: -5}.get(regime.tier, 0)
            cal_conf = min(99, max(1, cal_conf + tier_penalty))
            pick["confidence"] = cal_conf

            raw_stake = stake_multiplier(pick.get("type") or "match_result", raw_conf)
            capped_stake = apply_regime_stake_cap(raw_stake, doc.get("tournament"), doc.get("category"))

            pick["calibration"] = {
                "raw_confidence":    raw_conf,
                "win_rate":          cal.get("win_rate"),
                "samples":           cal.get("samples"),
                "double_down":       cal.get("double_down", False),
                "stake_multiplier":  capped_stake,
                "calibrated":        cal.get("calibrated", False),
                "regime_tier":       regime.tier,
                "regime_name":       regime.name,
                "regime_stake_cap":  regime.stake_cap,
                "memory_weighting":   memory,
                "learned_signal_adjustment": pick_learned_adj,
            }
            if memory.get("blended_win_rate") is not None:
                signals.append({
                    "name": "prediction_memory",
                    "value": memory,
                    "impact": memory_adj,
                })
            # Prefer CLV-based stake sizing if enough data exists
            try:
                from app.clv import clv_stake_multiplier
                clv_mult = clv_stake_multiplier(pick.get("type") or "match_result", raw_conf)
                if clv_mult != 1.0:
                    capped_clv = apply_regime_stake_cap(clv_mult, doc.get("tournament"), doc.get("category"))
                    pick["calibration"]["stake_multiplier"] = capped_clv
                    pick["calibration"]["stake_source"] = "clv"
                else:
                    pick["calibration"]["stake_source"] = "win_rate"
            except Exception:
                pass
    except Exception:
        pass

    picks = _curate_picks(picks)
    _attach_stake_sizing(doc, picks)

    prediction = {
        "match_id": doc.get("sportybet_id") or doc.get("id") or detail.get("id"),
        "name": doc.get("sportybet_name") or doc.get("name") or detail.get("name"),
        "source": "enriched_ensemble",
        "match_date": doc.get("match_date"),
        "tournament": doc.get("tournament") or ((detail.get("tournament") or {}).get("name") if isinstance(detail.get("tournament"), dict) else None),
        "category": doc.get("category") or _detail_country(detail),
        "country": doc.get("category") or _detail_country(detail),
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
            "finished_database_memory": finished_memory,
        },
        "web_context": doc.get("web_context") or {},
        "odds_movement": odds_movement,
        "value_bets": value_bets,
        "market_selector": market_picks,
        "live_inplay": live_picks,
        "signals": sorted(signals, key=lambda item: abs(item.get("impact") or 0), reverse=True),
        "picks": picks,
        "time_decay_applied": is_live and minute >= 46,
        "time_decay_multiplier": _time_decay_multiplier(minute) if is_live else 1.0,
        "regime": _regime_info(doc),
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


def _fallback_market_prediction(
    doc: dict[str, Any],
    detail: dict[str, Any],
    home_sample: int,
    away_sample: int,
) -> dict[str, Any]:
    """Prematch fallback when SofaScore matched but historical detail is thin."""
    sporty_doc = {
        **doc,
        "id": doc.get("id") or doc.get("sportybet_id"),
        "name": doc.get("name") or doc.get("sportybet_name"),
        "markets": doc.get("markets") or doc.get("sportybet_markets") or [],
    }
    rules = predict_sporty_match(sporty_doc)
    picks = _curate_picks(rules.get("picks") or [])
    is_live = bool((detail.get("status") or {}).get("type") == "inprogress") or bool(
        doc.get("period") and doc.get("period") not in ("Not start", "Not started", "", None)
    )
    odds_movement: dict[str, Any] = {}
    if is_live:
        try:
            from app.market import get_movement
            match_id_str = str(doc.get("sportybet_id") or doc.get("id") or "")
            odds_movement = get_movement(match_id_str) if match_id_str else {}
        except Exception:
            odds_movement = {}
    live_picks = _live_inplay_picks(doc, detail, rules, {}, {}, odds_movement) if is_live else []
    picks.extend(live_picks)
    _attach_stake_sizing(doc, picks)
    signals = list(rules.get("signals") or [])
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
    return {
        "match_id": doc.get("sportybet_id") or doc.get("id") or detail.get("id"),
        "name": doc.get("sportybet_name") or doc.get("name") or detail.get("name"),
        "source": "sportybet_market_fallback",
        "match_date": doc.get("match_date"),
        "tournament": doc.get("tournament") or ((detail.get("tournament") or {}).get("name") if isinstance(detail.get("tournament"), dict) else None),
        "category": doc.get("category") or _detail_country(detail),
        "country": doc.get("category") or _detail_country(detail),
        "time_context": doc.get("time_context") or match_time_context(doc),
        "teams": {
            "home": detail.get("home_team") or {"name": _team_name(doc, "home")},
            "away": detail.get("away_team") or {"name": _team_name(doc, "away")},
        },
        "rules": rules,
        "models": {},
        "web_context": doc.get("web_context") or {},
        "odds_movement": odds_movement,
        "value_bets": [],
        "market_selector": [],
        "live_inplay": live_picks,
        "signals": sorted(signals, key=lambda item: abs(item.get("impact") or 0), reverse=True),
        "picks": picks,
        "fallback_reason": "SofaScore detail exists but finished team history is below model threshold.",
        "data_quality": {
            "has_sofascore_detail": bool(detail),
            "has_sportybet_markets": bool(doc.get("sportybet_markets") or doc.get("markets")),
            "has_web_context": bool((doc.get("web_context") or {}).get("snippets")),
            "has_raw_sporty": bool(doc.get("raw_sporty")),
            "has_raw_sofascore": bool(doc.get("raw_sofascore_event") or doc.get("sofascore_event")),
            "manual_match": bool(doc.get("manual_match")),
            "thin_history_fallback": True,
        },
    }


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


def _attach_stake_sizing(doc: dict[str, Any], picks: list[dict[str, Any]]) -> None:
    """Attach conservative Kelly stake metadata to every pick with a matching price."""
    odds_index = _market_odds_index(doc)
    for pick in picks:
        selection = str(pick.get("selection") or pick.get("pick") or "")
        decimal = _odds_for_pick(selection, odds_index)
        probability = max(0.01, min(0.99, float(pick.get("confidence") or 0) / 100))
        stake_cap = float(((pick.get("calibration") or {}).get("stake_multiplier")) or 1.0)
        stake: dict[str, Any] = {
            "probability": round(probability, 3),
            "decimal_odds": decimal,
            "stake_source": "kelly" if decimal else "confidence_only",
            "stake_cap_multiplier": stake_cap,
            "recommended": False,
            "stake_per_100": 0,
        }
        if decimal and decimal > 1:
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


def _combined_picks(
    rules: dict[str, Any],
    ensemble: dict[str, Any],
    value_bets: list[dict[str, Any]],
    market_picks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    picks = []
    picks.extend(market_picks or [])
    if ensemble and not ensemble.get("error") and float(ensemble.get("confidence") or 0) >= 55:
        picks.append({
            "type": "ensemble_1x2",
            "selection": ensemble["prediction"],
            "confidence": round(float(ensemble["confidence"])),
            "reason": f"Weighted model blend using {', '.join(ensemble.get('models_used') or [])}",
        })
    # Add rules picks, excluding no_bet
    for pick in (rules.get("picks") or []):
        if pick.get("type") != "no_bet":
            picks.append(pick)
    if value_bets:
        top = value_bets[0]
        picks.insert(0, {
            "type": "value_bet",
            "selection": top["selection"],
            "confidence": round(top["kelly"]["probability"] * 100),
            "reason": f"{top['kelly']['edge_percent']}% model edge, stake {top['kelly']['stake_per_100']} per 100",
        })
    # If nothing meaningful, add a low-confidence ensemble pick rather than no_bet
    if not picks and ensemble and not ensemble.get("error"):
        picks.append({
            "type": "ensemble_1x2",
            "selection": ensemble["prediction"],
            "confidence": max(1, round(float(ensemble["confidence"])) - 10),
            "reason": "Low confidence ensemble — insufficient rule signals",
        })
    return _curate_picks(picks)


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
        home_name, away_name, score_diff, favorite, odds, minute, home_pressure, away_pressure, stats["summary"]
    )
    if team and team_conf >= 55 and minute < 86:
        picks.append(_pick("live_team_to_score", f"{team} next team to score", team_conf, team_reason))

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
    for signal in signals:
        if signal.get("name") == name:
            return _to_float(signal.get("value")) or _to_float(signal.get("impact")) or 0.0
    return 0.0


def _memory_goal_pressure(memory: dict[str, Any]) -> float:
    blended = memory.get("blended") or {}
    avg_goals = _to_float(blended.get("avg_goals")) or 0
    over_25 = _to_float(blended.get("over_2_5_rate")) or 0
    return avg_goals * 5 + over_25 * 10


def _learned_signal_adjustment_for_pick(
    doc: dict[str, Any],
    signals: list[dict[str, Any]],
    pick_type: str | None,
) -> int:
    """Apply self-learner feedback scoped by league and market type."""
    try:
        from app.self_learner import get_signal_weights

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
                adjustment += round(float(weight_adj) * 3 * direction)
        return max(-8, min(8, adjustment))
    except Exception:
        return 0


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
    }


def _stat_key(name: str) -> str | None:
    normalized = name.lower().replace("%", "").replace("-", " ").replace("_", " ")
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
            or _to_float((market.get("movement") or {}).get("percent"))
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
) -> tuple[str | None, str, float]:
    pressure_gap = home_pressure - away_pressure
    if abs(pressure_gap) >= 6:
        team = home_name if pressure_gap > 0 else away_name
        return team, f"live stats and prematch prior point to this side; pressure {stats_summary}", 58 + min(16, abs(pressure_gap) * 0.8)
    if score_diff < 0 and abs(score_diff) <= 1:
        return home_name, "home side is chasing a close live score with prematch/live pressure included", 60 + min(8, 90 - minute)
    if score_diff > 0 and abs(score_diff) <= 1:
        return away_name, "away side is chasing a close live score with prematch/live pressure included", 60 + min(8, 90 - minute)
    if favorite in {"home", "away"}:
        prob = odds.get(favorite, 0)
        team = home_name if favorite == "home" else away_name
        pressure_boost = max(0, pressure_gap if favorite == "home" else -pressure_gap) * 0.35
        return team, "live market still prices this side as the main scorer, supported by prematch/live pressure", 54 + min(16, prob * 28 + pressure_boost)
    return (home_name if score_diff <= 0 else away_name), "score state gives this side the cleaner next-goal angle", 55


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
    selection = str(pick.get("selection") or pick.get("pick") or "").lower()
    if kind.startswith("live_"):
        return "live"
    if kind in {"goals", "live_goals"} or any(word in selection for word in ("over", "under", "goal", "btts", "score")):
        return "goals"
    if kind == "value_bet":
        return "value"
    if kind in {"match_result", "double_chance", "ensemble_1x2", "market_value"}:
        return "outcome"
    return kind or "other"


def _curate_picks(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one primary pick plus a small set of non-conflicting alternatives."""
    candidates = []
    for pick in _dedupe_picks(picks):
        if pick.get("type") == "no_bet":
            continue
        confidence = int(pick.get("confidence") or 0)
        selection = pick.get("selection") or pick.get("pick")
        if not selection or confidence < 55:
            continue
        if pick.get("type") == "value_bet" and confidence < 65:
            continue
        if pick.get("type") == "ensemble_1x2" and confidence < 58:
            continue
        candidates.append({**pick, "selection": selection, "confidence": confidence, "family": _pick_family(pick)})

    candidates.sort(
        key=lambda item: (
            item.get("confidence") or 0,
            8 if item.get("family") == "value" else 4 if item.get("family") == "outcome" else 0,
        ),
        reverse=True,
    )

    curated: list[dict[str, Any]] = []
    used_families: set[str] = set()
    for pick in candidates:
        family = pick["family"]
        if family in used_families:
            continue
        curated.append({**pick, "role": "primary" if not curated else "alternative"})
        used_families.add(family)
        if len(curated) >= 3:
            break
    return curated


def _market_selector_picks(
    doc: dict[str, Any],
    ensemble: dict[str, Any],
    poisson: dict[str, Any] | None,
    dixon: dict[str, Any] | None,
    finished_memory: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Let data choose the best market instead of forcing one bet shape.

    Inputs are blended 1X2 model probabilities, goal distributions from
    Poisson/Dixon-Coles, and finished-score memory from tournament, country,
    then whole database. The output can be straight 1X2, double chance, BTTS,
    over, or under.
    """
    model_probs = _blended_model_probabilities(ensemble, poisson, dixon)
    memory = (finished_memory or {}).get("blended") or {}
    samples = int((finished_memory or {}).get("samples") or 0)
    sample_factor = min(1.0, samples / 120)
    picks: list[dict[str, Any]] = []

    home = float(model_probs.get("home_win") or 0)
    draw = float(model_probs.get("draw") or 0)
    away = float(model_probs.get("away_win") or 0)
    over_2_5 = _blend_rate(float(model_probs.get("over_2_5") or 0), float(memory.get("over_2_5_rate") or 0) * 100, sample_factor)
    btts = _blend_rate(float(model_probs.get("btts") or 0), float(memory.get("btts_rate") or 0) * 100, sample_factor)
    under_2_5 = 100 - over_2_5
    under_3_5 = _under_probability(poisson, dixon, 3.5)
    under_1_5 = _under_probability(poisson, dixon, 1.5)
    avg_goals = float(memory.get("avg_goals") or 0)

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
    if side_conf >= 45 and side_conf - second_side >= 7:
        picks.append(_selector_pick("match_result", best_side, side_conf, "1X2 model and finished-score memory agree"))

    dc_options = [
        ("Home or Draw", home + draw, away, "home avoids defeat profile"),
        ("Away or Draw", away + draw, home, "away avoids defeat profile"),
        ("Home or Away", home + away, draw, "draw risk is low from model and database"),
    ]
    for selection, probability, excluded, reason in dc_options:
        if probability >= 63 and excluded <= 37:
            picks.append(_selector_pick("double_chance", selection, min(probability, 86), reason))

    if under_1_5 >= 52 and avg_goals and avg_goals <= 1.8:
        picks.append(_selector_pick("goals", "Under 1.5 goals", under_1_5, "previous final scores point to a very low total"))
    if under_2_5 >= 56 and (not avg_goals or avg_goals <= 2.55):
        picks.append(_selector_pick("goals", "Under 2.5 goals", under_2_5, "goal model and finished database lean under"))
    if under_3_5 >= 64 and (not avg_goals or avg_goals <= 3.15):
        picks.append(_selector_pick("goals", "Under 3.5 goals", under_3_5, "score distribution keeps four-goal game unlikely"))
    if over_2_5 >= 58 and avg_goals >= 2.65:
        picks.append(_selector_pick("goals", "Over 2.5 goals", over_2_5, "model goals and historical finals both run high"))
    if btts >= 58:
        picks.append(_selector_pick("goals", "Both teams to score", btts, "both-side scoring probability is above market threshold"))
    elif btts <= 43 and samples >= 20:
        picks.append(_selector_pick("goals", "Both teams to score - No", 100 - btts, "finished-score memory shows weak both-team scoring"))

    return sorted(_dedupe_picks(picks), key=lambda item: item.get("confidence") or 0, reverse=True)[:5]


def _blended_model_probabilities(
    ensemble: dict[str, Any],
    poisson: dict[str, Any] | None,
    dixon: dict[str, Any] | None,
) -> dict[str, float]:
    totals = {"home_win": 0.0, "draw": 0.0, "away_win": 0.0, "over_2_5": 0.0, "btts": 0.0}
    weight = 0.0

    def add(probs: dict[str, Any], w: float) -> None:
        nonlocal weight
        if not probs:
            return
        for key in totals:
            totals[key] += float(probs.get(key) or 0) * w
        weight += w

    add((dixon or {}).get("probabilities") or {}, 0.45)
    add((poisson or {}).get("probabilities") or {}, 0.25)
    add((ensemble or {}).get("probabilities") or {}, 0.30)
    return {key: round(value / weight, 2) for key, value in totals.items()} if weight else totals


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


def _poisson_probability(lam: float, goals: int) -> float:
    return (math.exp(-lam) * (lam ** goals)) / math.factorial(goals)


def _blend_rate(model_rate: float, memory_rate: float, sample_factor: float) -> float:
    memory_weight = 0.35 * sample_factor
    return round(model_rate * (1 - memory_weight) + memory_rate * memory_weight, 1)


def _selector_pick(kind: str, selection: str, confidence: float, reason: str) -> dict[str, Any]:
    return {
        "type": kind,
        "selection": selection,
        "confidence": max(1, min(95, round(confidence))),
        "reason": reason,
        "source": "market_selector",
    }


def _dedupe_picks(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for pick in picks:
        key = (str(pick.get("type") or ""), str(pick.get("selection") or "").lower())
        current = deduped.get(key)
        if not current or int(pick.get("confidence") or 0) > int(current.get("confidence") or 0):
            deduped[key] = pick
    return list(deduped.values())


def _pick(kind: str, selection: str, confidence: float, reason: str) -> dict[str, Any]:
    return {
        "type": kind,
        "selection": selection,
        "confidence": max(1, min(95, round(confidence))),
        "reason": reason,
        "family": "live" if kind.startswith("live_") else kind,
    }


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
    if not blended:
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
    samples = int(memory.get("samples") or 0)
    sample_factor = min(1.0, samples / 100)
    return max(-6, min(6, round((rate - baseline) * 18 * sample_factor)))


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


def _detail_country(detail: dict[str, Any]) -> str | None:
    tournament = detail.get("tournament") or {}
    if isinstance(tournament, dict):
        category = tournament.get("category") or {}
        if isinstance(category, dict) and category.get("name"):
            return str(category.get("name"))
    return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _regime_info(doc: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.regime import get_regime_for_doc
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
