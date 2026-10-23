"""
health.py -GET /api/health (lightweight) + GET /api/ready (deep check).

/api/health is hit by Render healthchecks + uptime probes every few
seconds. It must NOT do Service-Initiated egress per poll (no Upstash
ping, no Supabase reads, no precision verify) or bandwidth burns 24/7
with zero players. Use /api/ready for deployment diagnostics.
/api/ready does the real backend checks (Upstash ping + precision verify).
"""

import time as _time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..lib.redis_client import cache
from ..lib.supabase_client import supabase
from ..lib import precision as _precision

# Build stamp: bump on every ship so /api/health proves which code is live.
# (Render gives no branch signal in logs; this ends "is the fix deployed" guessing.)
BUILD = "2026-09-13.1-bandwidth"

router = APIRouter()

# Cache expensive reachability results so even /api/ready doesn't
# hammer Upstash/Supabase when scraped frequently.
_ping_cache: dict = {"ts": 0.0, "reachable": False}
_PING_TTL_S = 30.0


async def _cached_ping() -> bool:
    now = _time.time()
    if now - _ping_cache["ts"] < _PING_TTL_S:
        return _ping_cache["reachable"]
    try:
        ok = await cache.ping(timeout=3) if cache.is_configured() else False
    except Exception:
        ok = False
    _ping_cache.update({"ts": now, "reachable": bool(ok)})
    return bool(ok)


@router.get("/api/health")
async def health():
    """Liveness only -zero outbound network. Safe to poll every 5s."""
    try:
        from .capture import engine as capture_engine
        engine_sessions = len(getattr(capture_engine, "_sessions", {}) or {})
    except Exception:
        engine_sessions = -1
    resp = JSONResponse({
        "ok": True,
        "build": BUILD,
        "upstash_configured": cache.is_configured(),
        "supabase_configured": bool(supabase),
        "engine_sessions": engine_sessions,
    })
    # Allow CDNs/probes to cache liveness briefly; clients revalidate fast.
    resp.headers["Cache-Control"] = "public, max-age=10"
    return resp


@router.get("/api/ready")
async def ready():
    """Readiness -real backend reachability + precision score. Poll sparingly."""
    upstash_configured = cache.is_configured()
    upstash_reachable = await _cached_ping() if upstash_configured else False
    try:
        from .capture import engine as capture_engine
        engine_sessions = len(getattr(capture_engine, "_sessions", {}) or {})
    except Exception:
        engine_sessions = -1
    # Settle any precision checks past their 2s delay, then report the score.
    try:
        await _precision.verify_due()
    except Exception:
        pass
    try:
        precision = await _precision.get_snapshot()
    except Exception:
        precision = {"precision_pct": 99.0, "verified": 0}
    resp = JSONResponse({
        "ok": True,
        "build": BUILD,
        "upstash_configured": upstash_configured,
        "upstash_reachable": upstash_reachable,
        "supabase_configured": bool(supabase),
        "engine_sessions": engine_sessions,
        "precision": precision,
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp
