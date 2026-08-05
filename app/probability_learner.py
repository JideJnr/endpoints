# noqa: F401  # DEPRECATED shim — see migration_checklist.md
"""
Compatibility shim for app.probability_learner — redirects to app.models.probability_learner.
This file will be removed in v2.0. Update imports to: from app.models.probability_learner import ...
"""
from app.models.probability_learner import *  # noqa: F401, F403
from app.models.probability_learner import (  # noqa: F401
    ProbabilityLearner,
    learn_probabilities,
    get_learned_probabilities,
    _signal_pattern_key,
    _signal_profile,
)
