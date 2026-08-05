"""
Risk domain package.

Risk governance — Kelly criterion, CLV (closing-line value), validation gate,
fallback logic, pick generation, pick role ranking, and the adaptive risk learner.

Modules:
    risk_manager     — Desk-style risk governance: confidence capping, stake limits, hard-block decisions
    risk_learner     — Adaptive learning of risk thresholds from graded prediction history
    kelly            — Kelly criterion stake sizing (full-Kelly and fractional-Kelly)
    clv              — Closing-line value tracking and analytics
    validation_gate  — Promotion gate: calibration, CLV quality, and drawdown checks
    fallback_logic   — Fallback pick generation when primary logic cannot produce a directional pick
    pick_generator   — Optimised betting pick generation from learned probability distributions
    pick_roles       — Role-based pick ranking (primary vs secondary) using learned win-rate history
"""

# ── risk_manager ──────────────────────────────────────────────────────────────
from app.risk.risk_manager import (  # noqa: F401
    apply_risk_controls,
    MAX_SINGLE_BET_STAKE_PER_100,
    MAX_DEGRADED_STAKE_PER_100,
    MAX_HIGH_RISK_STAKE_PER_100,
    LONGSHOT_ODDS,
    EXTREME_LONGSHOT_ODDS,
    LEARNED_RISK_MIN_SAMPLES,
)

# ── risk_learner ──────────────────────────────────────────────────────────────
from app.risk.risk_learner import (  # noqa: F401
    RiskOutcome,
    LearnedRiskControls,
    record_risk_outcome,
    get_learned_risk_controls,
    get_learned_risk_controls_for_pick,
    get_risk_control_summary,
    rebuild_risk_controls,
)

# ── kelly ─────────────────────────────────────────────────────────────────────
from app.risk.kelly import (  # noqa: F401
    kelly_fraction,
    kelly_for_prediction,
)

# ── clv ───────────────────────────────────────────────────────────────────────
from app.risk.clv import (  # noqa: F401
    CLV_MIN_SAMPLES,
    record_clv_entry,
    compute_clv_for_date,
    get_clv_summary,
    clv_stake_multiplier,
)

# ── validation_gate ───────────────────────────────────────────────────────────
from app.risk.validation_gate import (  # noqa: F401
    evaluate_promotion_gate,
    MIN_CALIBRATION_SAMPLES,
    MIN_CLV_SAMPLES,
    MAX_CALIBRATION_GAP_POINTS,
    MAX_RECENT_LOSS_RATE,
    MAX_RECENT_LOSS_STREAK,
)

# ── fallback_logic ────────────────────────────────────────────────────────────
from app.risk.fallback_logic import (  # noqa: F401
    FALLBACK_CONFIG,
    FallbackHandler,
    get_fallback_pick,
)

# ── pick_generator ────────────────────────────────────────────────────────────
from app.risk.pick_generator import (  # noqa: F401
    CONFIDENCE_THRESHOLDS,
    PickGenerator,
    generate_picks,
    generate_optimized_slip,
)

# ── pick_roles ────────────────────────────────────────────────────────────────
from app.risk.pick_roles import (  # noqa: F401
    learned_best_pick,
    load_role_memory_rows,
    backfill_role_learning,
    fast_role_memory,
    attach_fast_learned_decision,
)
