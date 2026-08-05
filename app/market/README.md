# market

## Purpose

This domain models the betting market environment around each fixture. It classifies market types, detects odds movement patterns, identifies market regimes (sharp vs. recreational money), and determines season stage — all signals that inform enrichment and risk decisions downstream.

## Member Modules

| Module | Responsibility |
|---|---|
| `market.py` | Core market classification logic — maps fixtures to market categories and available bet types |
| `market_intent.py` | Infers bettor intent from market liquidity and odds shape |
| `odds_pattern.py` | Detects significant odds movement patterns (steam moves, reverse line movement) |
| `regime.py` | Classifies current market regime (sharp, public, mixed) based on line movement signals |
| `season_stage.py` | Determines the stage of the season (early, mid, run-in) to adjust model weightings |

## Dependency Direction

**Depends on:** `config`, `storage`

**Depended on by:** `enrichment`, `risk`, `ai`, `scheduling`

## Notes

Market data is time-sensitive; modules here should be designed for low-latency access. Odds pattern detection in `odds_pattern.py` operates over historical snapshots stored in `storage` and should not block the live enrichment path.
