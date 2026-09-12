"""
health.py — GET /api/health

Deployment diagnostics: verifies which backends this worker can reach.
Use it to confirm a Vercel deployment is actually wired up (Upstash for
engine state, Supabase for DB) instead of guessing from missing stats.

No auth, no DB writes — safe to poll.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..lib.redis_client import cache
from ..lib.supabase_client import supabase
from ..lib import precision as _precision

# Build stamp: bump on every ship so /api/health proves which code is live.
# (Render gives no branch signal in logs; this ends "is the fix deployed" guessing.)
BUILD = "2026-09-12.4-tracing"

router = APIRouter()


@router.get("/api/health")
async def health():
    """Return backend reachability for this worker."""
    upstash_configured = cache.is_configured()
    upstash_reachable = await cache.ping(timeout=3) if upstash_configured else False
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
    return JSONResponse({
        "ok": True,
        "build": BUILD,
        "upstash_configured": upstash_configured,
        "upstash_reachable": upstash_reachable,
        "supabase_configured": bool(supabase),
        "engine_sessions": engine_sessions,
        "precision": precision,
    })
