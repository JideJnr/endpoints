# Learning & Calibration

These modules make the engine smarter over time. They read graded prediction history, identify which signals and models are working, and update weights and thresholds accordingly.

---

## `league_memory.py`

The central SQLite database interface. Nearly every other module imports from here. It owns:

- Database initialisation and schema migrations (`_init_db()`)
- All SQLite table definitions (40+ tables)
- Prediction history read/write (`record_prediction`, `list_prediction_history`)
- Grading functions (`grade_predictions_for_date`, `grade_overdue_predictions`)
- Late-goal snapshot storage and aggregation
- Team history cache (`store_team_history`, `get_cached_team_history`)
- ELO ratings read/write
- CLV entries

**Database path:** `data/predictx_memory.sqlite3` (configurable via `PREDICTX_DB_PATH`)

Key helper used everywhere:
```python
from app.league_memory import DB_PATH, _init_db
```

---

## `confidence_calibrator.py`

Rebuilds the confidence calibration bands after each grading cycle.

- `rebuild_calibration()` — scans `prediction_history` for graded predictions, groups by pick type + confidence band (0-9, 10-19, ..., 90-99), computes actual win rate per band, writes to `confidence_calibration` table
- `calibrate_confidence(pick_type, raw_confidence)` → returns `{adjusted_confidence, win_rate, calibrated_probability, samples}`

Example: If the model outputs 70% confidence for match_result picks but historical win rate at that confidence is only 58%, the calibrator adjusts down accordingly.

---

## `self_learner.py`

Updates which signals have historically been helpful.

- `get_signal_weights(league)` → per-signal weights for a league, used by `enriched_prediction` to boost or suppress signals
- After grading: scans wins and losses, tallies which signals were present, updates `signal_weights` table
- Also updates `league_accuracy` rows per tournament

Current learned weights (as of last grading):
```
dixon_coles: 0.3336, elo: 0.2771, poisson: 0.1668, rules: 0.2224
```

---

## `weight_optimiser.py`

Optimises the ensemble model weights based on historical accuracy.

`optimise_ensemble_weights()` — reads graded predictions, determines which model's prediction aligned with the actual result, updates `learned_model_weights` table.

The ensemble reads these weights on each prediction cycle (cached, refreshes every 50 predictions).

---

## `clv.py`

Closing-line value — measures whether our pick had positive value at prediction time relative to the closing odds.

- `compute_clv_for_date(date)` — for each prediction on that date, finds the closing odds from `odds_snapshots` and computes CLV
- `clv_stake_multiplier(pick_type, confidence)` → preferred stake size when enough CLV data exists
- CLV data feeds into `validation_gate.py` to check if a pick type has historically had positive market edge

---

## `odds_pattern.py`

Detects and scores odds movement patterns (shape of how odds moved over time).

- `pattern_signal(match_id)` → returns `{pattern_type, confidence_adjustment, sharp_signal}`
- `grade_patterns_for_date(date)` → update pattern accuracy after results are known

Patterns tracked: sustained shortening, late steam, reversal, stable, etc.

---

## `similar_matches.py`

Finds historical matches that are structurally similar to a target match. Used by the AI bet builder to give DeepSeek contextual evidence.

**Similarity dimensions:**
- ELO proximity (weight 0.45) — same team strength balance
- Odds proximity (weight 0.40) — same market distribution (implied probabilities)
- League bonus (weight 0.15) — same competition

**Candidate pool:** Drawn from `prediction_history` (graded results with `sr:match:...` IDs that join with `odds_snapshots`). The old `matches` table (SportyBet IDs) is NOT used as it doesn't join with odds data.

**Quality gate:** Minimum similarity score 0.25 before including a match. Results include `similarity_breakdown` with individual dimension scores.

---

## `league_strength.py`

Scores each league by competition level (tier 1 England > tier 2 England > tier 1 Croatia, etc.).

`league_strength_edge(event, home_history, away_history)` → float edge based on the average league quality of each team's recent opponents.

This prevents a team with 5 wins against weak opposition from being over-rated vs a team with 3 wins against strong opposition.

---

## `sofascore_grades.py`

Reads SofaScore's own expert pick grades for matches (when available) as an additional signal.

`grade_signal_for_match(detail, ...)` → `{available, grade, impact}` — used as a small confidence adjustment in `enriched_prediction.py`.

---

## `sos.py`

Strength-of-schedule scoring.

`strength_of_schedule(home_team_id, away_team_id)` — returns `{home_sos, away_sos, soft_losses_home, quality_wins_home, ...}` to give the prediction agent context about whether a team's form came against easy or hard opponents.
