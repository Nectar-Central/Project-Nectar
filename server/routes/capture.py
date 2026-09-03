"""
capture.py — Thin ingest that feeds the Engine and returns abstract data.

This replaces the old authoritative handler that did DB writes for kills/wins.
Now it just: validate token → engine.feed(session_id, hex) per record → return
{new_req_token, packets: [AbstractPacket], session: SessionView}

No win/kills math here — caller does it from the returned packets/session.
"""

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db
from ..lib.redis_client import cache
from ..lib.tokens import generate_request_token, validate_request_token, is_session_expired
from ..lib.logger import log_sys, log_err
from ..lib.photon_engine import Engine
from ..lib.alerts import create_alert

router = APIRouter()

# Global engine — one per FastAPI worker, many sessions inside
engine = Engine()

@router.post("/api/capture")
async def capture_handler(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    session_id = body.get("sessionId")
    records = body.get("records")
    auth_session_id = body.get("authSessionId") or body.get("auth_session_id")
    req_token = body.get("req_token")
    discord_id = body.get("discord_id")
    token = body.get("token")
    host_mode = bool(body.get("hostMode") or body.get("host_mode"))
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "-")

    if not isinstance(records, list):
        return JSONResponse({"error": "records required"}, status_code=400)
    if not auth_session_id and not token:
        return JSONResponse({"error": "Token not validated"}, status_code=401)

    # Token validation (same as before, but no replay 401 — just validate window)
    new_token = None
    new_counter = None
    if auth_session_id:
        res = db.get_session_sync(auth_session_id)
        sess = res.get("data")
        if not sess:
            return JSONResponse({"error": "Invalid auth session"}, status_code=401)
        if is_session_expired(sess):
            try:
                db.update_session_sync(auth_session_id, {"is_active": False})
            except Exception:
                pass
            log_sys(f"CAPTURE expired session {auth_session_id[:8]} — client must re-link")
            return JSONResponse({"error": "Session expired - please re-link"}, status_code=401)
        if sess.get("is_locked"):
            scope = str(sess.get("lock_scope") or ("admin" if sess.get("is_admin_locked") else "default")).lower()
            is_admin = scope == "admin" or bool(sess.get("is_admin_locked"))
            try:
                create_alert(sess.get("discord_id") or discord_id, "locked", request,
                    details={"reason": sess.get("lock_reason") or "capture while locked", "ip": ip, "scope": scope}, severity="critical")
            except Exception:
                pass
            msg = "Account admin-locked" if is_admin else "Account locked"
            return JSONResponse({"error": msg, "lock_scope": scope, "is_admin_locked": is_admin}, status_code=403)
        if req_token:
            if sess.get("discord_id") != discord_id:
                return JSONResponse({"error": "Session mismatch"}, status_code=403)
            v = validate_request_token(sess["req_secret"], req_token, sess["token_counter"])
            if not v["valid"] and req_token == sess["current_token"]:
                v = {"valid": True, "matched_counter": sess["token_counter"]}
            if not v["valid"]:
                exp = generate_request_token(sess["req_secret"], sess["token_counter"] + 1)
                return JSONResponse({"error": "Invalid req_token", "expected": exp, "current": sess["current_token"]}, status_code=401)
            await cache.blacklist_token(req_token, 60)
            base = v.get("matched_counter", sess["token_counter"])
            if base < sess["token_counter"]:
                base = sess["token_counter"]
            new_counter = base + 1
            new_token = generate_request_token(sess["req_secret"], new_counter)
            db.update_session_sync(auth_session_id, {"current_token": new_token, "token_counter": new_counter})
            log_sys(f"CAPTURE token ok auth={str(auth_session_id)[:8]} req={req_token} → new {new_token} counter {sess['token_counter']}->{new_counter}")

    # Serverless: restore engine state from Upstash when this worker has none.
    # Without this, every fresh lambda starts from a blank session (rooms/players/
    # FSM rebuilt per batch in isolation) — the states bug seen on Vercel.
    if auth_session_id and engine.get_session(auth_session_id) is None:
        try:
            cached = await cache.get_session_state(auth_session_id)
            if cached:
                from ..lib.photon_engine.models import SessionView
                restored = SessionView.from_dict(cached)
                if restored is not None:
                    engine.put_session(auth_session_id, restored)
                    log_sys(f"CAPTURE restored engine state for {auth_session_id[:8]} rooms={len(restored.rooms)}")
        except Exception as e:
            log_err(f"CAPTURE restore failed {auth_session_id[:8]} {e}")

    # Feed engine per session — no DB writes, just abstract data
    # Build one packet dict PER record (processor needs every packet, not just the last)
    packets = []
    for rec in records:
        hex_str = rec.get("rawHex") or ""
        if not hex_str:
            continue
        # engine.feed does decrypt + update SessionView for this session_id
        pkt = engine.feed(auth_session_id or "anon", hex_str)
        # Attach seq/dir/timestamp from record + full decrypted gpb (same field names as server/logs/*.decrypted.json)
        d = pkt.to_dict() if hasattr(pkt, "to_dict") else {}
        d["seq"] = rec.get("seq")
        d["dir"] = rec.get("dir")
        d["t"] = rec.get("t")
        d["rawHex"] = hex_str  # keep original hex
        # decrypted gpb with same names: kind, opName/evName, gm, curScn, Party, killfeed, properties, playerProps, params, raw
        try:
            d["decrypted_hex"] = pkt.raw  # gpb dict from parse_gpbinary (not stripped)
            # also add human sentence for readability
            from server.lib.gpbinary import human_sentence
            d["human"] = human_sentence(pkt.raw)
        except Exception:
            pass
        packets.append(d)

    # persist host_mode on SessionView (lifetime of session) — individualist vs host collab
    if auth_session_id and host_mode:
        try:
            vw = engine.get_session(auth_session_id)
            if vw:
                vw.host_mode = True
        except Exception:
            pass

    # Get session view after batch
    view = engine.get_session(auth_session_id) if auth_session_id else None
    session_dict = view.to_dict() if view else None

    # Persist Engine state to Upstash so any lambda can serve currentmatch (Vercel serverless)
    if auth_session_id and session_dict:
        try:
            await cache.set_session_state(auth_session_id, session_dict, ttl=3600)
        except Exception:
            pass

    # Run DB writes inline (awaited) — BackgroundTasks are unreliable on serverless
    # (frozen after response) and this keeps local + Vercel behavior identical.
    # Never let DB failures break the 200 response (token already rotated inline).
    # Your script (server/scripts/processor.py) uses `packets` + `session` for kills/wins.
    if auth_session_id and packets and session_dict:
        try:
            from server.scripts.processor import process_batch
            try:
                await process_batch(auth_session_id, packets, view)
                log_sys(f"CAPTURE processed {auth_session_id[:8]} {len(packets)} packets session players={view.players_count if view else '?'}")
            except Exception as e:
                log_err(f"PROCESSOR FAILED session={auth_session_id[:8]} {type(e).__name__}: {e}")
        except Exception as e:
            log_err(f"CAPTURE processor import failed {e} — packets still returned, DB write skipped")

    # Also handle username/game_id change via engine's view.me
    # If view.me changed, include in response so userscript can update GM storage
    resp = {}
    if new_token:
        resp["new_req_token"] = new_token
        resp["new_counter"] = new_counter
    if view and view.me:
        # Only send if me was set (first join)
        resp["game_username"] = view.me.username
        resp["game_id"] = view.me.game_id

    resp["packets"] = packets
    if session_dict:
        resp["session"] = session_dict

    # If we have a token, always return 200 with token; else 204
    if new_token:
        return JSONResponse(resp)
    if packets:
        return JSONResponse(resp)
    return JSONResponse({}, status_code=204)
