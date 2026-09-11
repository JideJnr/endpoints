"""Central logging setup for PredictX.

Nothing else in this app configures logging. Every module just does
`logger = logging.getLogger(__name__)` and calls logger.warning(...) etc.,
but without a handler attached anywhere, Python's default behaviour is:
- only WARNING and above ever get printed (INFO/DEBUG calls go nowhere)
- output goes to stderr only -- nothing is ever written to a file
- if the console window closes (or the app runs as a background/service
  process with no attached console), that output is lost for good

configure_logging() must run once, before anything else logs, so every
existing logger.* call across the codebase actually gets captured -- both
to the console and to a rotating log file on disk.
"""
import logging
import logging.handlers
import os
from pathlib import Path

_CONFIGURED = False


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_dir = Path(os.environ.get("LOG_DIR", "data/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "predictx.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers if this ever runs twice in the same process
    # (e.g. uvicorn --reload's reloader importing the app module more than once).
    root.handlers = [file_handler, console_handler]

    logging.getLogger(__name__).info(
        "Logging configured: level=%s file=%s", level_name, log_dir / "predictx.log"
    )
