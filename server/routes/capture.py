"""
capture.py — Thin ingest that feeds the Engine and returns abstract data.

This replaces the old authoritative handler that did DB writes for kills/wins.
Now it just: validate token → engine.feed(session_id, hex) per record → return
{new_req_token, packets: [AbstractPacket], session: SessionView}

No win/kills math here — caller does it from the returned packets/session.
"""

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db, db_io
from ..lib.redis_client import cache
from ..lib.tokens import generate_request_token, validate_request_token, is_session_expired
from ..lib.logger import log_sys, log_err, log_dbg
from ..lib.photon_engine import Engine
from ..lib.alerts import create_alert

router = APIRouter()

# Global engine — one per FastAPI worker, many sessions inside
engine = Engine()

# Once-per-worker flag for the missing-Upstash warning (else it spams every batch)
_logged_no_upstash = False

# Bandwidth guard: ranked room lookup per capture batch was a Supabase
# RT every few seconds per player. Cache 30s per short_code.
_ranked_room_cache: dict = {}
_RANKED_ROOM_TTL_S = 30.0


def _cached_ranked_room(code_r):
    try:
        import sys as _sys
        if "pytest" not in _sys.modules:
            import time as _t
            now = _t.time()
            hit = _ranked_room_cache.get(str(code_r).upper())
            if hit and now < hit[0]:
                return hit[1]
        else:
            import time as _t
            now = _t.time()
        from server.lib import ranked as ranked_mod
        room_data = ranked_mod.get_ranked_room(code_r)
        try:
            if len(_ranked_room_cache) > 500:
                _ranked_room_cache.clear()
            _ranked_room_cache[str(code_r).upper()] = (now + _RANKED_ROOM_TTL_S, room_data)
        except Exception:
            pass
        return room_data
    except Exception:
        try:
            from server.lib import ranked as ranked_mod
            return ranked_mod.get_ranked_room(code_r)
        except Exception:
            return None

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

    import time as _t
    _ct = {"t0": _t.time()}  # per-stage timings (measure-first: find the ack cost)

    if not isinstance(records, list):
        return JSONResponse({"error": "records required"}, status_code=400)
    if not auth_session_id and not token:
        return JSONResponse({"error": "Token not validated"}, status_code=401)

    # Token validation (same as before, but no replay 401 — just validate window)
    new_token = None
    new_counter = None
    if auth_session_id:
        res = await db_io(db.get_session_sync, auth_session_id)
        sess = res.get("data")
        db_err = res.get("error")
        # DB connection blip ("Server disconnected"): the session probably
        # still exists — skip rotation but DON'T 401. The client retries
        # with the same token on the next batch; if the session was truly
        # deleted the next call will see data=None without an error and
        # return 401 then.
        if db_err and not sess:
            log_err(f"CAPTURE session lookup DB error ({db_err[:40]}) — passing through")
            sess = None  # fall through to the token-free path below
        elif not sess:
            return JSONResponse({"error": "Invalid auth session"}, status_code=401)
        if sess and is_session_expired(sess):
            try:
                await db_io(db.update_session_sync, auth_session_id, {"is_active": False})
            except Exception:
                pass
            log_sys(f"CAPTURE expired session {auth_session_id[:13]} — client must re-link")
            return JSONResponse({"error": "Session expired - please re-link"}, status_code=401)
        if sess and sess.get("is_locked"):
            scope = str(sess.get("lock_scope") or ("admin" if sess.get("is_admin_locked") else "default")).lower()
            is_admin = scope == "admin" or bool(sess.get("is_admin_locked"))
            try:
                create_alert(sess.get("discord_id") or discord_id, "locked", request,
                    details={"reason": sess.get("lock_reason") or "capture while locked", "ip": ip, "scope": scope}, severity="critical")
            except Exception:
                pass
            msg = "Account admin-locked" if is_admin else "Account locked"
            return JSONResponse({"error": msg, "lock_scope": scope, "is_admin_locked": is_admin}, status_code=403)
        if sess and req_token:
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
            await db_io(db.update_session_sync, auth_session_id, {"current_token": new_token, "token_counter": new_counter})
            log_sys(f"CAPTURE token ok auth={str(auth_session_id)[:8]} req={req_token} → new {new_token} counter {sess['token_counter']}->{new_counter}")
    _ct["tok"] = _t.time()

    # Serverless: restore engine state from Upstash when this worker has none.
    # Without this, every fresh lambda starts from a blank session (rooms/players/
    # FSM rebuilt per batch in isolation) — the states bug seen on Vercel.
    if auth_session_id and engine.get_session(auth_session_id) is None:
        if not cache.is_configured():
            global _logged_no_upstash
            if not _logged_no_upstash:
                _logged_no_upstash = True
                log_err("CAPTURE Upstash NOT configured — engine state cannot survive across serverless instances (set UPSTASH_REDIS_REST_URL/TOKEN)")
        try:
            cached = await cache.get_session_state(auth_session_id)
            if cached:
                from ..lib.photon_engine.models import SessionView
                restored = SessionView.from_dict(cached)
                if restored is not None:
                    engine.put_session(auth_session_id, restored)
                    log_sys(f"CAPTURE restored engine state for {auth_session_id[:13]} rooms={len(restored.rooms)}")
        except Exception as e:
            log_err(f"CAPTURE restore failed {auth_session_id[:13]} {e}")
    _ct["restore"] = _t.time()

    # Feed engine per session — no DB writes, just abstract data
    # Build one packet dict PER record (processor needs every packet, not just the last)
    packets = []
    for rec in records:
        hex_str = rec.get("rawHex") or ""
        if not hex_str:
            # heartbeat / empty slot ({hb:1}): keeps the session warm
            # (_cached_at slides, stats/presence stay fresh) but never
            # reaches engine.feed or the processor - zero gameplay effect.
            continue
        # engine.feed does decrypt + update SessionView for this session_id.
        # Direction ("out" = own client) powers identity healing: an outbound
        # killfeed echo adopts view.me when joins never carried a username.
        pkt = engine.feed(auth_session_id or "anon", hex_str, rec.get("dir"))
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
    _ct["feed"] = _t.time()

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
    if session_dict:
        try:
            import time as _t
            session_dict["_cached_at"] = _t.time()  # stats expiry for kicked/closed clients
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
                log_dbg(f"CAPTURE processed {auth_session_id[:13]} {len(packets)} packets session players={view.players_count if view else '?'}")
            except Exception as e:
                log_err(f"PROCESSOR FAILED session={auth_session_id[:13]} {type(e).__name__}: {e}")
        except Exception as e:
            log_err(f"CAPTURE processor import failed {e} — packets still returned, DB write skipped")
    elif auth_session_id and packets:
        # Gameplay packets that will NEVER reach the processor (no view):
        # this is the silent-drop hole — log it loud.
        log_err(f"CAPTURE skip processor session={auth_session_id[:13]} pkts={len(packets)} view={'none' if view is None else 'no-dict'} — DB write skipped")
    _ct["proc"] = _t.time()

    # Persist Engine state to Upstash ONCE, after the processor (finally
    # semantics): it mutates flags/markers/counters/winner that must survive
    # for the next lambda (Vercel serverless) — without this the next lambda
    # restores stale markers and re-records defeats/wins (doubles). A second
    # persist before the processor only doubles the biggest cost of this
    # endpoint (full session serialize + REST) for no durability gain.
    if auth_session_id and view is not None:
        try:
            import time as _t2
            _final = view.to_dict()
            _final["_cached_at"] = _t2.time()
            _final_ok = await cache.set_session_state(auth_session_id, _final, ttl=3600)
            if not _final_ok:
                log_err(f"CAPTURE persist FAILED for {auth_session_id[:13]} — next lambda starts blank")
        except Exception:
            pass
    _ct["persist"] = _t.time()

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

    # Ranked bind for live UI: which ranked room (if any) this room code
    # belongs to. The client renders optimistic deltas locally (+8 kill /
    # +50 win / -5 loss) and corrects from this + /api/stats — it never
    # polls ranked endpoints. No point rules: both sides hardcode 8/50/-5.
    if view and discord_id:
        try:
            for room_id_r, room_r in view.rooms.items():
                code_r = getattr(room_r, "short_code", None)
                if code_r and room_r.state.value == "match":
                    room_data = _cached_ranked_room(code_r)
                    if room_data and room_data.get("status") in ("lobby", "live"):
                        resp["ranked_room"] = {
                            "code": code_r,
                            "status": room_data.get("status"),
                            "match_version": getattr(room_r, "match_version", 0),
                            "is_host": discord_id == room_data.get("host_discord_id"),
                        }
                        break
        except Exception:
            pass

    resp["packets"] = packets
    if session_dict:
        resp["session"] = session_dict

    # Per-stage timings (measure-first): one line per batch with packets.
    # t0->tok covers body parse + session/token DB + rotation; the rest is
    # labeled below. Skipped stages read 0; the next present stage measures
    # from the last present stamp (carry-forward, no absolute leakage).
    if packets:
        try:
            _parts: dict = {}
            _last = _ct.get("t0") or 0
            for _k in ("tok", "restore", "feed", "proc", "persist"):
                _v = _ct.get(_k)
                if _v:
                    _parts[_k] = int((_v - _last) * 1000)
                    _last = _v
                else:
                    _parts[_k] = 0
            _tend = _t.time()
            log_sys(f"CAPTURE times n={len(packets)} total={int((_tend - (_ct.get('t0') or _tend)) * 1000)}ms "
                    f"tok={_parts['tok']} restore={_parts['restore']} feed={_parts['feed']} "
                    f"proc={_parts['proc']} persist={_parts['persist']} resp={int((_tend - _last) * 1000)}ms")
        except Exception:
            pass

    # If we have a token, always return 200 with token; else 200 with an
    # empty object — never 204: the {} body is still serialized while the
    # status forbids one, and uvicorn 500s ("Response content longer than
    # Content-Length"). Heartbeats land here every 5s, so this must not 500.
    if new_token:
        return JSONResponse(resp)
    if packets:
        return JSONResponse(resp)
    return JSONResponse({"ok": True})
