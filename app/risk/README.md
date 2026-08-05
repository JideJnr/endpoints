# risk

## Purpose

This domain implements the risk governance layer that gates, sizes, and validates every bet before it is submitted. It applies Kelly criterion stake sizing, Closed Line Value (CLV) tracking, pick-generation logic, and fallback handling when primary signals are unavailable. All stake decisions and validation checks flow through this domain.

## Member Modules

| Module | Responsibility |
|---|---|
| `risk_manager.py` | Central risk controller — applies stake limits, exposure caps, and approval gates |
| `risk_learner.py` | Adaptive learner that adjusts risk parameters based on recent P&L feedback |
| `kelly.py` | Kelly criterion implementation for optimal stake fraction calculation |
| `clv.py` | Closed Line Value calculation to measure edge realised vs. closing odds |
| `validation_gate.py` | Pre-submission validation rules (odds range, confidence threshold, market liquidity) |
| `fallback_logic.py` | Fallback strategies when primary model signals are missing or unreliable |
| `pick_generator.py` | Generates ranked bet picks from validated enriched predictions |
| `pick_roles.py` | Defines pick role taxonomy (main pick, backup, value pick) and selection logic |

## Dependency Direction

**Depends on:** `config`, `storage`, `market`, `models`

**Depended on by:** `enrichment` (risk_manager called from enriched_prediction), `ai`

## Notes

Risk parameters (max stake, Kelly fraction, CLV threshold) should be loaded from `config` and never hard-coded. The `risk_manager` is the only public entry point for external callers — other modules in this domain are internal implementation details.
