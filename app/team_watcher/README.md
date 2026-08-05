# team_watcher

## Purpose

This domain monitors team form and squad availability in near-real time, and generates LLM-powered narrative briefs summarising a team's current condition. It feeds enrichment and AI domains with up-to-date team intelligence beyond what raw statistical models capture.

## Member Modules

| Module | Responsibility |
|---|---|
| `team_watcher.py` | Public interface for querying the latest team state, injuries, and form summary |
| `team_watcher_engine.py` | Background engine that polls data sources, maintains form windows, and invokes the LLM to produce team briefs |

## Dependency Direction

**Depends on:** `config`, `storage`, `data_clients`, `utils`

**Depended on by:** `enrichment`, `ai`

## Notes

LLM calls within `team_watcher_engine.py` should be treated as best-effort enrichment — downstream callers must handle the case where a brief is unavailable or stale. Brief generation should be cached in `storage` with a TTL to avoid redundant LLM requests.
