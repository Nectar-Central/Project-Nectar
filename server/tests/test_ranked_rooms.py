"""Ranked lifecycle endpoints: create/roster/start/backout/end/code/host/seals/review.

Handlers called directly with stub requests; a dict-backed fake stands in
for Supabase. No network.
"""
import asyncio
import json

import pytest

from server.lib import supabase_client as sc_mod
from server.routes import ranked as ranked_route


class _Res:
    def __init__(self, data=None):
        self.data = data


class _Q:
    def __init__(self, fake, table):
        self.fake = fake
        self.table = table
        self.op = "select"
        self.payload = None
        self.conflict = None
        self.filters = []
        self.order_col = None
        self.order_desc = True
        self.lo = None
        self.hi = None

    def select(self, *a):
        self.op = "select"
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, list(vals)))
        return self

    def order(self, col, desc=True):
        self.order_col, self.order_desc = col, bool(desc)
        return self

    def limit(self, n):
        self.lo, self.hi = 0, n - 1
        return self

    def range(self, lo, hi):
        self.lo, self.hi = lo, hi
        return self

    def upsert(self, payload, on_conflict=None):
        self.op, self.payload, self.conflict = "upsert", payload, on_conflict
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def delete(self):
        self.op = "delete"
        return self

    def _rows(self):
        rows = list(getattr(self.fake, self.table, []))
        for kind, col, val in self.filters:
            if kind == "eq":
                rows = [r for r in rows if str(r.get(col)) == str(val)]
            elif kind == "in":
                if isinstance(val, str):
                    rows = [r for r in rows if str(r.get(col)) == val]
                else:
                    rows = [r for r in rows if str(r.get(col)) in {str(v) for v in val}]
        if self.order_col:
            rows = sorted(rows, key=lambda r: (r.get(self.order_col) or 0),
                          reverse=self.order_desc)
        if self.lo is not None:
            rows = rows[self.lo:(self.hi + 1) if self.hi is not None else None]
        return rows

    def _match(self, row):
        for kind, col, val in self.filters:
            if kind == "eq" and str(row.get(col)) != str(val):
                return False
            if kind == "in" and str(row.get(col)) not in {str(v) for v in val}:
                return False
        return True

    def execute(self):
        store = getattr(self.fake, self.table)
        if self.op == "select":
            return _Res(self._rows())
        if self.op in ("upsert", "insert"):
            store.append(dict(self.payload))
            return _Res([self.payload])
        if self.op == "update":
            for r in store:
                if self._match(r):
                    r.update(self.payload)
            return _Res([])
        if self.op == "delete":
            store[:] = [r for r in store if not self._match(r)]
            return _Res([])
        return _Res([])


class _FakeDB:
    def __init__(self):
        self.ranked_rooms = []
        self.ranked_bans = []
        self.ranked_points = []
        self.ranked_results = []
        self.bngc_users = []
        self.bngc_game_events = []
        self.ranked_seasons = []

    def table(self, name):
        return _Q(self, name)


class _Req:
    def __init__(self, params=None, body=None):
        self.query_params = dict(params or {})
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _call(fn, *args, params=None, body=None):
    resp = asyncio.run(fn(*args, _Req(params, body)))
    assert resp.status_code in (200, 400, 403, 404, 409, 500), resp.body[:200]
    return resp.status_code, json.loads(resp.body.decode())


@pytest.fixture
def fx(monkeypatch):
    fake = _FakeDB()
    fake.bngc_users = [
        {"discord_id": "H", "game_username": "Host1"},
        {"discord_id": "P1", "game_username": "Alpha"},
        {"discord_id": "P2", "game_username": "Beta"},
    ]
    monkeypatch.setattr(sc_mod, "supabase", fake)
    return fake


def _mkroom(fx, **kw):
    body = {"short_code": "ABCDE", "host_discord_id": "H", "gm": "zw_lg",
            "max_players": 4, "max_matches": 5}
    body.update(kw)
    code, out = _call(ranked_route.ranked_create, params=None, body=body)
    assert code == 200, out
    return out["room"]["id"]


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create_ok(fx):
    code, out = _call(ranked_route.ranked_create, params=None, body={
        "short_code": "abcde", "host_discord_id": "H", "gm": "zw_lg"})
    assert code == 200
    assert out["room"]["short_code"] == "ABCDE"
    assert out["room"]["status"] == "lobby"


def test_create_host_only_empty_roster(fx):
    code, out = _call(ranked_route.ranked_create, params=None, body={
        "short_code": "ABCDE", "host_discord_id": "H", "gm": "df", "host_only": True})
    assert code == 200
    assert out["room"]["players"] == 0


def test_create_unregistered_host(fx):
    code, out = _call(ranked_route.ranked_create, params=None, body={
        "short_code": "ABCDE", "host_discord_id": "NOBODY", "gm": "zw_lg"})
    assert code == 403


def test_create_banned_host(fx):
    fx.ranked_bans.append({"discord_id": "H"})
    code, out = _call(ranked_route.ranked_create, params=None, body={
        "short_code": "ABCDE", "host_discord_id": "H", "gm": "zw_lg"})
    assert code == 403


def test_create_validation(fx):
    bad_code, _ = _call(ranked_route.ranked_create, params=None, body={
        "short_code": "TOOLONG", "host_discord_id": "H", "gm": "zw_lg"})
    assert bad_code == 400
    bad_gm, _ = _call(ranked_route.ranked_create, params=None, body={
        "short_code": "ABCDE", "host_discord_id": "H", "gm": "r1v1"})
    assert bad_gm == 400
    _mkroom(fx)
    dup, _ = _call(ranked_route.ranked_create, params=None, body={
        "short_code": "ABCDE", "host_discord_id": "P1", "gm": "zw_lg"})
    assert dup == 409


# ---------------------------------------------------------------------------
# roster
# ---------------------------------------------------------------------------

def test_roster_add_and_guards(fx):
    rid = _mkroom(fx, min_points=500)
    fx.ranked_points = [{"discord_id": "P1", "points": 600}]
    ok, out = _call(ranked_route.ranked_roster, rid, params=None,
                    body={"discord_id": "P1", "action": "add"})
    assert ok == 200 and "P1" in out["roster"]
    poor, _ = _call(ranked_route.ranked_roster, rid, params=None,
                    body={"discord_id": "P2", "action": "add"})
    assert poor == 403  # 0 pts < 500 lock
    ghost, _ = _call(ranked_route.ranked_roster, rid, params=None,
                     body={"discord_id": "NOBODY", "action": "add"})
    assert ghost == 403
    fx.ranked_bans.append({"discord_id": "P1"})
    rid2 = _mkroom(fx, short_code="ZZZZZ")
    banned2, _ = _call(ranked_route.ranked_roster, rid2, params=None,
                       body={"discord_id": "P1", "action": "add"})
    assert banned2 == 403


def test_roster_remove_and_host_guard(fx):
    rid = _mkroom(fx)
    fx.ranked_points = [{"discord_id": "P1", "points": 0}]
    _call(ranked_route.ranked_roster, rid, params=None,
          body={"discord_id": "P1", "action": "add"})
    ok, out = _call(ranked_route.ranked_roster, rid, params=None,
                    body={"discord_id": "P1", "action": "remove"})
    assert ok == 200 and "P1" not in out["roster"]
    host_out, _ = _call(ranked_route.ranked_roster, rid, params=None,
                        body={"discord_id": "H", "action": "remove"})
    assert host_out == 409


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def test_start_full_flow(fx):
    rid = _mkroom(fx)
    fx.ranked_points = [{"discord_id": "H", "points": 100},
                        {"discord_id": "P1", "points": 100}]
    _call(ranked_route.ranked_roster, rid, params=None,
          body={"discord_id": "P1", "action": "add"})
    fx.bngc_game_events = [
        {"event_type": "kill",
         "event_data": {"code": "ABCDE", "killer": "Host1", "victim": "X"},
         "discord_id": "H", "created_at": "2099-01-01T00:00:00+00:00"},
    ]
    import datetime as _dt
    fx.bngc_game_events[0]["created_at"] = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)).isoformat()
    code, out = _call(ranked_route.ranked_start, rid, params=None, body={"via": "manual"})
    assert code == 200, out
    assert out["short_code"] == "ABCDE"
    assert set(out["roster"]) == {"H", "P1"}
    assert out["host_status"] == "in room"
    assert out["dropped"] == []
    room = [r for r in fx.ranked_rooms if r["id"] == rid][0]
    assert room["status"] == "live"


def test_start_host_not_in_room(fx):
    rid = _mkroom(fx)
    fx.ranked_points = [{"discord_id": "H", "points": 0},
                        {"discord_id": "P1", "points": 0}]
    _call(ranked_route.ranked_roster, rid, params=None,
          body={"discord_id": "P1", "action": "add"})
    fx.bngc_game_events = [
        {"event_type": "kill",
         "event_data": {"code": "ABCDE", "killer": "Stranger", "victim": "X"},
         "discord_id": "P9", "created_at": "2099-01-01T00:00:00+00:00"},
    ]
    import datetime as _dt
    fx.bngc_game_events[0]["created_at"] = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)).isoformat()
    code, out = _call(ranked_route.ranked_start, rid, params=None, body={})
    assert code == 409
    assert out["error"] == "host not in room"


def test_start_drops_broke_and_banned(fx):
    rid = _mkroom(fx, fee=50)
    fx.ranked_points = [{"discord_id": "H", "points": 1000},
                        {"discord_id": "P1", "points": 10},
                        {"discord_id": "P2", "points": 1000}]
    _call(ranked_route.ranked_roster, rid, params=None,
          body={"discord_id": "P1", "action": "add"})
    _call(ranked_route.ranked_roster, rid, params=None,
          body={"discord_id": "P2", "action": "add"})
    fx.ranked_bans.append({"discord_id": "P2"})
    fx.bngc_game_events = [
        {"event_type": "kill",
         "event_data": {"code": "ABCDE", "killer": "Host1", "victim": "X"},
         "discord_id": "H", "created_at": "2099-01-01T00:00:00+00:00"},
    ]
    import datetime as _dt
    fx.bngc_game_events[0]["created_at"] = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)).isoformat()
    code, out = _call(ranked_route.ranked_start, rid, params=None, body={})
    assert code == 409  # only H left -> < 2 players
    assert out["error"] == "need at least 2 players to start"
    assert {d["discord_id"] for d in out["dropped"]} == {"P1", "P2"}


def test_start_needs_two(fx):
    rid = _mkroom(fx)
    code, out = _call(ranked_route.ranked_start, rid, params=None, body={})
    assert code == 409


# ---------------------------------------------------------------------------
# backout / end / code / host / seals / review / list
# ---------------------------------------------------------------------------

def _live_room(fx):
    rid = _mkroom(fx)
    fx.ranked_points = [{"discord_id": "H", "points": 0},
                        {"discord_id": "P1", "points": 0}]
    _call(ranked_route.ranked_roster, rid, params=None,
          body={"discord_id": "P1", "action": "add"})
    fx.bngc_game_events = [
        {"event_type": "kill",
         "event_data": {"code": "ABCDE", "killer": "Host1", "victim": "X"},
         "discord_id": "H", "created_at": "2099-01-01T00:00:00+00:00"},
    ]
    import datetime as _dt
    fx.bngc_game_events[0]["created_at"] = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)).isoformat()
    code, _ = _call(ranked_route.ranked_start, rid, params=None, body={})
    assert code == 200
    return rid


def test_backout_and_end(fx):
    rid = _live_room(fx)
    ok, out = _call(ranked_route.ranked_backout, rid, params=None, body={"reason": "crash"})
    assert ok == 200 and out["room"]["status"] == "lobby"
    again, _ = _call(ranked_route.ranked_backout, rid, params=None, body={})
    assert again == 409
    ok, out = _call(ranked_route.ranked_end, rid, params=None, body={})
    assert ok == 200 and out["room"]["status"] == "ended"


def test_code_change(fx):
    rid = _mkroom(fx)
    ok, out = _call(ranked_route.ranked_code, rid, params=None, body={"short_code": "zzzzz"})
    assert ok == 200 and out["room"]["short_code"] == "ZZZZZ"
    bad, _ = _call(ranked_route.ranked_code, rid, params=None, body={"short_code": "no"})
    assert bad == 400


def test_host_transfer(fx):
    rid = _mkroom(fx)
    ok, out = _call(ranked_route.ranked_host, rid, params=None, body={"discord_id": "P1"})
    assert ok == 200
    assert out["room"]["host_discord_id"] == "P1"
    assert "P1" in [r["discord_id"] for r in
                    [x for x in fx.ranked_rooms if x["id"] == rid][0]["roster"]]
    bad, _ = _call(ranked_route.ranked_host, rid, params=None, body={"discord_id": "NOBODY"})
    assert bad == 403


def test_seals_and_review(fx):
    rid = _live_room(fx)
    fx.ranked_results = [{"room_id": rid, "match_version": 1, "gm": "zw_lg",
                          "winner_discord_id": "H", "kills": {"H": 2},
                          "points": {"H": 85}, "placements": {"H": 1}}]
    code, out = _call(ranked_route.ranked_seals, rid, params=None)
    assert code == 200 and out["count"] == 1
    code, out = _call(ranked_route.ranked_review, rid, params=None)
    assert code == 200
    assert out["room"]["sealed"] == 1
    assert any(r["discord_id"] == "H" and r["is_host"] for r in out["room"]["roster"])
    code, out = _call(ranked_route.ranked_list, params={"status": "live"})
    assert code == 200 and any(r["id"] == rid for r in out["rooms"])


def test_by_code_live_member(fx):
    rid = _live_room(fx)
    code, out = _call(ranked_route.ranked_by_code, "abcde", params={"discord_id": "H"})
    assert code == 200
    assert out["room"]["id"] == rid and out["room"]["status"] == "live"
    assert out["on_roster"] is True and out["is_host"] is True


def test_by_code_outsider(fx):
    _live_room(fx)
    code, out = _call(ranked_route.ranked_by_code, "ABCDE", params={"discord_id": "STRANGER"})
    assert code == 200
    assert out["room"] is not None
    assert out["on_roster"] is False and out["is_host"] is False


def test_by_code_unknown_and_bad(fx):
    code, out = _call(ranked_route.ranked_by_code, "QQQQQ", params={"discord_id": "H"})
    assert code == 200 and out["room"] is None
    bad, _ = _call(ranked_route.ranked_by_code, "TOOLONG", params={})
    assert bad == 400


def test_by_code_ended(fx):
    rid = _live_room(fx)
    _call(ranked_route.ranked_end, rid, params=None, body={})
    code, out = _call(ranked_route.ranked_by_code, "ABCDE", params={"discord_id": "H"})
    assert code == 200
    assert out["room"]["status"] == "ended"
    assert out["on_roster"] is True


def test_mine_prefers_live(fx):
    rid = _live_room(fx)
    code, out = _call(ranked_route.ranked_mine, params={"discord_id": "H"})
    assert code == 200
    assert out["room"]["id"] == rid and out["on_roster"] is True


def test_mine_none_when_uninvolved(fx):
    _live_room(fx)
    code, out = _call(ranked_route.ranked_mine, params={"discord_id": "STRANGER"})
    assert code == 200 and out["room"] is None


def test_mine_none_after_end(fx):
    rid = _live_room(fx)
    _call(ranked_route.ranked_end, rid, params=None, body={})
    code, out = _call(ranked_route.ranked_mine, params={"discord_id": "H"})
    assert code == 200 and out["room"] is None
