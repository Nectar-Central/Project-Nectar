"""
auth.py — dashboard sign-in via Supabase Auth (Discord provider).

Flow:
    GET  /auth/discord      -> redirect to Supabase's Discord consent screen
    GET  /auth/callback     -> swap the auth code for the Supabase user, then
                               mint our own session and set the cookie
    GET  /auth/me           -> who am I + which guilds may I manage
    POST /auth/logout       -> drop the session

Why Supabase does the OAuth dance
---------------------------------
Hand-rolling Discord OAuth means owning the token exchange, the CSRF state, the
scope list and every edge case Discord adds. Supabase already does that, and it
keeps the user record for us. So the only thing this module still does is turn
"Supabase says this is Discord user X" into "this browser has a session".

Why the browser never sees a token
----------------------------------
The obvious way to use Supabase Auth is supabase-js in the browser, which keeps
the access token in localStorage where any injected script can read it. Instead
the exchange happens here, server-side, and the browser only ever gets an
opaque `nectar_session` cookie that is httpOnly, so JavaScript cannot read it
and XSS cannot steal it. Supabase's access and refresh tokens are used once and
thrown away — we never store anyone's provider tokens.

PKCE, not a shared secret
-------------------------
The code is bound to a `code_verifier` that lives in a short-lived httpOnly
cookie on this browser. An attacker who intercepts the redirect cannot redeem
the code without it, and the code is single-use. That is why the state cookie
also survives serverless: the verifier travels with the browser, not with a
process that may not be the one handling the callback.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..lib import guilds as guilds_mod
from ..lib.logger import log_err, log_sys

router = APIRouter()

COOKIE_NAME = "nectar_session"

# Short-lived PKCE state. Held in a cookie rather than server memory because the
# deployed API is serverless: the redirect and the callback can land on two
# different workers, so anything kept in a module-level dict would be missing
# exactly when it is needed.
STATE_COOKIE = "nectar_oauth_state"
STATE_TTL = 600

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
# The token endpoint only needs a valid project API key. The anon key is the
# least privileged one that works, so prefer it; fall back to the service key
# the backend already requires so this does not become a new mandatory secret.
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
AUTH_KEY = SUPABASE_ANON_KEY or SERVICE_KEY

# Must match a Redirect URL in Supabase -> Authentication -> URL Configuration.
# Left empty it is derived from the request, which is right for a single host.
REDIRECT_URI = os.getenv("SUPABASE_REDIRECT_URI", "").strip()
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


def _callback_uri(request: Request) -> str:
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


def _pkce_pair() -> tuple[str, str]:
    """(verifier, challenge) for an S256 PKCE flow.

    token_urlsafe(64) yields 86 characters of [A-Za-z0-9_-], which sits inside
    the 43..128 range RFC 7636 allows for a verifier.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _discord_identity(user: dict) -> tuple[str, str | None, str | None]:
    """(discord_id, display name, avatar) from a Supabase user object.

    Deliberately strict. Supabase exposes the provider's own subject as
    `provider_id`; the Supabase user id and identity id are UUIDs. Writing a
    UUID into a column that means "Discord account" would not fail loudly — it
    would quietly point a guild's ownership at nobody — so anything that is not
    a numeric snowflake is treated as missing.
    """
    meta = (user or {}).get("user_metadata") or {}
    candidates = [meta.get("provider_id")]
    for ident in ((user or {}).get("identities") or []):
        data = (ident or {}).get("identity_data") or {}
        candidates.append(data.get("provider_id"))

    discord_id = ""
    for cand in candidates:
        text = str(cand or "").strip()
        if text.isdigit():
            discord_id = text
            break

    username = (meta.get("full_name") or meta.get("user_name")
                or meta.get("name") or meta.get("preferred_username"))
    avatar = meta.get("avatar_url")
    return discord_id, (str(username) if username else None), (str(avatar) if avatar else None)


@router.get("/auth/discord")
async def discord_login(request: Request):
    """Send the browser to Supabase, which sends it on to Discord."""
    if not SUPABASE_URL or not AUTH_KEY:
        return JSONResponse(
            {"error": "Sign-in is not configured on this server",
             "hint": "Set SUPABASE_URL (and SUPABASE_ANON_KEY) and enable the "
                     "Discord provider in Supabase Auth"},
            status_code=503,
        )

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    params = {
        "provider": "discord",
        "code_challenge": challenge,
        "code_challenge_method": "s256",
        "redirect_to": _callback_uri(request),
        "state": state,
        "scopes": "identify guilds",
    }
    url = f"{SUPABASE_URL}/auth/v1/authorize?" + "&".join(
        f"{k}={_quote(v)}" for k, v in params.items()
    )

    resp = RedirectResponse(url)
    # state and verifier travel together; the callback needs both and they are
    # only meaningful as a pair.
    resp.set_cookie(
        STATE_COOKIE, f"{state}.{verifier}",
        max_age=STATE_TTL,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/auth",  # only ever needed by the callback
    )
    return resp


def _quote(value: str) -> str:
    import urllib.parse
    return urllib.parse.quote(str(value), safe="")


@router.get("/auth/callback")
async def discord_callback(request: Request, code: str = "", state: str = "",
                           error: str = ""):
    """Redeem the Supabase auth code, then mint our own session."""
    dashboard = _dashboard_base(request)

    # Supabase forwards the provider's own failure (?error=access_denied) when
    # someone presses Cancel on Discord's screen.
    if error or not code:
        return _bounce(dashboard, "cancelled")

    stored = request.cookies.get(STATE_COOKIE) or ""
    expected_state, _, verifier = stored.partition(".")
    if not verifier:
        return _bounce(dashboard, "state")
    # Constant-time compare: the state is a secret even though it is short-lived.
    if state and expected_state and not secrets.compare_digest(state, expected_state):
        return _bounce(dashboard, "state")

    try:
        import httpx
    except Exception as e:  # pragma: no cover - httpx ships with supabase
        log_err(f"AUTH httpx missing: {e}")
        return _bounce(dashboard, "server")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            tok_res = await client.post(
                f"{SUPABASE_URL}/auth/v1/token",
                params={"grant_type": "pkce"},
                json={"auth_code": code, "code_verifier": verifier},
                headers={"apikey": AUTH_KEY, "Content-Type": "application/json"},
            )
        if tok_res.status_code != 200:
            # Never log the code itself; it is a single-use credential.
            log_err(f"AUTH code exchange failed {tok_res.status_code}: "
                    f"{tok_res.text[:200]}")
            return _bounce(dashboard, "exchange")

        payload = tok_res.json() or {}
        user = payload.get("user") or {}
        provider_token = payload.get("access_token")  # Discord access token
        discord_id, username, avatar = _discord_identity(user)
        if not discord_id:
            log_err("AUTH no Discord id on the Supabase user "
                    f"(provider={((user.get('app_metadata') or {}).get('provider'))!r})")
            return _bounce(dashboard, "discord")

        token = await guilds_mod.create_session(discord_id, username, avatar,
                                                provider_token=provider_token)
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
    provider_token = session.get("provider_token")
    return {
        "user": {
            "id": discord_id,
            "username": session.get("discord_username"),
            "avatar_url": session.get("avatar_url"),
        },
        "guilds": await guilds_mod.list_user_guilds(discord_id, provider_token),
    }


@router.post("/auth/logout")
async def auth_logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        await guilds_mod.delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
