"""
ranked.py — /api/ranked/* room lifecycle (server-side authority).

The bot owns Discord delivery (embeds/threads/DMs) and Discord-side
permissions (host/admin); THESE endpoints own state + points + validation:
rooms CRUD, roster add/remove (bans/locks/capacity), start (eligibility,
host-in-room verify, fee burn, lobby->live), backout (live->lobby),
end (->ended), code/host transfer, seals listing, room review.

Conventions (match neighboring routes): {"error": msg} + status codes,
validate everything at the boundary, fail-open reads where safe but
fail-CLOSED on anything that moves points. Never raises out (500 JSON).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase as _supabase_global
from ..lib import ranked as ranked_mod

router = APIRouter()

ALLOWED_GMS = ("zw_lg", "df", "wwzw")
HOST_FRESH_S = 900  # room-active evidence window (matches /api/ranked/verify)


def _db():
    from ..lib import supabase_client as _sc
    return _sc.supabase


def _err(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


def _rid():
    import secrets
    return "R-" + secrets.token_hex(3).upper()


def _get_room(rid):
    try:
        r = _db().table("ranked_rooms").select("*").eq("id", str(rid or "").strip().upper()).limit(1).execute()
        rows = r.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def _registered(discord_id):
    try:
        r = _db().table("bngc_users").select("discord_id,game_username").eq("discord_id", str(discord_id)).limit(1).execute()
        rows = r.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def _is_banned(discord_id) -> bool:
    try:
        r = _db().table("ranked_bans").select("discord_id").eq("discord_id", str(discord_id)).limit(1).execute()
        return bool(r.data)
    except Exception:
        return False


def _fresh_points(discord_ids) -> dict:
    """Live points map (no 60s cache — fees need fresh numbers). Missing = 0."""
    try:
        ids = [str(x) for x in (discord_ids or []) if x]
        if not ids:
            return {}
        r = _db().table("ranked_points").select("discord_id,points").in_("discord_id", ids[:64]).execute()
        out = {}
        for x in (r.data or []):
            try:
                out[str(x.get("discord_id"))] = int(x.get("points") or 0)
            except Exception:
                continue
        for i in ids:
            out.setdefault(i, 0)
        return out
    except Exception:
        return {str(x): 0 for x in (discord_ids or []) if x}


def _entry_fee(room: dict, player_points: int) -> int:
    """Total burned on start: flat room fee + milestone tier fee."""
    try:
        _name, tier_fee = ranked_mod.milestone_for(player_points)
        return int(room.get("fee") or 0) + int(tier_fee or 0)
    except Exception:
        return int(room.get("fee") or 0)


def _roster_ids(room: dict) -> list:
    try:
        return [str(x.get("discord_id")) for x in (room.get("roster") or []) if x.get("discord_id")]
    except Exception:
        return []


def _save_roster(rid: str, ids: list):
    from datetime import datetime, timezone
    roster = [{"discord_id": str(i), "joined_at": datetime.now(timezone.utc).isoformat()} for i in ids]
    _db().table("ranked_rooms").update({"roster": roster}).eq("id", rid).execute()


def _burn_points(discord_id: str, delta: int):
    """Direct read-modify-write (fresh). Returns new total. May raise."""
    from datetime import datetime, timezone
    cur = _fresh_points([discord_id]).get(str(discord_id), 0)
    total = cur + int(delta)
    _db().table("ranked_points").upsert({
        "discord_id": str(discord_id),
        "points": total,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="discord_id").execute()
    return total


def _scan_code_presence(code: str, window_s: int = HOST_FRESH_S):
    """Recent game events carrying this room code -> (active, {usernames}).

    Shared by /api/ranked/verify and the start gate. Fail-open (False, set())
    on read errors — callers decide how to treat unknown.
    """
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        res = _db().table("bngc_game_events").select(
            "event_type,event_data,discord_id,created_at"
        ).order("created_at", desc=True).limit(300).execute()
        names = set()
        dids = set()
        seen = 0
        for row in (res.data or []):
            try:
                ed = (row or {}).get("event_data") or {}
                c = str(ed.get("code") or ed.get("short_code") or "").strip().upper()
                if c != code:
                    continue
                try:
                    ts = str((row or {}).get("created_at") or "").replace("Z", "+00:00")
                    age = (now - datetime.fromisoformat(ts)).total_seconds()
                except Exception:
                    continue
                if age < 0 or age > window_s:
                    continue
                seen += 1
                for key in ("killer", "victim", "winner"):
                    v = ed.get(key)
                    if v and str(v).strip():
                        names.add(str(v).strip())
                if (row or {}).get("discord_id"):
                    dids.add(str(row["discord_id"]))
            except Exception:
                continue
        if dids:
            try:
                ures = _db().table("bngc_users").select("game_username").in_("discord_id", sorted(dids)[:64]).execute()
                for u in (ures.data or []):
                    if u.get("game_username"):
                        names.add(str(u["game_username"]))
            except Exception:
                pass
        return seen > 0, names
    except Exception:
        return False, set()


def _strip_dc(name: str) -> str:
    try:
        s = str(name or "").strip()
        return s[3:].strip() if s.lower().startswith("dc:") else s
    except Exception:
        return ""


def _check_join(room: dict, did: str):
    """Return None if did may join, else a human reason. Pure checks."""
    try:
        if _is_banned(did):
            return "banned from ranked"
        pts = _fresh_points([did]).get(str(did), 0)
        if pts < (room.get("min_points") or 0):
            tier = ranked_mod.milestone_for(room.get("min_points") or 0).get("name")
            return f"needs {tier} ({room['min_points']}+ points)"
        if room.get("max_points") is not None and pts > room["max_points"]:
            return f"capped at {room['max_points']} points"
        if len(_roster_ids(room)) >= (room.get("max_players") or 16):
            return "roster is full"
        return None
    except Exception as exc:
        return f"check failed: {exc}"


def _room_lite(room: dict) -> dict:
    try:
        sealed = _db().table("ranked_results").select("match_version").eq("room_id", room.get("id")).execute()
        nsealed = len(sealed.data or [])
    except Exception:
        nsealed = 0
    return {
        "id": room.get("id"),
        "short_code": room.get("short_code"),
        "gm": room.get("gm"),
        "status": room.get("status"),
        "host_discord_id": room.get("host_discord_id"),
        "host_only": bool(room.get("host_only")),
        "players": len(_roster_ids(room)),
        "max_players": room.get("max_players"),
        "sealed": nsealed,
        "max_matches": room.get("max_matches"),
        "min_points": room.get("min_points") or 0,
        "max_points": room.get("max_points"),
        "fee": room.get("fee") or 0,
    }


# ---------------------------------------------------------------------------
# POST /api/ranked/rooms — create
# ---------------------------------------------------------------------------
@router.post("/api/ranked/rooms")
async def ranked_create(request: Request):
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        body = await request.json()
    except Exception:
        return _err("JSON body required")
    try:
        host = str(body.get("host_discord_id") or "").strip()
        code = str(body.get("short_code") or "").strip().upper()
        gm = str(body.get("gm") or "").strip().lower()
        try:
            max_players = int(body.get("max_players") or 16)
            max_matches = int(body.get("max_matches") or 5)
            min_points = int(body.get("min_points") or 0)
            fee = int(body.get("fee") or 0)
        except Exception:
            return _err("max_players/max_matches/min_points/fee must be ints")
        maxp_raw = body.get("max_points")
        try:
            max_points = int(maxp_raw) if maxp_raw not in (None, "", 0, "0") else None
        except Exception:
            return _err("max_points must be an int")
        host_only = bool(body.get("host_only"))
        starts_at = body.get("starts_at")
        if not host or not _registered(host):
            return _err("host must be a registered player", 403)
        if _is_banned(host):
            return _err("host is banned from ranked", 403)
        if len(code) != 5 or not code.isalnum():
            return _err("short_code must be 5 letters/digits")
        if gm not in ALLOWED_GMS:
            return _err(f"gm must be one of {', '.join(ALLOWED_GMS)}")
        if not (2 <= max_players <= 16):
            return _err("max_players must be 2-16")
        if not (1 <= max_matches <= 50):
            return _err("max_matches must be 1-50")
        if min_points < 0 or fee < 0:
            return _err("min_points/fee must be >= 0")
        try:
            dup = _db().table("ranked_rooms").select("id").eq("short_code", code).in_("status", ["lobby", "live"]).limit(1).execute()
            if dup.data:
                return _err(f"code {code} already has an open ranked room", 409)
        except Exception:
            pass
        rid = None
        for _ in range(5):
            cand = _rid()
            try:
                chk = _db().table("ranked_rooms").select("id").eq("id", cand).limit(1).execute()
                if not chk.data:
                    rid = cand
                    break
            except Exception:
                rid = cand
                break
        if not rid:
            return _err("R-ID collision, try again", 500)
        from datetime import datetime, timezone
        row = {
            "id": rid,
            "short_code": code,
            "host_discord_id": host,
            "gm": gm,
            "max_players": max_players,
            "max_matches": max_matches,
            "min_points": min_points,
            "max_points": max_points,
            "fee": fee,
            "starts_at": starts_at,
            "status": "lobby",
            "host_only": host_only,
            "roster": [] if host_only else [{"discord_id": host, "joined_at": datetime.now(timezone.utc).isoformat()}],
        }
        try:
            _db().table("ranked_rooms").insert(row).execute()
        except Exception as exc:
            return _err(f"create failed: {exc}", 500)
        return JSONResponse({"ok": True, "room": _room_lite(_get_room(rid) or row)})
    except Exception as exc:
        return _err(str(exc), 500)


# ---------------------------------------------------------------------------
# GET /api/ranked/rooms[?status=] — list
# ---------------------------------------------------------------------------
@router.get("/api/ranked/rooms")
async def ranked_list(request: Request):
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        status = (request.query_params.get("status") or "").strip().lower() or None
        q = _db().table("ranked_rooms").select("*").order("created_at", desc=True).limit(50)
        if status:
            if status not in ("lobby", "live", "ended"):
                return _err("status must be lobby|live|ended")
            q = q.eq("status", status)
        res = q.execute()
        return JSONResponse({"rooms": [_room_lite(r) for r in (res.data or [])]})
    except Exception as exc:
        return _err(str(exc), 500)


# ---------------------------------------------------------------------------
# GET /api/ranked/rooms/{rid} — review detail
# ---------------------------------------------------------------------------
@router.get("/api/ranked/rooms/{rid}")
async def ranked_review(rid: str, request: Request):
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        room = _get_room(rid)
        if not room:
            return _err("no such ranked room", 404)
        ids = _roster_ids(room)
        pts = _fresh_points(ids)
        names = {}
        if ids:
            try:
                ures = _db().table("bngc_users").select("discord_id,game_username,discord_username").in_("discord_id", ids[:64]).execute()
                for u in (ures.data or []):
                    names[str(u.get("discord_id"))] = u.get("game_username") or u.get("discord_username")
            except Exception:
                pass
        roster = []
        for did in ids:
            p = pts.get(did, 0)
            tier = ranked_mod.milestone_for(p).get("name")
            roster.append({"discord_id": did, "name": names.get(did),
                           "points": p, "milestone": tier,
                           "is_host": did == str(room.get("host_discord_id"))})
        try:
            sres = _db().table("ranked_results").select(
                "match_version,winner_discord_id,kills,points").eq("room_id", room["id"]).order("match_version").limit(50).execute()
            seals = sres.data or []
        except Exception:
            seals = []
        out = _room_lite(room)
        out["roster"] = roster
        out["seals"] = seals
        return JSONResponse({"room": out})
    except Exception as exc:
        return _err(str(exc), 500)


# ---------------------------------------------------------------------------
# POST /api/ranked/rooms/{rid}/roster — {discord_id, action: add|remove}
# ---------------------------------------------------------------------------
@router.post("/api/ranked/rooms/{rid}/roster")
async def ranked_roster(rid: str, request: Request):
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        body = await request.json()
    except Exception:
        return _err("JSON body required")
    try:
        room = _get_room(rid)
        if not room:
            return _err("no such ranked room", 404)
        did = str(body.get("discord_id") or "").strip()
        action = str(body.get("action") or "").strip().lower()
        if not did:
            return _err("discord_id required")
        if action not in ("add", "remove"):
            return _err("action must be add|remove")
        ids = _roster_ids(room)
        if action == "add":
            if room.get("status") != "lobby":
                return _err("roster is locked (room not in lobby)", 409)
            if not _registered(did):
                return _err("player is not registered", 403)
            if did in ids:
                return JSONResponse({"ok": True, "roster": ids, "unchanged": True})
            problem = _check_join(room, did)
            if problem:
                return _err(problem, 403)
            ids.append(did)
        else:
            if did == str(room.get("host_discord_id")):
                return _err("host can't leave (transfer host first)", 409)
            if did not in ids:
                return JSONResponse({"ok": True, "roster": ids, "unchanged": True})
            ids = [x for x in ids if x != did]
        try:
            _save_roster(room["id"], ids)
        except Exception as exc:
            return _err(f"roster write failed: {exc}", 500)
        return JSONResponse({"ok": True, "roster": ids})
    except Exception as exc:
        return _err(str(exc), 500)


# ---------------------------------------------------------------------------
# POST /api/ranked/rooms/{rid}/start — eligibility + host verify + fees
# ---------------------------------------------------------------------------
@router.post("/api/ranked/rooms/{rid}/start")
async def ranked_start(rid: str, request: Request):
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        via = str((body or {}).get("via") or "manual")
        room = _get_room(rid)
        if not room:
            return _err("no such ranked room", 404)
        if room.get("status") != "lobby":
            return _err("room is not in lobby", 409)
        ids = _roster_ids(room)
        # eligibility re-check (bans/locks shift after joining)
        pts = _fresh_points(ids)
        dropped = []
        kept = []
        for did in ids:
            reason = None
            if _is_banned(did):
                reason = "banned"
            elif pts.get(did, 0) < (room.get("min_points") or 0):
                reason = "below lock"
            elif room.get("max_points") is not None and pts.get(did, 0) > room["max_points"]:
                reason = "above cap"
            if reason:
                dropped.append({"discord_id": did, "reason": reason})
            else:
                kept.append(did)
        # host-in-room verify (needs the host's game name)
        host_status = "unverified"
        try:
            hrow = _registered(str(room.get("host_discord_id")))
            hname = (hrow or {}).get("game_username")
        except Exception:
            hname = None
        if hname:
            try:
                active, names = _scan_code_presence(str(room.get("short_code") or ""))
                if not active:
                    try:
                        _save_roster(room["id"], kept)
                    except Exception:
                        pass
                    return JSONResponse({"error": "room not active in game",
                                         "detail": "no recent game events for this code",
                                         "dropped": dropped}, status_code=409)
                hn = _strip_dc(hname)
                if not any(n and (n == hname or _strip_dc(n) == hn) for n in names):
                    try:
                        _save_roster(room["id"], kept)
                    except Exception:
                        pass
                    return JSONResponse({"error": "host not in room",
                                         "detail": "join the room first, then start",
                                         "dropped": dropped}, status_code=409)
                host_status = "in room"
            except Exception as exc:
                if isinstance(exc, KeyError):
                    raise
                host_status = "unverified"
        # fee burn on the survivors
        pts = _fresh_points(kept)
        broke = []
        for did in kept:
            fee = _entry_fee(room, pts.get(did, 0))
            if fee > 0:
                if pts.get(did, 0) < fee:
                    broke.append({"discord_id": did, "reason": "can't cover fee"})
                else:
                    try:
                        _burn_points(did, -fee)
                    except Exception:
                        broke.append({"discord_id": did, "reason": "fee charge failed"})
        final = [d for d in kept if d not in {b["discord_id"] for b in broke}]
        for b in broke:
            dropped.append(b)
        if len(final) < 2:
            try:
                _save_roster(room["id"], final)
            except Exception:
                pass
            return JSONResponse({"error": "need at least 2 players to start",
                                 "dropped": dropped}, status_code=409)
        try:
            _save_roster(room["id"], final)
            _db().table("ranked_rooms").update({"status": "live"}).eq("id", room["id"]).execute()
        except Exception as exc:
            return _err(f"start failed: {exc}", 500)
        return JSONResponse({"ok": True, "room_id": room["id"],
                             "short_code": room.get("short_code"),
                             "gm": room.get("gm"),
                             "max_matches": room.get("max_matches"),
                             "roster": final, "dropped": dropped,
                             "host_status": host_status, "via": via})
    except Exception as exc:
        return _err(str(exc), 500)


# ---------------------------------------------------------------------------
# POST /api/ranked/rooms/{rid}/backout — live -> lobby (session continues)
# ---------------------------------------------------------------------------
@router.post("/api/ranked/rooms/{rid}/backout")
async def ranked_backout(rid: str, request: Request):
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        room = _get_room(rid)
        if not room:
            return _err("no such ranked room", 404)
        if room.get("status") != "live":
            return _err("only a live room can back out", 409)
        try:
            _db().table("ranked_rooms").update({"status": "lobby"}).eq("id", room["id"]).execute()
        except Exception as exc:
            return _err(f"backout failed: {exc}", 500)
        return JSONResponse({"ok": True, "room": _room_lite(_get_room(rid) or room)})
    except Exception as exc:
        return _err(str(exc), 500)


# ---------------------------------------------------------------------------
# POST /api/ranked/rooms/{rid}/end — whole session over
# ---------------------------------------------------------------------------
@router.post("/api/ranked/rooms/{rid}/end")
async def ranked_end(rid: str, request: Request):
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        room = _get_room(rid)
        if not room:
            return _err("no such ranked room", 404)
        if room.get("status") == "ended":
            return JSONResponse({"ok": True, "room": _room_lite(room), "unchanged": True})
        try:
            _db().table("ranked_rooms").update({"status": "ended"}).eq("id", room["id"]).execute()
        except Exception as exc:
            return _err(f"end failed: {exc}", 500)
        out = _room_lite(_get_room(rid) or room)
        out["status"] = "ended"
        return JSONResponse({"ok": True, "room": out})
    except Exception as exc:
        return _err(str(exc), 500)


# ---------------------------------------------------------------------------
# PATCH /api/ranked/rooms/{rid}/code — change room code
# ---------------------------------------------------------------------------
@router.patch("/api/ranked/rooms/{rid}/code")
async def ranked_code(rid: str, request: Request):
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        body = await request.json()
    except Exception:
        return _err("JSON body required")
    try:
        room = _get_room(rid)
        if not room:
            return _err("no such ranked room", 404)
        if room.get("status") == "ended":
            return _err("session ended", 409)
        code = str((body or {}).get("short_code") or "").strip().upper()
        if len(code) != 5 or not code.isalnum():
            return _err("short_code must be 5 letters/digits")
        if code != room.get("short_code"):
            try:
                dup = _db().table("ranked_rooms").select("id").eq("short_code", code).in_("status", ["lobby", "live"]).limit(1).execute()
                if dup.data:
                    return _err(f"code {code} already has an open ranked room", 409)
            except Exception:
                pass
            try:
                _db().table("ranked_rooms").update({"short_code": code}).eq("id", room["id"]).execute()
            except Exception as exc:
                return _err(f"code change failed: {exc}", 500)
        return JSONResponse({"ok": True, "room": _room_lite(_get_room(rid) or room)})
    except Exception as exc:
        return _err(str(exc), 500)


# ---------------------------------------------------------------------------
# PATCH /api/ranked/rooms/{rid}/host — transfer host
# ---------------------------------------------------------------------------
@router.patch("/api/ranked/rooms/{rid}/host")
async def ranked_host(rid: str, request: Request):
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        body = await request.json()
    except Exception:
        return _err("JSON body required")
    try:
        room = _get_room(rid)
        if not room:
            return _err("no such ranked room", 404)
        if room.get("status") == "ended":
            return _err("session ended", 409)
        ndid = str((body or {}).get("discord_id") or "").strip()
        if not ndid:
            return _err("discord_id required")
        if not _registered(ndid):
            return _err("new host is not registered", 403)
        if _is_banned(ndid):
            return _err("new host is banned from ranked", 403)
        try:
            _db().table("ranked_rooms").update({"host_discord_id": ndid}).eq("id", room["id"]).execute()
        except Exception as exc:
            return _err(f"host transfer failed: {exc}", 500)
        room = _get_room(rid) or room
        ids = _roster_ids(room)
        if ndid not in ids:
            ids.append(ndid)
            try:
                _save_roster(room["id"], ids)
            except Exception:
                pass
        return JSONResponse({"ok": True, "room": _room_lite(_get_room(rid) or room)})
    except Exception as exc:
        return _err(str(exc), 500)


# ---------------------------------------------------------------------------
# GET /api/ranked/by-code/{code}?discord_id= — userscript authority check
# ---------------------------------------------------------------------------
@router.get("/api/ranked/by-code/{code}")
async def ranked_by_code(code: str, request: Request):
    """Am I in a ranked session for this room code?

    Returns the open (live preferred, then lobby) room for the code, or the
    latest ended one so clients can hide UI deliberately instead of guessing.
    {room: lite|null, on_roster, is_host}. 200 with room null = not ranked.
    """
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        code = str(code or "").strip().upper()
        if len(code) != 5 or not code.isalnum():
            return _err("code must be 5 letters/digits")
        did = str(request.query_params.get("discord_id") or "").strip()
        room = None
        try:
            for status in ("live", "lobby"):
                r = _db().table("ranked_rooms").select("*").eq("short_code", code).eq("status", status).limit(1).execute()
                if r.data:
                    room = r.data[0]
                    break
            if room is None:
                r = _db().table("ranked_rooms").select("*").eq("short_code", code).eq("status", "ended").order("created_at", desc=True).limit(1).execute()
                if r.data:
                    room = r.data[0]
        except Exception:
            room = None
        if room is None:
            return JSONResponse({"room": None, "on_roster": False, "is_host": False})
        ids = _roster_ids(room)
        return JSONResponse({
            "room": _room_lite(room),
            "on_roster": did in ids if did else False,
            "is_host": bool(did) and did == str(room.get("host_discord_id")),
        })
    except Exception as exc:
        return _err(str(exc), 500)


# ---------------------------------------------------------------------------
# GET /api/ranked/mine?discord_id= — my open ranked room, if any
# ---------------------------------------------------------------------------
@router.get("/api/ranked/mine")
async def ranked_mine(request: Request):
    """Which ranked session (if any) involves me? Open rooms only — an ended
    room means you're not in a session anymore, so it returns room null."""
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        did = str(request.query_params.get("discord_id") or "").strip()
        if not did:
            return _err("discord_id required")
        found = None
        try:
            r = _db().table("ranked_rooms").select("*").in_("status", ["lobby", "live"]).order("created_at", desc=True).limit(50).execute()
            cands = r.data or []
            for status in ("live", "lobby"):
                for row in cands:
                    if row.get("status") == status and did in _roster_ids(row):
                        found = row
                        break
                if found:
                    break
        except Exception:
            found = None
        if found is None:
            return JSONResponse({"room": None, "on_roster": False, "is_host": False})
        return JSONResponse({
            "room": _room_lite(found),
            "on_roster": True,
            "is_host": did == str(found.get("host_discord_id")),
        })
    except Exception as exc:
        return _err(str(exc), 500)


# ---------------------------------------------------------------------------
# GET /api/ranked/rooms/{rid}/seals — sealed matches
# ---------------------------------------------------------------------------
@router.get("/api/ranked/rooms/{rid}/seals")
async def ranked_seals(rid: str, request: Request):
    if _db() is None:
        return _err("Server not configured", 500)
    try:
        room = _get_room(rid)
        if not room:
            return _err("no such ranked room", 404)
        try:
            res = _db().table("ranked_results").select(
                "match_version,gm,winner_discord_id,kills,points,placements,created_at"
            ).eq("room_id", room["id"]).order("match_version").limit(50).execute()
            seals = res.data or []
        except Exception:
            seals = []
        return JSONResponse({"room_id": room["id"], "count": len(seals), "seals": seals})
    except Exception as exc:
        return _err(str(exc), 500)
