"""Manual dashboard controls: start / unseal / elim / add-player (fake supabase).

Covers the Slice 1+2 backend: Match Started stamping, sealed->manual reopen,
hand-added eliminations (manual alv rows, tracked untouched) and zero-stat
subs (presence row boards at 0 via the seen-participant universe).
"""
import asyncio
import copy
import json

from server.routes import comp as comp_mod


class _R:
    def __init__(self, data):
        self.data = data


class _T:
    def __init__(self, state, name):
        self._state = state
        self._name = name
        self._rows = list(state.get(name, []))
        self._update = None
        self._delete = False
        self._insert = None

    def select(self, *a):
        return self

    def eq(self, c, v):
        self._rows = [r for r in self._rows if str(r.get(c)) == str(v)]
        return self

    def order(self, c, desc=False):
        self._rows = sorted(self._rows, key=lambda r: (r.get(c) is None, r.get(c)),
                            reverse=bool(desc))
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def insert(self, rows):
        if isinstance(rows, dict):
            rows = [rows]
        self._insert = [dict(r) for r in rows]
        return self

    def update(self, data):
        self._update = dict(data)
        return self

    def delete(self):
        self._delete = True
        return self

    def execute(self):
        base = self._state.setdefault(self._name, [])
        if self._insert is not None:
            for r in self._insert:
                if "id" not in r:
                    r["id"] = max([x.get("id", 0) for x in base] + [0]) + 1
                base.append(r)
            return _R([dict(r) for r in self._insert])
        if self._update is not None:
            for r in self._rows:
                r.update(self._update)
            return _R([dict(r) for r in self._rows])
        if self._delete:
            ids = {id(r) for r in self._rows}
            self._state[self._name] = [r for r in base if id(r) not in ids]
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
        self.headers = {}

    async def json(self):
        return dict(self._body)


def _state():
    return {
        "bngc_comp_tourneys": [{"id": 3, "name": "T3"}],
        "bngc_comp_matches": [
            {"id": 11, "tourney_id": 3, "version": 0, "status": "manual",
             "started_at": "2026-01-01T00:00:00+00:00", "sealed_at": None,
             "removed_discord_ids": [], "placements": {}, "point_awards": {},
             "winner_discord_id": None},
            {"id": 12, "tourney_id": 3, "version": 1, "status": "open",
             "started_at": None, "sealed_at": None,
             "removed_discord_ids": [], "placements": {}, "point_awards": {},
             "winner_discord_id": None},
            {"id": 13, "tourney_id": 3, "version": 2, "status": "sealed",
             "started_at": "2026-01-01T00:00:00+00:00",
             "sealed_at": "2026-01-01T01:00:00+00:00",
             "removed_discord_ids": [], "placements": {}, "point_awards": {},
             "winner_discord_id": None},
        ],
        "bngc_comp_events": [
            {"id": 1, "match_id": 11, "etype": "kill", "discord_id": "A",
             "actor_name": "alice", "target_name": "x", "alive": None},
            {"id": 2, "match_id": 11, "etype": "alv", "discord_id": "B",
             "actor_name": "bobby", "target_name": None, "alive": False},
        ],
        "bngc_users": [
            {"discord_id": "Z", "game_username": "zed", "discord_username": "z"},
        ],
    }


def _call(monkeypatch, state, fn, body=None, params=None):
    monkeypatch.setattr(comp_mod, "supabase", _SB(state))
    resp = asyncio.run(fn(_Req(body, params)))
    return resp.status_code, json.loads(resp.body.decode())


def _match(state, mid):
    return next(r for r in state["bngc_comp_matches"] if r["id"] == mid)


def _duo_state():
    s = _state()
    s["bngc_comp_registrations"] = [
        {"id": 1, "tourney_id": 3, "status": "accepted",
         "member_ids": ["A", "B"], "team_color": "\U0001f535"},
        {"id": 2, "tourney_id": 3, "status": "accepted",
         "member_ids": ["C", "D"], "team_color": "\U0001f534"},
        {"id": 3, "tourney_id": 3, "status": "banned",
         "member_ids": ["E", "F"], "team_color": "\U0001f7e0"},
    ]
    return s


def test_remove_with_partner_drops_the_whole_duo(monkeypatch):
    # a team must never leave one half in the match
    state = _duo_state()
    code, j = _call(monkeypatch, state, comp_mod.comp_match_remove,
                    {"match_id": 11, "discord_id": "A", "with_partner": True})
    assert code == 200, j
    assert set(j["removed_discord_ids"]) == {"A", "B"}
    assert j["opponent_id"] == "B"
    assert set(_match(state, 11)["removed_discord_ids"]) == {"A", "B"}


def test_remove_without_partner_still_removes_only_that_player(monkeypatch):
    state = _duo_state()
    code, j = _call(monkeypatch, state, comp_mod.comp_match_remove,
                    {"match_id": 11, "discord_id": "A"})
    assert code == 200, j
    assert j["removed_discord_ids"] == ["A"]
    assert j.get("opponent_id") is None


def test_remove_honours_an_explicit_opponent_over_the_roster(monkeypatch):
    state = _duo_state()
    code, j = _call(monkeypatch, state, comp_mod.comp_match_remove,
                    {"match_id": 11, "discord_id": "A", "opponent_id": "D",
                     "with_partner": True})
    assert code == 200, j
    assert set(j["removed_discord_ids"]) == {"A", "D"}


def test_remove_ignores_a_banned_rows_partner(monkeypatch):
    # a banned registration is not a live team: nobody gets dragged in
    state = _duo_state()
    code, j = _call(monkeypatch, state, comp_mod.comp_match_remove,
                    {"match_id": 11, "discord_id": "E", "with_partner": True})
    assert code == 200, j
    assert j["removed_discord_ids"] == ["E"]


def test_remove_partner_is_idempotent_and_never_duplicates(monkeypatch):
    state = _duo_state()
    _call(monkeypatch, state, comp_mod.comp_match_remove,
          {"match_id": 11, "discord_id": "A", "with_partner": True})
    _call(monkeypatch, state, comp_mod.comp_match_remove,
          {"match_id": 11, "discord_id": "A", "with_partner": True})
    assert _match(state, 11)["removed_discord_ids"].count("A") == 1
    assert _match(state, 11)["removed_discord_ids"].count("B") == 1


def test_add_back_can_restore_the_whole_duo(monkeypatch):
    # the inverse must not leave a half-pair on the roster
    state = _duo_state()
    _call(monkeypatch, state, comp_mod.comp_match_remove,
          {"match_id": 11, "discord_id": "A", "with_partner": True})
    code, j = _call(monkeypatch, state, comp_mod.comp_match_add_back,
                    {"match_id": 11, "discord_id": "A", "with_partner": True})
    assert code == 200, j
    assert set(_match(state, 11)["removed_discord_ids"]) == set()


# -- start ---------------------------------------------------------------

def test_start_stamps_open_match(monkeypatch):
    state = _state()
    code, j = _call(monkeypatch, state, comp_mod.comp_match_start, {"match_id": 12})
    assert code == 200, j
    assert j["started_at"]
    assert _match(state, 12)["started_at"] == j["started_at"]


def test_start_idempotent(monkeypatch):
    state = _state()
    code, j = _call(monkeypatch, state, comp_mod.comp_match_start, {"match_id": 11})
    assert code == 200, j
    assert j["started_at"] == "2026-01-01T00:00:00+00:00"


def test_start_rejects_sealed_and_missing(monkeypatch):
    state = _state()
    code, _ = _call(monkeypatch, state, comp_mod.comp_match_start, {"match_id": 13})
    assert code == 409
    code, _ = _call(monkeypatch, state, comp_mod.comp_match_start, {"match_id": 99})
    assert code == 404
    code, _ = _call(monkeypatch, state, comp_mod.comp_match_start, {})
    assert code == 400


# -- unseal --------------------------------------------------------------

def test_unseal_reopens_to_manual(monkeypatch):
    state = _state()
    code, j = _call(monkeypatch, state, comp_mod.comp_match_unseal, {"match_id": 13})
    assert code == 200, j
    assert j["status"] == "manual"
    m = _match(state, 13)
    assert m["status"] == "manual" and m["sealed_at"] is None


def test_unseal_live_is_noop_and_missing_404(monkeypatch):
    state = _state()
    code, j = _call(monkeypatch, state, comp_mod.comp_match_unseal, {"match_id": 11})
    assert code == 200, j
    assert _match(state, 11)["status"] == "manual"
    code, _ = _call(monkeypatch, state, comp_mod.comp_match_unseal, {"match_id": 99})
    assert code == 404


# -- elim ----------------------------------------------------------------

def test_elim_add_and_remove_manual(monkeypatch):
    state = _state()
    code, j = _call(monkeypatch, state, comp_mod.comp_match_elim,
                    {"match_id": 11, "discord_id": "A", "delta": 2})
    assert code == 200, j
    assert j["elims"] == 2
    rows = [r for r in state["bngc_comp_events"]
            if r["match_id"] == 11 and r["discord_id"] == "A" and r["etype"] == "alv"]
    assert len(rows) == 2 and all(r["alive"] is False and r["manual"] is True for r in rows)

    code, j = _call(monkeypatch, state, comp_mod.comp_match_elim,
                    {"match_id": 11, "discord_id": "A", "delta": -1})
    assert code == 200, j
    assert j["elims"] == 1


def test_elim_never_touches_tracked(monkeypatch):
    state = _state()
    # B's only alv row is tracked: removing finds no manual row, count stands
    code, j = _call(monkeypatch, state, comp_mod.comp_match_elim,
                    {"match_id": 11, "discord_id": "B", "delta": -5})
    assert code == 200, j
    assert j["elims"] == 1 and j["tracked_kept"] == 1
    assert any(r["id"] == 2 for r in state["bngc_comp_events"])


def test_elim_validation(monkeypatch):
    state = _state()
    for body, want in [({}, 400), ({"match_id": 11}, 400),
                       ({"match_id": 11, "discord_id": "A"}, 400),
                       ({"match_id": 11, "discord_id": "A", "delta": 51}, 400),
                       ({"match_id": 99, "discord_id": "A", "delta": 1}, 404)]:
        code, _ = _call(monkeypatch, state, comp_mod.comp_match_elim, body)
        assert code == want, (body, code)


def test_elim_scores_as_placement(monkeypatch):
    # manual elim joins the death order: last out wins without override
    evs = [
        {"etype": "kill", "discord_id": "A", "actor_name": "a", "alive": None},
        {"etype": "alv", "discord_id": "B", "actor_name": "b", "alive": False},
        {"etype": "alv", "discord_id": "A", "actor_name": "a", "alive": False,
         "manual": True},
    ]
    rows = comp_mod.compute_match_points(
        evs, {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]})
    by = {r["discord_id"]: r for r in rows}
    assert by["A"]["won"] is True and by["A"]["placement"] == 1
    assert by["B"]["placement"] == 2


# -- add-player ----------------------------------------------------------

def test_add_player_boards_at_zero(monkeypatch):
    state = _state()
    code, j = _call(monkeypatch, state, comp_mod.comp_match_add_player,
                    {"match_id": 11, "discord_id": "Z"})
    assert code == 200, j
    rows = comp_mod.compute_match_points(
        [dict(r) for r in state["bngc_comp_events"] if r["match_id"] == 11],
        {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]})
    by = {r["discord_id"]: r for r in rows}
    assert by["Z"]["points"] == 0 and by["Z"]["kills"] == 0
    assert by["Z"]["placement"] is None and by["Z"]["won"] is False
    # idempotent: no duplicate presence row
    code, _ = _call(monkeypatch, state, comp_mod.comp_match_add_player,
                    {"match_id": 11, "discord_id": "Z"})
    assert code == 200
    assert sum(1 for r in state["bngc_comp_events"]
               if r["match_id"] == 11 and r["discord_id"] == "Z") == 1


def test_add_player_unremoves(monkeypatch):
    state = _state()
    _match(state, 11)["removed_discord_ids"] = ["Z"]
    code, j = _call(monkeypatch, state, comp_mod.comp_match_add_player,
                    {"match_id": 11, "discord_id": "Z"})
    assert code == 200, j
    assert _match(state, 11)["removed_discord_ids"] == []


def test_add_player_validation(monkeypatch):
    state = _state()
    for body, want in [({}, 400), ({"match_id": 11}, 400),
                       ({"match_id": 99, "discord_id": "Z"}, 404)]:
        code, _ = _call(monkeypatch, state, comp_mod.comp_match_add_player, body)
        assert code == want, (body, code)


# -- seen-participant universe (pure) ------------------------------------

def test_knock_and_presence_board_at_zero():
    evs = [
        {"etype": "kill", "discord_id": "A", "actor_name": "a", "alive": None},
        {"etype": "knock", "discord_id": "K", "actor_name": "k", "alive": None},
        {"etype": "alv", "discord_id": "P", "actor_name": "p", "alive": True},
        {"etype": "kill", "discord_id": None, "actor_name": "Ghost", "alive": None},
    ]
    rows = comp_mod.compute_match_points(evs)
    by = {r["discord_id"]: r for r in rows}
    assert by["K"]["points"] == 0 and by["P"]["points"] == 0
    assert None not in by and len(rows) == 3


def test_open_match_sets_started_at(monkeypatch):
    state = _state()
    code, j = _call(monkeypatch, state, comp_mod.comp_match_open, {"tourney_id": 3})
    assert code == 200, j
    m = _match(state, j["match_id"])
    assert m["status"] == "manual" and m["started_at"]
