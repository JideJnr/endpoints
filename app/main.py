from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from app.config.config import get_settings, public_settings
from app.storage.db import DB_PATH, close_db
from app.storage.league_memory import _init_db
from app.routers import agent, frontend, mobile_bridge, mongo, platform, sporty, sofascore, user_behavior, betbuilder
from app.routers import sofa_pipeline as sofa_pipeline_router
from app.routers import pipelines as pipelines_router
from app.routers import scheduler as scheduler_router
from app.routers import diagnostics as diagnostics_router
from app.routers import composite as composite_router
from app.routers import competition_analysis as competition_analysis_router
from app.scheduling.scheduler import start_scheduler
from app.ai.llm_model_manager import stop_keep_alive

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db()
    try:
        from app.scheduling.pipeline_registry import ensure_default_states
        initialised = ensure_default_states()
        if initialised:
            print(f"[startup] pipeline defaults set: {initialised}")
    except Exception as exc:
        print(f"[startup] pipeline default init failed: {exc}")
    try:
        from app.storage.mongo_store import cleanup_buffer
        result = cleanup_buffer()
        if result.get("deleted_finished") or result.get("deleted_stale_unenriched"):
            print(f"[startup] buffer cleanup: removed {result.get('deleted_finished')} finished, {result.get('deleted_stale_unenriched')} stale")
    except Exception as exc:
        print(f"[startup] buffer cleanup failed: {exc}")
    try:
        from app.scheduling.job_state import recover_abandoned_jobs
        recovery = recover_abandoned_jobs(stale_after_seconds=180)
        if recovery.get("recovered"):
            print(f"[startup] recovered abandoned jobs: {recovery.get('jobs')}")
    except Exception as exc:
        print(f"[startup] job recovery failed: {exc}")
    try:
        if settings.environment != "test":
            start_scheduler()
        print(
            "[startup] prediction thresholds: "
            f"calibration_samples={settings.validation_gate_min_calibration_samples}, "
            f"clv_samples={settings.validation_gate_min_clv_samples}, "
            f"volatility_hard_block={settings.risk_manager_volatility_hard_block_threshold}, "
            f"bootstrap_confidence_ceiling={settings.risk_manager_bootstrap_confidence_ceiling}, "
            f"clear_winner_gap={settings.clear_winner_probability_gap}"
        )
        yield
    finally:
        await _close_live_websockets()
        stop_keep_alive()
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
app.include_router(competition_analysis_router.router)
app.include_router(user_behavior.router)
app.include_router(betbuilder.router)


connected_clients: list[WebSocket] = []


async def _close_live_websockets() -> None:
    for websocket in list(connected_clients):
        try:
            await websocket.close(code=1001)
        except Exception:
            pass
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
    except Exception:
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
    except Exception:
        return False
