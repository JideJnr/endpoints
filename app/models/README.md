# models

## Purpose

This domain contains the statistical and machine-learning scoring engines used to generate raw match outcome probabilities. It includes classical models (Poisson, Dixon-Coles, Elo), an ensemble combiner, odds-based predictors, and the tooling needed to train, calibrate, and weight these models over time.

## Member Modules

| Module | Responsibility |
|---|---|
| `poisson.py` | Bivariate Poisson goal-scoring model; trained on historical SofaScore match data |
| `dixon_coles.py` | Dixon-Coles correction applied on top of Poisson for low-score matches |
| `elo.py` | Elo rating system for relative team strength estimation |
| `ensemble.py` | Weighted ensemble combiner that merges outputs from individual models |
| `odds_predictor.py` | Derives implied probabilities from bookmaker odds and applies margin removal |
| `sporty_only_predictor.py` | Lightweight predictor that operates solely on SportyBet market data |
| `probability_learner.py` | Online learner that adjusts probability estimates from recent match outcomes |
| `weight_optimiser.py` | Optimises ensemble model weights by minimising calibration loss on historical data |

## Dependency Direction

**Depends on:** `config`, `storage`, `data_clients` (specifically `sofascore_client` for Poisson training data)

**Depended on by:** `enrichment`, `risk`, `ai`

## Notes

Model training is intended to run offline or as a scheduled job; live prediction paths should consume pre-trained artefacts stored via `storage`. Avoid adding domain logic (risk gates, enrichment signals) inside this package — keep models stateless and focused on probability output.
