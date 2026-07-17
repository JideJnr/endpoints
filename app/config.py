from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_name: str
    environment: str
    cors_origins: list[str]
    database_path: Path
    ai_provider: str
    hf_url: str
    hf_model: str
    hf_token_present: bool
    ollama_url: str
    ollama_model: str
    ai_timeout_seconds: int
    web_search_enabled: bool
    web_search_backends: list[str]
    web_search_max_results: int
    web_scrape_max_chars: int
    web_search_timeout_seconds: int
    web_scrape_timeout_seconds: int
    local_storage_only: bool
    disable_pruning: bool
    mongodb_uri: str
    mongodb_db: str
    odds_track_mode: str
    odds_track_markets: list[str]
    odds_track_max_market_rows: int
    odds_track_min_change: float
    over25_upgrade_goal_pressure: float
    over25_upgrade_requires_market_steam: bool
    validation_gate_min_calibration_samples: int
    validation_gate_min_clv_samples: int
    risk_manager_volatility_hard_block_threshold: float
    risk_manager_bootstrap_confidence_ceiling: int
    clear_winner_probability_gap: float


def get_settings() -> Settings:
    return Settings(
        app_name=os.getenv("PREDICTX_APP_NAME", "PredictX Football Stats Agent"),
        environment=os.getenv("PREDICTX_ENV", "development"),
        cors_origins=_csv(os.getenv("PREDICTX_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")),
        database_path=Path(os.getenv("PREDICTX_DB_PATH", str(BASE_DIR / "data" / "predictx_memory.sqlite3"))),
        ai_provider=os.getenv("PREDICTX_AI_PROVIDER", "auto").strip().lower(),
        hf_url=os.getenv("PREDICTX_HF_URL", "https://router.huggingface.co/v1/chat/completions"),
        hf_model=os.getenv("PREDICTX_HF_MODEL", "Qwen/Qwen2.5-7B-Instruct:fastest"),
        hf_token_present=bool(_hf_token()),
        ollama_url=os.getenv("PREDICTX_OLLAMA_URL", "http://localhost:11434/api/chat"),
        ollama_model=os.getenv("PREDICTX_AI_MODEL", "llama3.2:3b"),
        ai_timeout_seconds=_int_env("PREDICTX_AI_TIMEOUT_SECONDS", 15),
        web_search_enabled=_bool_env("PREDICTX_WEB_SEARCH_ENABLED", True),
        web_search_backends=_csv(os.getenv("PREDICTX_SEARCH_BACKENDS", "duckduckgo")),
        web_search_max_results=_int_env("PREDICTX_WEB_SEARCH_MAX_RESULTS", 3),
        web_scrape_max_chars=_int_env("PREDICTX_WEB_SCRAPE_MAX_CHARS", 1500),
        web_search_timeout_seconds=_int_env("PREDICTX_WEB_SEARCH_TIMEOUT_SECONDS", 4),
        web_scrape_timeout_seconds=_int_env("PREDICTX_WEB_SCRAPE_TIMEOUT_SECONDS", 6),
        local_storage_only=_bool_env("PREDICTX_LOCAL_STORAGE_ONLY", False),
        disable_pruning=_bool_env("PREDICTX_DISABLE_PRUNING", False),
        mongodb_uri=os.getenv("MONGODB_URI", ""),
        mongodb_db=os.getenv("MONGODB_DB", "predictx"),
        odds_track_mode=os.getenv("PREDICTX_ODDS_TRACK_MODE", "lean").strip().lower(),
        odds_track_markets=_csv(os.getenv("PREDICTX_ODDS_TRACK_MARKETS", "1x2,double_chance,total_goals,btts")),
        odds_track_max_market_rows=_int_env("PREDICTX_ODDS_TRACK_MAX_MARKET_ROWS", 60),
        odds_track_min_change=float(os.getenv("PREDICTX_ODDS_TRACK_MIN_CHANGE", "0.01")),
        over25_upgrade_goal_pressure=float(os.getenv("PREDICTX_OVER25_UPGRADE_GOAL_PRESSURE", "36")),
        over25_upgrade_requires_market_steam=_bool_env("PREDICTX_OVER25_UPGRADE_REQUIRES_MARKET_STEAM", False),
        validation_gate_min_calibration_samples=_int_env("VALIDATION_GATE_MIN_CALIBRATION_SAMPLES", 30),
        validation_gate_min_clv_samples=_int_env("VALIDATION_GATE_MIN_CLV_SAMPLES", 25),
        risk_manager_volatility_hard_block_threshold=float(os.getenv("RISK_MANAGER_VOLATILITY_HARD_BLOCK_THRESHOLD", "30")),
        risk_manager_bootstrap_confidence_ceiling=_int_env("RISK_MANAGER_BOOTSTRAP_CONFIDENCE_CEILING", 72),
        clear_winner_probability_gap=float(os.getenv("CLEAR_WINNER_PROBABILITY_GAP", "12")),
    )


def public_settings() -> dict[str, object]:
    settings = get_settings()
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "cors_origins": settings.cors_origins,
        "database_path": str(settings.database_path),
        "ai": {
            "provider": settings.ai_provider,
            "hf_model": settings.hf_model,
            "hf_token_present": settings.hf_token_present,
            "ollama_url": settings.ollama_url,
            "ollama_model": settings.ollama_model,
            "timeout_seconds": settings.ai_timeout_seconds,
        },
        "web_search": {
            "enabled": settings.web_search_enabled,
            "backends": settings.web_search_backends,
            "max_results": settings.web_search_max_results,
            "scrape_max_chars": settings.web_scrape_max_chars,
            "search_timeout_seconds": settings.web_search_timeout_seconds,
            "scrape_timeout_seconds": settings.web_scrape_timeout_seconds,
        },
        "storage": {
            "local_only": settings.local_storage_only,
            "pruning_disabled": settings.disable_pruning,
        },
        "mongodb": {
            "configured": bool(settings.mongodb_uri) and not settings.local_storage_only,
            "database": settings.mongodb_db,
        },
        "prediction_thresholds": {
            "validation_gate_min_calibration_samples": settings.validation_gate_min_calibration_samples,
            "validation_gate_min_clv_samples": settings.validation_gate_min_clv_samples,
            "risk_manager_volatility_hard_block_threshold": settings.risk_manager_volatility_hard_block_threshold,
            "risk_manager_bootstrap_confidence_ceiling": settings.risk_manager_bootstrap_confidence_ceiling,
            "clear_winner_probability_gap": settings.clear_winner_probability_gap,
        },
    }


def _hf_token() -> str | None:
    return (
        os.getenv("HF_TOKEN")
        or os.getenv("HUGGINGFACE_HUB_TOKEN")
        or os.getenv("HUGGING_FACE_HUB_TOKEN")
    )


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
