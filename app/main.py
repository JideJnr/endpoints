from app.logging_config import configure_logging
configure_logging()

from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from app.config.config import get_settings, public_settings
from app.storage.db import DB_PATH, close_db
from app.storage.league_memory import _init_db
from app.routers import agent, frontend, mobile_bridge, mongo, platform, sporty, sofascore, user_behavior, betbuilder, public, auth as auth_router
from app.routers import sofa_pipeline as sofa_pipeline_router
from app.routers import pipelines as pipelines_router
from app.routers import scheduler as scheduler_router
from app.routers import diagnostics as diagnostics_router
from app.routers import composite as composite_router
from app.scheduling.scheduler import start_scheduler

import logging

logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db()
    try:
        from app.scheduling.pipeline_registry import ensure_default_states
        initialised = ensure_default_states()
        if initialised:
            logger.info("[startup] pipeline defaults set: %s", initialised)
    except Exception as exc:
        logger.warning("[startup] pipeline default init failed: %s", exc)
    try:
        from app.storage.mongo_store import cleanup_buffer
        result = cleanup_buffer()
        if result.get("deleted_finished") or result.get("deleted_stale_unenriched"):
            logger.info("[startup] buffer cleanup: removed %s finished, %s stale", result.get('deleted_finished'), result.get('deleted_stale_unenriched'))
    except Exception as exc:
        logger.warning("[startup] buffer cleanup failed: %s", exc)
    try:
        from app.scheduling.job_state import recover_abandoned_jobs
        recovery = recover_abandoned_jobs(stale_after_seconds=180)
        if recovery.get("recovered"):
            logger.info("[startup] recovered abandoned jobs: %s", recovery.get('jobs'))
    except Exception as exc:
        logger.warning("[startup] job recovery failed: %s", exc)
    try:
        if settings.environment != "test":
            start_scheduler()
        logger.info(
            "[startup] prediction thresholds: "
            "calibration_samples=%s, clv_samples=%s, volatility_hard_block=%s, "
            "bootstrap_confidence_ceiling=%s, clear_winner_gap=%s",
            settings.validation_gate_min_calibration_samples,
            settings.validation_gate_min_clv_samples,
            settings.risk_manager_volatility_hard_block_threshold,
            settings.risk_manager_bootstrap_confidence_ceiling,
            settings.clear_winner_probability_gap,
        )
        yield
    finally:
        await _close_live_websockets()
        from app.scheduling.scheduler import stop_scheduler
        stop_scheduler(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _release_db_connection(request: Request, call_next):
    """Return the thread-local SQLite connection to the pool after each request."""
    try:
        return await call_next(request)
    finally:
        close_db()

app.include_router(sporty.router)
app.include_router(sofascore.router)
app.include_router(agent.router)
app.include_router(frontend.router)
app.include_router(platform.router)
app.include_router(mongo.router)
app.include_router(mobile_bridge.router)
app.include_router(sofa_pipeline_router.router)
app.include_router(pipelines_router.router)
app.include_router(scheduler_router.router)
app.include_router(diagnostics_router.router)
app.include_router(composite_router.router)
app.include_router(user_behavior.router)
app.include_router(betbuilder.router)
app.include_router(public.router)
app.include_router(auth_router.router)


connected_clients: list[WebSocket] = []


async def _close_live_websockets() -> None:
    for websocket in list(connected_clients):
        try:
            await websocket.close(code=1001)
        except Exception as exc:
            logger.debug("_close_live_websockets: error closing a websocket: %s", exc)
        finally:
            if websocket in connected_clients:
                connected_clients.remove(websocket)


@app.get("/health")
@app.head("/health")
def health():
    """Health check — supports both GET and HEAD for uptime monitors."""
    return {"status": "ok"}


@app.get("/readiness")
def readiness():
    checks = {
        "database_parent_exists": DB_PATH.parent.exists(),
        "database_parent_writable": _is_writable(DB_PATH.parent),
        "ai_configured": settings.ai_provider in {"rules", "none"} or settings.hf_token_present or settings.ai_provider in {"auto", "openrouter"},
        "web_search_enabled": settings.web_search_enabled,
    }
    try:
        _init_db()
        checks["database_init"] = True
    except Exception as exc:
        logger.error("readiness: database init check failed: %s", exc)
        checks["database_init"] = False
        checks["database_error"] = str(exc)
    checks["mongodb_configured"] = bool(settings.mongodb_uri)
    ready = all(value is not False for value in checks.values())
    return {"status": "ready" if ready else "degraded", "checks": checks, "settings": public_settings()}


@app.get("/config")
def config():
    return {"status": "success", "settings": public_settings()}


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        while True:
            from app.storage.buffer import get_live_buffered_matches
            from app.utils.match_view import match_summary

            matches = get_live_buffered_matches(limit=50)
            await websocket.send_json(
                {
                    "type": "live_update",
                    "count": len(matches),
                    "matches": [match_summary(match) for match in matches],
                }
            )
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        if websocket in connected_clients:
            connected_clients.remove(websocket)
    except asyncio.CancelledError:
        if websocket in connected_clients:
            connected_clients.remove(websocket)
        raise
    except Exception as exc:
        logger.warning("websocket_live: unexpected error, dropping client: %s", exc)
        if websocket in connected_clients:
            connected_clients.remove(websocket)


@app.get("/contract", response_class=PlainTextResponse)
def contract():
    return Path("API_CONTRACT.md").read_text()


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".predictx_write_probe"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
        return True
    except Exception as exc:
        logger.debug("readiness: %s is not writable: %s", path, exc)
        return False
