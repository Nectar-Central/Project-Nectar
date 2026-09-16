"""Ranked tourneys: independent tables, comp-identical shapes, ladder rank.

Handlers called directly with a writable fake supabase (chainable stub).
Covers: route registration (29 mirrored + create/status, no collisions),
tourney create validation, rank display from the ladder, dynamic scoring
(freeze + replay + legacy fallback), and the userscript shape dispatchers
(GET match ?room_id=, POST winner {room_id}) still reaching ranked_api.
No network.
"""
import asyncio
import json

from server.main import app
from server.routes import ranked_tourney as rt_mod
from server.routes import ranked_api as ranked_mod


MIRROR_GET = [
    "/ranked/status",
    "/api/ranked/tourneys",
    "/api/ranked/tourney",
    "/api/ranked/names",
    "/api/ranked/regs",
    "/api/ranked/match",
    "/api/ranked/match/unmapped",
]

MIRROR_POST = [
    "/api/ranked/tourney/host-playing",
    "/api/ranked/tourney/hosts",
    "/api/ranked/tourney/table",
    "/api/ranked/calculate",
    "/api/ranked/reg/status",
    "/api/ranked/adjust",
    "/api/ranked/match/open",
    "/api/ranked/match/seal",
    "/api/ranked/match/start",
    "/api/ranked/match/unseal",
    "/api/ranked/match/add-player",
    "/api/ranked/match/winner",
    "/api/ranked/match/tie",
    "/api/ranked/match/reset",
    "/api/ranked/match/delete",
    "/api/ranked/match/place",
    "/api/ranked/match/kills",
    "/api/ranked/match/elim",
    "/api/ranked/match/points",
    "/api/ranked/match/remove",
    "/api/ranked/match/add-back",
    "/api/ranked/match/link",
]

EXTRA = [
    "/api/ranked/tourney/create",
]


def _routes():
    out = {}
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = set(getattr(r, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
        if path and methods:
            out.setdefault(path, set()).update(methods)
    return out


def test_all_29_mirror_paths_registered_once():
    routes = _routes()
    for path in MIRROR_GET:
        assert routes.get(path) == {"GET"}, (path, routes.get(path))
    for path in MIRROR_POST:
        assert routes.get(path) == {"POST"}, (path, routes.get(path))
    assert len(MIRROR_GET) + len(MIRROR_POST) == 29


def test_ranked_extras_registered():
    routes = _routes()
    assert routes.get("/api/ranked/tourney/create") == {"POST"}
    assert "/api/ranked/elo" not in routes  # retired: rank points instead


def test_ranked_tables_independent_from_comp():
    import inspect
    src = inspect.getsource(rt_mod)
    assert "bngc_comp_" not in src
    for tbl in ("bngc_ranked_tourneys", "bngc_ranked_tourney_matches",
                "bngc_ranked_tourney_events",
                "bngc_ranked_tourney_registrations",
                "bngc_ranked_tourney_results",
                "bngc_ranked_tourney_adjustments"):
        assert tbl in src, tbl


def test_kept_ranked_room_endpoints_still_registered():
    routes = _routes()
    assert routes.get("/api/ranked/table") == {"GET"}
    assert routes.get("/api/ranked/match/recalc") == {"POST"}
    assert routes.get("/api/ranked/room/backout") == {"POST"}
    assert routes.get("/api/ranked/room/end") == {"POST"}


def test_removed_dashboard_endpoints_gone():
    routes = _routes()
    for path in ("/api/ranked/rooms", "/api/ranked/room",
                 "/api/ranked/match/placements", "/api/ranked/match/removed",
                 "/api/ranked/link", "/api/ranked/link/lock",
                 "/api/ranked/link/search"):
        assert path not in routes, path


class _R:
    def __init__(self, data):
        self.data = data


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

    def order(self, c, desc=False):
        self._rows = sorted(self._rows, key=lambda r: (r.get(c) is None, r.get(c)),
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
            for r in self._upsert:
                r = dict(r)
                base[:] = [x for x in base if not (
                    str(x.get("tourney_id")) == str(r.get("tourney_id"))
                    and str(x.get("discord_id")) == str(r.get("discord_id")))]
                base.append(r)
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
        "bngc_ranked_tourneys": [{
            "id": 0, "code": "ABCDE", "name": "Ranked Cup", "status": "started",
            "mode": "solo", "series_len": 3,
            "points_table": {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]},
            "start_elo": 1000, "avg_elo": None, "elo_k": 32,
            "scheduled_at": None, "started_at": None, "ended_at": None,
            "created_at": "2026-01-01",
        }],
        "bngc_ranked_tourney_matches": [{
            "id": 11, "tourney_id": 0, "version": 0, "room_id": "room",
            "status": "sealed", "winner_discord_id": None, "placements": {},
            "removed_discord_ids": [], "point_awards": {},
            "started_at": None, "sealed_at": None,
        }],
        "bngc_ranked_tourney_events": [
            {"id": 1, "match_id": 11, "etype": "kill", "discord_id": "A",
             "actor_name": "alice", "alive": None},
            {"id": 2, "match_id": 11, "etype": "alv", "discord_id": "B",
             "actor_name": "bobby", "alive": False},
            {"id": 3, "match_id": 11, "etype": "alv", "discord_id": "A",
             "actor_name": "alice", "alive": False},
        ],
        "bngc_ranked_tourney_registrations": [{
            "tourney_id": 0, "team_label": None, "member_ids": ["A", "B"],
            "member_names": {}, "captain_id": "A", "status": "accepted",
        }],
        "bngc_users": [],
    }


def _run(fn, req):
    resp = asyncio.run(fn(req))
    return resp.status_code, json.loads(resp.body.decode())


def _call(monkeypatch, state, fn, body=None, params=None):
    monkeypatch.setattr(rt_mod, "supabase", _SB(state))
    return _run(fn, _Req(body, params))


def test_tourney_detail_rank_display_and_legacy_replay(monkeypatch):
    state = _state()
    monkeypatch.setattr(rt_mod, "_ladder_points",
                        lambda dids, fresh=False: {"A": 1200, "B": 300})
    code, j = _call(monkeypatch, state, rt_mod.tourney_detail, params={"id": "0"})
    assert code == 200, j
    # rank snapshot from the ladder (Gold avg), not the Elo table
    assert j["tourney"]["rank_avg"] == 750.0
    assert j["tourney"]["rank_milestone"] == "Silver"
    by = {r["discord_id"]: r for r in j["standings"]}
    assert by["A"]["rank_pts"] == 1200
    assert by["B"]["rank_pts"] == 300
    assert "elo" not in by["A"]
    # legacy replay: no frozen table -> old static math, no loss
    assert by["A"]["points"] == 1 + 3 + 10
    assert by["B"]["points"] == 7


def test_tourney_detail_rank_empty_without_ladder(monkeypatch):
    state = _state()
    monkeypatch.setattr(rt_mod, "_ladder_points", lambda dids, fresh=False: {})
    code, j = _call(monkeypatch, state, rt_mod.tourney_detail, params={"id": "0"})
    assert code == 200, j
    assert j["tourney"]["rank_avg"] is None
    assert j["tourney"]["rank_milestone"] is None
    assert all("rank_pts" not in r for r in j["standings"])


def test_tourney_create_validation_and_seed(monkeypatch):
    state = {"bngc_ranked_tourneys": []}
    code, j = _call(monkeypatch, state, rt_mod.ranked_tourney_create,
                    {"name": "  Cup  "})
    assert code == 200, j
    assert j["tourney_id"] == 1 and j["name"] == "Cup"
    row = state["bngc_ranked_tourneys"][0]
    assert row["name"] == "Cup"
    for body, want in [({}, 400), ({"name": "x", "mode": "trio"}, 400),
                       ({"name": "x", "code": "ab!"}, 400),
                       ({"name": "x", "code": "abcd"}, 400)]:
        code, _ = _call(monkeypatch, state, rt_mod.ranked_tourney_create, body)
        assert code == want, (body, code)


def test_match_dispatcher_routes_by_shape(monkeypatch):
    calls = []

    async def fake_ranked(request):
        calls.append("ranked")
        from fastapi.responses import JSONResponse
        return JSONResponse({"via": "ranked"})

    monkeypatch.setattr(ranked_mod, "ranked_match_detail", fake_ranked)
    code, j = _run(rt_mod.comp_match_detail,
                   _Req(params={"room_id": "R-X", "match_version": "2"}))
    assert code == 200 and j == {"via": "ranked"}
    assert calls == ["ranked"]


def test_winner_dispatcher_routes_by_shape(monkeypatch):
    calls = []

    async def fake_ranked(request):
        calls.append("ranked")
        from fastapi.responses import JSONResponse
        return JSONResponse({"via": "ranked"})

    monkeypatch.setattr(ranked_mod, "ranked_match_winner", fake_ranked)
    code, j = _run(rt_mod.comp_match_winner,
                   _Req(body={"room_id": "R-X", "match_version": 2,
                              "discord_id": "H", "winner_discord_id": "A"}))
    assert code == 200 and j == {"via": "ranked"}
    assert calls == ["ranked"]


def test_tourney_delta_formulas():
    # upset direction: weak killer vs champ victim pays more than reverse
    hi = rt_mod.tourney_kill_delta(0, 4000, 1000, 8)
    lo = rt_mod.tourney_kill_delta(4000, 0, 1000, 8)
    assert 4 <= lo < hi <= 20
    # bigger lobbies pay more, all else equal
    assert (rt_mod.tourney_kill_delta(500, 500, 500, 12)
            > rt_mod.tourney_kill_delta(500, 500, 500, 2))
    assert rt_mod.tourney_kill_delta(None, None, None, None) == 8
    # win grows with winner points and lobby size, bounded
    assert rt_mod.tourney_win_delta(0, 2) < rt_mod.tourney_win_delta(7500, 16)
    assert 35 <= rt_mod.tourney_win_delta(0, 2) <= 95
    assert rt_mod.tourney_win_delta(99999, 99) == 95
    assert rt_mod.tourney_win_delta(None, None) == 52  # n floors at 1
    # placement: better place pays more; stronger player + lobby pay more
    assert rt_mod.tourney_place_delta(1, 0, 0) > rt_mod.tourney_place_delta(5, 0, 0)
    assert (rt_mod.tourney_place_delta(1, 5000, 4000)
            > rt_mod.tourney_place_delta(1, 0, 0))
    assert rt_mod.tourney_place_delta(0, 0, 0) == 0
    assert rt_mod.tourney_place_delta(6, 0, 0) == 0


def test_freeze_match_table_from_ladder(monkeypatch):
    state = _state()
    state["bngc_ranked_tourney_events"][0]["target_name"] = "bobby"
    state["bngc_ranked_tourney_registrations"][0]["member_names"] = {
        "B": {"username": "bobby", "display_name": "bobby"}}
    monkeypatch.setattr(rt_mod, "supabase", _SB(state))
    monkeypatch.setattr(rt_mod, "_ladder_points",
                        lambda dids, fresh=False: {"A": 1200, "B": 300})
    table = rt_mod.freeze_match_table(11)
    assert table["mode"] == "tourney-dynamic-v1"
    assert table["players"] == 2 and table["avg_points"] == 750.0
    assert table["kill_awards"] == [{
        "killer": "A", "victim": "B", "nth": 1,
        "points": rt_mod.tourney_kill_delta(1200, 300, 750.0, 2)}]
    assert table["win"] == rt_mod.tourney_win_delta(1200, 2)
    assert table["placement"][:2] == [
        rt_mod.tourney_place_delta(1, 1200, 750.0),
        rt_mod.tourney_place_delta(2, 300, 750.0)]
    assert table["loss_awards"] == {
        "A": rt_mod._ladder_loss(1200), "B": rt_mod._ladder_loss(300)}
    # stored back on the match row
    mrow = next(r for r in state["bngc_ranked_tourney_matches"] if r["id"] == 11)
    assert mrow["points_table"] == table and mrow["avg_points"] == 750.0


def test_score_replays_frozen_and_legacy():
    events = [
        {"etype": "kill", "discord_id": "A", "actor_name": "a", "alive": None},
        {"etype": "alv", "discord_id": "B", "actor_name": "b", "alive": False},
        {"etype": "alv", "discord_id": "A", "actor_name": "a", "alive": False},
    ]
    base = {"id": 11, "winner_discord_id": None, "placements": {},
            "removed_discord_ids": [], "point_awards": {}}
    frozen = dict(base, points_table={
        "mode": "tourney-dynamic-v1", "avg_points": 750.0, "players": 2,
        "kill": None, "win": 68, "placement": [12, 8, 6, 6, 6],
        "kill_awards": [{"killer": "A", "victim": "B", "nth": 1, "points": 15}],
        "loss_awards": {"A": -14, "B": -8}})
    by = {r["discord_id"]: r for r in rt_mod._score_tourney_match({}, frozen, events)}
    assert by["A"]["points"] == 68 + 12 + 15 and by["A"]["won"] is True
    assert by["B"]["points"] == 8 - 8 and by["B"]["won"] is False
    # legacy replay (no frozen table): old static math, no loss
    legacy = dict(base, points_table=None)
    t = {"points_table": {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}}
    by2 = {r["discord_id"]: r for r in rt_mod._score_tourney_match(t, legacy, events)}
    assert by2["A"]["points"] == 1 + 3 + 10
    assert by2["B"]["points"] == 7


def test_tourney_create_stores_game_mode(monkeypatch):
    state = {"bngc_ranked_tourneys": []}
    code, j = _call(monkeypatch, state, rt_mod.ranked_tourney_create,
                    {"name": "Cup", "game_mode": "zw_lg"})
    assert code == 200, j
    assert state["bngc_ranked_tourneys"][0]["game_mode"] == "zw_lg"
