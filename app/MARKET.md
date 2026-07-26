# Market & Odds

These modules track how odds move over time, detect sharp money movement, and compute closing-line value. They don't produce picks — they produce signals that the prediction engine and risk manager use.

---

## `market.py`

Tracks 1x2 odds over time and detects meaningful movements.

**Key functions:**
- `snapshot_odds(doc)` — write a new odds snapshot to `odds_snapshots` if odds have changed by at least `PREDICTX_ODDS_TRACK_MIN_CHANGE` (default 0.01). Called after every SportyBet ingest.
- `get_movement(match_id)` → `{snapshots, sharp_signal, strongest_pull, market_snapshots}`

**Sharp signal detection:**
- Tracks the direction and size of odds movement
- `sharp_signal = "HOME" | "AWAY" | "DRAW" | null`
- Used by `enriched_prediction.py` as a `market_adjustment` on pick confidence
- A sharp signal agreeing with the prediction adds confidence; opposing it reduces it

**Env vars:**
- `PREDICTX_ODDS_TRACK_MODE` — `lean` (only 1x2) or `full` (all markets)
- `PREDICTX_ODDS_TRACK_MARKETS` — which markets to snapshot (default: `1x2,double_chance,total_goals,btts`)
- `PREDICTX_ODDS_TRACK_MIN_CHANGE` — minimum decimal odds change to trigger a snapshot (default 0.01)

---

## `odds_pattern.py`

Recognises shapes in odds movement and scores them as signals.

**Key functions:**
- `pattern_signal(match_id)` → `{pattern_type, confidence_adjustment, sharp_signal}`
- `grade_patterns_for_date(date)` — after results are known, update which patterns were predictive

**Patterns tracked:**
- `sustained_shortening` — odds consistently moving in one direction
- `late_steam` — sharp movement in the last hour before kickoff
- `reversal` — odds moved one way then sharply reversed
- `stable` — minimal movement throughout

The pattern signal feeds into `enriched_prediction.py` as a `pattern_adj` confidence modifier.
