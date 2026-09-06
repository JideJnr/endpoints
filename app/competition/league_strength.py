"""League strength scoring — hybrid three-tier approach.

Resolution order for any given league name:

1. Hardcoded prior (curated catalogue)
   TOP_30_COMPETITIONS entries each carry an explicit ``league_strength``
   score representing real footballing quality (20–98 scale). World Cup
   is hardcoded at 98. These are ground-truth anchors that never drift
   based on prediction performance.

2. ELO-derived refinement (blended with the prior when available)
   get_elo_league_strength() computes the average ELO of teams that have
   played ≥3 matches in this league. When ≥4 rated teams exist, the ELO
   score is blended with the prior: 70% prior + 30% ELO. This lets the
   system self-correct for leagues where the curated score and observed
   team quality diverge over time, without the prior ever being swamped.
   ELO refinement is also the ONLY source for leagues not in the catalogue
   (e.g. League One, Bundesliga 2, Serie B, etc.) that have built up
   enough ELO history.

3. Prediction accuracy fallback (legacy, catalogue-miss only)
   For leagues with no ELO data either, fall back to the old
   tournament_preferences priority → score mapping. This keeps behaviour
   unchanged for leagues the system already tracks via graded predictions.

4. Unknown-league downplay
   Leagues that reach none of the above (cold start, obscure competitions,
   youth/reserve leagues) default to 42 instead of the old 55 neutral.
   This is intentional: when we don't know a league we should be sceptical
   of results against its teams, not treat them as average-quality opposition.
"""
from __future__ import annotations

from statistics import mean
from typing import Any

# Mapping of normalised league name → curated score.
# Built lazily from TOP_30_COMPETITIONS + DEFAULT_WORLD_CUP on first use.
_CATALOGUE_SCORES: dict[str, int] | None = None


def _build_catalogue() -> dict[str, int]:
    """Build a name→score lookup from the curated catalogue. Cached."""
    global _CATALOGUE_SCORES
    if _CATALOGUE_SCORES is not None:
        return _CATALOGUE_SCORES

    mapping: dict[str, int] = {}
    try:
        from app.competition.competition_special import TOP_30_COMPETITIONS, DEFAULT_WORLD_CUP
        for entry in TOP_30_COMPETITIONS:
            strength = int(entry.get("league_strength") or 55)
            mapping[_clean(entry["name"])] = strength
            mapping[_clean(entry["key"])] = strength
        # World Cup — not in TOP_30 but equally elite
        mapping[_clean(DEFAULT_WORLD_CUP["name"])] = 98
        mapping[_clean(DEFAULT_WORLD_CUP["key"])] = 98
    except Exception:
        pass

    _CATALOGUE_SCORES = mapping
    return _CATALOGUE_SCORES


def _catalogue_score(name: str) -> int | None:
    """Return the curated prior score for a league name, or None if not found."""
    key = _clean(name)
    if not key:
        return None
    catalogue = _build_catalogue()
    return catalogue.get(key)


def league_strength_score(name: str | None) -> dict[str, Any]:
    """Return the hybrid league strength score for *name* on a 20–98 scale.

    See module docstring for the full resolution order. The returned dict
    always contains at least ``{"name", "score", "country", "basis"}`` so
    every existing caller continues to work unchanged. ``"tier"`` and
    ``"source"`` are added as extra metadata.
    """
    cleaned = _clean(name)
    if not cleaned:
        return {
            "name": name,
            "score": 42,
            "country": None,
            "basis": "unknown_league_downplayed",
            "tier": _score_to_tier(42),
            "source": "unknown",
        }

    # ── Step 1: Hardcoded prior ───────────────────────────────────────────
    prior = _catalogue_score(str(name))

    # ── Step 2: ELO-derived refinement ───────────────────────────────────
    elo_result: dict[str, Any] = {}
    try:
        from app.models.elo import get_elo_league_strength
        elo_result = get_elo_league_strength(str(name))
    except Exception:
        pass

    elo_available = bool(elo_result.get("available"))
    elo_score: int | None = int(elo_result["score"]) if elo_available else None

    # ── Combine prior + ELO ───────────────────────────────────────────────
    if prior is not None and elo_available and elo_score is not None:
        # We have both: blend 70% curated prior + 30% ELO evidence.
        # The prior anchors the score to real footballing quality; ELO
        # nudges it as team-level data accumulates.
        blended = round(prior * 0.70 + elo_score * 0.30)
        return {
            "name": name,
            "score": max(20, min(98, blended)),
            "country": None,
            "basis": "catalogue_prior_elo_blend",
            "tier": _score_to_tier(blended),
            "source": "hybrid",
            "prior_score": prior,
            "elo_score": elo_score,
            "elo_rated_teams": elo_result.get("rated_teams"),
            "avg_elo": elo_result.get("avg_elo"),
        }

    if prior is not None:
        # Catalogue match only — no ELO data yet for this league.
        return {
            "name": name,
            "score": prior,
            "country": None,
            "basis": "catalogue_prior",
            "tier": _score_to_tier(prior),
            "source": "catalogue",
        }

    if elo_available and elo_score is not None:
        # Not in catalogue but has ELO history — trust the data.
        # Apply a small conservative haircut (×0.95) since there's no
        # catalogue anchor: we're less certain this is representative.
        elo_adjusted = max(20, min(98, round(elo_score * 0.95)))
        return {
            "name": name,
            "score": elo_adjusted,
            "country": None,
            "basis": "elo_derived",
            "tier": _score_to_tier(elo_adjusted),
            "source": "elo",
            "elo_score": elo_score,
            "elo_rated_teams": elo_result.get("rated_teams"),
            "avg_elo": elo_result.get("avg_elo"),
        }

    # ── Step 3: Prediction accuracy fallback ─────────────────────────────
    try:
        from app.monitoring.self_learner import get_tournament_priority
        learned = get_tournament_priority(str(name))
        if learned.get("known"):
            priority = int(learned.get("priority", 4))
            # Map priority (0=best → 7=avoid) to score.
            # Keep this identical to the old formula so existing leagues
            # whose accuracy we've tracked are unaffected.
            legacy_score = max(20, min(98, 85 - priority * 8))
            # Cap at 72: prediction accuracy alone can't tell us a league
            # is better than EFL Championship (our weakest curated entry).
            # Good accuracy in an unknown league means we predict it well,
            # not that its teams are strong.
            capped_score = min(legacy_score, 72)
            return {
                "name": name,
                "score": capped_score,
                "country": None,
                "basis": "prediction_accuracy_fallback",
                "tier": _score_to_tier(capped_score),
                "source": "learned",
            }
    except Exception:
        pass

    # ── Step 4: Unknown — downplay ────────────────────────────────────────
    # 42 sits firmly in the "weak-to-mid" range, discounting results
    # against teams from leagues we know nothing about.
    return {
        "name": name,
        "score": 42,
        "country": None,
        "basis": "unknown_league_downplayed",
        "tier": _score_to_tier(42),
        "source": "unknown",
    }


def history_league_strength(history: list[dict[str, Any]] | None, last_n: int = 10) -> dict[str, Any]:
    """Average league strength score across the last *last_n* finished events."""
    events = history or []
    scores = []
    leagues = []
    for event in events:
        if event.get("status", {}).get("type") != "finished":
            continue
        tournament = event.get("tournament") or {}
        league_name = tournament.get("name") if isinstance(tournament, dict) else str(tournament or "")
        strength = league_strength_score(league_name)
        scores.append(strength["score"])
        leagues.append(strength)
        if len(scores) >= last_n:
            break
    if not scores:
        return {"sample_size": 0, "avg_score": 42, "leagues": []}
    return {
        "sample_size": len(scores),
        "avg_score": round(mean(scores), 1),
        "leagues": leagues[:5],
    }


def league_strength_edge(
    event: dict[str, Any],
    home_history: list[dict[str, Any]] | None,
    away_history: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Signal: difference in avg opponent league quality between the two sides."""
    tournament = event.get("tournament") or {}
    tournament_name = tournament.get("name") if isinstance(tournament, dict) else str(tournament or "")
    match_league = league_strength_score(tournament_name)
    home = history_league_strength(home_history)
    away = history_league_strength(away_history)
    edge = 0.0
    if home["sample_size"] and away["sample_size"]:
        edge = (home["avg_score"] - away["avg_score"]) * 0.35
    return {
        "edge": round(max(-14, min(14, edge)), 2),
        "match_league": match_league,
        "home_recent_league_strength": home,
        "away_recent_league_strength": away,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _score_to_tier(score: int | float) -> int:
    """Convert a 20–98 score to a 1–6 tier integer (1=elite, 6=weak)."""
    s = int(score)
    if s >= 88:
        return 1
    if s >= 75:
        return 2
    if s >= 62:
        return 3
    if s >= 50:
        return 4
    if s >= 38:
        return 5
    return 6


def _clean(value: str | None) -> str:
    return " ".join(str(value or "").lower().replace("-", " ").split())
