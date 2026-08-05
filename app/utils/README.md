# utils

## Purpose

This domain provides cross-cutting utility functions and lightweight data structures shared across all other domains. It is intentionally kept thin — modules here must not contain business logic and must only import from `config`, `storage`, or the Python standard library to avoid coupling.

## Member Modules

| Module | Responsibility |
|---|---|
| `normalise.py` | String and numeric normalisation helpers (team name canonicalisation, odds formatting) |
| `time_context.py` | Time-zone-aware datetime helpers and match kick-off context utilities |
| `activity_log.py` | Structured activity logging interface backed by `storage` |
| `health_counters.py` | Lightweight in-process counters for system health metrics, persisted via `storage` |
| `match_state.py` | Enum and helpers for representing the current state of a match (pre, live, finished) |
| `match_view.py` | Read-only view model for presenting match data to routers and templates |
| `prediction_flow.py` | Tracks a prediction's journey through the pipeline stages, stored in `storage` |
| `current_predictions.py` | In-memory index of active predictions for fast lookup during live updates |
| `live_retry_queue.py` | Queue for retrying failed live-prediction enrichment steps |
| `bot2.py` | Telegram/messaging bot integration helpers for prediction delivery |
| `mobile_bridge.py` | Bridge utilities for serialising prediction data for the mobile frontend |
| `portfolio.py` | Portfolio aggregation helpers — grouping picks by competition, stake, and expected value |
| `desk_analytics.py` | Desk-level analytics calculations (ROI, strike rate, CLV summary) |

## Dependency Direction

**Depends on:** `config`, `storage` (for `activity_log`, `health_counters`, `prediction_flow`)

**Depended on by:** all other domains — utility functions are widely imported across the codebase

## Notes

`utils` must never import from `enrichment`, `ai`, `risk`, `models`, `data_clients`, `scheduling`, `market`, `competition`, or `team_watcher`. Any module that needs those domains does not belong in `utils`. If a utility starts requiring domain logic, extract it into the appropriate domain instead.
