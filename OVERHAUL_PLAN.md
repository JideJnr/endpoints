# PredictX Prediction System Overhaul — Plan

**Prepared:** August 28, 2026
**Status:** Draft for review — no code changed yet.

## Why this plan exists

You reported that PredictX keeps publishing wrong predictions even when a clear favorite exists. A full read-through of the pipeline (generation → pick selection → grading → self-learning) turned up five concrete root causes, one of which explains almost all of it. This plan fixes them in an order that gets you the visible improvement fast while keeping the learning system honest. Nothing below has been implemented — it's here for you to approve, reorder, or descope before any file changes.

## What's actually happening today (recap)

Every match is scored by four models (Dixon-Coles, Elo, Poisson, a heuristic "rules" model) blended in `app/models/ensemble.py`, and the blend correctly computes a `clear_winner` flag when one side is far enough ahead (`CLEAR_WINNER_PROBABILITY_GAP`, currently 12 points, in `app/config/config.py`). That part works.

The problem is one step later, in `app/enrichment/enriched_prediction.py`'s `_finalize_prediction_output`. It builds candidate picks across *every* market — outright winner, double chance, BTTS/goals — and simply sorts all of them by a raw confidence number and publishes whichever one is highest, with no rule that the winner has to actually be a match-result pick. Hedged markets like "Home or Draw" mathematically carry higher confidence almost by construction, so they win the sort almost every time. Your database bears this out: of the last 3,010 published predictions, 76% are double-chance hedges and only 1% are outright match-winner picks — even though the system's own `clear_winner` calculation, sitting right next to this code, is telling it a real favorite exists.

Four smaller issues compound this: an odds-value formula in `signal_aggregator.py` that lets a cheap longshot outscore a thin-edge true favorite; a fallback path in `fallback_logic.py` that checks "home" before "away" and returns the first one that clears the bar rather than the best one; grading logic (`market_intent.py` plus a separate string-matching fallback in `_helpers.py`) that infers win/loss by matching team-name text and can mislabel outcomes, which matters because every learned weight and calibration curve in the system is trained on those labels; and a `self_learner.py` update rule with no variance guardrail (flat 15-sample minimum), so a short losing streak can swing model weights on noise. There's also one fully dead learning loop (`risk_learner.rebuild_risk_controls` is never called) and several `learned_parameters.py` functions that are computed every cycle but read by nothing.

## Guiding principles for the fix

Three rules apply across every phase below, because getting them wrong is how you'd end up overfitting a fix to today's data or silently breaking something worse:

1. **Never compare confidence across incompatible markets.** Decide the 1X2 favorite first, on its own terms, and only replace it with a hedge when the favorite genuinely fails an explicit, documented check (not by feeding it into the same sort as a double-chance candidate).
2. **Backtest before cutover.** You have 3,010 historical graded predictions sitting in `data/predictx_memory.sqlite3`. Every change below gets replayed against that history (and ideally re-graded using the corrected grading logic from Phase 2) before it goes live, so we know the win rate actually improves rather than just changing which mistakes get made.
3. **Ship behind a flag, watch before trusting.** Each phase lands as a shadow/dry-run path first (log what the new logic *would* have picked, alongside what the live system actually picked) for an agreed observation window, then cuts over.

## Phased plan

**Phase 1 — Fix pick selection (highest impact, most directly matches your complaint).**
Rework `_finalize_prediction_output` so the flow is: compute the 1X2 favorite and its confidence gap first (reusing the existing `clear_winner` logic) → publish it directly if it passes the gap/sample checks → only fall through to a double-chance or BTTS candidate if the favorite check genuinely fails, and log why. Fix `signal_aggregator.score_pick_direction` so odds-value can only refine among directions that already clear a probability bar, not override a clear statistical favorite outright. Fix `fallback_logic._try_directional`/`_try_proven_directional` to evaluate home/draw/away and take the best-scoring one, not the first one in a hardcoded list order. Target files: `app/enrichment/enriched_prediction.py`, `app/enrichment/signal_aggregator.py`, `app/risk/fallback_logic.py`, `app/risk/pick_generator.py` (remove the dead duplicate "secondary pick" branch while in there).

**Phase 2 — Fix grading accuracy (foundation for everything the system learns).**
Consolidate the two divergent grading implementations (`app/market/market_intent.py::grade_market_intent` and `app/storage/league_memory/_helpers.py::_grade_pick_for_match`) into one path, replacing fragile team-name substring matching with a match-id/side-based lookup wherever the data supports it, and add a small regression test suite using known matches with tricky team names (short names, one name containing another) to catch mislabeling. This phase should land before you fully trust any *learned* weight changes, since Phase 4/5 both consume these labels.

**Phase 3 — Repair the self-learning loop.**
Add a variance-aware guardrail to `self_learner.py` (e.g., a minimum effective sample size with shrinkage toward the prior, not a flat count) so a short losing streak can't swing weights hard. Decide explicitly whether to wire up `risk_learner.rebuild_risk_controls` (currently dead) or remove the risk-controls read path in `risk_manager.py` that depends on it — right now it's silently reading stale defaults. Delete or wire the orphaned `learned_parameters.py` functions that have zero callers (`get_pick_generator_thresholds`, `get_prediction_agent_params`, `get_calibration_gap_thresholds`, `get_frontend_engine_params`, `get_frontend_api_limits`, `get_engine_learning_limits`) — dead computation that's easy to mistake for a working feedback loop.

**Phase 4 — Tighten calibration and the validation gate.**
`confidence_calibration` currently has only 28 rows against a 30-sample minimum, so `validation_gate.py` is permanently stuck in bootstrap mode and not actually gating anything. Once Phase 2 is producing trustworthy labels, monitor sample accumulation and confirm the gate starts biting as designed; consider lowering the bootstrap threshold temporarily with a wider gap tolerance rather than leaving it inert. Also fix `crud.py`'s silent filter that never writes sub-55-confidence or no_bet picks into `prediction_history` — it's biasing the very data calibration learns from.

**Phase 5 — Deduplicate and clean up.**
`self_learner.py` and `learned_parameters.py` maintain two independent, divergent notions of "the learned weights/thresholds" with different cache lifetimes. Pick one system of record and have the other delegate to it (or retire it) to remove drift risk. Also persist raw model probabilities in `picks_json` (currently null on real rows) so future audits don't need a full code read-through to reconstruct what the model actually thought.

**Phase 6 — Validate and roll out.**
Backtest Phases 1–2 together against the 3,010-row history, compare hit rate on match-result picks specifically (not pooled across markets), run shadow mode for an agreed window (suggest at least 100–150 graded matches, roughly 1–2 weeks at current volume), then cut over. Re-run the backtest after Phase 3–4 land to confirm calibration/learning changes are net positive before leaving shadow mode.

## What I need from you to start

Confirm the phase order above (or reprioritize), and let me know: (1) whether Phase 1 should ship as a feature-flagged shadow path or you're comfortable going straight to live once backtested, and (2) how much of the historical 3,010-row database you want used for backtesting vs. held out as a clean validation set.
