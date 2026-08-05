"""
sporty_only_predictor.py
------------------------
Derives predictions purely from SportyBet market data.

SportyBet embeds BetRadar probabilities on every outcome — these are the same
probabilities BetRadar sells to bookmakers. We extract them directly and build
a full prediction without needing SofaScore or any external enrichment.

Signals extracted per match:
  - 1X2 probabilities (home/draw/away win)
  - Over/Under 1.5, 2.5, 3.5 probabilities
  - Both Teams to Score (GG/NG)
  - Double Chance
  - 1st Half 1X2
  - 1st Half O/U 0.5, 1.5
  - Home O/U, Away O/U
  - Handicap lines
"""
from __future__ import annotations
from typing import Any, Optional


def _find_market(markets: list[dict], market_id: str, specifier: str = "") -> Optional[dict]:
    for m in markets:
        if str(m.get("id")) == str(market_id):
            if not specifier or m.get("specifier", "") == specifier:
                return m
    return None


def _outcome_prob(market: Optional[dict], outcome_id: str) -> Optional[float]:
    if not market:
        return None
    for o in market.get("outcomes", []):
        if str(o.get("id")) == str(outcome_id):
            try:
                return float(o.get("probability") or 0)
            except (TypeError, ValueError):
                return None
    return None


def _outcome_odds(market: Optional[dict], outcome_id: str) -> Optional[float]:
    if not market:
        return None
    for o in market.get("outcomes", []):
        if str(o.get("id")) == str(outcome_id):
            try:
                return float(o.get("odds") or 0)
            except (TypeError, ValueError):
                return None
    return None


def extract_sporty_signals(match: dict) -> dict:
    """Extract all prediction signals from a SportyBet match's markets."""
    markets = match.get("markets") or []

    # ── 1X2 (market id=1) ────────────────────────────────────────────────────
    m_1x2 = _find_market(markets, "1")
    home_prob  = _outcome_prob(m_1x2, "1")
    draw_prob  = _outcome_prob(m_1x2, "2")
    away_prob  = _outcome_prob(m_1x2, "3")
    home_odds  = _outcome_odds(m_1x2, "1")
    draw_odds  = _outcome_odds(m_1x2, "2")
    away_odds  = _outcome_odds(m_1x2, "3")

    # ── Over/Under (market id=18) ─────────────────────────────────────────────
    m_ou15 = _find_market(markets, "18", "total=1.5")
    m_ou25 = _find_market(markets, "18", "total=2.5")
    m_ou35 = _find_market(markets, "18", "total=3.5")
    over15_prob  = _outcome_prob(m_ou15, "12")
    under15_prob = _outcome_prob(m_ou15, "13")
    over25_prob  = _outcome_prob(m_ou25, "12")
    under25_prob = _outcome_prob(m_ou25, "13")
    over35_prob  = _outcome_prob(m_ou35, "12")
    under35_prob = _outcome_prob(m_ou35, "13")

    # ── BTTS GG/NG (market id=29) ─────────────────────────────────────────────
    m_btts = _find_market(markets, "29")
    btts_yes_prob = _outcome_prob(m_btts, "74")
    btts_no_prob  = _outcome_prob(m_btts, "76")

    # ── Double Chance (market id=10) ──────────────────────────────────────────
    m_dc = _find_market(markets, "10")
    dc_1x_prob = _outcome_prob(m_dc, "9")
    dc_12_prob = _outcome_prob(m_dc, "10")
    dc_x2_prob = _outcome_prob(m_dc, "11")

    # ── 1st Half 1X2 (market id=60) ───────────────────────────────────────────
    m_ht = _find_market(markets, "60")
    ht_home_prob = _outcome_prob(m_ht, "1")
    ht_draw_prob = _outcome_prob(m_ht, "2")
    ht_away_prob = _outcome_prob(m_ht, "3")

    # ── 1st Half O/U (market id=68) ───────────────────────────────────────────
    m_ht_ou05 = _find_market(markets, "68", "total=0.5")
    m_ht_ou15 = _find_market(markets, "68", "total=1.5")
    ht_over05_prob  = _outcome_prob(m_ht_ou05, "12")
    ht_over15_prob  = _outcome_prob(m_ht_ou15, "12")

    return {
        # 1X2
        "home_win_prob":   home_prob,
        "draw_prob":       draw_prob,
        "away_win_prob":   away_prob,
        "home_odds":       home_odds,
        "draw_odds":       draw_odds,
        "away_odds":       away_odds,
        # O/U
        "over15_prob":     over15_prob,
        "under15_prob":    under15_prob,
        "over25_prob":     over25_prob,
        "under25_prob":    under25_prob,
        "over35_prob":     over35_prob,
        "under35_prob":    under35_prob,
        # BTTS
        "btts_yes_prob":   btts_yes_prob,
        "btts_no_prob":    btts_no_prob,
        # Double Chance
        "dc_1x_prob":      dc_1x_prob,
        "dc_12_prob":      dc_12_prob,
        "dc_x2_prob":      dc_x2_prob,
        # 1st Half
        "ht_home_prob":    ht_home_prob,
        "ht_draw_prob":    ht_draw_prob,
        "ht_away_prob":    ht_away_prob,
        "ht_over05_prob":  ht_over05_prob,
        "ht_over15_prob":  ht_over15_prob,
    }


def predict_from_sporty(match: dict) -> dict:
    """
    Build a full prediction from SportyBet market signals only.
    Returns a prediction dict compatible with the existing prediction schema.
    """
    signals = extract_sporty_signals(match)

    home_prob  = signals.get("home_win_prob") or 0.0
    draw_prob  = signals.get("draw_prob") or 0.0
    away_prob  = signals.get("away_win_prob") or 0.0
    over25     = signals.get("over25_prob") or 0.0
    under25    = signals.get("under25_prob") or 0.0
    btts_yes   = signals.get("btts_yes_prob") or 0.0
    btts_no    = signals.get("btts_no_prob") or 0.0

    # No signals at all — can't predict
    if not home_prob and not draw_prob and not away_prob:
        return {
            "status": "skipped",
            "skip_reason": "no_market_probabilities",
            "signals": signals,
        }

    # ── 1X2 outcome ──────────────────────────────────────────────────────────
    probs = {"home": home_prob, "draw": draw_prob, "away": away_prob}
    result_1x2 = max(probs, key=probs.get)
    result_confidence = probs[result_1x2]

    # ── Over/Under 2.5 ───────────────────────────────────────────────────────
    if over25 or under25:
        goals_pick = "over" if over25 > under25 else "under"
        goals_confidence = max(over25, under25)
    else:
        goals_pick = None
        goals_confidence = None

    # ── BTTS ─────────────────────────────────────────────────────────────────
    if btts_yes or btts_no:
        btts_pick = "yes" if btts_yes > btts_no else "no"
        btts_confidence = max(btts_yes, btts_no)
    else:
        btts_pick = None
        btts_confidence = None

    # ── Assurance: how confident is the top pick ──────────────────────────────
    # High: top pick > 55%, margin over 2nd > 10%
    sorted_probs = sorted(probs.values(), reverse=True)
    margin = sorted_probs[0] - sorted_probs[1] if len(sorted_probs) > 1 else 0
    if result_confidence >= 0.60 and margin >= 0.15:
        assurance = "high"
    elif result_confidence >= 0.45 and margin >= 0.08:
        assurance = "medium"
    else:
        assurance = "low"

    # ── Half-time prediction ──────────────────────────────────────────────────
    ht_home = signals.get("ht_home_prob") or 0.0
    ht_draw = signals.get("ht_draw_prob") or 0.0
    ht_away = signals.get("ht_away_prob") or 0.0
    if ht_home or ht_draw or ht_away:
        ht_probs = {"home": ht_home, "draw": ht_draw, "away": ht_away}
        ht_pick = max(ht_probs, key=ht_probs.get)
        ht_confidence = ht_probs[ht_pick]
    else:
        ht_pick = None
        ht_confidence = None

    name = match.get("name") or ""
    parts = name.split(" vs ", 1)
    home_team = parts[0].strip() if len(parts) == 2 else name
    away_team = parts[1].strip() if len(parts) == 2 else ""

    return {
        "status": "predicted",
        "source": "sportybet_only",
        "prediction": {
            "result": result_1x2,
            "home_win_probability": round(home_prob, 4),
            "draw_probability":     round(draw_prob, 4),
            "away_win_probability": round(away_prob, 4),
            "confidence":           round(result_confidence, 4),
            "assurance":            assurance,
            "margin":               round(margin, 4),
            "goals": {
                "pick":       goals_pick,
                "line":       2.5,
                "over_prob":  round(over25, 4) if over25 else None,
                "under_prob": round(under25, 4) if under25 else None,
                "confidence": round(goals_confidence, 4) if goals_confidence else None,
            },
            "btts": {
                "pick":       btts_pick,
                "yes_prob":   round(btts_yes, 4) if btts_yes else None,
                "no_prob":    round(btts_no, 4) if btts_no else None,
                "confidence": round(btts_confidence, 4) if btts_confidence else None,
            },
            "half_time": {
                "pick":       ht_pick,
                "confidence": round(ht_confidence, 4) if ht_confidence else None,
            },
            "home_team": home_team,
            "away_team": away_team,
            "tournament": match.get("tournament"),
            "start_time": match.get("start_time"),
        },
        "signals": signals,
        "data_source": "sportybet",
    }
