"""
validate_token.py — One-time login token → session + first request token.

Flow:
    1. Client posts {token: "A1B2C3D4..."} from /register
    2. Server checks bngc_login_tokens, marks used, creates bngc_sessions
    3. Returns {session_id, initial_token, discord_id, ...}
    4. Client stores session_id + initial_token, never reuses login token

This mirrors app/api/validate-token.js but in clean Python.
"""

import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase, db
from ..lib.redis_client import cache
from ..lib.tokens import generate_request_token, is_session_expired
from ..lib.logger import log_sys, log_ok, log_err
from ..lib.alerts import create_alert

router = APIRouter()


@router.post("/api/validate-token")
@router.post("/api/auth/verify")  # Alias for Vercel
async def validate_token(request: Request):
    """
    Validate a one-time login token and issue a session.

    The login token is single-use. On success we create a
    bngc_sessions row with a random req_secret and initial
    request token (counter 0). The secret never leaves the server.
    """

    # -------------------------------------------------------------------
    # 1. Basic validation
    # -------------------------------------------------------------------

    body = await request.json()
    token = body.get("token")

    if not token:
        return JSONResponse({"error": "Missing token"}, status_code=400)

    # Client IP for audit logs
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not ip:
        ip = request.client.host if request.client else "0.0.0.0"

    log_sys(f"VALIDATE token={str(token)[:4]}… ip={ip}")

    # -------------------------------------------------------------------
    # 2. Check blacklist (prevent reuse)
    # -------------------------------------------------------------------

    if await cache.is_token_blacklisted(token):
        log_err(f"validate reuse {str(token)[:4]}…")
        # blacklist alert if we can resolve discord_id via token lookup
        try:
            _lt = supabase.table("bngc_login_tokens").select("discord_id").eq("token", token).single().execute().data if supabase else None
            if _lt and _lt.get("discord_id"):
                create_alert(_lt["discord_id"], "blacklist", request, details={"reason": "blacklisted token reuse", "token_prefix": str(token)[:4]}, severity="critical")
        except Exception:
            pass
        return JSONResponse(
            {"error": "Token already used - account locked. Use /unlock to unlock."},
            status_code=403,
        )

    # -------------------------------------------------------------------
    # 3. Look up login token
    # -------------------------------------------------------------------

    if not supabase:
        return JSONResponse({"error": "Server not configured (no Supabase)"}, status_code=500)

    try:
        # Try bngc_login_tokens first
        res = supabase.table("bngc_login_tokens").select("*").eq("token", token).single().execute()
        login_token = res.data
    except Exception:
        login_token = None

    # -------------------------------------------------------------------
    # 4. If it's a login token (from /register)
    # -------------------------------------------------------------------

    if login_token:
        # Already used? Check for idempotent reuse
        session_expired_refresh = False
        if login_token.get("used"):
            # If there's already an active, fresh session for this discord_id, return it
            # instead of locking — prevents userscript re-validate loops
            try:
                existing = (
                    supabase.table("bngc_sessions")
                    .select("session_id, current_token, created_at")
                    .eq("discord_id", login_token["discord_id"])
                    .eq("is_active", True)
                    .order("created_at", desc=True)
                    .limit(1)
                    .single()
                    .execute()
                )
                if existing.data and not is_session_expired(existing.data):
                    sess = existing.data
                    log_sys(
                        f"VALIDATE reuse but active session exists {str(sess['session_id'])[:8]} — returning existing"
                    )

                    # Fetch user for extra fields
                    try:
                        user_res = supabase.table("bngc_users").select("*").eq("discord_id", login_token["discord_id"]).single().execute()
                        user = user_res.data
                    except Exception:
                        user = None

                    return JSONResponse({
                        "valid": True,
                        "discord_username": (user.get("discord_username") if user else login_token["discord_id"]),
                        "discord_id": login_token["discord_id"],
                        "first_seen": not (user.get("game_username") if user else False),
                        "game_username": user.get("game_username") if user else None,
                        "game_id": user.get("game_id") if user else None,
                        "session_id": sess["session_id"],
                        "initial_token": sess["current_token"],
                        "reused": True,
                    })
                if existing.data:
                    # Active session is older than 2 days — deactivate it and fall through
                    # to fresh creation below (refresh, NOT a lock)
                    try:
                        supabase.table("bngc_sessions").update({"is_active": False}).eq("session_id", existing.data["session_id"]).execute()
                    except Exception:
                        pass
                    log_sys(f"VALIDATE refresh expired session {str(existing.data['session_id'])[:8]} — issuing new")
                    session_expired_refresh = True
            except Exception:
                pass

            if session_expired_refresh:
                pass  # fall through to fresh creation below (refresh, NOT a lock)
            else:
                # Otherwise, lock the account (real reuse) — friend-account enforcement (default lock, user can /unlock)
                try:
                    supabase.table("bngc_users").update({
                        "is_locked": True,
                        "lock_reason": "Login token reuse (friend account)",
                        "lock_scope": "default",
                        "is_admin_locked": False,
                    }).eq("discord_id", login_token["discord_id"]).execute()
                except Exception:
                    pass

                await cache.blacklist_token(token, 300)
                create_alert(login_token["discord_id"], "locked", request, details={"reason": "token reuse — possible friend account sharing", "token_prefix": str(token)[:4], "ip": ip, "scope": "default"}, severity="critical")
                create_alert(login_token["discord_id"], "blacklist", request, details={"reason": "token reuse", "scope": "default"}, severity="critical")

                return JSONResponse(
                    {"error": "Token already used - account locked. Use /unlock to unlock.", "lock_scope": "default", "is_admin_locked": False},
                    status_code=403,
                )

        # Check expiry (skipped on session refresh — the 2-day session TTL governs, not the login token)
        if not session_expired_refresh:
            try:
                expires = login_token.get("expires_at")
                if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                    return JSONResponse({"error": "Token expired"}, status_code=401)
            except Exception:
                pass

        # Mark as used
        try:
            supabase.table("bngc_login_tokens").update({
                "used": True,
                "used_at": datetime.now(timezone.utc).isoformat(),
                "used_ip": ip,
            }).eq("token", token).execute()
        except Exception as e:
            log_err(f"failed to mark login token used: {e}")

        # Store login token on user for legacy fallback
        try:
            supabase.table("bngc_users").update({
                "token": token,
                "first_seen": False,
            }).eq("discord_id", login_token["discord_id"]).execute()
        except Exception:
            pass

        # Create session
        session_id = f"sess_{int(datetime.now().timestamp() * 1000):x}_{secrets.token_hex(4)}"
        req_secret = secrets.token_hex(16)  # 32 hex chars, 16 bytes
        initial_token = generate_request_token(req_secret, 0)

        try:
            supabase.table("bngc_sessions").insert({
                "session_id": session_id,
                "discord_id": login_token["discord_id"],
                "session_key": req_secret,
                "req_secret": req_secret,
                "current_token": initial_token,
                "token_counter": 0,
                "is_active": True,
            }).execute()
        except Exception as e:
            log_err(f"failed to create session: {e}")
            return JSONResponse({"error": "Failed to create session"}, status_code=500)

        log_ok(f"VALIDATE OK {login_token['discord_id']} token={str(token)[:4]}… session={session_id[:13]} issued")

        # Fetch user for response
        try:
            user_res = supabase.table("bngc_users").select("*").eq("discord_id", login_token["discord_id"]).single().execute()
            user = user_res.data
        except Exception:
            user = None

        # Login alert DM (country + username + game_id)
        try:
            create_alert(login_token["discord_id"], "login", request,
                game_username=user.get("game_username") if user else None,
                game_id=user.get("game_id") if user else None,
                discord_username=user.get("discord_username") if user else None,
                details={"ip": ip, "session_id": session_id},
                severity="info")
        except Exception:
            pass

        return JSONResponse({
            "valid": True,
            "discord_username": (user.get("discord_username") if user else login_token["discord_id"]),
            "discord_id": login_token["discord_id"],
            "first_seen": not (user.get("game_username") if user else False),
            "game_username": user.get("game_username") if user else None,
            "game_id": user.get("game_id") if user else None,
            "session_id": session_id,
            "initial_token": initial_token,
        })

    # -------------------------------------------------------------------
    # 5. Fallback: check bngc_users.token (legacy)
    # -------------------------------------------------------------------

    try:
        user_res = supabase.table("bngc_users").select("*").eq("token", token).single().execute()
        user = user_res.data
    except Exception:
        user = None

    if not user:
        return JSONResponse({"error": "token incorrect"}, status_code=401)

    # Check if login token version is used
    try:
        lt2 = supabase.table("bngc_login_tokens").select("*").eq("token", token).single().execute().data
        if lt2 and lt2.get("used"):
            return JSONResponse({"error": "Token already used - account locked. Use /unlock to unlock."}, status_code=403)
    except Exception:
        pass

    if user.get("is_locked"):
        scope = str(user.get("lock_scope") or ("admin" if user.get("is_admin_locked") else "default")).lower()
        is_admin = scope == "admin" or bool(user.get("is_admin_locked"))
        create_alert(user["discord_id"], "locked", request,
            game_username=user.get("game_username"), game_id=user.get("game_id"),
            discord_username=user.get("discord_username"),
            details={"reason": user.get("lock_reason") or "account locked", "ip": ip, "scope": scope}, severity="critical")
        msg = "Account admin-locked. Ask an admin to run /adminunlock." if is_admin else "Account locked. Use /unlock to unlock."
        return JSONResponse({"error": msg, "lock_scope": scope, "is_admin_locked": is_admin}, status_code=403)

    return JSONResponse({
        "valid": True,
        "discord_username": user.get("discord_username"),
        "discord_id": user.get("discord_id"),
        "first_seen": bool(user.get("first_seen")),
        "game_username": user.get("game_username"),
        "game_id": user.get("game_id"),
    })
