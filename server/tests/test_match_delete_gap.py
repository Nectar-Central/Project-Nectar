"""Deleting a match must not leave a gap the bridge tries to refill.

The series gate counted SURVIVING sealed/manual rows and the next version was
max(surviving)+1. Deleting a match therefore did two things at once: it
un-finished the series, and - if the deleted match was the highest version -
re-issued the very same version number. Every later stG 1 in that room then
re-created the match the admin had just deleted.

A deleted version is now recorded on the tourney, so the slot it consumed
stays consumed and the number is never handed out twice.
"""
import asyncio
import json
from types import SimpleNamespace

import server.scripts.processor as proc
from server.routes import comp as comp_mod
from server.routes import ranked_tourney as rt_mod

COMP_TOURNEYS = "bngc_comp_tourneys"
COMP_MATCHES = "bngc_comp_matches"
COMP_EVENTS = "bngc_comp_events"
RT_TOURNEYS = "bngc_ranked_tourneys"
RT_MATCHES = "bngc_ranked_tourney_matches"
RT_EVENTS = "bngc_ranked_tourney_events"


class _R:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, sb, name):
        self._sb, self._name = sb, name
        self._rows = list(sb.t.get(name, []))
        self._insert = self._update = None
        self._delete = False

    def select(self, *a):
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

    def ilike(self, c, pattern):
        want = str(pattern or "").lower()
        self._rows = [r for r in self._rows
                      if str(r.get(c) or "").lower() == want]
        return self

    def order(self, c, desc=False):
        self._rows = sorted(self._rows,
                            key=lambda r: (r.get(c) is None, r.get(c)),
                            reverse=bool(desc))
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def insert(self, rows):
        self._insert = [dict(rows)] if isinstance(rows, dict) else [dict(r) for r in rows]
        return self

    def update(self, data):
        self._update = dict(data)
        return self

    def delete(self):
        self._delete = True
        return self

    def execute(self):
        base = self._sb.t.setdefault(self._name, [])
        if self._insert is not None:
            wrote = []
            for r in self._insert:
                r = dict(r)
                if "id" not in r:
                    r["id"] = max([x.get("id", 0) for x in base] + [0]) + 1
                base.append(r)
                wrote.append(dict(r))
            return _R(wrote)
        if self._update is not None:
            for r in self._rows:
                r.update(self._update)
            return _R([dict(r) for r in self._rows])
        if self._delete:
            doomed = {id(r) for r in self._rows}
            base[:] = [r for r in base if id(r) not in doomed]
            return _R([dict(r) for r in self._rows])
        return _R([dict(r) for r in self._rows])


class _SB:
    def __init__(self, tables):
        # keep the caller's list objects so the test can assert on the same
        # rows the "database" mutated
        self.t = dict(tables)

    def table(self, name):
        return _Q(self, name)


class _Req:
    def __init__(self, body=None):
        self._body = dict(body or {})
        self.query_params = {}
        self.headers = {}

    async def json(self):
        return dict(self._body)


def _tourney(**over):
    t = {"id": 7, "code": "GAPTX", "status": "started", "hosts": ["A"],
         "created_by": "A", "host_playing": True, "series_len": 3,
         "deleted_versions": []}
    t.update(over)
    return t


def _match(v, status="sealed", mid=None):
    return {"id": mid if mid is not None else 100 + v, "tourney_id": 7,
            "version": v, "status": status, "room_id": "room-x",
            "removed_discord_ids": []}


def _room(code="GAPTX", last_stg=1):
    return SimpleNamespace(
        players={1: SimpleNamespace(username="cis6led", actor_nr=1, is_alive=True)},
        recent_kills=[],
        me=SimpleNamespace(username="cis6led"),
        last_stg=last_stg, short_code=code)


def _bridge(monkeypatch, tourney, matches, rt=False):
    tname, mname, ename = ((RT_TOURNEYS, RT_MATCHES, RT_EVENTS) if rt
                           else (COMP_TOURNEYS, COMP_MATCHES, COMP_EVENTS))
    sb = _SB({tname: [dict(tourney)], mname: list(matches), ename: [],
              "bngc_users": [{"game_username": "cis6led", "discord_id": "A"}]})
    monkeypatch.setattr(proc, "supabase", sb)
    monkeypatch.setattr(proc, "_rt_calc", lambda tid, who="bridge": [])
    for c in ("_comp_namemap", "_comp_seen", "_rt_seen", "_comp_logged_codes",
              "_rt_logged_codes", "_comp_host_warned", "_rt_host_warned"):
        getattr(proc, c).clear()
    return sb


# --- the series gate must not un-finish when a match is deleted ------------

def test_comp_series_stays_complete_after_a_delete(monkeypatch):
    # 2 sealed rows + version 2 deleted == 3 of 3 slots consumed
    sb = _bridge(monkeypatch, _tourney(), [_match(0), _match(1)])
    assert proc._comp_series_complete(7, 3, sb.t[COMP_TOURNEYS][0]["deleted_versions"]) is False
    sb.t[COMP_TOURNEYS][0]["deleted_versions"] = [2]
    assert proc._comp_series_complete(7, 3, sb.t[COMP_TOURNEYS][0]["deleted_versions"]) is True


def test_ranked_series_stays_complete_after_a_delete(monkeypatch):
    sb = _bridge(monkeypatch, _tourney(code="RGAPX"), [_match(0), _match(1)], rt=True)
    burned = sb.t[RT_TOURNEYS][0]["deleted_versions"]
    assert proc._rt_series_complete(7, 3, burned) is False
    sb.t[RT_TOURNEYS][0]["deleted_versions"] = [2]
    assert proc._rt_series_complete(7, 3, [2]) is True


def test_series_gate_is_unchanged_without_deletes(monkeypatch):
    _bridge(monkeypatch, _tourney(), [_match(0), _match(1), _match(2)])
    assert proc._comp_series_complete(7, 3) is True


def test_open_ended_series_ignores_deletes(monkeypatch):
    _bridge(monkeypatch, _tourney(series_len=0), [_match(0)])
    proc.supabase.t[COMP_TOURNEYS][0]["deleted_versions"] = [1, 2, 3]
    assert proc._comp_series_complete(7, 0) is False


# --- a deleted version number is never handed out again -------------------

def test_comp_open_never_reuses_a_deleted_version(monkeypatch):
    sb = _bridge(monkeypatch, _tourney(series_len=5, deleted_versions=[2]),
                 [_match(0), _match(1)])
    proc.seal_comp_match("ses", "A", _room(), "room-x", "zw_lg")
    opened = [m for m in sb.t[COMP_MATCHES] if m.get("status") == "open"]
    assert len(opened) == 1
    assert opened[0]["version"] == 3  # not the deleted 2


def test_ranked_open_never_reuses_a_deleted_version(monkeypatch):
    sb = _bridge(monkeypatch, _tourney(code="RGAPX", series_len=5,
                                       deleted_versions=[2]),
                 [_match(0), _match(1)], rt=True)
    proc.seal_ranked_tourney_match("ses", "A", _room("RGAPX"), "room-x", "zw_lg")
    opened = [m for m in sb.t[RT_MATCHES] if m.get("status") == "open"]
    assert len(opened) == 1
    assert opened[0]["version"] == 3


def test_open_without_deletes_keeps_counting_up(monkeypatch):
    sb = _bridge(monkeypatch, _tourney(series_len=5), [_match(0), _match(1)])
    proc.seal_comp_match("ses", "A", _room(), "room-x", "zw_lg")
    opened = [m for m in sb.t[COMP_MATCHES] if m.get("status") == "open"]
    assert opened[0]["version"] == 2


def test_a_deleted_match_is_not_recreated_by_a_refresh(monkeypatch):
    # the reported symptom: a 3-match series is finished, an admin deletes the
    # newest match, and every later refresh opens a replacement to fill the gap
    sb = _bridge(monkeypatch, _tourney(series_len=3, deleted_versions=[2]),
                 [_match(0), _match(1)])
    assert proc._comp_series_complete(7, 3, [2]) is True
    proc.seal_comp_match("ses", "A", _room(), "room-x", "zw_lg")
    assert [m for m in sb.t[COMP_MATCHES] if m.get("status") == "open"] == []


# --- the delete endpoints must record the tombstone -----------------------

def _route_state():
    return {
        COMP_TOURNEYS: [{"id": 7, "code": "GAPTX", "series_len": 3,
                         "deleted_versions": []}],
        COMP_MATCHES: [_match(0, mid=11), _match(1, mid=12), _match(2, mid=13)],
        COMP_EVENTS: [{"id": 1, "match_id": 13, "etype": "kill"}],
        "bngc_comp_results": [{"id": 1, "match_id": 13}],
    }


def _call(monkeypatch, mod, state, fn, body):
    monkeypatch.setattr(mod, "supabase", _SB(state))
    resp = asyncio.run(fn(_Req(body)))
    return resp.status_code, json.loads(resp.body.decode())


def test_comp_delete_records_the_tombstone(monkeypatch):
    state = _route_state()
    code, j = _call(monkeypatch, comp_mod, state, comp_mod.comp_match_delete,
                    {"match_id": 13})
    assert code == 200, j
    assert j["version"] == 2
    assert [m["id"] for m in state[COMP_MATCHES]] == [11, 12]
    assert state[COMP_TOURNEYS][0]["deleted_versions"] == [2]


def test_comp_delete_keeps_earlier_tombstones(monkeypatch):
    state = _route_state()
    state[COMP_TOURNEYS][0]["deleted_versions"] = [4]
    code, _ = _call(monkeypatch, comp_mod, state, comp_mod.comp_match_delete,
                    {"match_id": 13})
    assert code == 200
    assert sorted(state[COMP_TOURNEYS][0]["deleted_versions"]) == [2, 4]


def test_comp_delete_never_twice_for_one_version(monkeypatch):
    state = _route_state()
    state[COMP_TOURNEYS][0]["deleted_versions"] = [2]
    code, _ = _call(monkeypatch, comp_mod, state, comp_mod.comp_match_delete,
                    {"match_id": 12})
    assert code == 200
    assert sorted(state[COMP_TOURNEYS][0]["deleted_versions"]) == [1, 2]


def test_ranked_delete_records_the_tombstone(monkeypatch):
    state = {
        RT_TOURNEYS: [{"id": 7, "code": "RGAPX", "series_len": 3,
                       "deleted_versions": []}],
        RT_MATCHES: [_match(0, mid=11), _match(1, mid=12), _match(2, mid=13)],
        RT_EVENTS: [{"id": 1, "match_id": 13, "etype": "kill"}],
        "bngc_ranked_tourney_results": [{"id": 1, "match_id": 13}],
    }
    code, j = _call(monkeypatch, rt_mod, state, rt_mod.comp_match_delete,
                    {"match_id": 13})
    assert code == 200, j
    assert [m["id"] for m in state[RT_MATCHES]] == [11, 12]
    assert state[RT_TOURNEYS][0]["deleted_versions"] == [2]
