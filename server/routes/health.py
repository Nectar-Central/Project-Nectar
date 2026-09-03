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
    return JSONResponse({
        "ok": True,
        "upstash_configured": upstash_configured,
        "upstash_reachable": upstash_reachable,
        "supabase_configured": supabase is not None,
        "engine_sessions": engine_sessions,
    })
