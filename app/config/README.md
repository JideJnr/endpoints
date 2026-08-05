# config

## Purpose

This domain is the single source of truth for application configuration. It loads environment variables via Pydantic `Settings` and exposes typed configuration objects consumed by every other domain. Nothing in this domain depends on any other app domain.

## Member Modules

| Module | Responsibility |
|---|---|
| `config.py` | Pydantic `BaseSettings` class, env-var parsing, default values, and configuration object instantiation |

## Dependency Direction

**Depends on:** nothing — this is the bottom of the DAG

**Depended on by:** all other domains (`storage`, `data_clients`, `models`, `enrichment`, `market`, `risk`, `competition`, `team_watcher`, `ai`, `scheduling`, `utils`, `monitoring`, `constants`)

## Notes

Because `config` has no inbound dependencies from other app domains, it can be imported freely without risk of circular imports. All domains should access settings through this package rather than reading `os.environ` directly.
