"""Seal-time alv snapshot for the comp bridge (fake supabase + fake room).

Contract: one alv row per roster player, dead in chronological kill order
first (alive=False), survivors last (alive=False) — the last row reads as
the winner, so compute_match_points resolves placements with no manual
override. Spectating hosts get no alv row; seal retries never duplicate.
"""
from types import SimpleNamespace

import server.scripts.processor as proc
from server.routes.comp import compute_match_points


class _R:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, sb, rows, name):
        self._sb, self._rows, self._name = sb, list(rows), name

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

    def order(self, c, desc=False):
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def execute(self):
        return _R(self._rows)

    def insert(self, rows):
        self._sb.inserts.append((self._name, [dict(r) for r in rows]))
        return self


class _SB:
    def __init__(self, tables):
        self.t = {k: list(v) for k, v in tables.items()}
        self.inserts = []

    def table(self, name):
        return _Q(self, self.t.get(name, []), name)


def _player(username, nr, alive):
    return SimpleNamespace(username=username, actor_nr=nr, is_alive=alive)


def _kill(killer, victim):
    return SimpleNamespace(event_type="killed", instigator=killer, instigated=victim)


def _room(players, kills, me_name="cis6led"):
    return SimpleNamespace(
        players=dict(players),
        recent_kills=list(kills),
        me=SimpleNamespace(username=me_name),
    )


USERS = [
    {"game_username": "cis6led", "discord_id": "A"},
    {"game_username": "chris7", "discord_id": "B"},
    {"game_username": "third", "discord_id": "C"},
]

ROOM = _room(
    {1: _player("cis6led", 1, True),
     2: _player("chris7", 2, False),
     3: _player("third", 3, False)},
    [_kill("cis6led", "chris7"), _kill("cis6led", "third")],
)


def _use_sb(monkeypatch, events):
    sb = _SB({"bngc_comp_events": list(events), "bngc_users": list(USERS)})
    monkeypatch.setattr(proc, "supabase", sb)
    proc._comp_namemap.clear()
    return sb


def test_alv_snapshot_order_and_winner(monkeypatch):
    sb = _use_sb(monkeypatch, [])
    proc._comp_bank_alv({"host_playing": True}, 9, ROOM)
    assert len(sb.inserts) == 1
    name, rows = sb.inserts[0]
    assert name == "bngc_comp_events"
    # dead in kill order, survivor last — all alive=False by convention
    assert [(r["actor_name"], r["alive"]) for r in rows] == [
        ("chris7", False), ("third", False), ("cis6led", False)]
    assert [r["actor_nr"] for r in rows] == [2, 3, 1]
    assert [r["discord_id"] for r in rows] == ["B", "C", "A"]
    assert all(r["etype"] == "alv" and r["match_id"] == 9 for r in rows)

    # end-to-end: kills + snapshot resolve winner + placements, no override
    table = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}
    kills = [
        {"etype": "kill", "discord_id": "A", "actor_name": "cis6led"},
        {"etype": "kill", "discord_id": "A", "actor_name": "cis6led"},
    ]
    scored = compute_match_points(kills + [
        {"etype": "alv", "discord_id": r["discord_id"],
         "actor_name": r["actor_name"], "alive": r["alive"]} for r in rows], table)
    by = {r["discord_id"]: r for r in scored}
    assert by["A"]["won"] is True and by["A"]["placement"] == 1
    assert by["A"]["points"] == 2 + 3 + 10
    # later death = better placement: third died after chris7
    assert by["C"]["placement"] == 2 and by["B"]["placement"] == 3


def test_alv_fuzzy_maps_drifted_name(monkeypatch):
    # roster name drifted a character (mobile player) - the snapshot still
    # resolves the did so the player boards instead of dropping out
    sb = _use_sb(monkeypatch, [])
    room = _room({1: _player("cis61ed", 1, False),
                  2: _player("chris7", 2, False)}, [], me_name="cis61ed")
    proc._comp_bank_alv({"host_playing": True}, 9, room)
    name, rows = sb.inserts[0]
    assert name == "bngc_comp_events"
    assert {r["actor_name"]: r["discord_id"] for r in rows} == {
        "cis61ed": "A", "chris7": "B"}


def test_alv_idempotent_on_retry(monkeypatch):
    sb = _use_sb(monkeypatch, [{"id": 1, "match_id": 9, "etype": "alv"}])
    proc._comp_bank_alv({"host_playing": True}, 9, ROOM)
    assert sb.inserts == []


def test_alv_skips_spectating_host(monkeypatch):
    sb = _use_sb(monkeypatch, [])
    proc._comp_bank_alv({"host_playing": False}, 9, ROOM)
    assert len(sb.inserts) == 1
    names = [r["actor_name"] for r in sb.inserts[0][1]]
    assert names == ["chris7", "third"]


def test_alv_no_players_no_write(monkeypatch):
    sb = _use_sb(monkeypatch, [])
    proc._comp_bank_alv({"host_playing": True}, 9, _room({}, []))
    assert sb.inserts == []


def test_alv_manual_rows_do_not_block_snapshot(monkeypatch):
    # dashboard elim corrections (manual alv) must not suppress the tracked
    # seal-time snapshot; tracked rows still bank.
    sb = _use_sb(monkeypatch, [
        {"id": 7, "match_id": 9, "etype": "alv", "discord_id": "B",
         "actor_name": "chris7", "alive": False, "manual": True}])
    proc._comp_bank_alv({"host_playing": True}, 9, ROOM)
    assert len(sb.inserts) == 1
    name, rows = sb.inserts[0]
    assert name == "bngc_comp_events"
    assert [r["actor_name"] for r in rows] == ["chris7", "third", "cis6led"]
