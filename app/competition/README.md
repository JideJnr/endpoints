# competition

## Purpose

This domain provides competition-level and league-level intelligence consumed by enrichment, AI, and scheduling. It analyses competitive strength, tracks special competition rules (two-legged ties, away-goals), and maintains a registry of known competitions with their metadata.

## Member Modules

| Module | Responsibility |
|---|---|
| `competition_analyser.py` | Computes competition-level metrics (average goal rate, home advantage, draw tendency) |
| `competition_registry.py` | Central registry mapping competition IDs to metadata (name, tier, country, format) |
| `competition_special.py` | Encodes special competition rules that affect outcome probabilities (e.g., away-goals, group-stage tiebreakers) |
| `league_strength.py` | Relative league strength index used to scale player and team ratings across competitions |
| `sos.py` | Strength-of-Schedule calculation for teams across a season |

## Dependency Direction

**Depends on:** `config`, `storage`, `data_clients`, `utils`

**Depended on by:** `enrichment`, `ai`, `scheduling`

## Notes

The competition registry should be treated as a read-mostly data store populated at startup. Expensive recalculations (league strength, SOS) should be scheduled as background jobs rather than computed inline during live prediction requests.
