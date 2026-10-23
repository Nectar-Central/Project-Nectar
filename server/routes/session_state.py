"""
session_state.py -GET /api/session/{session_id}/state

Read-only view of the live Engine SessionView for a session_id.
No token rotation, no DB writes -just returns what Engine currently holds
for that session_id (populated by POST /api/capture → engine.feed).

This is what photon-client / overlay polls.
"""

from fastapi import APIRouter, HTTPException

from ..lib.photon_engine import Engine
from ..lib.redis_client import cache

# Reuse the same global Engine instance as capture.py
# We import it to ensure we share the same in-process dict
from .capture import engine as capture_engine

router = APIRouter()


@router.get("/api/session/{session_id}/state")
async def get_session_state(session_id: str):
    """
    Get live session state.

    Returns SessionView.to_dict() or 404 if never fed.
    Tries in-memory Engine first, then Upstash cached state (survives Vercel lambda cold start).
    Example response:
        {
            "session_id": "sess_mth",
            "me": {"username": "true jew", "actor_nr": 2, "skin": "cct_..."},
            "players": {"2": {"username": "true jew", ...}, "3": {"username": "Bob", ...}},
            "players_count": 6,
            "max_players": 12,
            "state": "match",
            "game_mode": "zw_lg",
            "cur_scn": "Game",
            "zone": {"time": 30, "phase": "2"}
        }
    """

    from fastapi.responses import JSONResponse

    view = capture_engine.get_session(session_id)

    if view:
        resp = JSONResponse(view.to_dict())
        resp.headers["Cache-Control"] = "public, max-age=3"
        return resp

    # Fallback: Upstash persisted state (any lambda can read it)
    cached = await cache.get_session_state(session_id)
    if cached:
        resp = JSONResponse(cached)
        resp.headers["Cache-Control"] = "public, max-age=3"
        return resp

    raise HTTPException(status_code=404, detail="session not found -no packets fed yet")
