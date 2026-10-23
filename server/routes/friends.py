"""
friends.py -friend requests + party invites (Discord-registered users only).

Tables (supabase/add_friends_v220.sql): bngc_friends, bngc_party_invites.

  POST /api/friends/request {requester_discord_id, addressee_discord_id}
  POST /api/friends/accept  {discord_id, requester_discord_id}   # addressee accepts
  POST /api/friends/decline {discord_id, requester_discord_id}   # addressee declines
  POST /api/friends/remove  {discord_id, friend_discord_id}      # either side removes
  GET  /api/friends/list?discord_id=                             # friends+inbox+outbox
  POST /api/party/invite    {from_discord_id, to_discord_id, code}
  GET  /api/party/invites?discord_id=                            # incoming (live) + outgoing

Trust level follows the existing public-by-discord_id convention
(/api/stats, /api/players take discord_id openly). Sender-side gating
("invite only while holding a live private pID") lives in the drawer UI,
which reads the live short_code from /api/stats; the server validates
code format + accepted friendship + 5-min expiry. No secrets returned.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.presence import presence_for
from ..lib.redis_client import cache
from ..lib.supabase_client import supabase

router = APIRouter()

_INVITE_TTL_S = 300


def is_valid_code(code: str | None) -> str | None:
    """Normalized 5-char alnum pID, or None. Pure, unit-tested."""
    try:
        c = str(code or "").strip().upper()
    except Exception:
        return None
    return c if len(c) == 5 and c.isalnum() else None


def invite_expiry(now: datetime | None = None) -> str:
    """ISO expiry for a new invite (TTL 5 min). Pure, unit-tested."""
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(seconds=_INVITE_TTL_S)).isoformat()


def is_invite_live(row: dict | None, now: datetime | None = None) -> bool:
    """True when the invite row exists and has not expired. Pure, unit-tested."""
    try:
        if not row or not row.get("expires_at"):
            return False
        base = now or datetime.now(timezone.utc)
        exp = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        return exp > base
    except Exception:
        return False


def _need_supabase():
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    return None


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def _ids(*vals) -> tuple:
    return tuple(str(v or "").strip() for v in vals)


@router.post("/api/friends/request")
async def friends_request(request: Request):
    err = _need_supabase()
    if err:
        return err
    b = await _body(request)
    req_id, addr_id = _ids(b.get("requester_discord_id"), b.get("addressee_discord_id"))
    if not req_id or not addr_id:
        return JSONResponse({"error": "requester_discord_id and addressee_discord_id required"}, status_code=400)
    if req_id == addr_id:
        return JSONResponse({"error": "cannot friend yourself"}, status_code=400)
    try:
        # either direction existing -> 409 (no duplicates, no cross-requests)
        existing = supabase.table("bngc_friends").select("status").or_(
            f"and(requester_discord_id.eq.{req_id},addressee_discord_id.eq.{addr_id}),"
            f"and(requester_discord_id.eq.{addr_id},addressee_discord_id.eq.{req_id})"
        ).limit(2).execute()
        if existing.data:
            return JSONResponse({"error": "already friends or request pending", "status": existing.data[0].get("status")}, status_code=409)
        supabase.table("bngc_friends").insert(
            {"requester_discord_id": req_id, "addressee_discord_id": addr_id, "status": "pending"}
        ).execute()
        return JSONResponse({"ok": True, "status": "pending"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/friends/accept")
async def friends_accept(request: Request):
    err = _need_supabase()
    if err:
        return err
    b = await _body(request)
    me, req_id = _ids(b.get("discord_id"), b.get("requester_discord_id"))
    if not me or not req_id:
        return JSONResponse({"error": "discord_id and requester_discord_id required"}, status_code=400)
    try:
        res = supabase.table("bngc_friends").update({"status": "accepted"}).eq(
            "requester_discord_id", req_id).eq("addressee_discord_id", me).eq("status", "pending").execute()
        if not res.data:
            return JSONResponse({"error": "no pending request"}, status_code=404)
        return JSONResponse({"ok": True, "status": "accepted"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/friends/decline")
async def friends_decline(request: Request):
    err = _need_supabase()
    if err:
        return err
    b = await _body(request)
    me, req_id = _ids(b.get("discord_id"), b.get("requester_discord_id"))
    if not me or not req_id:
        return JSONResponse({"error": "discord_id and requester_discord_id required"}, status_code=400)
    try:
        supabase.table("bngc_friends").delete().eq(
            "requester_discord_id", req_id).eq("addressee_discord_id", me).eq("status", "pending").execute()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/friends/remove")
async def friends_remove(request: Request):
    err = _need_supabase()
    if err:
        return err
    b = await _body(request)
    me, friend = _ids(b.get("discord_id"), b.get("friend_discord_id"))
    if not me or not friend:
        return JSONResponse({"error": "discord_id and friend_discord_id required"}, status_code=400)
    try:
        supabase.table("bngc_friends").delete().or_(
            f"and(requester_discord_id.eq.{me},addressee_discord_id.eq.{friend}),"
            f"and(requester_discord_id.eq.{friend},addressee_discord_id.eq.{me})"
        ).execute()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _enrich_ids(rows: list[dict]) -> tuple[list[str], dict]:
    dids = sorted({str(r.get("requester_discord_id") or r.get("addressee_discord_id") or "")
                   for r in rows if r} - {""})
    names: dict[str, dict] = {}
    if dids and supabase:
        try:
            ures = supabase.table("bngc_users").select(
                "discord_id,discord_username,game_username").in_("discord_id", dids[:100]).execute()
            for u in (ures.data or []):
                names[str(u.get("discord_id"))] = u
        except Exception:
            pass
    return dids, names


@router.get("/api/friends/list")
async def friends_list(request: Request):
    err = _need_supabase()
    if err:
        return err
    me = str(request.query_params.get("discord_id") or "").strip()
    if not me:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    try:
        res = supabase.table("bngc_friends").select(
            "requester_discord_id,addressee_discord_id,status,created_at").or_(
            f"requester_discord_id.eq.{me},addressee_discord_id.eq.{me}").limit(500).execute()
        friends, incoming, outgoing = [], [], []
        for r in (res.data or []):
            other = str(r.get("addressee_discord_id") if str(r.get("requester_discord_id")) == me
                        else r.get("requester_discord_id"))
            entry = {"discord_id": other, "status": r.get("status"), "created_at": r.get("created_at")}
            if r.get("status") == "accepted":
                friends.append(entry)
            elif str(r.get("addressee_discord_id")) == me:
                incoming.append({**entry, "requester_discord_id": str(r.get("requester_discord_id"))})
            else:
                outgoing.append(entry)
        _, names = _enrich_ids([{"requester_discord_id": e["discord_id"]} for e in friends + incoming + outgoing])
        for e in friends + incoming + outgoing:
            u = names.get(e["discord_id"]) or {}
            e["discord_username"] = u.get("discord_username")
            e["game_username"] = u.get("game_username")
        await _attach_presence(friends)
        return JSONResponse({"friends": friends, "incoming": incoming, "outgoing": outgoing,
                             "counts": {"friends": len(friends), "incoming": len(incoming), "outgoing": len(outgoing)}})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def _attach_presence(entries: list[dict]) -> None:
    """Attach live presence to friend entries (best-effort, never raises).

    Latest active bngc_sessions row per friend -> cached engine state ->
    presence_for(). Friends with no/stale session read offline. Room tags
    resolve "In Scrims/Tourney" names in one batched query.
    """
    import asyncio as _aio

    def _offline():
        return {"status": "offline", "detail": "offline", "gm": None,
                "code": None, "count": 0, "tag": None}

    dids = [str(e.get("discord_id")) for e in (entries or []) if e.get("discord_id")]
    if not dids or not supabase:
        for e in (entries or []):
            e["presence"] = _offline()
        return
    try:
        sres = supabase.table("bngc_sessions").select(
            "discord_id,session_id,created_at").in_("discord_id", dids[:100]).eq(
            "is_active", True).order("created_at", desc=True).limit(len(dids) * 3).execute()
        sess_by_did: dict[str, str] = {}
        for s in (sres.data or []):
            d = str(s.get("discord_id") or "")
            if d and d not in sess_by_did and s.get("session_id"):
                sess_by_did[d] = str(s["session_id"])
    except Exception:
        for e in (entries or []):
            e["presence"] = _offline()
        return

    async def _one(did: str):
        try:
            sid = sess_by_did.get(did)
            if not sid:
                return did, None
            return did, await cache.get_session_state(sid)
        except Exception:
            return did, None

    try:
        states = dict(await _aio.gather(*[_one(d) for d in dids]))
    except Exception:
        states = {}
    try:
        prelim = {d: presence_for(states.get(d), {}) for d in dids}
        codes = sorted({p["code"] for p in prelim.values() if p.get("code")})
        tags: dict[str, dict] = {}
        if codes:
            try:
                tres = supabase.table("bngc_room_tags").select(
                    "code,kind,name").in_("code", codes[:100]).execute()
                for t in (tres.data or []):
                    if t.get("code"):
                        tags[str(t["code"])] = t
            except Exception:
                pass
        for e in (entries or []):
            try:
                e["presence"] = presence_for(states.get(str(e.get("discord_id"))), tags)
            except Exception:
                e["presence"] = _offline()
    except Exception:
        for e in (entries or []):
            e.setdefault("presence", _offline())


async def _are_friends(a: str, b: str) -> bool:
    try:
        res = supabase.table("bngc_friends").select("status").or_(
            f"and(requester_discord_id.eq.{a},addressee_discord_id.eq.{b}),"
            f"and(requester_discord_id.eq.{b},addressee_discord_id.eq.{a})"
        ).eq("status", "accepted").limit(1).execute()
        return bool(res.data)
    except Exception:
        return False


@router.post("/api/party/invite")
async def party_invite(request: Request):
    err = _need_supabase()
    if err:
        return err
    b = await _body(request)
    frm, to = _ids(b.get("from_discord_id"), b.get("to_discord_id"))
    code = is_valid_code(b.get("code"))
    if not frm or not to:
        return JSONResponse({"error": "from_discord_id and to_discord_id required"}, status_code=400)
    if frm == to:
        return JSONResponse({"error": "cannot invite yourself"}, status_code=400)
    if not code:
        return JSONResponse({"error": "code must be 5 letters/digits (hold a private room first)"}, status_code=400)
    try:
        if not await _are_friends(frm, to):
            return JSONResponse({"error": "not friends"}, status_code=403)
        # refresh any live invite for this pair+code instead of stacking rows
        try:
            supabase.table("bngc_party_invites").delete().eq(
                "from_discord_id", frm).eq("to_discord_id", to).execute()
        except Exception:
            pass
        supabase.table("bngc_party_invites").insert(
            {"code": code, "from_discord_id": frm, "to_discord_id": to,
             "expires_at": invite_expiry()}).execute()
        return JSONResponse({"ok": True, "code": code, "expires_in_s": _INVITE_TTL_S})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/party/invites")
async def party_invites(request: Request):
    err = _need_supabase()
    if err:
        return err
    me = str(request.query_params.get("discord_id") or "").strip()
    if not me:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    try:
        now = datetime.now(timezone.utc)
        inc = supabase.table("bngc_party_invites").select(
            "code,from_discord_id,to_discord_id,expires_at,created_at").eq(
            "to_discord_id", me).order("created_at", desc=True).limit(25).execute()
        out = supabase.table("bngc_party_invites").select(
            "code,from_discord_id,to_discord_id,expires_at,created_at").eq(
            "from_discord_id", me).order("created_at", desc=True).limit(25).execute()
        incoming = [r for r in (inc.data or []) if is_invite_live(r, now)]
        outgoing = [r for r in (out.data or []) if is_invite_live(r, now)]
        _, names = _enrich_ids([{"requester_discord_id": r.get("from_discord_id")} for r in incoming]
                               + [{"requester_discord_id": r.get("to_discord_id")} for r in outgoing])
        for r in incoming:
            u = names.get(str(r.get("from_discord_id"))) or {}
            r["from_discord_username"] = u.get("discord_username")
            r["from_game_username"] = u.get("game_username")
        for r in outgoing:
            u = names.get(str(r.get("to_discord_id"))) or {}
            r["to_discord_username"] = u.get("discord_username")
            r["to_game_username"] = u.get("game_username")
        return JSONResponse({"incoming": incoming, "outgoing": outgoing,
                             "counts": {"incoming": len(incoming), "outgoing": len(outgoing)}})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Room tags: hosts label a private code so friends see
# "In Scrims - <mode>" / "In Tourney - <name>". Re-tagging overwrites.
# ---------------------------------------------------------------------------

@router.post("/api/room/tag")
async def room_tag(request: Request):
    err = _need_supabase()
    if err:
        return err
    b = await _body(request)
    code = is_valid_code(b.get("code"))
    kind = str(b.get("kind") or "").strip().lower()
    name = str(b.get("name") or "").strip()[:64]
    by = str(b.get("created_by") or "").strip() or None
    if not code:
        return JSONResponse({"error": "code must be 5 letters/digits"}, status_code=400)
    if kind not in ("scrim", "tourney"):
        return JSONResponse({"error": "kind must be scrim or tourney"}, status_code=400)
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    try:
        supabase.table("bngc_room_tags").upsert(
            {"code": code, "kind": kind, "name": name, "created_by": by},
            on_conflict="code").execute()
        return JSONResponse({"ok": True, "code": code, "kind": kind, "name": name})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/room/untag")
async def room_untag(request: Request):
    err = _need_supabase()
    if err:
        return err
    b = await _body(request)
    code = is_valid_code(b.get("code"))
    if not code:
        return JSONResponse({"error": "code must be 5 letters/digits"}, status_code=400)
    try:
        supabase.table("bngc_room_tags").delete().eq("code", code).execute()
        return JSONResponse({"ok": True, "code": code})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
