"""
Regime Detection — League Liquidity Tiers
------------------------------------------
Markets behave differently depending on how much money flows through them.

Tier 1 — Elite (Premier League, La Liga, Champions League, etc.)
  - Odds are efficient within minutes of release
  - Sharp money moves lines fast — entry window is narrow
  - High data quality: SofaScore coverage, web context, stats all reliable
  - Strategy: be selective, require higher confidence, CLV decays fast

Tier 2 — Major (Championship, Bundesliga, Serie A, Ligue 1, MLS, etc.)
  - Good liquidity, reasonable efficiency
  - Edges exist but are smaller and shorter-lived
  - Strategy: standard thresholds

Tier 3 — Mid (lower domestic leagues, second divisions, regional cups)
  - Thinner markets, bookmakers price lazily
  - Edges persist longer but data quality drops
  - Strategy: lower confidence threshold OK, but require more data signals

Tier 4 — Fringe (women's leagues, youth, obscure regional, unknown)
  - Very thin markets, high variance, low data quality
  - Strategy: avoid or require very high confidence + odds pattern confirmation

Usage:
    from app.market.regime import get_regime, RegimeTier

    regime = get_regime("Premier League")
    # regime.tier          → 1
    # regime.name          → "Elite"
    # regime.min_confidence → 78
    # regime.clv_min_samples → 20
    # regime.edge_threshold  → 0.06
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Regime:
    tier:              int    # 1 = elite, 2 = major, 3 = mid, 4 = fringe
    name:              str    # "Elite" / "Major" / "Mid" / "Fringe"
    min_confidence:    int    # minimum confidence to surface a pick
    edge_threshold:    float  # minimum model edge required for value bets
    clv_min_samples:   int    # minimum CLV entries before trusting the band
    stake_cap:         float  # max stake multiplier allowed for this tier
    description:       str


TIER_1 = Regime(
    tier=1, name="Elite",
    min_confidence=78,
    edge_threshold=0.06,
    clv_min_samples=20,
    stake_cap=2.0,
    description="Top-flight leagues — efficient markets, narrow entry windows",
)

TIER_2 = Regime(
    tier=2, name="Major",
    min_confidence=72,
    edge_threshold=0.05,
    clv_min_samples=15,
    stake_cap=1.75,
    description="Major leagues — good liquidity, standard thresholds",
)

TIER_3 = Regime(
    tier=3, name="Mid",
    min_confidence=68,
    edge_threshold=0.04,
    clv_min_samples=10,
    stake_cap=1.5,
    description="Mid-tier leagues — thinner markets, edges persist longer",
)

TIER_4 = Regime(
    tier=4, name="Fringe",
    min_confidence=82,
    edge_threshold=0.08,
    clv_min_samples=5,
    stake_cap=1.0,
    description="Fringe markets — high variance, low data quality, avoid unless very confident",
)


# ── League → Tier mapping ─────────────────────────────────────────────────────
# Matched case-insensitively. First match wins.
# Format: (pattern, tier)

_TIER_RULES: list[tuple[str, int]] = [

    # ── Tier 1: Elite ─────────────────────────────────────────────────────────
    # European top flights
    ("premier league",          1),
    ("la liga",                 1),
    ("laliga",                  1),
    ("bundesliga",              1),   # 1. Bundesliga only
    ("serie a",                 1),   # Italian Serie A
    ("ligue 1",                 1),
    ("eredivisie",              1),
    ("primeira liga",           1),
    ("pro league",              1),   # Belgian
    ("super lig",               1),   # Turkish
    ("premier liga",            1),   # Russian / Ukrainian
    ("scottish premiership",    1),

    # European competitions
    ("champions league",        1),
    ("europa league",           1),
    ("conference league",       1),

    # International
    ("world cup",               1),
    ("fifa world cup",          1),
    ("euros",                   1),
    ("euro 2024",               1),
    ("copa america",            1),
    ("nations league",          1),
    ("african cup",             1),
    ("afcon",                   1),
    ("asian cup",               1),

    # South American top flights
    ("brasileirao",             1),
    ("brasileiro serie a",      1),
    ("liga mx",                 1),   # top division only
    ("primera division",        1),   # Argentina
    ("superliga",               1),   # Argentina / Denmark top

    # ── Tier 2: Major ─────────────────────────────────────────────────────────
    ("championship",            2),   # English Championship
    ("serie b",                 2),   # Italian
    ("2. bundesliga",           2),
    ("ligue 2",                 2),
    ("segunda division",        2),
    ("segunda liga",            2),
    ("serie c",                 2),
    ("mls",                     2),
    ("major league soccer",     2),
    ("a-league",                2),
    ("j1 league",               2),
    ("j. league",               2),
    ("k league",                2),
    ("chinese super league",    2),
    ("indian super league",     2),
    ("saudi pro league",        2),
    ("saudi professional",      2),
    ("usl championship",        2),
    ("liga portugal",           2),
    ("allsvenskan",             2),
    ("eliteserien",             2),
    ("veikkausliiga",           2),
    ("ekstraklasa",             2),
    ("czech liga",              2),
    ("fortuna liga",            2),
    ("super league",            2),   # Swiss / Greek
    ("super league greece",     2),
    ("jupiler",                 2),
    ("pro league",              2),
    ("primeira liga",           2),
    ("liga 1",                  2),   # Romania / Peru
    ("liga 2",                  2),
    ("primera federacion",      2),
    ("primera nacional",        2),
    ("brasileiro serie b",      2),
    ("brasileiro serie c",      2),
    ("copa libertadores",       2),
    ("copa sudamericana",       2),
    ("concacaf",                2),
    ("caf champions",           2),

    # ── Tier 3: Mid ───────────────────────────────────────────────────────────
    ("league one",              3),
    ("league two",              3),
    ("national league",         3),
    ("3. liga",                 3),
    ("serie d",                 3),
    ("tercera",                 3),
    ("segunda b",               3),
    ("regional",                3),
    ("division one",            3),
    ("division two",            3),
    ("division three",          3),
    ("premier division",        3),
    ("first division",          3),
    ("second division",         3),
    ("third division",          3),
    ("1. liga",                 3),
    ("2. liga",                 3),
    ("3. liga",                 3),
    ("i-league",                3),
    ("brasileiro serie d",      3),
    ("torneo federal",          3),
    ("mls next pro",            3),
    ("usl league",              3),
    ("national premier",        3),
    ("coupe",                   3),   # domestic cups
    ("copa del rey",            3),
    ("fa cup",                  3),
    ("carabao cup",             3),
    ("dfb pokal",               3),
    ("coppa italia",            3),
    ("coupe de france",         3),
    ("league cup",              3),
    ("super cup",               3),
    ("supercopa",               3),
    ("trophee",                 3),
    ("playoff",                 3),
    ("promotion playoff",       3),
    ("relegation playoff",      3),
    ("leinster",                3),
    ("munster",                 3),
    ("connacht",                3),
    ("ulster",                  3),
    ("ligapro",                 3),
    ("superliga",               3),

    # ── Tier 4: Fringe ────────────────────────────────────────────────────────
    ("women",                   4),
    ("u20",                     4),
    ("u21",                     4),
    ("u23",                     4),
    ("u18",                     4),
    ("under-20",                4),
    ("under-21",                4),
    ("under-23",                4),
    ("youth",                   4),
    ("reserve",                 4),
    ("b team",                  4),
    ("ii",                      4),   # reserve teams
    ("futsal",                  4),
    ("beach soccer",            4),
    ("friendly",                4),
    ("friendlies",              4),
    ("test match",              4),
]


def get_regime(tournament: str | None, category: str | None = None) -> Regime:
    """
    Return the Regime for a given tournament name.
    Tier 4 (fringe) patterns are checked first — they override any tier 1/2/3 match.
    Falls back to Tier 3 (Mid) for unknown leagues.
    """
    text = _normalise(tournament or "") + " " + _normalise(category or "")

    # Check Tier 4 first — fringe markers override everything
    tier4_patterns = [p for p, t in _TIER_RULES if t == 4]
    for pattern in tier4_patterns:
        if pattern in text:
            return TIER_4

    # Then check Tier 1 → 2 → 3 in order
    for pattern, tier in _TIER_RULES:
        if tier == 4:
            continue
        if pattern in text:
            return _tier(tier)

    # Unknown league — default to Mid
    return TIER_3


def get_regime_for_doc(doc: dict[str, Any]) -> Regime:
    """Convenience: extract tournament/category from a match doc and return regime."""
    tournament = doc.get("tournament") or ""
    if isinstance(tournament, dict):
        tournament = tournament.get("name") or ""
    category = doc.get("category") or ""
    return get_regime(str(tournament), str(category))


def _tier(n: int) -> Regime:
    return {1: TIER_1, 2: TIER_2, 3: TIER_3, 4: TIER_4}[n]


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip())


# ── Regime-aware confidence gate ──────────────────────────────────────────────

def passes_regime_gate(
    tournament: str | None,
    confidence: int,
    edge: float = 0.0,
    category: str | None = None,
) -> dict[str, Any]:
    """
    Returns whether a pick passes the regime's minimum thresholds.
    Use this to filter picks before surfacing them to the user.

    Returns:
        passed:      bool
        regime:      Regime name + tier
        reason:      why it failed (if it did)
        adjusted_confidence: confidence after regime penalty/boost
    """
    regime = get_regime(tournament, category)

    # Tier 4 fringe: apply a confidence penalty — we trust the model less
    penalty = {1: 0, 2: 0, 3: 0, 4: -5}.get(regime.tier, 0)
    adjusted = max(1, confidence + penalty)

    passed = adjusted >= regime.min_confidence
    edge_ok = edge == 0.0 or edge >= regime.edge_threshold  # 0.0 = edge not applicable

    reason = None
    if not passed:
        reason = f"confidence {adjusted}% below {regime.name} threshold ({regime.min_confidence}%)"
    elif not edge_ok:
        reason = f"edge {edge*100:.1f}% below {regime.name} threshold ({regime.edge_threshold*100:.0f}%)"

    return {
        "passed":               passed and edge_ok,
        "tier":                 regime.tier,
        "regime":               regime.name,
        "min_confidence":       regime.min_confidence,
        "adjusted_confidence":  adjusted,
        "stake_cap":            regime.stake_cap,
        "reason":               reason,
        "description":          regime.description,
    }


# ── Regime-aware stake cap ────────────────────────────────────────────────────

def apply_regime_stake_cap(
    raw_multiplier: float,
    tournament: str | None,
    category: str | None = None,
) -> float:
    """
    Cap the stake multiplier based on the league's liquidity tier.
    Prevents over-betting in thin markets where CLV data is sparse.
    """
    regime = get_regime(tournament, category)
    return min(raw_multiplier, regime.stake_cap)


# ── Summary for analytics ─────────────────────────────────────────────────────

def regime_summary_for_predictions(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Given a list of prediction dicts, return a breakdown by regime tier.
    Useful for the analytics page to show where picks are coming from.
    """
    counts: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0}
    for pred in predictions:
        tournament = pred.get("tournament") or pred.get("league_name") or ""
        regime = get_regime(str(tournament))
        counts[regime.tier] = counts.get(regime.tier, 0) + 1

    total = sum(counts.values()) or 1
    return {
        "by_tier": [
            {
                "tier":    tier,
                "name":    _tier(tier).name,
                "count":   counts[tier],
                "pct":     round(counts[tier] / total * 100, 1),
                "description": _tier(tier).description,
            }
            for tier in [1, 2, 3, 4]
            if counts[tier] > 0
        ]
    }

