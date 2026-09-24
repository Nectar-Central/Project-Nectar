"""Ranked auto-tag: a room the HOST created auto-tags their one codeless tourney.

Contract (processor._rt_autotag, called from the bridge miss path):
  created room + valid code + host owns exactly one non-ended CODELESS tourney
  + no other tourney claims the code + room traffic is fresh (<=90s) ->
  tourneys.code set, caches cleared.
Never: joined rooms, manual codes (same or different), ambiguous tourneys,
stolen codes, non-host sessions, or tags from a STALE room snapshot (the
sweep also visits the previous room — tagging from it would bind the OLD
code). Never raises.
"""
import time
from types import SimpleNamespace

import server.scripts.processor as proc
from server.lib.photon_engine.models import RoomView


class _R:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, sb, rows, name):
        self._sb, self._rows, self._name = sb, list(rows), name
        self._insert = None
        self._update = None

    def select(self, *a):
        return self

    def eq(self, c, v):
        self._rows = [r for r in self._rows if str(r.get(c)) == str(v)]
        return self

    def neq(self, c, v):
        self._rows = [r for r in self._rows if str(r.get(c)) != str(v)]
        return self

    def contains(self, c, vs):
        want = {str(v) for v in vs}
        self._rows = [r for r in self._rows
                      if want <= {str(h) for h in ((r.get(c) or []))}]
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def update(self, data):
        self._update = dict(data)
        return self

    def execute(self):
        base = self._sb.t.setdefault(self._name, [])
        if self._update is not None:
            for r in self._rows:
                r.update(self._update)
            return _R([dict(r) for r in self._rows])
        return _R([dict(r) for r in self._rows])


class _SB:
    def __init__(self, tables):
        self.t = {k: list(v) for k, v in tables.items()}

    def table(self, name):
        return _Q(self, self.t.get(name, []), name)


def _sb(monkeypatch, tourneys):
    sb = _SB({"bngc_ranked_tourneys": list(tourneys)})
    monkeypatch.setattr(proc, "supabase", sb)
    proc._rt_logged_codes.clear()
    return sb


def _room(code="WIEHG", created=True, fresh=True):
    return SimpleNamespace(short_code=code, created_by_me=created,
                           last_packet_at=time.time() if fresh else time.time() - 600)


def _tourney(tid=6, code=None, by="HOST1"):
    return {"id": tid, "code": code, "status": "started",
            "hosts": [by], "created_by": by}


def test_created_room_tags_codeless_tourney(monkeypatch):
    sb = _sb(monkeypatch, [_tourney()])
    proc._rt_autotag("ses", "HOST1", _room(), "WIEHG")
    assert sb.t["bngc_ranked_tourneys"][0]["code"] == "WIEHG"


def test_joined_room_never_tags(monkeypatch):
    sb = _sb(monkeypatch, [_tourney()])
    proc._rt_autotag("ses", "HOST1", _room(created=False), "WIEHG")
    assert sb.t["bngc_ranked_tourneys"][0]["code"] is None


def test_manual_code_never_clobbered(monkeypatch):
    sb = _sb(monkeypatch, [_tourney(code="ABCDE")])
    proc._rt_autotag("ses", "HOST1", _room(), "WIEHG")
    assert sb.t["bngc_ranked_tourneys"][0]["code"] == "ABCDE"


def test_same_code_is_quiet_noop(monkeypatch):
    sb = _sb(monkeypatch, [_tourney(code="WIEHG")])
    proc._rt_autotag("ses", "HOST1", _room(), "WIEHG")
    assert sb.t["bngc_ranked_tourneys"][0]["code"] == "WIEHG"


def test_two_codeless_tourneys_stay_manual(monkeypatch):
    sb = _sb(monkeypatch, [_tourney(tid=6), _tourney(tid=7)])
    proc._rt_autotag("ses", "HOST1", _room(), "WIEHG")
    assert [t["code"] for t in sb.t["bngc_ranked_tourneys"]] == [None, None]


def test_coded_sibling_does_not_block_tag(monkeypatch):
    sb = _sb(monkeypatch, [_tourney(tid=6, code="ABCDE"), _tourney(tid=7)])
    proc._rt_autotag("ses", "HOST1", _room(), "WIEHG")
    by_id = {t["id"]: t["code"] for t in sb.t["bngc_ranked_tourneys"]}
    assert by_id == {6: "ABCDE", 7: "WIEHG"}


def test_claimed_code_not_stolen(monkeypatch):
    sb = _sb(monkeypatch, [_tourney(tid=6),
                           {"id": 7, "code": "WIEHG", "status": "started",
                            "hosts": ["OTHER"], "created_by": "OTHER"}])
    proc._rt_autotag("ses", "HOST1", _room(), "WIEHG")
    assert sb.t["bngc_ranked_tourneys"][0]["code"] is None


def test_non_host_session_never_tags(monkeypatch):
    sb = _sb(monkeypatch, [_tourney()])
    proc._rt_autotag("ses", "STRANGER", _room(), "WIEHG")
    assert sb.t["bngc_ranked_tourneys"][0]["code"] is None


def test_bad_code_ignored(monkeypatch):
    sb = _sb(monkeypatch, [_tourney()])
    proc._rt_autotag("ses", "HOST1", _room(code="TOOLONG"), "TOOLONG")
    proc._rt_autotag("ses", "HOST1", _room(code=None), None)
    assert sb.t["bngc_ranked_tourneys"][0]["code"] is None


def test_bridge_miss_path_tags_and_next_batch_binds(monkeypatch):
    sb = _sb(monkeypatch, [_tourney()])
    room = _room()
    proc.seal_ranked_tourney_match("ses", "HOST1", room, "room-x", "zw_lg")
    assert sb.t["bngc_ranked_tourneys"][0]["code"] == "WIEHG"


def test_non_current_room_never_tags(monkeypatch):
    # Regression: after a backout the sweep visits the stale room (old
    # code still on it) before the fresh create. Tagging from it rebound
    # the OLD code instead of the new room's.
    sb = _sb(monkeypatch, [_tourney()])
    proc.seal_ranked_tourney_match("ses", "HOST1", _room(code="DMF2W"), "room-old", "zw_lg",
                                   is_current=False)
    assert sb.t["bngc_ranked_tourneys"][0]["code"] is None


def test_stale_room_snapshot_never_tags(monkeypatch):
    # Regression: the seal sweep also visits the previous room (still
    # state=match, old code). Tagging from it bound sessions to the OLD
    # code instead of the fresh create.
    sb = _sb(monkeypatch, [_tourney()])
    proc._rt_autotag("ses", "HOST1", _room(code="APMKQ", fresh=False), "APMKQ")
    assert sb.t["bngc_ranked_tourneys"][0]["code"] is None


def test_created_by_me_roundtrips_view():
    r = RoomView(room_id="abc", code="abc")
    assert r.created_by_me is False
    r.created_by_me = True
    d = r.to_dict()
    assert d["created_by_me"] is True
    assert RoomView.from_dict(d).created_by_me is True
    assert RoomView.from_dict({"room_id": "x"}).created_by_me is False
