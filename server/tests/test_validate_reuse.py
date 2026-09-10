"""validate-token reuse: active session returns, lookup error 503s, proven empty locks.

Regression: a transport/DB error during the existing-session lookup fell
through to the account-lock branch — a blip during auto re-validation
could lock an account. Only a PROVEN empty lookup may lock.
"""
import asyncio

from server.routes import validate_token as vt_mod


class _R:
    def __init__(self, data):
        self.data = data


class _T:
    def __init__(self, state, table):
        self.s = state
        self.table = table
        self._rows = list(state.get(table, []))
        self._single = False
        self._update = None

    def select(self, *a):
        return self

    def eq(self, c, v):
        self._rows = [r for r in self._rows if str(r.get(c)) == str(v)]
        return self

    def order(self, c, desc=False):
        self._rows = sorted(self._rows, key=lambda r: (r.get(c) or ""),
                            reverse=bool(desc))
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def single(self):
        self._single = True
        return self

    def update(self, data):
        self._update = data
        return self

    def execute(self):
        if self.s.get("transport_error") and self.table == "bngc_sessions":
            raise ConnectionError("db down")
        if self._update is not None:
            self.s.setdefault("updates", []).append((self.table, dict(self._update),
                                                     [dict(r) for r in self._rows]))
            for r in self._rows:
                r.update(self._update)
            return _R(list(self._rows))
        if self._single:
            if not self._rows:
                raise Exception("PGRST116 no rows")
            return _R(self._rows[0])
        return _R(list(self._rows))


class _SB:
    def __init__(self, state):
        self.s = state

    def table(self, name):
        return _T(self.s, name)


class _Cache:
    def __init__(self, state):
        self.s = state

    async def is_token_blacklisted(self, tok):
        return False

    async def blacklist_token(self, tok, ttl):
        self.s.setdefault("blacklisted", []).append(tok)


class _Req:
    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.client = None

    async def json(self):
        return dict(self._body)


def _run(monkeypatch, state, token):
    monkeypatch.setattr(vt_mod, "supabase", _SB(state))
    monkeypatch.setattr(vt_mod, "cache", _Cache(state))
    monkeypatch.setattr(vt_mod, "create_alert",
                        lambda *a, **k: state.setdefault("alerts", []).append(a[1] if len(a) > 1 else None))
    import json as _j
    resp = asyncio.run(vt_mod.validate_token(_Req({"token": token})))
    return resp.status_code, _j.loads(resp.body.decode())


def _login(used=True):
    return {"token": "TOK123", "discord_id": "7", "used": used}


def test_used_token_with_active_session_returns_it(monkeypatch):
    state = {"bngc_login_tokens": [_login(True)],
             "bngc_sessions": [{"session_id": "sess_abc", "discord_id": "7",
                                "current_token": "tok9", "token_counter": 5,
                                "is_active": True,
                                "created_at": "2099-01-01T00:00:00+00:00"}],
             "bngc_users": [{"discord_id": "7", "game_username": "g"}]}
    code, j = _run(monkeypatch, state, "TOK123")
    assert code == 200, j
    assert j["session_id"] == "sess_abc" and j.get("reused") is True
    assert not state.get("blacklisted")
    assert not [u for u in state.get("updates", []) if u[1].get("is_locked")]


def test_used_token_lookup_error_is_transient_not_lock(monkeypatch):
    state = {"transport_error": True, "bngc_login_tokens": [_login(True)],
             "bngc_users": [{"discord_id": "7"}]}
    code, j = _run(monkeypatch, state, "TOK123")
    assert code == 503, (code, j)
    assert not state.get("blacklisted")
    assert not [u for u in state.get("updates", []) if u[1].get("is_locked")]
    assert not state.get("alerts")


def test_used_token_proven_empty_still_locks(monkeypatch):
    state = {"bngc_login_tokens": [_login(True)], "bngc_sessions": [],
             "bngc_users": [{"discord_id": "7"}]}
    code, j = _run(monkeypatch, state, "TOK123")
    assert code == 403, (code, j)
    assert state.get("blacklisted") == ["TOK123"]
    assert [u for u in state.get("updates", []) if u[1].get("is_locked")]
