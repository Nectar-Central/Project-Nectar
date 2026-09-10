"""POST /api/comp/tourney/host-playing + /hosts (update-capable fake supabase)."""
import asyncio
import json

from server.routes import comp as comp_mod


class _R:
    def __init__(self, data):
        self.data = data


class _T:
    def __init__(self, rows):
        self._all = rows
        self._rows = list(rows)
        self._update = None

    def select(self, *a):
        return self

    def eq(self, c, v):
        self._rows = [r for r in self._rows if str(r.get(c)) == str(v)]
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def update(self, data):
        self._update = dict(data)
        return self

    def execute(self):
        if self._update is not None:
            for r in self._rows:
                r.update(self._update)
        return _R([dict(r) for r in self._rows])


class _SB:
    def __init__(self, tables):
        self.t = tables

    def table(self, name):
        return _T(self.t.setdefault(name, []))


class _Req:
    def __init__(self, body):
        self._body = body
        self.headers = {}

    async def json(self):
        return dict(self._body)


def _state():
    return {"bngc_comp_tourneys": [
        {"id": 3, "name": "T3", "host_playing": True, "hosts": ["7"],
         "created_by": "7"}]}


def _call(monkeypatch, state, fn, body):
    monkeypatch.setattr(comp_mod, "supabase", _SB(state))
    resp = asyncio.run(fn(_Req(body)))
    return resp.status_code, json.loads(resp.body.decode())


def test_host_playing_flip(monkeypatch):
    state = _state()
    code, j = _call(monkeypatch, state, comp_mod.comp_tourney_host_playing,
                    {"tourney_id": 3, "host_playing": False})
    assert code == 200, j
    assert j["host_playing"] is False
    assert state["bngc_comp_tourneys"][0]["host_playing"] is False


def test_host_playing_validation(monkeypatch):
    state = _state()
    code, _ = _call(monkeypatch, state, comp_mod.comp_tourney_host_playing,
                    {"tourney_id": 3})
    assert code == 400
    code, _ = _call(monkeypatch, state, comp_mod.comp_tourney_host_playing,
                    {"host_playing": True})
    assert code == 400
    code, _ = _call(monkeypatch, state, comp_mod.comp_tourney_host_playing,
                    {"tourney_id": 99, "host_playing": True})
    assert code == 404


def test_hosts_add_dedupes(monkeypatch):
    state = _state()
    code, j = _call(monkeypatch, state, comp_mod.comp_tourney_hosts,
                    {"tourney_id": 3, "add": "9"})
    assert code == 200, j
    assert j["hosts"] == ["7", "9"]
    code, j = _call(monkeypatch, state, comp_mod.comp_tourney_hosts,
                    {"tourney_id": 3, "add": "9"})
    assert j["hosts"] == ["7", "9"]


def test_hosts_remove(monkeypatch):
    state = _state()
    code, j = _call(monkeypatch, state, comp_mod.comp_tourney_hosts,
                    {"tourney_id": 3, "remove": "7"})
    assert code == 200, j
    assert j["hosts"] == []


def test_hosts_validation(monkeypatch):
    state = _state()
    for body, want in [({}, 400), ({"tourney_id": 3}, 400),
                       ({"tourney_id": 3, "add": "9", "remove": "9"}, 400),
                       ({"tourney_id": 99, "add": "9"}, 404)]:
        code, _ = _call(monkeypatch, state, comp_mod.comp_tourney_hosts, body)
        assert code == want, (body, code)
