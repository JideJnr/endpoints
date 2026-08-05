# storage

## Purpose

This domain owns the entire persistence layer. It provides a SQLite interface for relational data, a MongoDB interface for document storage, an in-memory league knowledge store, and a match-event buffer for high-frequency write operations. All database connection lifecycle and schema management live here.

## Member Modules

| Module | Responsibility |
|---|---|
| `db.py` | SQLite connection management, table creation, and generic query helpers |
| `mongo_store.py` | MongoDB client wrapper, collection access, upsert/query helpers |
| `league_memory.py` | In-process cache of league-level aggregated statistics used for rapid enrichment lookups |
| `buffer.py` | Short-lived match-event buffer that batches writes before flushing to persistent storage |

## Dependency Direction

**Depends on:** `config`

**Depended on by:** `data_clients`, `models`, `enrichment`, `risk`, `market`, `competition`, `team_watcher`, `ai`, `scheduling`, `utils`, `monitoring`

## Notes

Modules in this domain should never import from higher-level domains such as `enrichment` or `ai` — doing so would create a cycle. Any schema migrations should be coordinated through `db.py`; ad-hoc DDL in other domains is prohibited.
