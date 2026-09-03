"""
main.py — FastAPI entry point for BNGC Python backend.

Clean, spaced, well-commented — mirrors the old Node app/dev.js
but in Python. Each route is clearly separated, with good logging.

Run locally:
    pip install -r requirements.txt
    uvicorn server.main:app --host 127.0.0.1 --port 3000 --reload

Or:
    python -m server.main  (if run as module)
"""

import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Environment — load from multiple possible .env locations
# ---------------------------------------------------------------------------

# Try server/.env, then app/.env, then root .env
for env_path in [
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / "app" / ".env",
    Path(__file__).parent.parent / ".env",
]:
    if env_path.exists():
        load_dotenv(env_path, override=False)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="BNGC Python API",
    description="Photon capture → Supabase, clean Python port of the Node backend",
    version="1.0.0",
)

# Allow all origins for local dev (userscript uses GM_xmlhttpRequest, so CORS is not strictly needed,
# but we keep it permissive for fetch fallback)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import loggers after app creation (so they can use the same config)
from server.lib.logger import log_sys, log_err, log_ok, banner

# Import routers
from server.routes.capture import router as capture_router
from server.routes.validate_token import router as validate_router
from server.routes.session_state import router as session_state_router
from server.routes.stats import router as stats_router
from server.routes.discovery import router as discovery_router
from server.routes.players import router as players_router
from server.routes.health import router as health_router

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
@app.get("/status")
async def health():
    """Simple health check — no auth required."""

    return {
        "ok": True,
        "service": "photon-server (python)",
        "port": int(os.getenv("PORT", "3000")),
        "uptime": time.time(),
    }


# ---------------------------------------------------------------------------
# Include routers
# ---------------------------------------------------------------------------

# Capture is the main route — all packets go here now
app.include_router(capture_router)
app.include_router(validate_router)
app.include_router(session_state_router)
app.include_router(stats_router)
app.include_router(discovery_router)
app.include_router(players_router)
app.include_router(health_router)

# TODO: Port remaining routes if needed (player_sync, event, playtime are now via capture + engine)
# For now, GET /api/session/{id}/state is the poll endpoint for the photon-client SDK

# ---------------------------------------------------------------------------
# Request logging middleware (like logReq in Node)
# ---------------------------------------------------------------------------

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with timing and status, like the Node logger."""

    start = time.time()

    response = await call_next(request)

    duration = int((time.time() - start) * 1000)
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "-")

    # Use the same logger as capture for consistency
    from server.lib.logger import log_req

    # log_req is not in the Python logger yet — we emulate it
    status = response.status_code
    if status >= 500:
        log_err(f"{request.method} {request.url.path} → {status} {duration}ms ip={ip}")
    elif status >= 400:
        # Warn level (amber) — we reuse log_err for visibility
        log_err(f"{request.method} {request.url.path} → {status} {duration}ms ip={ip}")
    else:
        log_ok(f"{request.method} {request.url.path} → {status} {duration}ms ip={ip}")

    return response


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "3000"))
    banner(port)

    # Run with reload for local dev
    uvicorn.run(
        "server.main:app",
        host="127.0.0.1",
        port=port,
        reload=True,
        log_level="info",
    )
