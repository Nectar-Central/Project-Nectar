"""GET /api/comp/match (board + killfeed) and open matches in tourney detail.

Handler called directly with faked supabase (chainable stub). No network.
"""
import asyncio
import json

from server.routes import comp as comp_mod


class _R:
    def __init__(self, data):
        self.data = data


class _T:
    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]

    def select(self, *a):
        return self

    def eq(self, c, v):
        self._rows = [r for r in self._rows if str(r.get(c)) == str(v)]
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

    def execute(self):
        return _R(self._rows)


class _SB:
    def __init__(self, tables):
        self.t = tables

    def table(self, name):
        return _T(self.t.get(name, []))


class _Req:
    def __init__(self, params=None):
        self.query_params = dict(params or {})
        self.headers = {}


TABLE = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}
TOURNEY = {"id": 3, "name": "T3", "code": "GZ8GU", "status": "started",
           "points_table": dict(TABLE), "series_len": 0}
SEALED = {"id": 11, "tourney_id": 3, "version": 0, "room_id": "roomaaaa",
          "status": "sealed", "winner_discord_id": None, "placements": {},
          "started_at": None, "sealed_at": None}
OPEN = {"id": 12, "tourney_id": 3, "version": 1, "room_id": "roombbbb",
        "status": "open", "winner_discord_id": None, "placements": {},
        "started_at": None, "sealed_at": None}
EVENTS = [
    {"id": 1, "match_id": 11, "etype": "kill", "actor_nr": 1,
     "actor_name": "Alice", "target_name": "Bob", "discord_id": "A",
     "alive": None, "created_at": "t1"},
    {"id": 2, "match_id": 11, "etype": "kill", "actor_nr": 1,
     "actor_name": "Alice", "target_name": "Cara", "discord_id": "A",
     "alive": None, "created_at": "t2"},
    {"id": 3, "match_id": 11, "etype": "alv", "actor_nr": 2,
     "actor_name": "Bob", "target_name": None, "discord_id": "B",
     "alive": False, "created_at": "t3"},
    {"id": 4, "match_id": 11, "etype": "alv", "actor_nr": 3,
     "actor_name": "Cara", "target_name": None, "discord_id": "C",
     "alive": False, "created_at": "t4"},
]
USERS = [
    {"discord_id": "A", "game_username": "alice", "discord_username": "a"},
    {"discord_id": "B", "game_username": "bobby", "discord_username": "b"},
    {"discord_id": "C", "game_username": None, "discord_username": "cee"},
]
REGS = [{"team_label": None, "member_ids": ["A", "B", "C"],
         "captain_id": "A", "status": "accepted"}]


def _fake_tables():
    return {"bngc_comp_tourneys": [TOURNEY],
            "bngc_comp_matches": [SEALED, OPEN],
            "bngc_comp_events": list(EVENTS),
            "bngc_users": list(USERS),
            "bngc_comp_registrations": list(REGS)}


def _call(monkeypatch, fn, params):
    monkeypatch.setattr(comp_mod, "supabase", _SB(_fake_tables()))
    resp = asyncio.run(fn(_Req(params)))
    return resp.status_code, json.loads(resp.body.decode())


def test_match_detail_board_and_killfeed(monkeypatch):
    code, j = _call(monkeypatch, comp_mod.comp_match_detail, {"id": "11"})
    assert code == 200, j
    assert j["match"]["version"] == 0
    assert j["match"]["status"] == "sealed"
    by = {r["discord_id"]: r for r in j["board"]}
    assert by["A"]["kills"] == 2
    assert by["A"]["name"] == "alice"  # registered name wins
    assert [e["id"] for e in j["killfeed"]] == [1, 2, 3, 4]
    assert j["killfeed"][0]["etype"] == "kill"
    assert j["killfeed"][0]["target_name"] == "Bob"


def test_match_detail_open_match_has_live_board(monkeypatch):
    code, j = _call(monkeypatch, comp_mod.comp_match_detail, {"id": "12"})
    assert code == 200, j
    assert j["match"]["status"] == "open"
    assert j["board"] == [] and j["killfeed"] == []


def test_match_detail_bad_id(monkeypatch):
    code, j = _call(monkeypatch, comp_mod.comp_match_detail, {})
    assert code == 400
    code, j = _call(monkeypatch, comp_mod.comp_match_detail, {"id": "999"})
    assert code == 404


def test_tourney_detail_lists_open_matches(monkeypatch):
    code, j = _call(monkeypatch, comp_mod.tourney_detail, {"id": "3"})
    assert code == 200, j
    assert len(j["matches"]) == 2  # sealed v0 + open v1
    assert [m["version"] for m in j["matches"]] == [0, 1]
    assert len(j["match_boards"]) == 1  # boards: sealed only
    assert j["standings"]  # series board from the sealed match
