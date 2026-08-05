"""OpenAPI contract check.

Usage:
    # Capture current routes as the baseline (run once before refactor):
    python check_openapi.py --save-baseline

    # Verify current routes match the saved baseline (run after refactor):
    python check_openapi.py --check

    # Default (no flags): behaves like --check if a baseline exists,
    # otherwise saves baseline and exits 0.
"""
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Import the FastAPI app without starting the lifespan (no scheduler, no DB).
# We only need app.routes and app.openapi() which are available at import time.
# ---------------------------------------------------------------------------
# Ensure we can import from the predictx/app package regardless of cwd.
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

os.environ.setdefault("PREDICTX_ENV", "test")  # suppress scheduler start

from app.main import app  # noqa: E402

BASELINE_PATH = _HERE.parent / "docs" / "openapi_baseline.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_routes(fastapi_app) -> list[dict]:
    """Return a sorted list of {method, path} dicts from the FastAPI app."""
    routes = []
    for route in fastapi_app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            for method in sorted(route.methods or []):
                routes.append({"method": method, "path": route.path})
    routes.sort(key=lambda r: (r["path"], r["method"]))
    return routes


def _route_set(routes: list[dict]) -> set[tuple]:
    return {(r["method"], r["path"]) for r in routes}


def _save_baseline(routes: list[dict], openapi_schema: dict) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"routes": routes, "openapi": openapi_schema}
    BASELINE_PATH.write_text(json.dumps(payload, indent=2))
    print(f"[check_openapi] Baseline saved → {BASELINE_PATH}")
    print(f"[check_openapi] Routes captured: {len(routes)}")
    for r in routes:
        print(f"  {r['method']:7} {r['path']}")


def _load_baseline() -> list[dict]:
    if not BASELINE_PATH.exists():
        print(f"[check_openapi] ERROR: No baseline found at {BASELINE_PATH}")
        print("[check_openapi] Run with --save-baseline first.")
        sys.exit(1)
    data = json.loads(BASELINE_PATH.read_text())
    return data["routes"]


def _check(current_routes: list[dict]) -> int:
    """Diff current routes against saved baseline. Returns exit code (0=pass)."""
    baseline_routes = _load_baseline()
    baseline_set = _route_set(baseline_routes)
    current_set = _route_set(current_routes)

    missing = baseline_set - current_set   # in baseline but NOT in current
    added   = current_set - baseline_set   # in current but NOT in baseline

    passed = not missing and not added

    print(f"[check_openapi] Baseline routes : {len(baseline_set)}")
    print(f"[check_openapi] Current routes  : {len(current_set)}")

    if missing:
        print(f"\n[check_openapi] FAIL — {len(missing)} route(s) MISSING from current app:")
        for method, path in sorted(missing):
            print(f"  - {method:7} {path}")

    if added:
        print(f"\n[check_openapi] FAIL — {len(added)} route(s) ADDED to current app (not in baseline):")
        for method, path in sorted(added):
            print(f"  + {method:7} {path}")

    if passed:
        print(f"\n[check_openapi] PASS — all {len(current_set)} routes match baseline. No regressions.")
        return 0
    else:
        return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    args = sys.argv[1:]
    current_routes = _collect_routes(app)

    if "--save-baseline" in args:
        openapi_schema = app.openapi()
        _save_baseline(current_routes, openapi_schema)
        return 0

    if "--check" in args or BASELINE_PATH.exists():
        return _check(current_routes)

    # No baseline and no explicit flag — treat as first run: save baseline.
    print("[check_openapi] No baseline found and no flag given — saving baseline.")
    openapi_schema = app.openapi()
    _save_baseline(current_routes, openapi_schema)
    return 0


if __name__ == "__main__":
    sys.exit(main())
