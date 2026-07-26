# Scheduler & Jobs

The scheduler drives the full prediction pipeline automatically in the background using APScheduler. No manual triggers are needed for normal operation.

---

## Job Schedule (defaults)

| Job | Interval | What it does |
|-----|----------|-------------|
| `unified_live` | 60 sec | Fetch live SportyBet → patch scores → enrich + predict live matches |
| `unified_upcoming` | 300 sec (5 min) | Fetch upcoming SportyBet → match to SofaScore → enrich + predict |
| `ingest_live` | 180 sec | Fast score patches only (no enrichment) |
| `ingest_upcoming` | 120 sec | Fast ingestion only (no enrichment) |
| `enrich_worker` | 30 sec | Background enrichment for unenriched/stale matches |
| `enrich_future` | 1800 sec (30 min) | Enrich fixtures beyond tomorrow |
| `sofa_pipeline` | 300 sec | SofaScore-only cycle (cloud-safe, when enabled) |
| `competition_special` | 300 sec | World Cup 2026 dedicated lane (when enabled) |
| `live_priority_toggle` | 60 sec | Run extra live enrichment pass when enabled |
| `flush_to_mongo` | 120 sec | Archive finished matches to MongoDB |
| `system_supervisor` | 180 sec | Audit pipeline health + safe auto-corrections |
| `prediction_monitor` | 3600 sec (1 hr) | Grade results, detect mismatches, update learning |
| `autopilot_guardian` | 300 sec | Self-heal: catch up if any job stalled |
| `archive_finished` | 900 sec (15 min) | Archive SofaScore finished events |
| `grade_overdue_predictions` | 1800 sec (30 min) | Grade predictions 2h after kickoff |
| `grade_predictions` | 21600 sec (6 hr) | Full analytics + ELO + learning update |
| `keep_alive` | 600 sec (10 min) | Ping `/health` to prevent Render cold start |
| `prune_mongo` | 7 days | Remove MongoDB records older than 90 days |

Intervals are **customisable** via the Settings → Scheduler UI or `PATCH /scheduler/intervals`. Changes persist in SQLite.

---

## `scheduler.py`

Contains all job function implementations and the APScheduler setup.

**Key job functions:**
- `job_unified_live()` — SportyBet live fetch + score patch + live enrichment (6 matches per cycle)
- `job_unified_upcoming()` — SportyBet upcoming + SofaScore match + full enrichment (12 matches per cycle, batch size configurable via `UNIFIED_UPCOMING_BATCH_SIZE`)
- `job_enrich_worker()` — background enrichment with live-first priority
- `job_grade_predictions()` — grading + ELO + calibration + weight optimisation
- `job_system_supervisor()` — calls `system_supervisor.run_system_supervisor(auto_correct=True)`

**Backpressure protection:** All jobs have `max_instances=1` and `coalesce=True` — if a job takes longer than its interval, the next run is skipped (not queued). `misfire_grace_time` is set generously (60–240s) so a late-starting job isn't dropped.

---

## `job_state.py`

Persistent job run tracking in SQLite (`job_runs` table).

- `run_job_with_guard(fn, guard_job_id, ...)` — runs a job only if no other instance is holding the guard
- Stores: `started_at`, `heartbeat_at`, `finished_at`, `last_result_json`, `run_count`, `fail_count`
- `recover_abandoned_jobs(stale_after_seconds=180)` — releases guards for jobs that crashed without releasing them (called on startup)

---

## `live_retry_queue.py`

A small in-memory queue for live matches that failed enrichment and need a retry after a cooldown.

- `add_to_retry_queue(match_id, reason)` — mark a match for retry
- `active_pending_count()` → how many are waiting
- `expire_stale_entries()` → remove entries older than the retry window

---

## `loop_authority.py`

Prevents competing correction loops from fighting each other.

- `acquire_authority(scope, source, expires_in_seconds)` → get an exclusive lease for a correction scope (`"operational"` or `"learning"`)
- Only one process can hold a lease for each scope at a time
- The `system_supervisor` holds `"operational"` authority; `prediction_monitor` holds `"learning"` authority

---

## `health_counters.py`

Lightweight in-memory counters for operational health events.

`record_health_event(module, event_type, exc)` — called from except blocks throughout the codebase to count how often specific errors occur, without writing to SQLite.

---

## `activity_log.py`

Human-readable activity log visible in Settings → System Activity.

`record_activity(message, job, status, match_id, match_name, details)` — writes to `system_activity` SQLite table. The frontend polls this every 6 seconds.

---

## `pipeline_registry.py`

Toggle individual pipeline jobs on/off via the Settings UI.

`is_pipeline_enabled(pipeline_id)` — each job checks this at startup and returns early if disabled.

Pipeline IDs:
- `unified_live`, `unified_upcoming` — primary live and upcoming pipelines
- `sportybet_ingest_live`, `sportybet_ingest_upcoming` — legacy ingest jobs
- `sportybet_enrich_live`, `sportybet_enrich_prematch` — legacy enrichment jobs
- `sofa_pipeline` — SofaScore-only cloud mode
- `live_priority_mode` — extra live enrichment pass toggle
- `competition_special` — World Cup 2026 dedicated lane

---

## `sofa_pipeline.py`

SofaScore-only ingest → enrich → predict cycle. Safe to run on cloud deployments where SportyBet blocks datacenter IPs.

`run_sofa_pipeline_cycle(enrich_batch, include_live)` — fetches today's SofaScore events, enriches a batch, runs predictions. Does not depend on SportyBet at all.

---

## `competition_special.py`

Dedicated prediction lane for high-priority competitions (currently World Cup 2026). Has its own buffer table, settings, and enrichment loop independent of the main pipeline.
