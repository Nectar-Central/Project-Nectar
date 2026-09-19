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
    "/api/ranked/tourney/code",
    "/api/ranked/calculate",
    "/api/ranked/reg/status",
    "/api/ranked/reg/add",
    "/api/ranked/reg/remove",
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
    assert len(MIRROR_GET) + len(MIRROR_POST) == 32


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
        self._delete = False

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

    def delete(self):
        self._delete = True
        return self

    def execute(self):
        base = self._state.setdefault(self._name, [])
        if self._delete:
            doomed = list(self._rows)
            base[:] = [x for x in base if x not in doomed]
            return _R([dict(r) for r in doomed])
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
    # rank snapshot from the ladder (Bronze avg), not the Elo table
    assert j["tourney"]["rank_avg"] == 750.0
    assert j["tourney"]["rank_milestone"] == "Bronze"
    by = {r["discord_id"]: r for r in j["standings"]}
    assert by["A"]["rank_pts"] == 1200
    assert by["B"]["rank_pts"] == 300
    assert "elo" not in by["A"]
    # legacy replay: no frozen table -> old static math, no loss
    assert by["A"]["points"] == 1 + 3 + 10
    assert by["B"]["points"] == 7


def test_tourney_detail_rank_zeros_without_ladder_rows(monkeypatch):
    """Unranked rosters still show an average (0.0 / Unranked), never a dash."""
    state = _state()
    monkeypatch.setattr(rt_mod, "_ladder_points", lambda dids, fresh=False: {})
    code, j = _call(monkeypatch, state, rt_mod.tourney_detail, params={"id": "0"})
    assert code == 200, j
    assert j["tourney"]["rank_avg"] == 0.0
    assert j["tourney"]["rank_milestone"] == "Unranked"
    by = {r["discord_id"]: r for r in j["standings"]}
    assert by["A"]["rank_pts"] == 0 and by["B"]["rank_pts"] == 0


def test_tourney_detail_hosts_payload(monkeypatch):
    """The dashboard hosts panel needs creator + hosts + host_playing."""
    state = _state()
    state["bngc_ranked_tourneys"][0].update({
        "created_by": "A", "hosts": ["B"], "host_playing": False})
    monkeypatch.setattr(rt_mod, "_ladder_points",
                        lambda dids, fresh=False: {"A": 1200, "B": 300})
    code, j = _call(monkeypatch, state, rt_mod.tourney_detail, params={"id": "0"})
    assert code == 200, j
    t = j["tourney"]
    assert t["created_by"] == "A" and t["hosts"] == ["B"]
    assert t["host_playing"] is False
    # roster ids decide per-host status client-side (A and B both play)
    assert {p["discord_id"] for p in j["players"]} == {"A", "B"}


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
    # win grows with winner points and lobby size, bounded under the clamp
    # so weak lobbies feel the scale instead of saturating at +30 every win
    assert rt_mod.tourney_win_delta(0, 2) < rt_mod.tourney_win_delta(7500, 16)
    assert 12 <= rt_mod.tourney_win_delta(0, 2) <= 30
    assert rt_mod.tourney_win_delta(99999, 99) == 30
    assert rt_mod.tourney_win_delta(None, None) == 19  # n floors at 1
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
    # top-half purse: 2 players -> P1 only, last place pays nothing
    assert table["placement"] == [
        rt_mod.tourney_place_delta(1, 1200, 750.0)]
    # killer-relative losses: B died to a much stronger killer (cheap),
    # A (winner, unkilled) keeps the flat tier stake
    assert table["loss_awards"] == {"A": -11, "B": -3}
    # per-player seal components stored for toasts/embeds (clamped totals)
    assert set(table["breakdown"]) == {"A", "B"}
    assert table["breakdown"]["A"]["total"] == 30
    assert table["breakdown"]["A"]["capped"] is True
    assert table["breakdown"]["B"] == {"kills": 0, "win": 0, "placement": 0,
                                       "loss": -3, "total": -3, "capped": False}
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
    # frozen economy clamps to the ranked per-match clamp (-50/+30, rooms parity)
    assert by["A"]["points"] == 30 and by["A"]["won"] is True
    assert by["B"]["points"] == 8 - 8 and by["B"]["won"] is False
    rows, bd = rt_mod._score_frozen_with_breakdown(
        frozen["points_table"], frozen, events)
    assert {r["discord_id"]: r["points"] for r in rows} == {"A": 30, "B": 0}
    assert bd["A"] == {"kills": 15, "win": 68, "placement": 12, "loss": 0,
                       "total": 30, "capped": True}
    assert bd["B"] == {"kills": 0, "win": 0, "placement": 8, "loss": -8,
                       "total": 0, "capped": False}
    # legacy replay (no frozen table): old static math, no loss
    legacy = dict(base, points_table=None)
    t = {"points_table": {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}}
    by2 = {r["discord_id"]: r for r in rt_mod._score_tourney_match(t, legacy, events)}
    assert by2["A"]["points"] == 1 + 3 + 10
    assert by2["B"]["points"] == 7


def test_frozen_loss_and_breakdown_shape():
    # placement softens the loss; components stay intact for toasts
    events = [
        {"etype": "alv", "discord_id": "B", "actor_name": "b", "alive": False},
        {"etype": "alv", "discord_id": "A", "actor_name": "a", "alive": False},
    ]
    m = {"id": 11, "winner_discord_id": "A", "placements": {},
         "removed_discord_ids": [], "point_awards": {},
         "points_table": {
             "mode": "tourney-dynamic-v1", "avg_points": 0.0, "players": 2,
             "kill": None, "win": 35, "placement": [6, 6, 6, 6, 6],
             "kill_awards": [], "loss_awards": {"A": -5, "B": -50}}}
    rows, bd = rt_mod._score_frozen_with_breakdown(m["points_table"], m, events)
    by = {r["discord_id"]: r for r in rows}
    assert by["B"]["points"] == -44
    assert bd["B"] == {"kills": 0, "win": 0, "placement": 6, "loss": -50,
                       "total": -44, "capped": False}
    assert bd["A"]["capped"] is True and bd["A"]["total"] == 30


def test_frozen_loss_only_victim_boards():
    # Eliminated victim with no kills and no mapped alv row: no engine
    # row, but the frozen loss must still board (loss-only) or the death
    # vanishes — no board, no ladder pay, no toast.
    events = [
        {"etype": "kill", "discord_id": "A", "actor_name": "a",
         "target_name": "b", "alive": None},
        {"etype": "alv", "discord_id": "A", "actor_name": "a", "alive": False},
    ]
    m = {"id": 11, "winner_discord_id": "A", "placements": {},
         "removed_discord_ids": [], "point_awards": {},
         "points_table": {
             "mode": "tourney-dynamic-v1", "avg_points": 0.0, "players": 2,
             "kill": None, "win": 35, "placement": [6, 6, 6, 6, 6],
             "kill_awards": [{"killer": "A", "victim": "B", "nth": 1, "points": 5}],
             "loss_awards": {"A": -5, "B": -32}}}
    rows, bd = rt_mod._score_frozen_with_breakdown(m["points_table"], m, events)
    by = {r["discord_id"]: r for r in rows}
    assert "B" in by, "victim without engine row must still board"
    assert by["B"]["points"] == -32 and by["B"]["won"] is False
    assert by["B"]["kills"] == 0 and by["B"]["placement"] is None
    assert bd["B"] == {"kills": 0, "win": 0, "placement": 0, "loss": -32,
                       "total": -32, "capped": False}
    assert by["B"]["breakdown"] == bd["B"]


def test_pay_sealed_ladders_dynamic_only(monkeypatch):
    dyn = {"id": 21, "tourney_id": 0, "version": 3, "status": "sealed",
           "points_table": {
               "mode": "tourney-dynamic-v1", "win": 68,
               "placement": [12, 8, 6, 6, 6], "kill_awards": [],
               "loss_awards": {},
               "breakdown": {"A": {"kills": 0, "win": 68, "placement": 12,
                                   "loss": 0, "total": 30, "capped": True},
                             "B": {"kills": 0, "win": 0, "placement": 8,
                                   "loss": -8, "total": 0, "capped": False}}}}
    bundle = {"id": 22, "tourney_id": 0, "version": 4, "status": "sealed",
              "points_table": {"mode": "room-seal-v1", "points": {"A": 30}}}
    manual = {"id": 23, "tourney_id": 0, "version": 5, "status": "manual",
              "points_table": dict(dyn["points_table"])}
    state = {"bngc_ranked_tourney_matches": [dyn, bundle, manual]}
    monkeypatch.setattr(rt_mod, "supabase", _SB(state))
    monkeypatch.setattr(rt_mod, "_ranked_sleeping", lambda: False)
    bumped = []
    monkeypatch.setattr(rt_mod, "_ladder_bump",
                        lambda d: bumped.append(dict(d)) or {})
    paid = rt_mod._pay_sealed_ladders([dyn, bundle, manual])
    assert bumped == [{"A": 30}]
    assert paid == {21: {"A": 30}}
    assert dyn["points_table"]["ladder_paid"] == {"A": 30}
    assert "ladder_paid" not in bundle["points_table"]
    assert "ladder_paid" not in manual["points_table"]
    # second run: marker makes it exactly-once
    assert rt_mod._pay_sealed_ladders([dyn, bundle, manual]) == {}
    assert len(bumped) == 1


def test_pay_sealed_ladders_skipped_when_sleeping(monkeypatch):
    dyn = {"id": 21, "tourney_id": 0, "version": 3, "status": "sealed",
           "points_table": {
               "mode": "tourney-dynamic-v1",
               "breakdown": {"A": {"kills": 0, "win": 35, "placement": 6,
                                   "loss": 0, "total": 30, "capped": True}}}}
    monkeypatch.setattr(rt_mod, "supabase", _SB({"bngc_ranked_tourney_matches": [dyn]}))
    monkeypatch.setattr(rt_mod, "_ranked_sleeping", lambda: True)
    bumped = []
    monkeypatch.setattr(rt_mod, "_ladder_bump",
                        lambda d: bumped.append(dict(d)) or {})
    assert rt_mod._pay_sealed_ladders([dyn]) == {}
    assert bumped == [] and "ladder_paid" not in dyn["points_table"]


def test_code_tourney_points_last_result(monkeypatch):
    state = _state()
    state["bngc_ranked_tourney_matches"][0].update({
        "version": 3,
        "points_table": {
            "mode": "tourney-dynamic-v1", "win": 68,
            "placement": [12, 8, 6, 6, 6], "kill_awards": [],
            "loss_awards": {},
            "breakdown": {"A": {"kills": 0, "win": 68, "placement": 12,
                                "loss": 0, "total": 30, "capped": True}}},
    })
    monkeypatch.setattr(rt_mod, "supabase", _SB(state))
    monkeypatch.setattr(rt_mod, "_ladder_points",
                        lambda dids, fresh=False: {"A": 1200, "B": 300})
    out = rt_mod.code_tourney_points("ABCDE", "A")
    assert out["tourney_id"] == 0
    lr = out["last_result"]
    assert lr["match_version"] == 3 and lr["gained"] == 30
    assert lr["breakdown"] == {"kills": 0, "win": 68, "placement": 12,
                               "loss": 0, "total": 30, "capped": True}
    assert out["series_len"] == 3 and out["sealed"] == 1


def test_match_ladder_paid_states():
    dyn_paid = {"status": "sealed", "points_table": {
        "mode": "tourney-dynamic-v1", "ladder_paid": {"A": 30}}}
    dyn_unpaid = {"status": "sealed", "points_table": {
        "mode": "tourney-dynamic-v1",
        "breakdown": {"A": {"total": 30}}}}
    bundle = {"status": "sealed", "points_table": {"mode": "room-seal-v1"}}
    legacy = {"status": "sealed", "points_table": None}
    manual_unpaid = {"status": "manual", "points_table": dict(dyn_paid["points_table"])}
    assert rt_mod._match_ladder_paid(dyn_paid, {}) is True
    assert rt_mod._match_ladder_paid(dyn_unpaid, {}) is False
    assert rt_mod._match_ladder_paid(bundle, {}) is True
    assert rt_mod._match_ladder_paid(legacy, {}) is False
    assert rt_mod._match_ladder_paid(manual_unpaid, {}) is False
    assert rt_mod._match_ladder_paid(manual_unpaid, {"points_paid": True}) is True
    assert rt_mod._match_ladder_paid(None, None) is False


def test_reset_loser_streaks_sync(monkeypatch):
    state = _state()
    state["bngc_ranked_tourney_matches"][0].update({
        "version": 3, "winner_discord_id": "A",
        "placements": {"A": 1, "B": 2, "C": 3},
        "removed_discord_ids": ["C"],
    })
    state["bngc_users"] = [
        {"discord_id": "A", "win_streak": 4},
        {"discord_id": "B", "win_streak": 3},
        {"discord_id": "C", "win_streak": 9},
    ]
    monkeypatch.setattr(rt_mod, "supabase", _SB(state))
    assert rt_mod.reset_loser_streaks_sync(11) == 1
    by = {u["discord_id"]: u["win_streak"] for u in state["bngc_users"]}
    assert by == {"A": 4, "B": 0, "C": 9}  # winner keeps, loser reset, leaver spared
    # idempotent-ish: nothing left to reset
    assert rt_mod.reset_loser_streaks_sync(11) == 1
    assert rt_mod.reset_loser_streaks_sync(999) == 0


def test_tourney_create_stores_game_mode(monkeypatch):
    state = {"bngc_ranked_tourneys": []}
    code, j = _call(monkeypatch, state, rt_mod.ranked_tourney_create,
                    {"name": "Cup", "game_mode": "zw_lg"})
    assert code == 200, j
    assert state["bngc_ranked_tourneys"][0]["game_mode"] == "zw_lg"

# ---------------------------------------------------------------------------
# Per-seal stored deltas: board replays the room seal verbatim
# ---------------------------------------------------------------------------

_SEAL_FT = {
    "mode": "room-seal-v1", "rid": "R-X", "room_match_version": 3,
    "winner": "A",
    "points": {"A": 20, "B": -3, "C": 0},
    "kills": {"A": 2},
    "placements": {"A": 1, "B": 2},
    "breakdown": {},
}
_SEAL_EVENTS = [
    {"discord_id": "A", "actor_name": "cis6led"},
    {"discord_id": "B", "actor_name": "chris7"},
]


def test_room_seal_bundle_scores_verbatim():
    m = {"winner_discord_id": "A", "point_awards": {},
         "points_table": dict(_SEAL_FT)}
    rows = rt_mod._score_tourney_match(None, m, list(_SEAL_EVENTS))
    assert [(r["discord_id"], r["points"], r["kills"], r["won"],
             r["placement"], r["name"]) for r in rows] == [
        ("A", 20, 2, True, 1, "cis6led"),
        ("C", 0, 0, False, None, "C"),
        ("B", -3, 0, False, 2, "chris7"),
    ]


def test_room_seal_bundle_applies_manual_awards():
    m = {"winner_discord_id": "A",
         "point_awards": {"B": 5},
         "points_table": dict(_SEAL_FT)}
    rows = rt_mod._score_tourney_match(None, m, list(_SEAL_EVENTS))
    assert next(r for r in rows if r["discord_id"] == "B")["points"] == 2


def test_room_seal_bundle_empty_points_scores_nothing():
    ft = dict(_SEAL_FT, points={})
    assert rt_mod._score_tourney_match(
        None, {"points_table": ft}, list(_SEAL_EVENTS)) == []


def test_room_seal_bundle_aggregates_like_ladder():
    from server.lib.scoring import aggregate_standings
    m1 = {"winner_discord_id": "A", "point_awards": {},
          "points_table": dict(_SEAL_FT)}
    m2 = {"winner_discord_id": "B", "point_awards": {},
          "points_table": dict(_SEAL_FT, winner="B",
                               points={"A": -4, "B": 18, "C": 2},
                               kills={"B": 1},
                               placements={"B": 1, "C": 2, "A": 3})}
    board = aggregate_standings([
        rt_mod._score_tourney_match(None, m1, list(_SEAL_EVENTS)),
        rt_mod._score_tourney_match(None, m2, list(_SEAL_EVENTS)),
    ])
    totals = {r["discord_id"]: r["points"] for r in board}
    assert totals == {"A": 16, "B": 15, "C": 2}
    assert next(r for r in board
                if r["discord_id"] == "A")["matches"] == 2


# ---------------------------------------------------------------------------
# Dashboard manual ops: code set, roster add/remove, intruder ignore
# ---------------------------------------------------------------------------

def test_tourney_code_set_clear_and_dup(monkeypatch):
    state = {"bngc_ranked_tourneys": [
        {"id": 0, "code": None, "status": "created"},
        {"id": 1, "code": "ZZZZZ", "status": "started"},
    ]}
    code, j = _call(monkeypatch, state, rt_mod.comp_tourney_code,
                    {"tourney_id": 0, "code": "abcde"})
    assert code == 200, j
    assert j["code"] == "ABCDE"
    assert state["bngc_ranked_tourneys"][0]["code"] == "ABCDE"
    # duplicate of another live session rejected
    code, j = _call(monkeypatch, state, rt_mod.comp_tourney_code,
                    {"tourney_id": 0, "code": "zzzzz"})
    assert code == 400 and "already used" in j["error"]
    # bad shape rejected
    code, j = _call(monkeypatch, state, rt_mod.comp_tourney_code,
                    {"tourney_id": 0, "code": "toolong"})
    assert code == 400
    # clear works
    code, j = _call(monkeypatch, state, rt_mod.comp_tourney_code,
                    {"tourney_id": 0, "code": ""})
    assert code == 200 and j["code"] is None


def test_reg_add_remove_roundtrip(monkeypatch):
    state = {"bngc_ranked_tourney_registrations": []}
    code, j = _call(monkeypatch, state, rt_mod.comp_reg_add,
                    {"tourney_id": 0, "discord_id": "N", "display_name": "Newbie"})
    assert code == 200 and j["present"] is False
    regs = state["bngc_ranked_tourney_registrations"]
    assert len(regs) == 1 and regs[0]["status"] == "accepted"
    assert regs[0]["member_ids"] == ["N"]
    # idempotent second add
    code, j = _call(monkeypatch, state, rt_mod.comp_reg_add,
                    {"tourney_id": 0, "discord_id": "N"})
    assert code == 200 and j["present"] is True
    assert len(state["bngc_ranked_tourney_registrations"]) == 1
    # remove deletes the solo accepted row
    code, j = _call(monkeypatch, state, rt_mod.comp_reg_remove,
                    {"tourney_id": 0, "discord_id": "N"})
    assert code == 200 and j["removed"] == 1
    assert state["bngc_ranked_tourney_registrations"] == []
    # removing a stranger is ok/not-present
    code, j = _call(monkeypatch, state, rt_mod.comp_reg_remove,
                    {"tourney_id": 0, "discord_id": "N"})
    assert code == 200 and j["present"] is True


def test_reg_add_refuses_banned(monkeypatch):
    state = {"bngc_ranked_tourney_registrations": [
        {"id": 9, "tourney_id": 0, "member_ids": ["B"],
         "member_names": {}, "status": "banned"},
    ]}
    code, j = _call(monkeypatch, state, rt_mod.comp_reg_add,
                    {"tourney_id": 0, "discord_id": "B"})
    assert code == 400 and "banned" in j["error"]


def test_match_remove_name_ignore_flow(monkeypatch):
    state = {
        "bngc_ranked_tourney_matches": [
            {"id": 11, "tourney_id": 0, "removed_discord_ids": []},
        ],
        "bngc_ranked_tourney_events": [
            {"id": 1, "match_id": 11, "etype": "alv", "discord_id": None,
             "actor_name": "Intruder", "target_name": None, "alive": False},
        ],
        "bngc_ranked_tourney_registrations": [],
        "bngc_users": [],
    }
    # unresolvable actor_name becomes an ignored name, not an error
    code, j = _call(monkeypatch, state, rt_mod.comp_match_remove,
                    {"match_id": 11, "actor_name": "Intruder"})
    assert code == 200, j
    assert j["ignored"] == "Intruder"
    assert state["bngc_ranked_tourney_matches"][0]["removed_discord_ids"] == ["name:intruder"]
    # unmapped hides it, ignored lists it
    code, j = _call(monkeypatch, state, rt_mod.comp_match_unmapped,
                    params={"match_id": "11"})
    assert code == 200, j
    assert j["unmapped"] == []
    assert j["ignored"] == ["Intruder"]
    # linking an ignored name is refused
    code, j = _call(monkeypatch, state, rt_mod.comp_match_link,
                    {"match_id": 11, "actor_name": "Intruder", "discord_id": "Z"})
    assert code == 400 and "ignored" in j["error"]
    # un-ignore restores the mapping flow
    code, j = _call(monkeypatch, state, rt_mod.comp_match_add_back,
                    {"match_id": 11, "actor_name": "Intruder"})
    assert code == 200, j
    assert state["bngc_ranked_tourney_matches"][0]["removed_discord_ids"] == []
    code, j = _call(monkeypatch, state, rt_mod.comp_match_unmapped,
                    params={"match_id": "11"})
    assert code == 200 and len(j["unmapped"]) == 1
    assert j["unmapped"][0]["name"] == "Intruder"


def test_ignored_names_unit():
    assert rt_mod._ignored_names({"removed_discord_ids": ["A", "name:foo bar", "name:"]}) == {"foo bar"}
    assert rt_mod._ignored_names({}) == set()
    assert rt_mod._norm_ignored_name("<color>DC: Foo Bar </color>") == "foo bar"
