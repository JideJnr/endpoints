# enrichment

## Purpose

This domain orchestrates the enrichment pipeline that transforms raw model probabilities into fully contextualised, calibrated predictions. It aggregates signals from multiple sources, applies confidence calibration, pulls in contextual intelligence (web context, similar historical matches), and produces the `EnrichedPrediction` data structure consumed by downstream risk and AI domains.

## Member Modules

| Module | Responsibility |
|---|---|
| `enriched_prediction.py` | Data class / model defining the `EnrichedPrediction` structure and its serialisation |
| `enrichment.py` | Main enrichment orchestrator — coordinates signal aggregation and calibration steps |
| `match_enrichment.py` | Per-match enrichment pass: team form, head-to-head, venue, referee data |
| `match_intelligence.py` | Higher-order match intelligence derived from multiple enrichment signals |
| `signal_aggregator.py` | Combines heterogeneous signals (form, odds drift, market intent) into a single feature vector |
| `confidence_calibrator.py` | Applies Platt scaling / isotonic regression to output calibrated confidence scores |
| `contextual_intelligence.py` | Integrates external context (news, suspensions, weather) into prediction confidence |
| `web_context.py` | Fetches and parses web-sourced contextual data for a given fixture |
| `similar_matches.py` | Retrieves historically similar matches to provide analogy-based context |

## Dependency Direction

**Depends on:** `config`, `storage`, `data_clients`, `models`, `market`, `risk` (risk_manager called from enriched_prediction), `utils`

**Depended on by:** `risk`, `ai`, `scheduling`

## Notes

The circular-looking dependency between `enrichment` and `risk` (risk_manager is called from enriched_prediction) is resolved by ensuring only a thin interface from `risk` (`risk_manager`) is imported, never the full risk pipeline. Consider extracting this interface to break the cycle cleanly in a future refactor.
