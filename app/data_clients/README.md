# data_clients

## Purpose

This domain encapsulates all communication with external sports-data APIs. It provides thin, cacheable client wrappers for SofaScore, SportyBet, and Sportradar, plus a dedicated ingest pipeline that orchestrates SofaScore data into the storage layer. Network concerns (retries, rate-limiting, response parsing) are confined to this domain.

## Member Modules

| Module | Responsibility |
|---|---|
| `sofascore_client.py` | HTTP client for the SofaScore API — fixtures, events, statistics |
| `sofascore_grades.py` | Converts raw SofaScore performance metrics into normalised grade scores |
| `sportybet_client.py` | HTTP client for the SportyBet API — odds, markets, coupons |
| `sportybet_booking.py` | Bet-slip booking logic against the SportyBet platform |
| `sportradar_client.py` | HTTP client for the Sportradar unified API |
| `sportradar_gismo_client.py` | Specialised client for Sportradar GISMO (squad/player data) endpoints |
| `sofa_pipeline.py` | End-to-end ingest pipeline: fetches SofaScore data, normalises it, and persists it via `storage` |

## Dependency Direction

**Depends on:** `config`, `storage`

**Depended on by:** `enrichment`, `models`, `competition`, `team_watcher`, `ai`, `scheduling`

## Notes

All external HTTP calls must be routed through the clients in this domain. Other domains must not import `httpx`, `requests`, or similar libraries to make their own external calls — they should request data through the `data_clients` public API instead.
