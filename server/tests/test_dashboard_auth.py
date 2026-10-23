"""Dashboard sign-in: Supabase Auth (Discord) in front, httpOnly cookie behind.

Handlers are called directly with a stub request — the same approach as the
other route tests here, because this environment's starlette/httpx mix has a
broken TestClient. Nothing touches the network or the database.

The properties worth protecting, and therefore worth asserting:

  * the PKCE challenge really is S256 of the verifier, not the verifier itself
  * a UUID can never be mistaken for a Discord ID (that would silently point a
    guild's ownership at nobody)
  * the session cookie is httpOnly, so XSS cannot read it
  * a callback with no state cookie is refused
  * Supabase's tokens are never handed to the browser
"""

import asyncio
import base64
import hashlib
import types

import pytest

from server.routes import auth


class _Req:
    """Just enough of a Request for these handlers."""

    def __init__(self, cookies=None, query=None, scheme="http", host="test.local"):
        self.cookies = dict(cookies or {})
        self.query_params = dict(query or {})
        self.headers = {"host": host}
        self.url = types.SimpleNamespace(scheme=scheme)
        self.base_url = f"{scheme}://{host}/"


def _set_cookies(resp) -> list[str]:
    return [v.decode("latin-1") for k, v in resp.raw_headers
            if k.lower() == b"set-cookie"]


def _cookie(resp, name: str) -> str:
    for c in _set_cookies(resp):
        if c.startswith(f"{name}="):
            return c
    return ""


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Pin the config so a developer's .env cannot change what is tested."""
    monkeypatch.setattr(auth, "SUPABASE_URL", "https://proj.supabase.co", raising=False)
    monkeypatch.setattr(auth, "AUTH_KEY", "test-key", raising=False)
    monkeypatch.setattr(auth, "REDIRECT_URI", "", raising=False)
    monkeypatch.setattr(auth, "DASHBOARD_URL", "", raising=False)


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = auth._pkce_pair()

    assert 43 <= len(verifier) <= 128
    assert all(ch.isalnum() or ch in "-_" for ch in verifier)
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert challenge == expected
    assert "=" not in challenge          # unpadded, per RFC 7636
    assert verifier != challenge         # never send the verifier in the URL


def test_pkce_pairs_are_unique():
    assert auth._pkce_pair()[0] != auth._pkce_pair()[0]


# ---------------------------------------------------------------------------
# Identity extraction
# ---------------------------------------------------------------------------

def test_discord_identity_reads_provider_id():
    user = {
        "user_metadata": {
            "provider_id": "1512788791805349888",
            "full_name": "Ace",
            "avatar_url": "https://cdn.example/a.png",
        },
    }
    assert auth._discord_identity(user) == (
        "1512788791805349888", "Ace", "https://cdn.example/a.png")


def test_discord_identity_falls_back_to_identity_data():
    user = {"user_metadata": {},
            "identities": [{"identity_data": {"provider_id": "998877665544332211"}}]}
    assert auth._discord_identity(user)[0] == "998877665544332211"


def test_discord_identity_rejects_a_supabase_uuid():
    """A UUID in a Discord column would look like a real user and match nobody."""
    uuid = "9f1c0a2e-1111-4b2c-8d3e-1234567890ab"
    user = {"user_metadata": {"provider_id": uuid},
            "identities": [{"identity_data": {"provider_id": uuid}}]}
    assert auth._discord_identity(user) == ("", None, None)


def test_discord_identity_handles_missing_metadata():
    assert auth._discord_identity({}) == ("", None, None)


# ---------------------------------------------------------------------------
# /auth/discord
# ---------------------------------------------------------------------------

def test_login_sends_browser_to_supabase_with_pkce():
    resp = asyncio.run(auth.discord_login(_Req()))

    assert resp.status_code in (302, 307)
    loc = resp.headers["location"]
    assert loc.startswith("https://proj.supabase.co/auth/v1/authorize?")
    assert "provider=discord" in loc
    assert "code_challenge_method=s256" in loc
    assert "code_challenge=" in loc
    assert "redirect_to=http%3A%2F%2Ftest.local%2Fauth%2Fcallback" in loc
    # The verifier must never travel in the URL — only its hash does.
    verifier = _cookie(resp, auth.STATE_COOKIE).split(".", 1)[1].split(";")[0]
    assert verifier not in loc


def test_login_state_cookie_is_httponly_and_narrow():
    resp = asyncio.run(auth.discord_login(_Req()))
    cookie = _cookie(resp, auth.STATE_COOKIE)

    assert cookie
    assert "HttpOnly" in cookie
    assert "Path=/auth" in cookie        # only the callback needs it
    assert "SameSite=lax" in cookie


def test_login_cookie_is_secure_behind_https():
    req = _Req()
    req.headers["x-forwarded-proto"] = "https"
    cookie = _cookie(asyncio.run(auth.discord_login(req)), auth.STATE_COOKIE)
    assert "Secure" in cookie


def test_login_reports_missing_configuration(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_KEY", "", raising=False)
    resp = asyncio.run(auth.discord_login(_Req()))
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /auth/callback
# ---------------------------------------------------------------------------

def test_callback_without_code_returns_to_login():
    resp = asyncio.run(auth.discord_callback(_Req(), code="", state=""))
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "http://test.local/login?error=cancelled"


def test_callback_without_state_cookie_is_refused():
    """No verifier means the code cannot be redeemed, so there is nothing to do."""
    resp = asyncio.run(auth.discord_callback(_Req(), code="abc", state="abc"))
    assert resp.headers["location"] == "http://test.local/login?error=state"


def test_callback_with_mismatched_state_is_refused():
    req = _Req(cookies={auth.STATE_COOKIE: "expected.verifier-value"})
    resp = asyncio.run(auth.discord_callback(req, code="abc", state="tampered"))
    assert resp.headers["location"] == "http://test.local/login?error=state"


def test_callback_success_sets_an_httponly_session(monkeypatch):
    """The browser gets our opaque token — never Supabase's access token."""
    async def _create_session(discord_id, username=None, avatar_url=None):
        assert discord_id == "1512788791805349888"
        assert username == "Ace"
        return "opaque-session-token"

    monkeypatch.setattr(auth.guilds_mod, "create_session", _create_session)

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "access_token": "SUPABASE-ACCESS-TOKEN",
                "refresh_token": "SUPABASE-REFRESH-TOKEN",
                "user": {"user_metadata": {"provider_id": "1512788791805349888",
                                           "full_name": "Ace"}},
            }

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    req = _Req(cookies={auth.STATE_COOKIE: "st.verifier-value"})
    resp = asyncio.run(auth.discord_callback(req, code="auth-code", state="st"))

    assert resp.headers["location"] == "http://test.local/dashboard"
    cookie = _cookie(resp, auth.COOKIE_NAME)
    assert "opaque-session-token" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/" in cookie
    assert "SameSite=lax" in cookie

    # The one-shot state cookie is cleared, and no provider token leaks out.
    assert "Max-Age=0" in _cookie(resp, auth.STATE_COOKIE)
    for leaked in ("SUPABASE-ACCESS-TOKEN", "SUPABASE-REFRESH-TOKEN"):
        assert leaked not in "".join(_set_cookies(resp))


def test_callback_exchange_failure_is_not_a_login(monkeypatch):
    class _Resp:
        status_code = 400
        text = '{"error_code":"flow_state_not_found"}'

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    req = _Req(cookies={auth.STATE_COOKIE: "st.verifier-value"})
    resp = asyncio.run(auth.discord_callback(req, code="bad", state="st"))

    assert resp.headers["location"] == "http://test.local/login?error=exchange"
    assert _cookie(resp, auth.COOKIE_NAME) == ""


def test_callback_refuses_a_user_with_no_discord_id(monkeypatch):
    async def _create_session(*a, **kw):
        raise AssertionError("must not create a session without a Discord ID")

    monkeypatch.setattr(auth.guilds_mod, "create_session", _create_session)

    class _Resp:
        status_code = 200

        def json(self):
            return {"user": {"user_metadata": {"provider_id": "not-a-snowflake"}}}

    class _Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    req = _Req(cookies={auth.STATE_COOKIE: "st.verifier-value"})
    resp = asyncio.run(auth.discord_callback(req, code="c", state="st"))

    assert resp.headers["location"] == "http://test.local/login?error=discord"
    assert _cookie(resp, auth.COOKIE_NAME) == ""
