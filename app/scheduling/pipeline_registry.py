"""
Pipeline Registry
-----------------
Single source of truth for all toggleable scheduler pipelines.

Each pipeline entry defines:
  - engine_id  : key in the `engine_state` SQLite table
  - label      : human-readable name for the UI
  - description: what it does
  - interval   : human-readable schedule (informational only)
  - source     : "SportyBet" | "SofaScore" | "Internal"
  - job_ids    : scheduler job IDs controlled by this pipeline
  - default    : "active" | "paused" on first boot

Always-on jobs (grading, flushing, monitoring) are NOT in this registry —
they run unconditionally and do not appear as toggleable pipelines.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SourceType = Literal["SportyBet", "SofaScore", "Internal"]
StatusType = Literal["active", "paused"]


@dataclass(frozen=True)
class PipelineDef:
    engine_id: str
    label: str
    description: str
    interval: str
    source: SourceType
    job_ids: tuple[str, ...]
    default: StatusType = "active"

    def to_dict(self) -> dict:
        return {
            "engine_id": self.engine_id,
            "label": self.label,
            "description": self.description,
            "interval": self.interval,
            "source": self.source,
            "job_ids": list(self.job_ids),
            "default": self.default,
            "always_on": False,
        }


# ── Pipeline definitions ───────────────────────────────────────────────────────

PIPELINES: list[PipelineDef] = [
    PipelineDef(
        engine_id="ai_prediction_queue",
        label="AI Prediction Queue",
        description="Prioritises enriched prematch buffer rows, then runs the evidence-first Ollama pipeline with rules-engine fallback.",
        interval="Every 5 min",
        source="Internal",
        job_ids=("ai_prediction_queue",),
        default="active",
    ),
    PipelineDef(
        engine_id="sportybet_ingest_live",
        label="Live Ingest (SportyBet)",
        description="Fetches live matches from SportyBet every 30 s and patches scores/periods. "
                    "Disable on cloud where SportyBet blocks datacenter IPs.",
        interval="Every 30 s",
        source="SportyBet",
        job_ids=("ingest_live",),
        default="active",
    ),
    PipelineDef(
        engine_id="sportybet_ingest_upcoming",
        label="Upcoming Ingest (SportyBet)",
        description="Fetches upcoming fixtures from SportyBet every 2 min. "
                    "Disable on cloud where SportyBet blocks datacenter IPs.",
        interval="Every 2 min",
        source="SportyBet",
        job_ids=("ingest_upcoming",),
        default="active",
    ),
    PipelineDef(
        engine_id="sportybet_enrich_prematch",
        label="Prematch Enrichment",
        description="Enriches upcoming (non-live) matches with SofaScore detail and web context. "
                    "When AI Prediction Queue is enabled, it owns prematch prediction order.",
        interval="Every 30 s",
        source="SofaScore",
        job_ids=("enrich_worker",),
        default="active",
    ),
    PipelineDef(
        engine_id="sportybet_enrich_live",
        label="Live Match Enrichment",
        description="Enriches live (in-play) matches with SofaScore live data and "
                    "updates predictions every 30 s during matches.",
        interval="Every 30 s",
        source="SofaScore",
        job_ids=("enrich_worker",),
        default="active",
    ),
    PipelineDef(
        engine_id="sofa_pipeline",
        label="SofaScore-Only Pipeline",
        description="Full Ingest → Enrich → Predict using SofaScore as the sole data source. "
                    "Safe on Render/cloud where SportyBet blocks datacenter IPs.",
        interval="Every 5 min",
        source="SofaScore",
        job_ids=("sofa_pipeline",),
        default="paused",
    ),
    PipelineDef(
        engine_id="live_priority_mode",
        label="Live Priority Lane",
        description="Continuous high-frequency live enrichment lane. "
                    "When enabled, live matches are processed first on every 60 s tick.",
        interval="Every 60 s",
        source="SofaScore",
        job_ids=("live_priority_toggle",),
        default="paused",
    ),
    PipelineDef(
        engine_id="competition_special",
        label="Competition Special",
        description="Dedicated enrichment and prediction lane for all enabled top-30 competitions. "
                    "Auto-pulls future fixtures and predicts them. Enable individual competitions in the Competition settings.",
        interval="Every 5 min",
        source="SofaScore",
        job_ids=("competition_special",),
        default="active",
    ),
    PipelineDef(
        engine_id="unified_upcoming",
        label="Unified Upcoming Pipeline",
        description="Fetches upcoming matches from both SportyBet and SofaScore, matches them together, "
                    "then runs predictions. Once matched, both data sources are merged per match.",
        interval="Every 5 min",
        source="SofaScore",
        job_ids=("unified_upcoming",),
        default="paused",
    ),
    PipelineDef(
        engine_id="unified_live",
        label="Unified Live Pipeline",
        description="Fetches live matches from both SportyBet and SofaScore, matches them together, "
                    "then updates predictions every 60 s during matches.",
        interval="Every 60 s",
        source="SofaScore",
        job_ids=("unified_live",),
        default="paused",
    ),
    PipelineDef(
        engine_id="competition_analysis",
        label="Competition Analysis",
        description="Detects newly completed matchdays for enabled competitions and generates "
                    "Ollama-powered post-matchday analysis with top table and weekly disappointments. "
                    "Runs automatically each match week.",
        interval="Every 24 hrs",
        source="Internal",
        job_ids=("competition_analysis",),
        default="active",
    ),
    PipelineDef(
        engine_id="sporty_only_upcoming",
        label="SportyBet-Only Upcoming Pipeline",
        description="Fetches all 300 upcoming matches from SportyBet and predicts directly from "
                    "odds, probabilities, and market signals — no SofaScore required. "
                    "Works anywhere SportyBet is accessible.",
        interval="Every 5 min",
        source="SportyBet",
        job_ids=("sporty_only_upcoming",),
        default="paused",
    ),
]

# Fast lookup by engine_id
PIPELINE_MAP: dict[str, PipelineDef] = {p.engine_id: p for p in PIPELINES}

# All valid toggle IDs
TOGGLEABLE_IDS: frozenset[str] = frozenset(PIPELINE_MAP.keys())

# ── Preset definitions ─────────────────────────────────────────────────────────

PRESETS: dict[str, dict[str, StatusType]] = {
    # Keep only the data lanes required to fill the prematch buffer plus the
    # priority AI decision lane. All live/special/duplicate pipelines are off.
    "ai_prematch": {
        p.engine_id: ("active" if p.engine_id in {
            "ai_prediction_queue", "sportybet_ingest_upcoming", "sportybet_enrich_prematch"
        } else "paused")
        for p in PIPELINES
    },
    # Cloud: disable SportyBet ingest, enable SofaScore pipeline
    "cloud": {
        "sportybet_ingest_live":     "paused",
        "sportybet_ingest_upcoming": "paused",
        "sportybet_enrich_prematch": "paused",
        "sportybet_enrich_live":     "paused",
        "sofa_pipeline":             "active",
        # leave live_priority_mode and competition_special unchanged
    },
    # Local: enable everything
    "local": {p.engine_id: "active" for p in PIPELINES},
    # Off: disable everything toggleable
    "off": {p.engine_id: "paused" for p in PIPELINES},
}


# ── Default initialisation ─────────────────────────────────────────────────────

def ensure_default_states() -> dict[str, str]:
    """
    On first boot, write default engine states for any pipeline that has no row
    yet.  Existing rows are NEVER overwritten.
    """
    from app.storage.league_memory import get_engine_states, set_engine_status

    existing = get_engine_states()
    initialised: dict[str, str] = {}
    for pipeline in PIPELINES:
        if pipeline.engine_id not in existing:
            set_engine_status(pipeline.engine_id, pipeline.default)
            initialised[pipeline.engine_id] = pipeline.default
    return initialised


# ── Runtime helpers ────────────────────────────────────────────────────────────

def is_pipeline_enabled(engine_id: str) -> bool:
    """Return True if the pipeline engine state is 'active'."""
    from app.storage.league_memory import get_engine_states
    states = get_engine_states()
    return states.get(engine_id, "paused") == "active"


def get_enrich_worker_mode() -> dict[str, bool]:
    """
    Derive live_only / exclude_live / disabled from the two enrichment toggles.

    Called by job_enrich_worker to decide its run mode.
    """
    live_on = is_pipeline_enabled("sportybet_enrich_live")
    prematch_on = is_pipeline_enabled("sportybet_enrich_prematch")
    return {
        "disabled": not live_on and not prematch_on,
        "live_only": live_on and not prematch_on,
        "exclude_live": not live_on and prematch_on,
        "both": live_on and prematch_on,
    }


def get_all_pipeline_states() -> list[dict]:
    """
    Return the full pipeline state list for the API response.
    Merges static pipeline definitions with live engine states and job_run data.
    """
    from app.storage.league_memory import get_engine_states
    from app.scheduling.job_state import list_job_states

    engine_states = get_engine_states()
    job_run_map = {row["job_id"]: row for row in list_job_states()}

    # Detect conflict: sofa_pipeline active AND any sportybet ingest active
    sofa_on = engine_states.get("sofa_pipeline") == "active"
    sportybet_ingest_on = (
        engine_states.get("sportybet_ingest_live") == "active"
        or engine_states.get("sportybet_ingest_upcoming") == "active"
    )
    has_ingest_conflict = sofa_on and sportybet_ingest_on

    result = []
    for pipeline in PIPELINES:
        enabled = engine_states.get(pipeline.engine_id, pipeline.default) == "active"

        # Aggregate job run data across all job_ids
        last_run_at = None
        last_error = None
        consecutive_failures = 0
        run_count = 0
        for job_id in pipeline.job_ids:
            row = job_run_map.get(job_id)
            if not row:
                continue
            run_count += row.get("run_count") or 0
            consecutive_failures += row.get("fail_count") or 0
            last_err = row.get("last_error")
            if last_err:
                last_error = last_err
            finished_at = row.get("finished_at") or row.get("heartbeat_at")
            if finished_at:
                if not last_run_at or finished_at > last_run_at:
                    last_run_at = finished_at

        entry = {
            **pipeline.to_dict(),
            "enabled": enabled,
            "last_run_at": last_run_at,
            "last_error": last_error,
            "consecutive_failures": consecutive_failures,
            "run_count": run_count,
        }

        if has_ingest_conflict and pipeline.engine_id in (
            "sofa_pipeline", "sportybet_ingest_live", "sportybet_ingest_upcoming"
        ):
            entry["conflict_warning"] = (
                "Both SofaScore pipeline and SportyBet ingest are active simultaneously. "
                "Consider enabling only one ingest source to avoid duplicate buffer entries."
            )

        result.append(entry)

    return result
