"""/api/capture must never 500 on a Supabase blip.

A read timeout in the session lookup escaped the handler
(`res = await db_io(db.get_session_sync, ...)`, unwrapped) and surfaced as a
500 with a CancelledError through the ASGI middleware. The client requeues on
a 500 and retries — but if the failure lands AFTER the token rotation write,
the retry carries the old token, gets `Invalid req_token`, and the client
**throws its queue away and asks the user to re-link**. One slow database read
could therefore cost a player their whole capture buffer and their session.

Pinned here: a DB that raises or times out yields 200 (with the token whenever
we managed to mint one), the lookup is time-bounded, and nothing in the
handler turns an unexpected error into a 5xx.
"""
import asyncio
import importlib
import sys
from pathlib import Path

import pytest


def _load_capture():
    root = str(Path(".").resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    import server.routes.capture as cap
    importlib.reload(cap)
    return cap


class _Req:
    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.client = None

    async def json(self):
        return self._body


class _View:
    """Minimal stand-in for a restored SessionView."""

    players_count = 1
    rooms: dict = {}
    me = None
    playtime = None
    current_room_id = None

    def to_dict(self):
        return {"rooms": {}}


def _patch_surroundings(cap, monkeypatch, *, records):
    """Silence everything except the DB calls under test."""
    async def _cache_get(_sid):
        return None
    async def _cache_set(*a, **k):
        return True
    async def _cache_bl(*a, **k):
        return True
    monkeypatch.setattr(cap.cache, "get_session_state", _cache_get)
    monkeypatch.setattr(cap.cache, "set_session_state", _cache_set)
    monkeypatch.setattr(cap.cache, "blacklist_token", _cache_bl)
    monkeypatch.setattr(cap.engine, "get_session", lambda _s: _View())
    monkeypatch.setattr(cap.engine, "put_session", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_capture_returns_200_when_the_session_lookup_raises(monkeypatch):
    cap = _load_capture()
    _patch_surroundings(cap, monkeypatch, records=[])

    async def _boom(*a, **k):
        raise RuntimeError("The read operation timed out")
    monkeypatch.setattr(cap, "db_io", _boom)

    body = {"authSessionId": "sess_1", "discord_id": "129547",
            "req_token": "abc123", "records": []}
    resp = await cap.capture_handler(_Req(body), None)
    assert resp.status_code == 200
    assert b"error" not in resp.body.lower()


@pytest.mark.asyncio
async def test_capture_returns_200_when_the_lookup_hangs(monkeypatch):
    cap = _load_capture()
    _patch_surroundings(cap, monkeypatch, records=[])

    async def _hang(*a, **k):
        await asyncio.sleep(30)
    monkeypatch.setattr(cap, "db_io", _hang)
    monkeypatch.setattr(cap, "SESSION_LOOKUP_TIMEOUT_SEC", 0.2)

    body = {"authSessionId": "sess_1", "discord_id": "129547",
            "req_token": "abc123", "records": []}
    resp = await asyncio.wait_for(
        cap.capture_handler(_Req(body), None), timeout=5)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_capture_returns_200_when_the_rotation_write_fails(monkeypatch):
    # The token was minted: it MUST reach the client, or the next request
    # fails validation and the client discards its queue.
    cap = _load_capture()
    _patch_surroundings(cap, monkeypatch, records=[])

    calls = {"n": 0}

    async def _io(fn, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"data": {"discord_id": "129547", "token_counter": 7,
                             "req_secret": "a1b2c3d4", "current_token": "old",
                             "is_active": True, "created_at": "2030-01-01"}}
        raise RuntimeError("write timed out")
    monkeypatch.setattr(cap, "db_io", _io)

    body = {"authSessionId": "sess_1", "discord_id": "129547",
            "req_token": "old", "records": []}
    resp = await cap.capture_handler(_Req(body), None)
    assert resp.status_code == 200
    assert b"new_req_token" in resp.body


@pytest.mark.asyncio
async def test_a_bad_body_still_gets_a_clean_400(monkeypatch):
    # The guard must not swallow the deliberate client errors.
    cap = _load_capture()
    resp = await cap.capture_handler(_Req({"sessionId": "s"}), None)
    assert resp.status_code == 400
