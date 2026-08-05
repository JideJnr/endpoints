# predictx/app/signal_aggregator.py  (SHIM — deprecated, to be removed in v2)
# noqa: F401  # DEPRECATED shim — see migration_checklist.md
from app.enrichment.signal_aggregator import *  # noqa: F401
from app.enrichment.signal_aggregator import (
    SIGNAL_CATEGORIES,
    normalize_signal,
    SignalAggregator,
    calculate_win_probabilities,
    score_pick_direction,
    get_signal_stats_cache,
    reset_signal_stats_cache,
    prefetch_signal_stats,
    global_signal_stats,
    signal_value,
    signal_metric,
)  # noqa: F401
