from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from app.config import get_settings, public_settings
from app.league_memory import DB_PATH, _init_db
from app.routers import agent, frontend, mongo, platform, sporty, sofascore
from app.scheduler import start_scheduler

settings = get_settings()

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sporty.router)
app.include_router(sofascore.router)
app.include_router(agent.router)
app.include_router(frontend.router)
app.include_router(platform.router)
app.include_router(mongo.router)


@app.on_event("startup")
def startup():
    if settings.environment != "test":
        start_scheduler()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/readiness")
def readiness():
    checks = {
        "database_parent_exists": DB_PATH.parent.exists(),
        "database_parent_writable": _is_writable(DB_PATH.parent),
        "ai_configured": settings.ai_provider in {"rules", "none"} or settings.hf_token_present or settings.ai_provider in {"auto", "ollama"},
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
