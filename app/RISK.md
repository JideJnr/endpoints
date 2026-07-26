# Risk & Governance

These modules sit between the prediction models and the final published pick. Their job is governance — not prediction. They cap confidence, block bad picks, and ensure every published prediction has earned its place.

---

## `risk_manager.py`

The last gate before a pick is published. `apply_risk_controls(doc, picks, ...)` either stamps picks for publication or converts them all to `"Avoid game"`.

**Hard-block triggers** (convert everything to no_bet):
- `learned_history_high_risk` — league/market historically poor
- `market_volatility_spike` — volatility ≥ configured threshold (default 30%)
- `readiness_not_ready` — match doc still missing required data

**Soft violations** (cap confidence but allow publication):
- `degraded_provider_assurance` — SportyBet-only data → cap 62
- `contextual_high_risk` alone + highest pick ≥ 75 → demote to cap 72 (not hard block)
- Volatility 18–29% → soft violation only (`market_volatility_requires_recheck`)
- `smart_bet` learned class overrides `contextual_high_risk` hard block entirely

**Publish path override:** When confidence ≥ 65 AND ≥ 2 ensemble models available AND no `learned_history_high_risk` AND no `readiness_not_ready` → allow publication even with medium-risk violations.

**Confidence floor:** Cap never reduces a surviving pick below 52.

**`block_reason_summary`** field tells you exactly which violation fired the block.

---

## `validation_gate.py`

Checks historical calibration and CLV before promoting a pick.

`evaluate_promotion_gate(doc, pick)` queries SQLite:
1. **Calibration check** — is there enough win/loss history to trust the confidence band?
2. **CLV check** — has this pick type historically had positive closing-line value?
3. **Drawdown check** — recent loss rate and streak

**Bootstrap mode** (fresh deployment): When calibration AND CLV samples are both zero, returns `allowed=True, status="bootstrap"`. Only genuine quality failures (drawdown breach, negative CLV) block in bootstrap mode. Confidence is capped at `RISK_MANAGER_BOOTSTRAP_CONFIDENCE_CEILING` (default 72) for bootstrap picks.

**Thresholds** (all configurable via env vars):
- `VALIDATION_GATE_MIN_CALIBRATION_SAMPLES` (default 30)
- `VALIDATION_GATE_MIN_CLV_SAMPLES` (default 25)

---

## `regime.py`

Assigns each league/tournament to a performance tier (1–4) based on historical win rate.

- Tier 1–2: full confidence allowed
- Tier 3: small penalty
- Tier 4: −5 confidence penalty, higher minimum threshold

Used by `enriched_prediction.py` during calibration to apply per-league performance context.

---

## `market_intent.py`

Classifies a pick's market type and direction.

`classify_market_intent(type, selection, pick)` → `{market, direction, family, intent, line, ...}`

Examples:
- `("match_result", "Home Win")` → `{market: "1x2", direction: "home"}`
- `("goals", "Over 2.5 goals")` → `{market: "total_goals", intent: "over", line: 2.5}`

Used by `risk_manager.py` to check model agreement (opposing_models count) and by grading to classify the market type for win/loss determination.

---

## `portfolio.py`

`filter_correlated(predictions)` — removes picks that are too correlated with each other before the dashboard publishes them. Prevents showing 5 "Home or Draw" picks on similar matches that would all win or lose together.

---

## `pick_roles.py`

Manages primary vs secondary pick role learning. Tracks historical win rate per pick role, per league, per odds range.

- `learned_best_pick(picks, role_rows)` → returns the pick with the highest learned role score
- `backfill_role_learning(prediction, picks, match_id, role_rows)` → attach role memory to picks

The frontend uses this to show a "blue highlight" on whichever pick the learning system prefers — which may be the secondary pick if it has a better contextual win rate.

---

## `prediction_audit.py`

Builds structured audit records for every prediction and deferral.

- `build_prediction_audit(prediction, doc)` → full audit including model inputs, gate results, signal scores
- `build_deferred_prediction_audit(doc, readiness)` → audit for deferred matches explaining which readiness condition failed

Stored as `audit_json` in `prediction_history` for post-hoc debugging.

---

## `kelly.py`

Kelly criterion stake sizing.

`kelly_fraction(confidence, odds)` → recommended fraction of bankroll to stake.

Used as one input to stake sizing alongside calibration-based stake multipliers and regime stake caps.
