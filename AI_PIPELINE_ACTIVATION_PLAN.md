# AI Signal → Final-Decision Pipeline — Investigation & Activation Plan

**Prepared:** August 29, 2026
**Status:** Investigation only — no code changed. This is a companion to `OVERHAUL_PLAN.md`, which covers the deterministic pick-selection bug; this document covers the OpenRouter/LLM decision layer specifically.

## What you asked for

One coherent pipeline: every signal (form, H2H, odds, standings, statistical models, competition context, learned history) gets fed to AI to produce sub-decisions, and those sub-decisions plus everything the system has learned get fed to one final AI call that makes the final call — all running on OpenRouter.

## The short answer: it already exists, twice, and neither copy is what you're picturing

You don't need to build this from scratch. The pieces are already in `app/ai/` and they already call OpenRouter. But instead of one clean pipeline, there are **three overlapping implementations** that were built at different times and never consolidated, plus a fourth reviewer layer that's wired to the wrong place. The result is that OpenRouter calls ARE going out right now on a schedule, but the system is doing roughly 2x the LLM work it needs to per match, recording two different opinions to two different places, and never actually running the "final AI with all our learned information" step you're describing — that step exists as code, it's just not connected to the automatic pipeline.

## Current state, mapped

**1. `app/ai/ai_prediction_pipeline.py` — the one that's actually scheduled and live**
Runs every 5 minutes (`job_ai_prediction_queue`, `ai_prediction_queue` pipeline, default state `active`, batch of 10 matches per cycle). For each match it fires 6 evidence specialists in parallel over OpenRouter — H2H, common-opponent, form, market odds, similar-match, team-history — then one more OpenRouter call (`_call_decider`) that synthesizes all 6 into a market/outcome/confidence decision. That decider prompt already includes per-specialist historical accuracy weights pulled from a `specialist_performance` table — this is the closest thing in the codebase to "feed it everything we've learned."

**2. `app/ai/llm_pipeline.py` — a second, independent specialist system**
A different 5-specialist set (form, H2H, odds, standings, statistical-model consensus) feeding a `run_final_synthesis` call, optionally followed by a `run_brain_review` call. This is a complete, self-contained pipeline in its own right — and it is the one that actually gets **recorded to `prediction_history`** (the table your dashboard reads), because of how the two are wired together (next point).

**3. How they collide**
`run_ai_prediction_with_fallback` (pipeline #1) does its own 6-specialist + decider work, then calls `apply_prediction_state(..., use_llm_pipeline=True)`. Inside that call, `predict_and_record_enriched` sees `use_llm_pipeline=True` and runs **pipeline #2** (`run_llm_pipeline`) instead — and it's pipeline #2's output that gets written to `prediction_history` via `record_prediction`. Pipeline #1 then takes that already-recorded result and overwrites several of its fields (market, outcome, confidence, reasoning) with its own decider's answer, but only in the in-memory copy that goes back into the match buffer — not in the row already committed to the database.

Net effect per match, every 5 minutes: **13 separate OpenRouter calls** (6 + 1 from pipeline #1, 5 + 1 from pipeline #2) to produce two different "final" answers, one of which is silently discarded after being written to your database. This is very likely inflating your OpenRouter usage for no benefit and is a real risk for the free-tier rate limit (see below).

**4. `app/ai/ai_brain.py` — the actual "final AI with all our learned information" layer, wired to the wrong door**
This is the piece that matches what you described almost exactly: `oversee_prediction` builds a `memory_context` from `self_learner` (signal weights, top/cold signals, league accuracy), CLV performance, and the confidence-calibration table, then makes one more OpenRouter call asking the model to adjust confidence up or down based on that history. But it's only invoked from `app/routers/agent.py`'s on-demand endpoints (`_with_ai_brain`) — i.e. when someone calls the API directly. `job_ai_prediction_queue`, the thing that actually runs on a timer, calls `run_ai_prediction_with_fallback` with `attach_brain=False` (the default), so **this review step never runs automatically.** The richest "everything we've learned" context you're asking for exists in code and is never exercised by the scheduled pipeline.

**5. One more dead loop found along the way**
`record_specialist_outcome` / `grade_specialist_contributions` (the functions that would update the `specialist_performance` weights pipeline #1's decider prompt reads) are defined in `ai_prediction_pipeline.py` but **not called from anywhere else in the codebase** — confirmed by searching the grading code in `storage/league_memory/`. So the "historical accuracy" weights fed into the decider prompt have likely been sitting at neutral (1.0) since this was built; nothing is grading and feeding results back in. Same shape of bug as the `risk_learner.rebuild_risk_controls` and `match_snapshots` dead loops your other plan already flagged — this pipeline has its own version of it.

**6. Race condition between this and the deterministic pipeline**
`apply_prediction_state`'s cooldown check (`_recent_ungraded_prediction`) only looks at `match_id` + prediction mode, not source. So whichever system predicts a given match first — the deterministic ensemble path (`enriched_prediction.py`, covered in `OVERHAUL_PLAN.md`) or this AI queue — locks the other out for that match for the cooldown window (3–180 min). They are not currently designed to work together; they compete for the same matches.

## Is OpenRouter actually configured?

Yes. `OPENROUTER_API_KEY` is present in your `.env`, `OPENROUTER_MODEL=openrouter/free`, base URL is the standard `https://openrouter.ai/api/v1`. `openrouter/free` is a real OpenRouter product (their "Free Models Router," which auto-routes across free-tier models) — not a typo, so that part is fine as configured.

**Caveat I couldn't fully verify this session:** OpenRouter's free-tier rate limits (I hit a tool-level fetch limit partway through checking their current docs, and this is exactly the kind of number that changes over time, so please confirm on openrouter.ai/docs/api_reference/limits directly). Historically their free models are capped per-key at a small number of requests per minute and a modest daily cap that's higher once you've added at least a few dollars of credit to the account. At **13 LLM calls per match × 10 matches per 5-minute cycle**, this system can burst to 130 calls in under 5 minutes — that's a plausible way for you to be silently rate-limited (which just falls back to the rules engine, so it can look "active" while quietly not doing much). This is worth checking directly against your OpenRouter dashboard's usage/rate-limit numbers before assuming everything is flowing.

## What "active" should actually look like

One pipeline, one final answer, per match:

1. **Signal specialists (parallel OpenRouter calls)** — keep pipeline #1's 6-specialist set (it's more thorough than #2's — H2H, common-opponent, form, odds, similar-match, team-history) and retire #2 (`llm_pipeline.py`) or repurpose it rather than running both.
2. **Statistical model consensus** folded in as one more evidence input (Poisson/Dixon-Coles/Elo/ensemble `clear_winner`) — pipeline #2 already had a `run_model_specialist` for this that's worth keeping, just as an input to the one decider, not a second pipeline.
3. **One decider call** that receives all specialist outputs + model consensus + specialist historical weights (already exists — `_call_decider`).
4. **One final review call** — this is `ai_brain.oversee_prediction`, called with the full `memory_context` (self-learner signal weights, league accuracy, CLV, calibration bands) as you described. This should run automatically on every queued prediction, not just on-demand API calls.
5. **One write** to `prediction_history` — the reviewed, adjusted result, so grading and the dashboard both see the same thing the decider and brain actually agreed on.
6. **Close the specialist-learning loop** — wire `grade_specialist_contributions` into whatever already grades outcomes for the deterministic path, so specialist weights actually update instead of sitting at neutral forever.

## Concrete steps to activate it (in order)

1. Confirm current OpenRouter usage/rate-limit status on your dashboard — settle whether the free router is actually getting through 13 calls/match reliably, or silently failing over to rules. This determines whether steps below are worth doing before or after a rate-limit fix.
2. Decide: keep `openrouter/free`, or pin a specific inexpensive paid/free model ID for more predictable behavior (the free router picks among underlying models automatically, which is convenient but less consistent for JSON-structured output than a fixed model).
3. In `job_ai_prediction_queue` → `run_ai_prediction_with_fallback`, change `attach_brain=False` to `attach_brain=True` so `ai_brain.oversee_prediction`'s memory-aware review actually runs on every automatic prediction — this alone is the smallest change that turns on the "final AI with all our learned information" behavior you asked about.
4. Decide which of pipeline #1 vs #2 is canonical (recommend #1 — richer specialist set, already has weight-learning scaffolding) and stop the other from double-running — either remove the `use_llm_pipeline=True` branch's call into `run_llm_pipeline` inside `predict_and_record_enriched`, or repoint it to call pipeline #1's own logic instead of a second independent specialist round.
5. Wire `grade_specialist_contributions` into the grading path (same place `market_intent.py`/`_helpers.py` grade the deterministic picks) so the specialist weights the decider trusts are actually earned from results, not permanently neutral.
6. Reduce call count if rate limits are tight: 7 calls/match (6 specialists + 1 decider) is defensible; adding the brain review makes 8. Batch size 10 every 5 min = up to 80 calls/cycle — check that's within whatever OpenRouter confirms as your actual limit before relying on this at that volume.
7. Once wired, run one manual cycle against a small batch and read the recorded `prediction_history` row plus `ai_brain` field back out, to confirm the reviewed/adjusted confidence is actually what lands in the row the dashboard reads — don't trust it from code reading alone.
8. Same discipline as `OVERHAUL_PLAN.md`: land this behind the existing `ai_prediction_queue` pipeline toggle (already there), watch a sample of live output before trusting it fully.

## Open questions for you

1. Do you want pipeline #1 (`ai_prediction_pipeline.py`) or #2 (`llm_pipeline.py`) as the one that survives, or should I fold the best parts of each into one?
2. Should the AI/LLM path and the deterministic statistical path (the one `OVERHAUL_PLAN.md` is fixing) stay as two competing systems that race for each match, or should they be merged so the AI final-review step sees the deterministic ensemble's `clear_winner` output too, rather than only the LLM specialists' own model-consensus reading of it?
3. Want me to proceed with implementing steps 3–5 above (the smallest, highest-value changes: turn on brain review, stop double-pipeline execution, wire the dead grading loop) once you confirm the OpenRouter rate-limit question?
