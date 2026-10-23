"""
players.py -GET /api/players?session_id=xxx

Returns live players in current room, enriched with DB info if present.
Per spec: show discord username + gameid + skin image if in DB, else just name.

Response: { count, players: [ { username, game_id, skin, discord_username, discord_id, is_self, is_alive, pt, late_join, in_db } ] }
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db, db_io
from ..lib.redis_client import cache
from .capture import engine as capture_engine

router = APIRouter()

_BLANK = ("", "nan", "null", "none", "undefined")

# Bandwidth guard: players polls every few seconds per client; each poll
# was 1x Upstash GET (10-60KB) + 2 Supabase IN queries. Cache 5s per session.
_PLAYERS_CACHE: dict = {}
_PLAYERS_TTL_S = 5.0


def _players_cache_bypassed() -> bool:
    try:
        import os as _os
        import sys as _sys
        if _os.getenv("PYTEST_CURRENT_TEST"):
            return True
        return "pytest" in _sys.modules
    except Exception:
        return False


def _ident_key(p: dict, idx: int):
    """Identity key for dedup: game_id, then discord_id, then username.

    Rows with no identity at all get a unique per-row key so they are
    never collapsed into each other."""
    for kind, val in (("gid", p.get("game_id")), ("did", p.get("discord_id"))):
        s = str(val or "").strip()
        if s and s.lower() not in _BLANK:
            return (kind, s.lower() if kind == "did" else s)
    un = str(p.get("username") or "").strip().lower()
    if un and un not in _BLANK:
        return ("un", un)
    return ("row", idx)


def _keep_rank(p: dict):
    return (bool(p.get("is_self")), bool(p.get("is_alive")), bool(p.get("in_db")))


def dedup_players(enriched: list) -> list:
    """Collapse duplicate rows for the same person (multi-device joins,
    ghost actors): one row per game_id/discord_id/username. The winner is
    self > alive > in_db (stable: first-seen on ties); missing discord /
    skin / game fields merge from the loser. Pure -unit-tested."""
    groups: dict = {}
    order: list = []
    for i, p in enumerate(enriched or []):
        try:
            k = _ident_key(p, i)
        except Exception:
            k = ("row", i)
        if k not in groups:
            groups[k] = dict(p)
            order.append(k)
            continue
        cur = groups[k]
        win, lose = (p, cur) if _keep_rank(p) > _keep_rank(cur) else (cur, p)
        win = dict(win)
        for f in ("discord_username", "discord_id", "game_id", "skin"):
            try:
                if not win.get(f) and lose.get(f):
                    win[f] = lose[f]
            except Exception:
                pass
        for f in ("is_self", "is_alive", "late_join"):
            win[f] = bool(win.get(f) or lose.get(f))
        if lose.get("in_db") and not win.get("in_db"):
            win["in_db"] = True
        groups[k] = win
    return [groups[k] for k in order]

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
    # Fast-path cache (no backend egress on HIT).
    try:
        if not _players_cache_bypassed():
            import time as _t
            _hit = _PLAYERS_CACHE.get(session_id)
            if _hit and _t.time() < _hit[0]:
                _r = JSONResponse(_hit[1])
                _r.headers["Cache-Control"] = "public, max-age=5"
                _r.headers["X-Cache"] = "HIT"
                return _r
    except Exception:
        pass

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
        return JSONResponse({"error": "session not found -no packets fed yet"}, status_code=404)

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
                res = await db_io(lambda: supabase.table("bngc_users").select("discord_id,discord_username,game_username,game_id,skin").in_("game_id", gids).execute())
                for r in (res.data or []):
                    if r.get("game_id"):
                        db_by_gid[str(r["game_id"])] = r
        except Exception:
            pass
        try:
            if unames:
                # need to avoid large IN, chunk 20
                chunk = unames[:30]
                res2 = await db_io(lambda: supabase.table("bngc_users").select("discord_id,discord_username,game_username,game_id,skin").in_("game_username", chunk).execute())
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

    # Dedup: same person under two actor numbers (mobile + desktop,
    # ghost actors) shares game_id/discord_id -show once.
    try:
        enriched = dedup_players(enriched)
    except Exception:
        pass

    # Sort: self first, then alive first, then name
    enriched.sort(key=lambda x: (not x.get("is_self"), not x.get("is_alive"), (x.get("username") or "").lower()))

    payload = {"count": len(enriched), "room_id": cur.get("room_id"), "game_mode": cur.get("game_mode"), "state": cur.get("state"), "short_code": cur.get("short_code"), "party": cur.get("party"), "players": enriched}
    try:
        if not _players_cache_bypassed():
            import time as _t2
            if len(_PLAYERS_CACHE) > 2000:
                _PLAYERS_CACHE.clear()
            _PLAYERS_CACHE[session_id] = (_t2.time() + _PLAYERS_TTL_S, payload)
    except Exception:
        pass
    _out = JSONResponse(payload)
    _out.headers["Cache-Control"] = "public, max-age=5"
    _out.headers["X-Cache"] = "MISS"
    return _out
