"""
auth.py — Discord OAuth for the Nectar dashboard.

Flow:
    GET  /auth/discord      -> redirect to Discord consent screen
    GET  /auth/callback     -> exchange code, create a server session, set cookie
    GET  /auth/me           -> who am I + which guilds may I manage
    POST /auth/logout       -> drop the session

The browser only ever holds an opaque `nectar_session` cookie (httpOnly).
The Discord access token is used once, in the callback, and thrown away —
we never store anyone's Discord token.
"""

from __future__ import annotations

import os
import secrets
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..lib import guilds as guilds_mod
from ..lib.logger import log_err, log_sys

router = APIRouter()

COOKIE_NAME = "nectar_session"

# Short-lived CSRF token for the OAuth round trip. Held in a cookie rather than
# server memory because the deployed API is serverless: the login redirect and
# the callback can land on two different worker processes, so anything kept in
# a module-level dict would be missing half the time.
STATE_COOKIE = "nectar_oauth_state"
STATE_TTL = 600

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
# Where Discord sends the browser back. Must be registered verbatim in the
# Discord developer portal -> OAuth2 -> Redirects.
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "").strip()
DASHBOARD_URL = os.getenv("NECTAR_DASHBOARD_URL", "").strip()


def _api_base(request: Request) -> str:
    """Public origin of this API (behind Render/Vercel proxies)."""
    try:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if host:
            return f"{proto}://{host}".rstrip("/")
    except Exception:
        pass
    return str(request.base_url).rstrip("/")


def _redirect_uri(request: Request) -> str:
    if REDIRECT_URI:
        return REDIRECT_URI
    return f"{_api_base(request)}/auth/callback"


def _dashboard_base(request: Request) -> str:
    """Where the dashboard lives.

    Same-origin in production (FastAPI serves the built SPA), so this is
    usually just the API origin. NECTAR_DASHBOARD_URL overrides it for the
    split Vite-dev-server case.
    """
    return DASHBOARD_URL or _api_base(request)


def _is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _bounce(dashboard: str, error: str | None = None) -> RedirectResponse:
    """Redirect back to the dashboard and drop the one-shot state cookie."""
    url = f"{dashboard}/login?error={error}" if error else f"{dashboard}/dashboard"
    resp = RedirectResponse(url)
    resp.delete_cookie(STATE_COOKIE, path="/auth")
    return resp


def _set_cookie(resp, token: str, max_age: int, secure: bool) -> None:
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=max_age,
        httponly=True,
        samesite="lax",   # lax, not strict: Discord redirects in from another origin
        secure=secure,
        path="/",
    )


@router.get("/auth/discord")
async def discord_login(request: Request):
    """Send the browser to Discord's consent screen."""
    if not CLIENT_ID or not CLIENT_SECRET:
        return JSONResponse(
            {"error": "Discord OAuth is not configured on this server",
             "hint": "Set DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET"},
            status_code=503,
        )
    state = secrets.token_urlsafe(24)

    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
        "prompt": "consent",
    })
    resp = RedirectResponse(f"https://discord.com/oauth2/authorize?{params}")
    resp.set_cookie(
        STATE_COOKIE, state,
        max_age=STATE_TTL,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/auth",  # only ever needed by the callback
    )
    return resp


@router.get("/auth/callback")
async def discord_callback(request: Request, code: str = "", state: str = ""):
    """Exchange the code, mint a session, bounce to the dashboard."""
    dashboard = _dashboard_base(request)

    if not code:
        return _bounce(dashboard, "cancelled")

    expected = request.cookies.get(STATE_COOKIE) or ""
    # Constant-time compare: the value is a secret even though it is short-lived.
    if not state or not expected or not secrets.compare_digest(state, expected):
        return _bounce(dashboard, "state")

    try:
        import httpx
    except Exception as e:  # pragma: no cover - httpx ships with supabase
        log_err(f"AUTH httpx missing: {e}")
        return _bounce(dashboard, "server")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            tok_res = await client.post(
                "https://discord.com/api/v10/oauth2/token",
                data={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _redirect_uri(request),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if tok_res.status_code != 200:
                log_err(f"AUTH token exchange failed {tok_res.status_code}: {tok_res.text[:160]}")
                return _bounce(dashboard, "exchange")
            access = (tok_res.json() or {}).get("access_token")
            if not access:
                return _bounce(dashboard, "exchange")

            me_res = await client.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bearer {access}"},
            )
            if me_res.status_code != 200:
                return _bounce(dashboard, "discord")
            user = me_res.json() or {}

        discord_id = str(user.get("id") or "")
        if not discord_id:
            return _bounce(dashboard, "discord")

        username = user.get("global_name") or user.get("username") or discord_id
        avatar = None
        if user.get("avatar"):
            avatar = (f"https://cdn.discordapp.com/avatars/{discord_id}/"
                      f"{user['avatar']}.png?size=128")

        token = await guilds_mod.create_session(discord_id, username, avatar)
        if not token:
            return _bounce(dashboard, "server")

        log_sys(f"AUTH login did={discord_id[:8]} user={username}")
        resp = _bounce(dashboard)
        _set_cookie(resp, token, guilds_mod.SESSION_TTL_DAYS * 86400, _is_https(request))
        return resp
    except Exception as e:
        log_err(f"AUTH callback failed: {e}")
        return _bounce(dashboard, "server")


@router.get("/auth/me")
async def auth_me(request: Request):
    """Current dashboard identity + manageable guilds. 401 when not logged in."""
    token = request.cookies.get(COOKIE_NAME)
    session = await guilds_mod.get_session(token) if token else None
    if not session:
        return JSONResponse({"error": "not logged in"}, status_code=401)
    await guilds_mod.touch_session(token)
    discord_id = str(session.get("discord_id") or "")
    return {
        "user": {
            "id": discord_id,
            "username": session.get("discord_username"),
            "avatar_url": session.get("avatar_url"),
        },
        "guilds": await guilds_mod.list_user_guilds(discord_id),
    }


@router.post("/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        await guilds_mod.delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
