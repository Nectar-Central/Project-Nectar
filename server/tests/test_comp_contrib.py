"""Tourney contributor banking: every roster player counts (fake supabase + fake room).

Contract (all-players fix):
- Match lifecycle (open / seal / auto-calc / alv snapshot / leaver removal)
  stays host-only: one writer, no duplicate matches.
- Kill/leave event banking is collaborative: any authenticated session whose
  discord_id maps to the room roster may append FRESH events into the already
  open match. Strangers (not host, not roster) and anonymous sessions write
  nothing. Contributors never open, seal, calculate, or snapshot alv.
- Name mapping is fold-tolerant (case drift, color tags, dc: prefix) with the
  same twin rule as lifetime stats: exact-first, fold-fallback, conflict None.
- Dedup keys are normalized so the same kill seen as "cis6led" by the host and
  "CIS6LED" by a teammate banks once.
"""
from types import SimpleNamespace

import pytest

import server.scripts.processor as proc


@pytest.fixture(autouse=True)
def _isolated_bridge_state():
    proc._comp_namemap.clear()
    proc._comp_seen.clear()
    proc._rt_seen.clear()
    yield
    proc._comp_namemap.clear()
    proc._comp_seen.clear()
    proc._rt_seen.clear()


class _R:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, sb, rows, name):
        self._sb, self._rows, self._name = sb, list(rows), name
        self._insert, self._update = None, None

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
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def insert(self, rows):
        self._insert = [rows] if isinstance(rows, dict) else [dict(r) for r in rows]
        return self

    def update(self, data):
        self._update = dict(data)
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
        return _R([dict(r) for r in self._rows])


class _SB:
    def __init__(self, tables):
        self.t = {k: list(v) for k, v in tables.items()}

    def table(self, name):
        return _Q(self, self.t.get(name, []), name)


def _player(username, nr=1, alive=True):
    return SimpleNamespace(username=username, actor_nr=nr, is_alive=alive)


def _kill(killer, victim):
    return SimpleNamespace(event_type="killed", instigator=killer, instigated=victim)


def _room(players, kills, me_name="cis6led", last_stg=1, short_code="CONTR"):
    return SimpleNamespace(
        players=dict(players),
        recent_kills=list(kills),
        me=SimpleNamespace(username=me_name),
        last_stg=last_stg,
        short_code=short_code,
    )


USERS = [
    {"game_username": "cis6led", "discord_id": "A"},
    {"game_username": "chris7", "discord_id": "B"},
]

PLAYERS = {1: _player("cis6led", 1, True), 2: _player("chris7", 2, True)}

TOURNEY = {"id": 7, "code": "CONTR", "status": "started", "hosts": ["A"],
           "created_by": "A", "host_playing": True, "series_len": 10}

RT_TOURNEY = {"id": 9, "code": "RCON1", "status": "started", "hosts": ["A"],
              "created_by": "A", "host_playing": True, "series_len": 10}


def _sb_comp(monkeypatch, users=None, tourney=None):
    sb = _SB({"bngc_comp_tourneys": [dict(tourney or TOURNEY)],
              "bngc_comp_matches": [],
              "bngc_comp_events": [],
              "bngc_users": list(users if users is not None else USERS)})
    monkeypatch.setattr(proc, "supabase", sb)
    proc._comp_namemap.clear()
    proc._comp_seen.clear()
    proc._comp_logged_codes.clear()
    proc._comp_host_warned.clear()
    return sb


def _sb_rt(monkeypatch):
    sb = _SB({"bngc_ranked_tourneys": [dict(RT_TOURNEY)],
              "bngc_ranked_tourney_matches": [],
              "bngc_ranked_tourney_events": [],
              "bngc_users": list(USERS)})
    monkeypatch.setattr(proc, "supabase", sb)
    monkeypatch.setattr(proc, "_rt_calc", lambda tid, who="bridge": [])
    proc._comp_namemap.clear()
    proc._rt_seen.clear()
    proc._rt_logged_codes.clear()
    proc._rt_host_warned.clear()
    return sb


# --- mapping: fold-tolerant, twins stay None, /link heals without restart ---

def test_map_case_drift_credits(monkeypatch):
    _sb_comp(monkeypatch)
    assert proc._comp_map_names(["CIS6LED"]) == {"CIS6LED": "A"}


def test_map_strips_tags_and_dc_prefix(monkeypatch):
    _sb_comp(monkeypatch)
    assert proc._comp_map_names(["<color=red>cis6led</color>"]) == {
        "<color=red>cis6led</color>": "A"}
    assert proc._comp_map_names(["dc: chris7"]) == {"dc: chris7": "B"}


def test_map_twin_conflict_stays_null(monkeypatch):
    # exact twin (same spelling, two owners): nobody is guessable
    _sb_comp(monkeypatch, users=USERS + [
        {"game_username": "cis6led", "discord_id": "Z"}])
    assert proc._comp_map_names(["cis6led"]) == {"cis6led": None}
    # fold ambiguity (nothing matches exactly, two owners case-insensitively)
    _sb_comp(monkeypatch, users=[
        {"game_username": "cis6led", "discord_id": "A"},
        {"game_username": "CIS6LED", "discord_id": "Z"}])
    assert proc._comp_map_names(["Cis6led"]) == {"Cis6led": None}
    # ...but an exact single owner still wins despite the fold twin
    assert proc._comp_map_names(["cis6led"]) == {"cis6led": "A"}


def test_map_heals_after_link_without_restart(monkeypatch):
    sb = _sb_comp(monkeypatch, users=list(USERS))
    assert proc._comp_map_names(["ghost"]) == {"ghost": None}
    sb.t["bngc_users"].append({"game_username": "ghost", "discord_id": "G"})
    assert proc._comp_map_names(["ghost"]) == {"ghost": "G"}


# --- comp contributor: append fresh events, never lifecycle ---

def _host_open(sb):
    room = _room(PLAYERS, [_kill("cis6led", "victim1")])
    proc.seal_comp_match("ses-host", "A", room, "room-x", "zw_lg")
    opened = [m for m in sb.t["bngc_comp_matches"] if m.get("status") == "open"]
    assert len(opened) == 1
    return opened[0]["id"]


def test_roster_contributor_appends_missed_kill(monkeypatch):
    sb = _sb_comp(monkeypatch)
    mid = _host_open(sb)
    # B saw the host's kill too (dup under drifted case) plus one the host missed
    room_b = _room(PLAYERS,
                   [_kill("CIS6LED", "victim1"), _kill("chris7", "victim2")],
                   me_name="chris7")
    proc.seal_comp_match("ses-b", "B", room_b, "room-x", "zw_lg")
    kills = [e for e in sb.t["bngc_comp_events"] if e.get("etype") == "kill"]
    assert [(k["actor_name"], k["discord_id"]) for k in kills] == [
        ("cis6led", "A"), ("chris7", "B")]
    assert all(k["match_id"] == mid for k in kills)
    assert len([m for m in sb.t["bngc_comp_matches"]
                if m.get("status") == "open"]) == 1  # no duplicate open


def test_stranger_session_writes_nothing(monkeypatch):
    sb = _sb_comp(monkeypatch)
    _host_open(sb)
    room_z = _room(PLAYERS, [_kill("stranger", "victim9")], me_name="stranger")
    proc.seal_comp_match("ses-z", "Z", room_z, "room-x", "zw_lg")
    assert len([e for e in sb.t["bngc_comp_events"]
                if e.get("actor_name") == "stranger"]) == 0


def test_contributor_cannot_open_or_seal(monkeypatch):
    sb = _sb_comp(monkeypatch)
    room_b = _room(PLAYERS, [_kill("chris7", "victim2")], me_name="chris7")
    proc.seal_comp_match("ses-b", "B", room_b, "room-x", "zw_lg")
    assert sb.t["bngc_comp_matches"] == []
    assert sb.t["bngc_comp_events"] == []
    # and with an open row, a contributor stG 3 seals nothing / snapshots no alv
    _host_open(sb)
    room_b3 = _room(PLAYERS, [_kill("chris7", "victim2")], me_name="chris7",
                    last_stg=3)
    proc.seal_comp_match("ses-b", "B", room_b3, "room-x", "zw_lg")
    assert [m.get("status") for m in sb.t["bngc_comp_matches"]] == ["open"]
    assert [e for e in sb.t["bngc_comp_events"]
            if e.get("etype") == "alv"] == []


# --- ranked-tourney mirror ---

def test_rt_roster_contributor_appends_missed_kill(monkeypatch):
    sb = _sb_rt(monkeypatch)
    room = _room(PLAYERS, [_kill("cis6led", "victim1")], short_code="RCON1")
    proc.seal_ranked_tourney_match("ses-host", "A", room, "room-x", "zw_lg")
    opened = [m for m in sb.t["bngc_ranked_tourney_matches"]
              if m.get("status") == "open"]
    assert len(opened) == 1
    room_b = _room(PLAYERS,
                   [_kill("cis6led", "victim1"), _kill("CHRIS7", "victim2")],
                   me_name="chris7", short_code="RCON1")
    proc.seal_ranked_tourney_match("ses-b", "B", room_b, "room-x", "zw_lg")
    kills = [e for e in sb.t["bngc_ranked_tourney_events"]
             if e.get("etype") == "kill"]
    assert [(k["actor_name"], k["discord_id"]) for k in kills] == [
        ("cis6led", "A"), ("CHRIS7", "B")]
