"""
players.py — GET /api/players?session_id=xxx

Returns live players in current room, enriched with DB info if present.
Per spec: show discord username + gameid + skin image if in DB, else just name.

Response: { count, players: [ { username, game_id, skin, discord_username, discord_id, is_self, is_alive, pt, late_join, in_db } ] }
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db
from ..lib.redis_client import cache
from .capture import engine as capture_engine

router = APIRouter()

@router.get("/api/players")
async def get_players(request: Request):
    qp = request.query_params
    session_id = qp.get("session_id") or qp.get("sid")
    if not session_id:
        try:
            body = await request.json()
            session_id = body.get("session_id") or body.get("sessionId")
        except Exception:
            pass
    if not session_id:
        return JSONResponse({"error": "session_id required"}, status_code=400)

    # Resolve live session (engine memory -> cache)
    live = None
    try:
        view = capture_engine.get_session(session_id)
        if view:
            live = view.to_dict()
    except Exception:
        pass
    if not live:
        try:
            live = await cache.get_session_state(session_id)
        except Exception:
            pass
    if not live:
        return JSONResponse({"error": "session not found — no packets fed yet"}, status_code=404)

    rooms = live.get("rooms") or {}
    cur_id = live.get("current_room_id")
    cur = None
    if cur_id and cur_id in rooms:
        cur = rooms[cur_id]
    elif rooms:
        cur = next(reversed(list(rooms.values())), None)
    if not cur:
        return JSONResponse({"count": 0, "players": []})

    players_dict = cur.get("players") or {}
    # players_dict is {actorNr: {username, game_id, skin, is_alive, is_self, pt, late_join ...}}
    raw_players = []
    for _k, p in players_dict.items():
        raw_players.append({
            "username": p.get("username"),
            "game_id": p.get("game_id"),
            "skin": p.get("skin"),
            "is_alive": bool(p.get("is_alive")),
            "is_self": bool(p.get("is_self")),
            "pt": p.get("pt"),
            "actor_nr": p.get("actor_nr"),
            "late_join": bool(p.get("late_join")),
        })

    # Enrich with DB info where game_id or username matches bngc_users
    enriched = []
    if supabase and raw_players:
        # collect game_ids and usernames for bulk lookup
        gids = [p["game_id"] for p in raw_players if p.get("game_id")]
        unames = [p["username"] for p in raw_players if p.get("username")]
        db_by_gid: dict[str, dict] = {}
        db_by_uname: dict[str, dict] = {}
        try:
            if gids:
                # supabase in_ filter: .in_("game_id", gids)
                res = supabase.table("bngc_users").select("discord_id,discord_username,game_username,game_id,skin").in_("game_id", gids).execute()
                for r in (res.data or []):
                    if r.get("game_id"):
                        db_by_gid[str(r["game_id"])] = r
        except Exception:
            pass
        try:
            if unames:
                # need to avoid large IN, chunk 20
                chunk = unames[:30]
                res2 = supabase.table("bngc_users").select("discord_id,discord_username,game_username,game_id,skin").in_("game_username", chunk).execute()
                for r in (res2.data or []):
                    if r.get("game_username"):
                        db_by_uname[str(r["game_username"])] = r
        except Exception:
            pass

        for p in raw_players:
            gid = str(p.get("game_id") or "")
            uname = str(p.get("username") or "")
            match = db_by_gid.get(gid) if gid in db_by_gid else db_by_uname.get(uname)
            if match:
                enriched.append({
                    "username": p["username"],
                    "game_id": match.get("game_id") or p.get("game_id"),
                    "skin": match.get("skin") or p.get("skin"),
                    "discord_username": match.get("discord_username"),
                    "discord_id": match.get("discord_id"),
                    "is_self": p["is_self"],
                    "is_alive": p["is_alive"],
                    "pt": p["pt"],
                    "late_join": p["late_join"],
                    "in_db": True,
                })
            else:
                enriched.append({
                    "username": p["username"],
                    "game_id": p.get("game_id"),
                    "skin": p.get("skin"),
                    "discord_username": None,
                    "discord_id": None,
                    "is_self": p["is_self"],
                    "is_alive": p["is_alive"],
                    "pt": p["pt"],
                    "late_join": p["late_join"],
                    "in_db": False,
                })
    else:
        for p in raw_players:
            enriched.append({**p, "discord_username": None, "discord_id": None, "in_db": False})

    # Sort: self first, then alive first, then name
    enriched.sort(key=lambda x: (not x.get("is_self"), not x.get("is_alive"), (x.get("username") or "").lower()))

    return JSONResponse({"count": len(enriched), "room_id": cur.get("room_id"), "game_mode": cur.get("game_mode"), "state": cur.get("state"), "players": enriched})
