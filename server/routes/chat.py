"""
chat.py — friend DMs (friends-only, accepted bngc_friends row required).

  POST /api/friends/message {from_discord_id, to_discord_id, body(1..500)}
  GET  /api/friends/messages?discord_id=&with=&limit=50[&before_id=]
         ascending, marks peer messages as read
  GET  /api/friends/unread?discord_id=  {total, by_friend:{did: n}}

No edit/delete in v1. Same discord_id-keyed trust as friends.py.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase
from .friends import _are_friends, _body, _ids, _need_supabase

router = APIRouter()

_MSG_LIMIT_MAX = 50
_BODY_MAX = 500


def clean_body(body) -> str | None:
    """Trimmed message or None when empty/too long. Pure, unit-tested."""
    try:
        s = str(body or "").strip()
    except Exception:
        return None
    if not s or len(s) > _BODY_MAX:
        return None
    return s


@router.post("/api/friends/message")
async def send_message(request: Request):
    err = _need_supabase()
    if err:
        return err
    b = await _body(request)
    frm, to = _ids(b.get("from_discord_id"), b.get("to_discord_id"))
    text = clean_body(b.get("body"))
    if not frm or not to:
        return JSONResponse({"error": "from_discord_id and to_discord_id required"}, status_code=400)
    if frm == to:
        return JSONResponse({"error": "cannot message yourself"}, status_code=400)
    if text is None:
        return JSONResponse({"error": f"body required (1..{_BODY_MAX} chars)"}, status_code=400)
    try:
        if not await _are_friends(frm, to):
            return JSONResponse({"error": "not friends"}, status_code=403)
        res = supabase.table("bngc_messages").insert(
            {"from_discord_id": frm, "to_discord_id": to, "body": text}).execute()
        row = (res.data or [{}])[0]
        return JSONResponse({"ok": True, "id": row.get("id"), "created_at": row.get("created_at")})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/friends/messages")
async def get_messages(request: Request):
    err = _need_supabase()
    if err:
        return err
    qp = request.query_params
    me, peer = _ids(qp.get("discord_id"), qp.get("with"))
    if not me or not peer:
        return JSONResponse({"error": "discord_id and with required"}, status_code=400)
    try:
        limit = max(1, min(int(qp.get("limit") or str(_MSG_LIMIT_MAX)), _MSG_LIMIT_MAX))
    except Exception:
        limit = _MSG_LIMIT_MAX
    try:
        before = qp.get("before_id")
        before_id = int(before) if before else None
    except Exception:
        before_id = None
    try:
        if not await _are_friends(me, peer):
            return JSONResponse({"error": "not friends"}, status_code=403)
        q = supabase.table("bngc_messages").select(
            "id,from_discord_id,to_discord_id,body,is_read,created_at").or_(
            f"and(from_discord_id.eq.{me},to_discord_id.eq.{peer}),"
            f"and(from_discord_id.eq.{peer},to_discord_id.eq.{me})")
        if before_id:
            q = q.lt("id", before_id)
        res = q.order("id", desc=True).limit(limit).execute()
        rows = sorted(res.data or [], key=lambda r: r.get("id") or 0)
        # mark peer messages as read (fire-and-forget)
        try:
            supabase.table("bngc_messages").update({"is_read": True}).eq(
                "to_discord_id", me).eq("from_discord_id", peer).eq("is_read", False).execute()
        except Exception:
            pass
        return JSONResponse({"count": len(rows), "messages": rows})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/friends/unread")
async def get_unread(request: Request):
    err = _need_supabase()
    if err:
        return err
    me = str(request.query_params.get("discord_id") or "").strip()
    if not me:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    try:
        res = supabase.table("bngc_messages").select("from_discord_id").eq(
            "to_discord_id", me).eq("is_read", False).limit(500).execute()
        by_friend: dict[str, int] = {}
        for r in (res.data or []):
            f = str(r.get("from_discord_id") or "")
            if f:
                by_friend[f] = by_friend.get(f, 0) + 1
        return JSONResponse({"total": sum(by_friend.values()), "by_friend": by_friend})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
