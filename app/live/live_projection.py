"""Live scoreline-grid prototype for in-play prediction.

PURPOSE
-------
Today `_live_inplay_picks()` in `app/enrichment/enriched_prediction.py` produces
four *independent* live picks (live_next_goal / live_total_goals over+under,
live_team_to_score, live_match_winner) from separate hand-tuned heuristics, each
with its own confidence number. That works for "who scores next" but does not
generalise: there is no live win/draw/away distribution, no live BTTS, no live
correct score, and no shared joint distribution a live bet builder could pull
correlated legs from (see `plans/prediction_engine_workflow_analysis.md` and the
research summary this prototype grew out of).

This module replaces "one heuristic per market" with ONE primitive: a
probability grid over how many MORE goals each side scores between now and
full time. Every live market is then a derived read of that same grid, which
means they are automatically mutually consistent (e.g. correct score, BTTS and
the O/U ladder can never contradict each other) — which is exactly what a
same-match bet builder needs for leg correlation.

It deliberately reuses the existing pre-match engine rather than reinventing
it: `app/models/dixon_coles.py` / `app/models/poisson.py` already compute a
full scoreline probability grid pre-match (`top_scorelines`) from
`home_lambda`/`away_lambda` (expected goals for the FULL 90 minutes, from
`_team_stats()`). This module takes those two numbers, scales them down to
"expected goals for the *remaining* minutes", blends in what has actually
happened *this* match so far (goals, and xG if the provider has it), and
re-runs the same Poisson/Dixon-Coles machinery over the remainder.

STATUS: prototype only. Not imported by `enriched_prediction.py` or anything
in the live pipeline yet — see the `demo()` function at the bottom for a
worked example, and `project_live_match()` for the integration entry point
once this is validated against real live matches and someone decides to wire
it in.

WHAT THIS DOES NOT MODEL (be honest about the limitations)
-----------------------------------------------------------
- Red cards are not yet a rate multiplier (a team down to 10 men should have
  its remaining-goals rate cut and the opponent's raised — not implemented
  here; `red_cards` is already ingested by `match_facts.py` so this is a
  straightforward follow-up, not a new ingest need).
- Extra time / stoppage time is treated as "just fewer minutes left"; a cup
  match going to 120 minutes needs `match_length_minutes=120` passed in once
  it enters extra time, not modelled automatically from period text.
- The observed-rate blend uses xG (or a shots-based proxy) as the *only* live
  signal. Possession/attacks/dangerous-attacks/corners are ingested but not
  used here — they are noisier proxies for goal rate than xG and were left
  out of v1 to keep the math auditable; see GAPS notes for how to add them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

# Reuse the exact same low-score correlation correction and Poisson pmf the
# pre-match engine already uses, instead of re-deriving/duplicating them.
from app.models.dixon_coles import _tau, RHO  # noqa: F401 (RHO re-exported for callers/tests)
from app.models.poisson import _poisson_prob

MAX_ADDITIONAL_GOALS = 6  # per side; covers >99.9% of realistic remaining-goal counts
DEFAULT_MATCH_LENGTH_MINUTES = 90.0
DEFAULT_PRIOR_STRENGTH_MINUTES = 30.0  # shrinkage constant K, see _blended_remaining_rate


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------

@dataclass
class LiveInputs:
    home_lambda_full: float  # mu from run_dixon_coles/run_poisson: full-match expected goals
    away_lambda_full: float  # lam
    elapsed_minutes: float
    home_score: int
    away_score: int
    match_length_minutes: float = DEFAULT_MATCH_LENGTH_MINUTES
    # Optional live signal. Everything degrades gracefully to a pure
    # time-decay of the pre-match prior if these are omitted (elapsed=0 or no
    # live stats yet -> live projection == pre-match projection scaled by
    # time remaining, which is the correct behaviour at kickoff).
    home_xg_so_far: Optional[float] = None
    away_xg_so_far: Optional[float] = None
    home_shots_on_target: int = 0
    away_shots_on_target: int = 0
    home_shots_total: int = 0
    away_shots_total: int = 0
    prior_strength_minutes: float = DEFAULT_PRIOR_STRENGTH_MINUTES

    def __post_init__(self) -> None:
        self.home_lambda_full = max(float(self.home_lambda_full), 0.05)
        self.away_lambda_full = max(float(self.away_lambda_full), 0.05)
        self.elapsed_minutes = max(float(self.elapsed_minutes), 0.0)
        self.home_score = max(int(self.home_score), 0)
        self.away_score = max(int(self.away_score), 0)


def _estimate_live_xg(shots_on_target: int, shots_total: int) -> Optional[float]:
    """Rough proxy xG for when the provider doesn't return real xG for this
    match: ~0.30 xG per shot on target, ~0.05 for the rest. This is a coarse
    fallback, not a replacement for real xG — flagged as such in the output
    (see LiveProjection.diagnostics['home_xg_source'])."""
    if shots_total <= 0 and shots_on_target <= 0:
        return None
    off_target = max(shots_total - shots_on_target, 0)
    return shots_on_target * 0.30 + off_target * 0.05


def _blended_remaining_rate(
    prior_full_lambda: float,
    elapsed: float,
    match_length: float,
    observed_goal_equiv_so_far: Optional[float],
    prior_strength_minutes: float,
) -> tuple[float, float]:
    """Bayesian shrinkage between the pre-match rate and this match's
    observed rate so far, weighted by how much of the match has been played.

    At kickoff (elapsed=0) this returns exactly the pre-match rate scaled by
    time remaining. As the match progresses, what's actually happening in
    THIS match (xG so far) is trusted more and more, controlled by
    `prior_strength_minutes` (K): weight_observed = elapsed / (elapsed + K).
    With the default K=30, at minute 30 the observed and prior rates get
    equal weight; at minute 60 observed gets ~2x the weight of the prior.

    Returns (remaining_expected_goals, weight_observed_used).
    """
    remaining = max(match_length - elapsed, 0.0)
    prior_rate_per_min = prior_full_lambda / match_length
    if elapsed <= 0 or observed_goal_equiv_so_far is None:
        blended_rate_per_min = prior_rate_per_min
        weight_observed = 0.0
    else:
        observed_rate_per_min = observed_goal_equiv_so_far / elapsed
        weight_observed = elapsed / (elapsed + prior_strength_minutes)
        blended_rate_per_min = (
            weight_observed * observed_rate_per_min + (1 - weight_observed) * prior_rate_per_min
        )
    remaining_lambda = max(blended_rate_per_min * remaining, 0.02)  # tiny floor, never exactly 0
    return remaining_lambda, weight_observed


@dataclass
class LiveProjection:
    home_lambda_remaining: float
    away_lambda_remaining: float
    grid: dict[tuple[int, int], float]  # (extra_home_goals, extra_away_goals) -> probability
    inputs: LiveInputs
    diagnostics: dict[str, Any] = field(default_factory=dict)

    # ---- derived markets: every one of these reads the SAME grid, so they
    # can never contradict each other (this is what makes it usable for a
    # same-match bet builder's leg correlation, unlike four separate
    # heuristics each with their own confidence number) ----

    def match_winner(self) -> dict[str, float]:
        home_win = draw = away_win = 0.0
        for (eh, ea), p in self.grid.items():
            final_home = self.inputs.home_score + eh
            final_away = self.inputs.away_score + ea
            if final_home > final_away:
                home_win += p
            elif final_home == final_away:
                draw += p
            else:
                away_win += p
        return {"home_win": home_win, "draw": draw, "away_win": away_win}

    def btts(self) -> float:
        home_already = self.inputs.home_score >= 1
        away_already = self.inputs.away_score >= 1
        if home_already and away_already:
            return 1.0
        prob = 0.0
        for (eh, ea), p in self.grid.items():
            home_scores = home_already or eh >= 1
            away_scores = away_already or ea >= 1
            if home_scores and away_scores:
                prob += p
        return prob

    def over_under(self, line: float) -> dict[str, float]:
        """line is a .5 total-goals line, e.g. 2.5, 3.5 — no push possible."""
        current_total = self.inputs.home_score + self.inputs.away_score
        over = 0.0
        for (eh, ea), p in self.grid.items():
            if current_total + eh + ea > line:
                over += p
        return {"line": line, "over": over, "under": 1.0 - over}

    def over_under_ladder(self, lines: tuple[float, ...] = (0.5, 1.5, 2.5, 3.5, 4.5)) -> list[dict[str, float]]:
        return [self.over_under(line) for line in lines]

    def correct_score_top(self, n: int = 5) -> list[dict[str, Any]]:
        scored = []
        for (eh, ea), p in self.grid.items():
            final_home = self.inputs.home_score + eh
            final_away = self.inputs.away_score + ea
            scored.append((final_home, final_away, p))
        # multiple (eh, ea) combos can't map to the same final score here
        # since home_score/away_score are fixed offsets, so no merge needed.
        scored.sort(key=lambda item: item[2], reverse=True)
        return [
            {"score": f"{h}-{a}", "probability": round(p * 100, 2)}
            for h, a, p in scored[:n]
        ]

    def next_goal(self) -> dict[str, float]:
        """Who scores the NEXT goal (or neither does). Derived directly from
        the two remaining-time rates via the standard competing-Poisson-
        processes result (not from the grid): for two independent Poisson
        processes with rates a and b, the probability the next event overall
        comes from process A is a/(a+b), independent of time horizon; the
        probability that NO event happens at all in the remaining time is
        exp(-(a+b)). Note this ignores the Dixon-Coles low-score tilt (tau
        adjusts the joint final-score PMF, it doesn't have a clean
        "next-event" interpretation), so treat this as a good approximation,
        not exact given the grid above.
        """
        a = self.home_lambda_remaining
        b = self.away_lambda_remaining
        p_no_more_goals = math.exp(-(a + b))
        p_home_next = (1 - p_no_more_goals) * (a / (a + b))
        p_away_next = (1 - p_no_more_goals) * (b / (a + b))
        return {
            "no_more_goals": p_no_more_goals,
            "home_scores_next": p_home_next,
            "away_scores_next": p_away_next,
        }

    def summary(self) -> dict[str, Any]:
        """Everything above, packaged for logging/inspection — this is the
        shape you'd want to sanity-check against `match_snapshots` outcomes
        or eyeball against real matches before wiring this in anywhere."""
        return {
            "minutes_remaining": round(self.inputs.match_length_minutes - self.inputs.elapsed_minutes, 1),
            "home_lambda_remaining": round(self.home_lambda_remaining, 3),
            "away_lambda_remaining": round(self.away_lambda_remaining, 3),
            "match_winner": {k: round(v * 100, 1) for k, v in self.match_winner().items()},
            "btts": round(self.btts() * 100, 1),
            "over_under_ladder": [
                {"line": r["line"], "over": round(r["over"] * 100, 1), "under": round(r["under"] * 100, 1)}
                for r in self.over_under_ladder()
            ],
            "correct_score_top5": self.correct_score_top(5),
            "next_goal": {k: round(v * 100, 1) for k, v in self.next_goal().items()},
            "diagnostics": self.diagnostics,
        }


# ---------------------------------------------------------------------------
# Core projection
# ---------------------------------------------------------------------------

def project(inputs: LiveInputs) -> LiveProjection:
    home_xg_so_far = inputs.home_xg_so_far
    home_xg_source = "provider_xg"
    if home_xg_so_far is None:
        home_xg_so_far = _estimate_live_xg(inputs.home_shots_on_target, inputs.home_shots_total)
        home_xg_source = "shots_proxy" if home_xg_so_far is not None else "none_prior_only"

    away_xg_so_far = inputs.away_xg_so_far
    away_xg_source = "provider_xg"
    if away_xg_so_far is None:
        away_xg_so_far = _estimate_live_xg(inputs.away_shots_on_target, inputs.away_shots_total)
        away_xg_source = "shots_proxy" if away_xg_so_far is not None else "none_prior_only"

    home_lambda_remaining, home_weight_observed = _blended_remaining_rate(
        inputs.home_lambda_full, inputs.elapsed_minutes, inputs.match_length_minutes,
        home_xg_so_far, inputs.prior_strength_minutes,
    )
    away_lambda_remaining, away_weight_observed = _blended_remaining_rate(
        inputs.away_lambda_full, inputs.elapsed_minutes, inputs.match_length_minutes,
        away_xg_so_far, inputs.prior_strength_minutes,
    )

    grid: dict[tuple[int, int], float] = {}
    total = 0.0
    for eh in range(MAX_ADDITIONAL_GOALS + 1):
        for ea in range(MAX_ADDITIONAL_GOALS + 1):
            p = (
                _poisson_prob(home_lambda_remaining, eh)
                * _poisson_prob(away_lambda_remaining, ea)
                * _tau(eh, ea, home_lambda_remaining, away_lambda_remaining, RHO)
            )
            grid[(eh, ea)] = p
            total += p
    if total <= 0:
        total = 1.0
    grid = {k: v / total for k, v in grid.items()}

    diagnostics = {
        "home_xg_so_far": round(home_xg_so_far, 3) if home_xg_so_far is not None else None,
        "home_xg_source": home_xg_source,
        "home_weight_on_live_signal": round(home_weight_observed, 2),
        "away_xg_so_far": round(away_xg_so_far, 3) if away_xg_so_far is not None else None,
        "away_xg_source": away_xg_source,
        "away_weight_on_live_signal": round(away_weight_observed, 2),
    }
    return LiveProjection(home_lambda_remaining, away_lambda_remaining, grid, inputs, diagnostics)


# ---------------------------------------------------------------------------
# Integration entry point — wraps the prototype around what the pipeline
# already has, so wiring this in later is a ~5 line change in
# enriched_prediction.py, not a rewrite.
# ---------------------------------------------------------------------------

def project_live_match(
    prematch_result: dict[str, Any],
    *,
    elapsed_minutes: float,
    home_score: int,
    away_score: int,
    live_statistics_summary: Optional[dict[str, Any]] = None,
    match_length_minutes: float = DEFAULT_MATCH_LENGTH_MINUTES,
    prior_strength_minutes: float = DEFAULT_PRIOR_STRENGTH_MINUTES,
) -> LiveProjection:
    """
    prematch_result: the dict returned by `run_dixon_coles()` / `run_poisson()`
        (app/models/dixon_coles.py, app/models/poisson.py) — uses its
        "home_lambda" / "away_lambda" keys.
    live_statistics_summary: `doc["live_statistics"]["summary"]` as produced by
        `enrich_match_facts()` in app/match_facts.py — a dict keyed by stat
        name (from LIVE_STAT_NAMES: "xg", "shots_on_target", "total_shots",
        ...) each holding {"home": val, "away": val, "name": raw_name}. Pass
        None if not available yet (e.g. very early in the match).
    """
    summary = live_statistics_summary or {}

    def _stat(key: str, side: str) -> Any:
        entry = summary.get(key) or {}
        return entry.get(side)

    home_xg = _stat("xg", "home")
    away_xg = _stat("xg", "away")

    inputs = LiveInputs(
        home_lambda_full=prematch_result.get("home_lambda", 1.4),
        away_lambda_full=prematch_result.get("away_lambda", 1.1),
        elapsed_minutes=elapsed_minutes,
        home_score=home_score,
        away_score=away_score,
        match_length_minutes=match_length_minutes,
        home_xg_so_far=float(home_xg) if home_xg is not None else None,
        away_xg_so_far=float(away_xg) if away_xg is not None else None,
        home_shots_on_target=int(_stat("shots_on_target", "home") or 0),
        away_shots_on_target=int(_stat("shots_on_target", "away") or 0),
        home_shots_total=int(_stat("total_shots", "home") or 0),
        away_shots_total=int(_stat("total_shots", "away") or 0),
        prior_strength_minutes=prior_strength_minutes,
    )
    return project(inputs)


# ---------------------------------------------------------------------------
# Self-check + worked demo (run with: python -m app.live.live_projection)
# ---------------------------------------------------------------------------

def _validate_grid(projection: LiveProjection, tol: float = 1e-6) -> None:
    total = sum(projection.grid.values())
    assert abs(total - 1.0) < tol, f"grid does not sum to 1.0 (got {total})"
    markets = projection.match_winner()
    assert abs(sum(markets.values()) - 1.0) < tol, f"match_winner does not sum to 1.0 (got {markets})"
    ng = projection.next_goal()
    assert abs(sum(ng.values()) - 1.0) < tol, f"next_goal does not sum to 1.0 (got {ng})"


def demo() -> None:
    import json

    print("=" * 78)
    print("Worked example: a match where pre-match Dixon-Coles gave")
    print("home_lambda=1.55 (favourite at home), away_lambda=1.15")
    print("=" * 78)
    prematch = {"home_lambda": 1.55, "away_lambda": 1.15}

    scenarios = [
        dict(
            label="Kickoff (minute 0, 0-0, no live stats yet)",
            elapsed_minutes=0, home_score=0, away_score=0, live_statistics_summary=None,
        ),
        dict(
            label="Minute 30, still 0-0, stats roughly matching pre-match expectation",
            elapsed_minutes=30, home_score=0, away_score=0,
            live_statistics_summary={
                "xg": {"home": 0.55, "away": 0.35},
                "shots_on_target": {"home": 3, "away": 2},
                "total_shots": {"home": 6, "away": 4},
            },
        ),
        dict(
            label="Minute 63, home leads 1-0, but AWAY is dominating on the ball "
                  "(higher live xG than home despite trailing on the scoreboard)",
            elapsed_minutes=63, home_score=1, away_score=0,
            live_statistics_summary={
                "xg": {"home": 0.6, "away": 1.7},
                "shots_on_target": {"home": 2, "away": 6},
                "total_shots": {"home": 5, "away": 13},
            },
        ),
        dict(
            label="Minute 85, still 1-0, home sitting deep / game management, "
                  "away still pushing but running out of time",
            elapsed_minutes=85, home_score=1, away_score=0,
            live_statistics_summary={
                "xg": {"home": 0.7, "away": 2.1},
                "shots_on_target": {"home": 2, "away": 8},
                "total_shots": {"home": 6, "away": 17},
            },
        ),
    ]

    for scenario in scenarios:
        label = scenario.pop("label")
        projection = project_live_match(prematch, **scenario)
        _validate_grid(projection)
        print(f"\n--- {label} ---")
        print(json.dumps(projection.summary(), indent=2))


if __name__ == "__main__":
    demo()
