"""
stats.py — GET /api/stats
Returns live user stats + session state for the drawer overlay.

Query:
  ?session_id=xxx  -> resolve discord_id via bngc_sessions, return bngc_users row + live session view
  ?discord_id=xxx  -> direct lookup (fallback)

Response:
  {
    "discord_username": "...", "discord_id": "...",
    "game_username": "...", "game_id": "...", "skin": "...", "pickaxe": "...",
    "kills": 12, "deaths": 5, "wins": 3, "defeats": 8,
    "playtime_global": 1234, "playtime_per_gm": {...},
    "kd": 2.4, "win_rate": 0.27,
    "session": { "game_mode": "zw_lg", "cur_scn": "Game", "players_count": 4, "zone": {...} } | null
  }
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db
from ..lib.redis_client import cache
from .capture import engine as capture_engine

router = APIRouter()


@router.get("/api/stats")
async def get_stats(request: Request):
    qp = request.query_params
    session_id = qp.get("session_id") or qp.get("sid")
    discord_id = qp.get("discord_id") or qp.get("did")
    # Allow POST body fallback
    if not session_id and not discord_id:
        try:
            body = await request.json()
            session_id = body.get("session_id") or body.get("sessionId")
            discord_id = body.get("discord_id")
        except Exception:
            pass

    # Resolve discord_id via session if session_id given
    live_session = None
    if session_id:
        # Try live engine first
        try:
            view = capture_engine.get_session(session_id)
            if view:
                live_session = view.to_dict()
                if not discord_id and view.me:
                    # will resolve below via DB session row
                    pass
        except Exception:
            pass
        if not live_session:
            try:
                cached = await cache.get_session_state(session_id)
                if cached:
                    live_session = cached
            except Exception:
                pass
        # Resolve discord_id from DB session row if not already
        if not discord_id:
            try:
                sres = db.get_session_sync(session_id)
                sdata = sres.get("data") if isinstance(sres, dict) else None
                if sdata and sdata.get("discord_id"):
                    discord_id = sdata["discord_id"]
            except Exception:
                pass
        # Also try discord_id header from userscript auth
        if not discord_id:
            discord_id = qp.get("discord_id") or request.headers.get("x-discord-id")

    if not discord_id:
        return JSONResponse({"error": "discord_id or session_id required"}, status_code=400)

    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)

    try:
        res = supabase.table("bngc_users").select("*").eq("discord_id", discord_id).single().execute()
        user = res.data
    except Exception as e:
        if "PGRST116" in str(e):
            return JSONResponse({"error": "user not found"}, status_code=404)
        return JSONResponse({"error": str(e)}, status_code=500)

    if not user:
        return JSONResponse({"error": "user not found"}, status_code=404)

    kills = int(user.get("kills") or 0)
    deaths = int(user.get("deaths") or 0)
    wins = int(user.get("wins") or 0)
    defeats = int(user.get("defeats") or 0)
    kd = round(kills / deaths, 2) if deaths else (float(kills) if kills else 0)
    total_games = wins + defeats
    win_rate = round(wins / total_games, 3) if total_games else 0

    # live session fallback: if no live view but user has game_mode hints, leave null
    session_out = None
    if live_session:
        # live_session is SessionView dict — extract current room fields
        try:
            cur_room = None
            rooms = live_session.get("rooms") or {}
            cur_id = live_session.get("current_room_id")
            if cur_id and cur_id in rooms:
                cur_room = rooms[cur_id]
            elif rooms:
                # take most recent
                cur_room = next(reversed(list(rooms.values())), None)
            if cur_room:
                session_out = {
                    "game_mode": (cur_room.get("game_mode") or live_session.get("game_mode")),
                    "cur_scn": cur_room.get("cur_scn") or live_session.get("cur_scn"),
                    "players_count": cur_room.get("players_count") or live_session.get("players_count"),
                    "max_players": cur_room.get("max_players"),
                    "zone": cur_room.get("zone"),
                    "room_id": cur_room.get("room_id"),
                    "short_code": cur_room.get("short_code"),
                    "state": cur_room.get("state"),
                }
            else:
                session_out = {
                    "game_mode": live_session.get("game_mode"),
                    "cur_scn": live_session.get("cur_scn"),
                    "players_count": live_session.get("players_count"),
                    "zone": live_session.get("zone"),
                    "state": live_session.get("state"),
                }
        except Exception:
            session_out = {"game_mode": live_session.get("game_mode"), "cur_scn": live_session.get("cur_scn")}

    # playtime_per_gm may be json string
    ppg = user.get("playtime_per_gm")
    if isinstance(ppg, str):
        try:
            import json as _j
            ppg = _j.loads(ppg)
        except Exception:
            ppg = {}

    return JSONResponse({
        "discord_id": user.get("discord_id"),
        "discord_username": user.get("discord_username"),
        "game_username": user.get("game_username"),
        "game_id": user.get("game_id"),
        "skin": user.get("skin"),
        "pickaxe": user.get("pickaxe"),
        "kills": kills,
        "deaths": deaths,
        "wins": wins,
        "defeats": defeats,
        "playtime_global": int(user.get("playtime_global") or 0),
        "playtime_per_gm": ppg or {},
        "kd": kd,
        "win_rate": win_rate,
        "is_locked": bool(user.get("is_locked")),
        "session": session_out,
    })
