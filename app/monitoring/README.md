# monitoring

## Purpose

This domain handles system health observation, prediction auditing, and the self-learning feedback loop. It observes the outputs of the prediction pipeline after match settlement, measures accuracy, and feeds learnings back into risk and model calibration. It is an observer domain — it imports widely but no domain (except scheduling and routers) should import from it.

## Member Modules

| Module | Responsibility |
|---|---|
| `system_audit.py` | Periodic system-wide health audit — checks database connectivity, scheduler liveness, and API quotas |
| `system_supervisor.py` | Supervisor process that watches critical subsystems and triggers alerts on anomalies |
| `prediction_audit.py` | Post-settlement audit that compares predicted vs. actual outcomes and records accuracy metrics |
| `prediction_monitor.py` | Live prediction monitor that tracks pending predictions and flags stale or inconsistent states |
| `self_learner.py` | Feedback loop that ingests settled prediction results and updates model and risk parameters |

## Dependency Direction

**Depends on:** `config`, `storage`, `enrichment`, `ai`, `scheduling`

**Depended on by:** `scheduling` (watchdog integration), `routers`

## Notes

Monitoring is a top-of-DAG observer domain. No core domain (`enrichment`, `risk`, `models`, etc.) should import from `monitoring` — doing so would create a dependency inversion. Alert delivery (email, Telegram, webhook) should be configured in `config` and invoked only from this domain.
