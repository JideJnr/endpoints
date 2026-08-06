"""
Models domain package.

Statistical and ML scoring models — Poisson, Dixon-Coles, ELO, ensemble scoring,
odds-only predictor, SportyBet-only predictor, probability learner, weight optimiser.

Note: Eager re-exports are intentionally omitted here to prevent circular imports.
dixon_coles imports from app.models.poisson (shim), which would create a circular loading
cycle if this __init__.py eagerly imported both. Import sub-modules directly:

    from app.models.poisson import run_poisson, MAX_GOALS, HOME_ADVANTAGE
    from app.models.dixon_coles import run_dixon_coles
    from app.models.elo import get_elo, update_elo, elo_prediction
    from app.models.ensemble import ensemble_prediction, WEIGHTS
    from app.models.odds_predictor import odds_only_prediction
    from app.models.sporty_only_predictor import predict_from_sporty, extract_sporty_signals
    from app.models.probability_learner import ProbabilityLearner, get_learned_probabilities
    from app.models.weight_optimiser import optimise_ensemble_weights, get_current_weights

Public symbols by module:

poisson.py
    run_poisson, MAX_GOALS, HOME_ADVANTAGE
    _team_stats, _local_team_matches, _poisson_prob, _to_int

dixon_coles.py
    run_dixon_coles, _tau, MAX_GOALS, HOME_ADVANTAGE, RHO

elo.py
    get_elo, update_elo, record_match_result_once, elo_prediction
    K_FACTOR, HOME_ADVANTAGE_ELO, _init_elo_table

ensemble.py
    ensemble_prediction, compute_ensemble_diversity
    WEIGHTS, _get_weights, _BASE_WEIGHTS

odds_predictor.py
    odds_only_prediction, _extract_1x2, _tournament_name

sporty_only_predictor.py
    predict_from_sporty, extract_sporty_signals
    _find_market, _outcome_prob, _outcome_odds

probability_learner.py
    ProbabilityLearner, learn_probabilities, get_learned_probabilities
    _signal_pattern_key, _signal_profile

weight_optimiser.py
    optimise_ensemble_weights, get_current_weights
"""
