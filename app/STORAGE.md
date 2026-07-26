# Storage

The system uses two storage layers: local SQLite for the operational database, and MongoDB for archiving finished matches and syncing to the cloud.

---

## Local SQLite (`league_memory.py`)

**File:** `data/predictx_memory.sqlite3` (configurable via `PREDICTX_DB_PATH`)

This is the primary operational database. Everything that needs to persist between restarts lives here. Schema is managed via `_init_db()` which creates tables and runs migrations on startup.

### Key Tables

| Table | Purpose |
|-------|---------|
| `match_buffer` | Today's and tomorrow's matches (hot queue) |
| `future_match_buffer` | Matches 2+ days ahead |
| `prediction_history` | Every prediction ever made — append-only |
| `elo_ratings` | Team ELO ratings (team_id, team_name, rating) |
| `elo_match_results` | Records which matches have had ELO updated (idempotency) |
| `confidence_calibration` | Actual win rate per pick type + confidence band |
| `clv_entries` | Closing-line value per prediction |
| `odds_snapshots` | Time-series 1x2 odds per match (1.4M+ rows) |
| `odds_market_snapshots` | Multi-market odds snapshots |
| `odds_snapshot_state` | Tracks last snapshot time per match |
| `late_goal_snapshots` | Live match state snapshots for late-goal learning |
| `snapshot_aggregates` | Aggregated late-goal rates by league + minute bucket |
| `signal_weights` | Per-signal accuracy weights (updated by self_learner) |
| `signal_outcomes` | Signal presence vs outcome tracking |
| `signal_pick_weights` | Per-signal pick-type weights |
| `league_accuracy` | Win rate per league/tournament |
| `learned_model_weights` | Optimised ensemble weights per model |
| `team_history_cache` | Cached SofaScore team history (avoids repeat API calls) |
| `scheduler_intervals` | Overridden scheduler intervals |
| `engine_state` | Pipeline enabled/disabled states |
| `job_runs` | Job execution history and guard state |
| `correction_authority_leases` | Exclusive locks for correction loops |
| `system_activity` | Human-readable activity log |
| `system_supervisor_snapshots` | Supervisor audit snapshots |
| `prediction_monitor_snapshots` | Monitor report history |
| `enriched_matches` | SofaScore-first match archive |
| `finished_matches` | Finished match archive with raw scores |
| `betbuilder_history` | Saved bet slips |
| `betbuilder_leg_history` | Individual leg grading |
| `sofascore_team_grades` | SofaScore expert grade data |
| `prediction_candidate_history` | Candidate pool tracking for deduplication |
| `prediction_decision_log` | Deferred decision audit trail |
| `match_snapshots` | Point-in-time match state snapshots |
| `match_duplicates` | Detected duplicate match entries |
| `competition_special_buffer` | World Cup 2026 dedicated match buffer |
| `competition_special_settings` | World Cup pipeline settings |

### Important Design Decisions

- **`prediction_history` is append-only.** Never update or delete prediction rows — grading happens by setting `graded_at`, `result`, `final_home`, `final_away`. Historical integrity is required for learning.
- **`odds_snapshots` grows fast.** Pruned automatically — rows older than 30 days are deleted by the learning cycle.
- **WAL mode enabled.** SQLite is configured with `journal_mode=WAL` and `synchronous=NORMAL` for concurrent read performance.

---

## MongoDB (`mongo_store.py`)

Optional archive layer. Used to:
- Store finished matches long-term (local SQLite only keeps recent data)
- Sync the enriched buffer to cloud when running locally
- Enable cross-device data access

**Env vars:**
- `MONGODB_URI` — connection string (leave empty to disable)
- `MONGODB_DB` — database name (default `predictx`)

**Key functions:**
- `flush_buffer_to_mongo()` — copy finished matches from local buffer to MongoDB
- `store_scheduled_matches(events, match_date)` — archive SofaScore events
- `cleanup_buffer()` — remove finished/stale rows from the local buffer
- `is_configured()` — returns `True` when MongoDB URI is set and local-only mode is off

**Collections:**
- `finished_matches` — graded matches with scores and predictions
- `enriched_matches` — enriched buffer snapshots
- `odds_snapshots` — odds history archive

When MongoDB is not configured, the system runs fully on local SQLite — no functionality is lost, only long-term archiving is skipped.
