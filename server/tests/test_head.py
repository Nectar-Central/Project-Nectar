"""HEAD must mirror GET status on GET routes (probes), never 405."""
import asyncio

import httpx

from server.main import app


def _request(method, path, **kw):
    async def _go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            return await c.request(method, path, **kw)

    return asyncio.run(_go())


def test_head_health_mirrors_get():
    get = _request("GET", "/api/health")
    head = _request("HEAD", "/api/health")
    assert get.status_code == 200
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers.get("content-length") == str(len(get.content))


def test_head_on_param_routes_routes_not_405():
    # Missing params -> 400/404 (routed), never 405.
    for path in ("/api/stats", "/api/players", "/api/user", "/api/search",
                 "/api/friends/list", "/api/party/invites", "/api/leaderboard"):
        r = _request("HEAD", path)
        assert r.status_code != 405, path
        assert r.content == b""


def test_head_on_post_only_routes_stays_405():
    assert _request("HEAD", "/api/capture").status_code == 405
    assert _request("GET", "/api/capture").status_code == 405
    assert _request("POST", "/api/friends/list").status_code == 405


def test_get_post_behavior_unchanged():
    assert _request("GET", "/api/leaderboard", params={"sort": "kills", "limit": "1"}).status_code == 200
    r = _request("POST", "/api/capture", json={})
    assert r.status_code == 400
