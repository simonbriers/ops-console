"""ops-console — FastAPI entrypoint.

Serves the dashboard's static frontend and the /health endpoint directly;
all client CRUD/status/token-fetch routes live in routes.py (mounted below)
to keep this file small. The Stage 1 skeleton (bare /health + empty
/api/clients, no real logic) proved the venv/Docker foundation boots and
serves before this real logic was layered on top of it.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.routes import router as api_router

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="ops-console")
app.include_router(api_router)


@app.on_event("startup")
def _start_ledger_collector() -> None:
    # Phase 2 (docs/TOKEN_ECONOMY_PLAN.md): background usage-snapshot
    # collector — metering must not depend on a dashboard tab being open.
    # Defensive import/start: a ledger problem must never stop the console
    # from booting.
    try:
        from backend import ledger
        ledger.start_collector()
    except Exception:
        pass


@app.get("/health")
def health() -> dict:
    # ops-console's own liveness check (used by its Dockerfile
    # HEALTHCHECK) — distinct from the /health of each MONITORED client,
    # which core.check_health() calls separately.
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
