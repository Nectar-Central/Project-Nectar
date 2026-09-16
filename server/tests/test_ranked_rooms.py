"""Ranked rooms admin surface: gate, seal browser, repair endpoints.

Handlers called directly with a writable fake supabase (chainable stub,
same pattern as test_ranked_tourney.py). Covers: admin allowlist gate
(fail closed), room/seal browsing shapes, unmapped-name link + recalc
hook, placement editing with holder eviction, remove/add-back toggle,
crown override with P1 swap, and route registration without resurrecting
the deliberately-removed legacy dashboard paths. No network.
"""
import asyncio
import json

from server.main import app
from server.routes import ranked_rooms as rr_mod
from server.routes import ranked_api as ranked_api_mod


ADMIN_GET = [
    "/api/ranked/admin/rooms",
    "/api/ranked/admin/room",
    "/api/ranked/admin/match",
    "/api/ranked/admin/match/unmapped",
]
ADMIN_POST = [
    "/api/ranked/admin/link",
    "/api/ranked/admin/match/place",
    "/api/ranked/admin/match/remove",
    "/api/ranked/admin/match/add-back",
    "/api/ranked/admin/match/winner",
    "/api/ranked/admin/match/recalc",
]


def _routes():
    out = {}
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = set(getattr(r, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
        if path and methods:
            out.setdefault(path, set()).update(methods)
    return out


def test_admin_paths_registered_once():
    routes = _routes()
    for path in ADMIN_GET:
        assert routes.get(path) == {"GET"}, (path, routes.get(path))
    for path in ADMIN_POST:
        assert routes.get(path) == {"POST"}, (path, routes.get(path))
    assert routes.get("/ranked/rooms") == {"GET"}
    # legacy removed paths stay removed (no accidental resurrection)
    for path in ("/api/ranked/rooms", "/api/ranked/room",
                 "/api/ranked/link", "/api/ranked/match/placements"):
        assert path not in routes, path


class _R:
    def __init__(self, data):
        self.data = data


class _Not:
    """`.not_.is_(c, "null")` -> keep rows where c IS NOT NULL."""
    def __init__(self, t):
        self._t = t

    def is_(self, c, v):
        t = self._t
        t._rows = [r for r in t._rows if r.get(c) is not None]
        return t


class _T:
    def __init__(self, state, name):
        self._state = state
        self._name = name
        self._rows = list(state.get(name, []))
        self._update = None
        self._insert = None
        self._upsert = None

    def select(self, *a, **k):
        return self

    def eq(self, c, v):
        self._rows = [r for r in self._rows if str(r.get(c)) == str(v)]
        return self

    def neq(self, c, v):
        self._rows = [r for r in self._rows if str(r.get(c)) != str(v)]
        return self

    def in_(self, c, vs):
        s = {str(v) for v in vs}
        self._rows = [r for r in self._rows if str(r.get(c)) in s]
        return self

    def is_(self, c, v):
        want_null = (v is None) or (str(v).lower() == "null")
        self._rows = [r for r in self._rows
                      if (r.get(c) is None) == want_null]
        return self

    def not_(self):
        return _Not(self)

    def order(self, c, desc=False):
        self._rows = sorted(self._rows,
                            key=lambda r: (r.get(c) is None, r.get(c)),
                            reverse=bool(desc))
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def insert(self, rows):
        self._insert = [rows] if isinstance(rows, dict) else [dict(r) for r in rows]
        return self

    def upsert(self, payload, on_conflict=None):
        self._upsert = [payload] if isinstance(payload, dict) else list(payload)
        return self

    def update(self, data):
        self._update = dict(data)
        return self

    def execute(self):
        base = self._state.setdefault(self._name, [])
        if self._insert is not None:
            wrote = []
            for r in self._insert:
                r = dict(r)
                if "id" not in r:
                    r["id"] = max([x.get("id", 0) for x in base] + [0]) + 1
                base.append(r)
                wrote.append(dict(r))
            return _R(wrote)
        if self._upsert is not None:
            return _R([dict(r) for r in self._upsert])
        if self._update is not None:
            for r in self._rows:
                r.update(self._update)
            return _R([dict(r) for r in self._rows])
        return _R([dict(r) for r in self._rows])


class _SB:
    def __init__(self, tables):
        self.t = tables

    def table(self, name):
        return _T(self.t, name)


class _Req:
    def __init__(self, body=None, params=None):
        self._body = dict(body or {})
        self.query_params = dict(params or {})

    async def json(self):
        return dict(self._body)


def _state():
    return {
        "ranked_admins": [{"discord_id": "ADM"}],
        "ranked_rooms": [{
            "id": "R-1", "short_code": "ABCDE", "status": "ended",
            "host_discord_id": "H", "match_version": 2, "max_matches": 5,
            "roster": [{"discord_id": "A"}, {"discord_id": "B"}],
            "updated_at": "2026-09-16T00:00:00",
        }],
        "bngc_ranked_matches": [
            {"room_id": "R-1", "match_version": 1, "status": "sealed",
             "started_at": "t1", "sealed_at": "t2",
             "removed_discord_ids": [], "point_awards": {}},
            {"room_id": "R-1", "match_version": 2, "status": "sealed",
             "started_at": "t3", "sealed_at": "t4",
             "removed_discord_ids": [], "point_awards": {}},
        ],
        "ranked_results": [{
            "room_id": "R-1", "match_version": 2,
            "winner_discord_id": "A",
            "placements": {"A": 1, "B": 2},
            "kills": {"A": 2},
            "points": {"A": 31, "B": -5},
            "points_table": None,
        }],
        "bngc_ranked_events": [
            {"id": 1, "room_id": "R-1", "match_version": 2, "etype": "kill",
             "actor_name": "alice", "target_name": "bobby", "discord_id": "A"},
            {"id": 2, "room_id": "R-1", "match_version": 2, "etype": "kill",
             "actor_name": "alice", "target_name": "bobby", "discord_id": "A"},
            {"id": 3, "room_id": "R-1", "match_version": 2, "etype": "alv",
             "actor_name": "bobby", "target_name": None, "discord_id": "B",
             "alive": False},
            {"id": 4, "room_id": "R-1", "match_version": 2, "etype": "alv",
             "actor_name": "carol", "target_name": None, "discord_id": None,
             "alive": False},
            {"id": 5, "room_id": "R-1", "match_version": 1, "etype": "kill",
             "actor_name": "dave", "target_name": "alice", "discord_id": None},
        ],
        "bngc_users": [
            {"discord_id": "A", "game_username": "alice", "discord_username": "al"},
            {"discord_id": "B", "game_username": "bobby", "discord_username": "bo"},
        ],
    }


def _run(fn, req):
    resp = asyncio.run(fn(req))
    return resp.status_code, json.loads(resp.body.decode())


def _patch(monkeypatch, state):
    sb = _SB(state)
    monkeypatch.setattr(rr_mod, "supabase", sb)
    monkeypatch.setattr(ranked_api_mod, "supabase", sb)

    async def fake_db_io(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr(rr_mod, "db_io", fake_db_io)
    return sb


def test_admin_gate_fails_closed(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    # no id at all
    code, j = _run(rr_mod.admin_rooms, _Req(params={}))
    assert code == 403 and "admin" in j["error"]
    # non-admin id
    code, j = _run(rr_mod.admin_rooms, _Req(params={"discord_id": "NOPE"}))
    assert code == 403 and j["error"] == "not a ranked admin"
    # empty admins table -> fail closed even for a "known" id
    state["ranked_admins"] = []
    code, _ = _run(rr_mod.admin_rooms, _Req(params={"discord_id": "ADM"}))
    assert code == 403
    # same for a mutating endpoint
    code, j = _run(rr_mod.admin_recalc, _Req(body={"room_id": "R-1",
                                                   "match_version": 2}))
    assert code == 403


def test_rooms_list_filters_and_counts(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    code, j = _run(rr_mod.admin_rooms, _Req(params={"discord_id": "ADM"}))
    assert code == 200 and len(j["rooms"]) == 1
    r = j["rooms"][0]
    assert r["id"] == "R-1" and r["players"] == 2 and r["short_code"] == "ABCDE"
    # roster blob is not leaked raw
    assert "roster" not in r
    code, j = _run(rr_mod.admin_rooms, _Req(params={"discord_id": "ADM",
                                                    "status": "live"}))
    assert code == 200 and j["rooms"] == []


def test_room_detail_seal_browser(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    code, j = _run(rr_mod.admin_room, _Req(params={"discord_id": "ADM",
                                                   "room_id": "R-1"}))
    assert code == 200, j
    room = j["room"]
    assert room["short_code"] == "ABCDE" and "roster" not in room
    names = [x["name"] for x in room["roster_view"]]
    assert names == ["alice", "bobby"]  # display names resolved
    versions = [m["match_version"] for m in j["matches"]]
    assert versions == [2, 1]  # newest first
    code, j = _run(rr_mod.admin_room, _Req(params={"discord_id": "ADM",
                                                   "room_id": "R-404"}))
    assert code == 404


def test_match_detail_shape(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    code, j = _run(rr_mod.admin_match, _Req(params={
        "discord_id": "ADM", "room_id": "R-1", "match_version": "2"}))
    assert code == 200, j
    assert j["sealed"] is True and j["winner_discord_id"] == "A"
    assert j["placements"] == {"A": 1, "B": 2}
    assert j["kills"] == {"A": 2}
    assert isinstance(j["points_table"], dict)
    assert len(j["killfeed"]) == 4
    dids = {b["discord_id"] for b in j["board"]}
    assert "A" in dids and "B" in dids


def test_unmapped_aggregates_per_seal(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    code, j = _run(rr_mod.admin_unmapped, _Req(params={
        "discord_id": "ADM", "room_id": "R-1", "match_version": "2"}))
    assert code == 200, j
    assert [u["name"] for u in j["unmapped"]] == ["carol"]
    code, j = _run(rr_mod.admin_unmapped, _Req(params={
        "discord_id": "ADM", "room_id": "R-1", "match_version": "1"}))
    # dave (killer) sorts first with the kill; alice shows via the
    # unattributed victim slot on his kill row
    assert code == 200 and [u["name"] for u in j["unmapped"]] == ["dave", "alice"]
    assert j["unmapped"][0]["kills"] == 1 and j["unmapped"][1]["deaths"] == 1


def test_link_updates_rows_and_recalcs(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    recalc_calls = []

    def fake_recalc(rid, mver):
        recalc_calls.append((rid, mver))
        return {"diffs": {"C": 8}, "totals": {"C": 8}}
    monkeypatch.setattr(ranked_api_mod, "_recalc_sync", fake_recalc)

    code, j = _run(rr_mod.admin_link, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM",
        "actor_name": "carol", "player_discord_id": "C"}))
    assert code == 200, j
    assert j["linked"] == 1
    assert recalc_calls == [("R-1", 2)]
    row = next(r for r in state["bngc_ranked_events"] if r["id"] == 4)
    assert row["discord_id"] == "C"
    # unlink: null did clears, no recalc failure
    code, j = _run(rr_mod.admin_link, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM",
        "actor_name": "carol", "player_discord_id": None}))
    assert code == 200 and j["linked"] == 1
    row = next(r for r in state["bngc_ranked_events"] if r["id"] == 4)
    assert row["discord_id"] is None


def test_link_apply_to_all_backfills_other_versions(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)

    def fake_recalc(rid, mver):
        return {"diffs": {}}
    monkeypatch.setattr(ranked_api_mod, "_recalc_sync", fake_recalc)

    code, j = _run(rr_mod.admin_link, _Req(body={
        "room_id": "R-1", "match_version": 1, "discord_id": "ADM",
        "actor_name": "dave", "player_discord_id": "D",
        "apply_to_all": True}))
    assert code == 200, j
    assert j["linked"] == 1  # v1 row
    # v2 has no dave rows; backfill visited but changed nothing
    assert j["linked_all"] == 0


def test_place_evicts_holder_and_recalcs(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    monkeypatch.setattr(ranked_api_mod, "_recalc_sync",
                        lambda rid, mver: {"diffs": {"C": 12, "A": -12}})

    code, j = _run(rr_mod.admin_place, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM",
        "player_discord_id": "C", "place": 1}))
    assert code == 200, j
    assert j["placements"] == {"C": 1, "B": 2}  # A evicted from P1
    res = state["ranked_results"][0]
    assert res["placements"] == {"C": 1, "B": 2}
    # clear a placement
    code, j = _run(rr_mod.admin_place, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM",
        "player_discord_id": "C", "place": None}))
    assert code == 200 and j["placements"] == {"B": 2}
    # validation
    code, _ = _run(rr_mod.admin_place, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM",
        "player_discord_id": "C", "place": 99}))
    assert code == 400


def test_remove_and_add_back_toggle(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    monkeypatch.setattr(ranked_api_mod, "_recalc_sync",
                        lambda rid, mver: {"diffs": {"B": 5}})

    code, j = _run(rr_mod.admin_remove, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM",
        "player_discord_id": "B"}))
    assert code == 200, j
    assert j["removed_discord_ids"] == ["B"]
    mrow = next(r for r in state["bngc_ranked_matches"]
                if r["match_version"] == 2)
    assert mrow["removed_discord_ids"] == ["B"]
    # idempotent toggle: removing again keeps one entry
    code, j = _run(rr_mod.admin_remove, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM",
        "player_discord_id": "B"}))
    assert code == 200 and j["removed_discord_ids"] == ["B"]
    # add back
    code, j = _run(rr_mod.admin_add_back, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM",
        "player_discord_id": "B"}))
    assert code == 200 and j["removed_discord_ids"] == []
    # missing match row -> 404
    code, j = _run(rr_mod.admin_remove, _Req(body={
        "room_id": "R-1", "match_version": 9, "discord_id": "ADM",
        "player_discord_id": "B"}))
    assert code == 404


def test_winner_crown_swap_and_recalc(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    monkeypatch.setattr(ranked_api_mod, "_recalc_sync",
                        lambda rid, mver: {"diffs": {"B": 55, "A": -55},
                                           "totals": {}})

    code, j = _run(rr_mod.admin_winner, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM",
        "winner_discord_id": "B"}))
    assert code == 200, j
    assert j["winner_discord_id"] == "B"
    res = state["ranked_results"][0]
    assert res["winner_discord_id"] == "B"
    # P1 swap: B takes 1, displaced A takes B's old place
    assert res["placements"] == {"A": 2, "B": 1}
    # clear the crown
    code, j = _run(rr_mod.admin_winner, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM",
        "winner_discord_id": None}))
    assert code == 200 and j["winner_discord_id"] is None
    res = state["ranked_results"][0]
    assert res["winner_discord_id"] is None
    assert res["placements"] == {"A": 2, "B": 1}  # order kept


def test_recalc_endpoint_differential(monkeypatch):
    state = _state()
    _patch(monkeypatch, state)
    calls = []

    def fake_recalc(rid, mver):
        calls.append((rid, mver))
        return {"points": {"A": 31}, "diffs": {}, "totals": {}}
    monkeypatch.setattr(ranked_api_mod, "_recalc_sync", fake_recalc)

    code, j = _run(rr_mod.admin_recalc, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM"}))
    assert code == 200 and j["ok"] is True and j["diffs"] == {}
    assert calls == [("R-1", 2)]
    # unsealed version -> 404 from the fake returning None
    monkeypatch.setattr(ranked_api_mod, "_recalc_sync",
                        lambda rid, mver: None)
    code, _ = _run(rr_mod.admin_recalc, _Req(body={
        "room_id": "R-1", "match_version": 2, "discord_id": "ADM"}))
    assert code == 404
