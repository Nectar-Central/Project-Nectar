"""Heartbeat capture batches: 200 (never 204), engine never fed.

Regression: the empty-batch tail returned JSONResponse({}, 204). The {}
body is still serialized while the status forbids one, and uvicorn 500s
every heartbeat ("Response content longer than Content-Length").

Handler called directly (TestClient is broken in this env); DB/cache/
engine faked. No network.
"""
import asyncio

from fastapi import BackgroundTasks

from server.routes import capture as cap_mod

AUTH = "auth_sess_1"
HB_BODY = {"sessionId": "cap-1", "authSessionId": AUTH, "discord_id": "7",
           "records": [{"hb": 1, "t": 123}]}


class _Req:
    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.client = None

    async def json(self):
        return dict(self._body)


class _FakeDB:
    def get_session_sync(self, auth_id):
        assert auth_id == AUTH
        return {"data": {"session_id": AUTH, "discord_id": "7",
                         "created_at": "2099-01-01T00:00:00+00:00",
                         "is_active": True, "req_secret": "x",
                         "token_counter": 0}}


async def _db_io(fn, *a, **k):
    return fn(*a, **k)


class _FakeCache:
    def is_configured(self):
        return False

    async def get_session_state(self, sid):
        return None

    async def set_session_state(self, sid, state, ttl=None):
        return True

    async def blacklist_token(self, tok, ttl):
        return None


class _FakeEngine:
    def __init__(self, view=None):
        self.view = view
        self.fed = []

    def get_session(self, sid):
        return self.view

    def put_session(self, sid, view):
        self.view = view

    def feed(self, sid, hex_str, direction=None):
        self.fed.append(hex_str)
        raise AssertionError("engine.feed must never run for heartbeat records")


class _FakeView:
    me = None
    rooms = {}
    players_count = 0

    def to_dict(self):
        return {"rooms": {}, "current_room_id": None}


def _run(monkeypatch, engine):
    monkeypatch.setattr(cap_mod, "db", _FakeDB())
    monkeypatch.setattr(cap_mod, "db_io", _db_io)
    monkeypatch.setattr(cap_mod, "cache", _FakeCache())
    monkeypatch.setattr(cap_mod, "engine", engine)
    return asyncio.run(cap_mod.capture_handler(_Req(HB_BODY), BackgroundTasks()))


def test_heartbeat_returns_200_never_204(monkeypatch):
    eng = _FakeEngine()
    resp = _run(monkeypatch, eng)
    assert resp.status_code == 200, resp.status_code
    assert eng.fed == []


def test_heartbeat_with_live_view_stays_clean(monkeypatch):
    eng = _FakeEngine(view=_FakeView())
    resp = _run(monkeypatch, eng)
    assert resp.status_code == 200, resp.status_code
    assert eng.fed == []
