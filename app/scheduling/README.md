# scheduling

## Purpose

This domain manages all background job execution. It wraps APScheduler with a job state machine, a pipeline registry that maps job names to execution functions, and a loop-authority watchdog that prevents duplicate scheduler instances from running concurrently. All periodic prediction, ingest, and maintenance jobs are registered and executed here.

## Member Modules

| Module | Responsibility |
|---|---|
| `scheduler.py` | APScheduler setup, job registration, start/stop lifecycle, and cron expression management |
| `job_state.py` | State machine tracking each job's lifecycle (pending → running → success / failed) |
| `loop_authority.py` | Distributed watchdog that ensures only one scheduler loop holds authority at any time |
| `pipeline_registry.py` | Registry mapping job identifiers to their pipeline callables and configuration |

## Dependency Direction

**Depends on:** `config`, `storage`, `data_clients`, `models`, `enrichment`, `risk`, `market`, `competition`, `ai`, `utils`, `monitoring`

**Depended on by:** `routers` (specifically `routers/scheduler.py`)

## Notes

`scheduling` sits near the top of the DAG and intentionally depends on most domains. Avoid adding business logic directly in `scheduler.py` — jobs should delegate immediately to the relevant domain pipeline. The `loop_authority` mechanism is critical in multi-process deployments; do not bypass it.
