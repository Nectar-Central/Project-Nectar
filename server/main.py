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
from starlette.responses import Response
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
from server.routes.insights import router as insights_router
from server.routes.friends import router as friends_router
from server.routes.chat import router as chat_router
from server.routes.players import router as players_router
from server.routes.health import router as health_router

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    """Landing page — keeps root probes (Render, browsers) out of the 404 logs."""
    return {
        "service": "photon-server (python)",
        "health": "/api/health",
        "docs": "/docs",
    }


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
app.include_router(insights_router)
app.include_router(friends_router)
app.include_router(chat_router)
app.include_router(players_router)
app.include_router(health_router)

# TODO: Port remaining routes if needed (player_sync, event, playtime are now via capture + engine)
# For now, GET /api/session/{id}/state is the poll endpoint for the photon-client SDK

# ---------------------------------------------------------------------------
# Request logging middleware (like logReq in Node)
# ---------------------------------------------------------------------------

@app.middleware("http")
async def head_to_get(request: Request, call_next):
    """Answer HEAD on any GET route (uptime probes, curl -I, Render/Vercel).

    Starlette matches the method before the handler, so HEAD to a GET-only
    route 405s. Rewrite to GET for routing, then return the same status +
    headers with an empty body and content-length 0. (A GET-accurate length
    with an empty body would be ideal, but uvicorn enforces
    bytes-sent == content-length and 500s the response — so 0 it is.
    Probes only ever check the status anyway.)
    HEAD to POST-only routes still 405s (correct: no GET exists there).
    Registered after log_requests so it runs outermost; the logger then
    sees (and quiets) the rewritten GET like any other poll.
    """

    if request.method != "HEAD":
        return await call_next(request)

    request.scope["method"] = "GET"
    response = await call_next(request)

    try:
        # Drain the body iterator so serverless runtimes don't leak it;
        # the bytes are discarded — HEAD must not carry a body.
        async for _ in response.body_iterator:
            pass
    except Exception:
        pass

    headers = dict(response.headers)
    headers.pop("content-length", None)
    headers["content-length"] = "0"
    return Response(content=b"", status_code=response.status_code, headers=headers)


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
        # Poll spam off the console: stats/players/capture 2xx fire every
        # few seconds per client (BNC_DEBUG=1 to see them).
        _quiet = request.url.path in ("/api/stats", "/api/players", "/api/capture")
        if _quiet:
            try:
                from server.lib.logger import log_dbg
                log_dbg(f"{request.method} {request.url.path} → {status} {duration}ms ip={ip}")
            except Exception:
                pass
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
