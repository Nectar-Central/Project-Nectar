"""
capture.py — Thin ingest that feeds the Engine and returns abstract data.

This replaces the old authoritative handler that did DB writes for kills/wins.
Now it just: validate token → engine.feed(session_id, hex) per record → return
{new_req_token, packets: [AbstractPacket], session: SessionView}

No win/kills math here — caller does it from the returned packets/session.
"""

import asyncio

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db, db_io
from ..lib.redis_client import cache
from ..lib.tokens import generate_request_token, validate_request_token, is_session_expired
from ..lib.logger import log_sys, log_err, log_ok, log_ranked, log_engine
from ..lib.photon_engine import Engine
from ..lib.alerts import create_alert

router = APIRouter()

# Global engine — one per FastAPI worker, many sessions inside
engine = Engine()

# Once-per-worker flag for the missing-Upstash warning (else it spams every batch)
_logged_no_upstash = False

# Once-per-worker flag for missing presence columns (pre-migration DBs)
_logged_no_presence_cols = False

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

# A slow/timeout DB must never turn a capture into a 500 (see capture_handler).
# The client requeues on a 5xx, but a failure AFTER the token rotation write
# makes the retry fail validation, and the client then throws its queue away
# and demands a re-link — so every DB wait here is bounded and degrades.
SESSION_LOOKUP_TIMEOUT_SEC = 6.0


async def _db_bounded(fn, *args, **kwargs):
    """db_io with a hard deadline — a hanging database must not stall a capture.

    The client's fetch gives up first, and the cancelled request surfaces as a
    500 with a CancelledError through the ASGI middleware. Every DB wait in
    this handler goes through here; callers already treat failures as
    best-effort (they log and carry on), so a timeout degrades a capture
    instead of breaking it.
    """
    return await asyncio.wait_for(db_io(fn, *args, **kwargs),
                                  timeout=SESSION_LOOKUP_TIMEOUT_SEC)


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
    # Client-known identity (userscript localStorage): breaks the null-identity
    # deadlock where join props never carried a 255 username, so view.me stays
    # None and every kill/win silently misses while playtime counts.
    body_game_username = body.get("game_username") or body.get("gameUsername")
    body_game_id = body.get("game_id") or body.get("gameId")
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "-")

    # CALL LINE — every single capture call, always visible
    try:
        _sid = str(auth_session_id or session_id or "?")[:13]
        _nrec = len(records) if isinstance(records, list) else "?"
        log_sys(f"CAPTURE CALL sid={_sid} did={str(discord_id or '?')[:8]} nrec={_nrec} ip={ip} host={int(host_mode)}")
    except Exception:
        pass

    import time as _t
    _ct = {"t0": _t.time()}  # per-stage timings (measure-first: find the ack cost)

    if not isinstance(records, list):
        return JSONResponse({"error": "records required"}, status_code=400)
    if not auth_session_id and not token:
        return JSONResponse({"error": "Token not validated"}, status_code=401)

    # Handled inline (not in a wrapper) so the deliberate 4xx returns below
    # stay untouched and the handler cannot raise out of the ASGI app.
    new_token = None
    new_counter = None
    if auth_session_id:
        try:
            res = await _db_bounded(db.get_session_sync, auth_session_id)
        except Exception as e:
            res = {"data": None, "error": f"{type(e).__name__}: {e}"[:120]}
            log_err(f"CAPTURE session lookup FAILED {auth_session_id[:13]} "
                    f"{type(e).__name__}: {str(e)[:80]} — passing through")
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
                await _db_bounded(db.update_session_sync, auth_session_id,
                                 {"is_active": False})
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
            try:
                await _db_bounded(db.update_session_sync, auth_session_id,
                                 {"current_token": new_token,
                                  "token_counter": new_counter})
            except Exception as e:
                # The token is minted and the client needs it: a 500 here would
                # cost the whole batch. Hand it over anyway — the DB counter
                # simply lags by one and self-corrects via the 401 `expected`.
                log_err(f"CAPTURE token rotate write FAILED "
                        f"{auth_session_id[:13]} {type(e).__name__}: {str(e)[:60]}"
                        f" — handing the new token to the client anyway")
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
        # RANKED/PACKET LINE — every single packet, always visible
        try:
            _pt = d.get("packet") or d.get("packet_type") or "?"
            _human = str(d.get("human") or "")[:120]
            log_sys(f"CAPTURE PKT sid={str(auth_session_id or 'anon')[:13]} seq={rec.get('seq')} dir={rec.get('dir')} type={_pt} {_human}")
        except Exception:
            pass
    _ct["feed"] = _t.time()

    # persist host_mode on SessionView (lifetime of session) — individualist vs host collab
    if auth_session_id and host_mode:
        try:
            vw = engine.get_session(auth_session_id)
            if vw:
                vw.host_mode = True
        except Exception:
            pass

    # Client identity adopt: when the engine never resolved view.me (join
    # props without a 255 username) but the client tells us who it is, adopt
    # it into the view AND heal a blank bngc_users.game_username at the
    # source. Only fills blanks — never overwrites a known identity.
    if auth_session_id and (body_game_username or body_game_id):
        try:
            vw2 = engine.get_session(auth_session_id)
            if vw2 is not None and getattr(vw2, "me", None) is None:
                _nm = str(body_game_username or "").strip()[:32] or None
                _gid = str(body_game_id or "").strip() or None
                if _nm or _gid:
                    from server.lib.photon_engine.models import Player as _Pl
                    vw2.me = _Pl(actor_nr=-1, username=_nm or "unknown",
                                 game_id=_gid, is_alive=True, is_self=True)
                    log_sys(f"CAPTURE identity adopted sid={str(auth_session_id)[:13]} "
                            f"name={_nm} gid={str(_gid)[:8] if _gid else None} via client body")
        except Exception as e:
            log_err(f"CAPTURE identity adopt failed {e}")
        try:
            if discord_id and (body_game_username or body_game_id):
                _u = await _db_bounded(
                    lambda: supabase.table("bngc_users").select(
                        "game_username,game_id").eq("discord_id", discord_id).single().execute())
                _row = _u.data if hasattr(_u, "data") else None
                _upd = {}
                if body_game_username and not ((_row or {}).get("game_username") or "").strip():
                    _upd["game_username"] = str(body_game_username).strip()[:32]
                if body_game_id and not ((_row or {}).get("game_id") or "").strip():
                    _upd["game_id"] = str(body_game_id).strip()
                if _upd:
                    await _db_bounded(
                        lambda: supabase.table("bngc_users").update(
                            _upd).eq("discord_id", discord_id).execute())
                    log_sys(f"CAPTURE identity healed did={str(discord_id)[:6]} {sorted(_upd)} via client body")
        except Exception as e:
            log_err(f"CAPTURE identity heal failed {e}")

    # Online + version stamp: EVERY batch incl. heartbeats. last_online
    # drives is-online (wagers) and /presence; companion_version records
    # the script build the player runs.
    if discord_id:
        try:
            _touch = {"last_online": _t.strftime('%Y-%m-%dT%H:%M:%SZ', _t.gmtime())}
            _cv = str(body.get("client_version") or "").strip()[:32] or None
            if _cv:
                _touch["companion_version"] = _cv
            await _db_bounded(
                lambda: supabase.table("bngc_users").update(
                    _touch).eq("discord_id", discord_id).execute())
        except Exception as e:
            _msg = str(e)
            if "last_online" in _msg or "companion_version" in _msg or "42703" in _msg:
                global _logged_no_presence_cols
                if not _logged_no_presence_cols:
                    _logged_no_presence_cols = True
                    log_err("CAPTURE presence columns missing — apply supabase/add_presence_v258.sql")
            else:
                log_err(f"CAPTURE online touch failed {e}")

    # Get session view after batch
    view = engine.get_session(auth_session_id) if auth_session_id else None
    # Presence stamp on the VIEW (not just the response dict): EVERY batch
    # counts, including heartbeat-only ones with zero packets. An idle but
    # connected lobby (sit still, wait for players) must not go stale after
    # 120s — game silence is not absence. Tab closed/reloaded stops all
    # batches so both timestamps still age out; kicks arrive as loud leave
    # events that park current off the ranked room first.
    if view is not None:
        try:
            import time as _t3
            view._cached_at = _t3.time()
        except Exception:
            pass
    session_dict = view.to_dict() if view else None

    # Run DB writes inline (awaited) — BackgroundTasks are unreliable on serverless
    # (frozen after response) and this keeps local + Vercel behavior identical.
    # Never let DB failures break the 200 response (token already rotated inline).
    # Your script (server/scripts/processor.py) uses `packets` + `session` for kills/wins.
    if auth_session_id and packets and session_dict:
        try:
            from server.scripts.processor import process_batch
            try:
                await process_batch(auth_session_id, packets, view)
                # PROCESSOR LINE — every batch result, always visible
                log_ok(f"CAPTURE→PROCESSOR OK sid={auth_session_id[:13]} n={len(packets)} players={view.players_count if view else '?'}")
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
                # RANKED LINE — every room code seen per batch, always visible
                try:
                    if code_r:
                        log_ranked(f"[ranked] capture sid={str(auth_session_id or '')[:13]} room={str(room_id_r)[:8]} code={code_r} state={getattr(getattr(room_r, 'state', None), 'value', '?')} ver={getattr(room_r, 'match_version', '?')}")
                except Exception:
                    pass
                if code_r and room_r.state.value == "match":
                    room_data = _cached_ranked_room(code_r)
                    if room_data and room_data.get("status") in ("lobby", "live"):
                        resp["ranked_room"] = {
                            "code": code_r,
                            "status": room_data.get("status"),
                            "match_version": getattr(room_r, "match_version", 0),
                            "is_host": discord_id == room_data.get("host_discord_id"),
                        }
                        try:
                            log_ranked(f"[ranked] bind sid={str(auth_session_id or '')[:13]} code={code_r} status={room_data.get('status')} ver={getattr(room_r, 'match_version', 0)} is_host={discord_id == room_data.get('host_discord_id')}")
                        except Exception:
                            pass
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
