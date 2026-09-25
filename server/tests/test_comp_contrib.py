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


def _sb_comp(monkeypatch, users=None, tourney=None, regs=None):
    sb = _SB({"bngc_comp_tourneys": [dict(tourney or TOURNEY)],
              "bngc_comp_matches": [],
              "bngc_comp_events": [],
              "bngc_comp_registrations": list(regs or []),
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


def test_map_fuzzy_typo_credits_single_owner(monkeypatch):
    # mobile player drifted one character in-game; unique close owner maps
    _sb_comp(monkeypatch)
    assert proc._comp_map_names(["cis61ed"]) == {"cis61ed": "A"}


def test_map_fuzzy_near_tie_stays_null(monkeypatch):
    # two registered owners sit within the margin: nobody is guessable
    _sb_comp(monkeypatch, users=[
        {"game_username": "Void Eight", "discord_id": "A"},
        {"game_username": "Void Eights", "discord_id": "Z"}])
    assert proc._comp_map_names(["Void Eigh"]) == {"Void Eigh": None}


def test_map_fuzzy_weak_stays_null(monkeypatch):
    _sb_comp(monkeypatch)
    assert proc._comp_map_names(["Totally Unrelated"]) == {
        "Totally Unrelated": None}


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


# --- killfeed banking: unknown name <-> unclaimed registrant ---------------

def test_killfeed_pairs_unknown_name_to_unclaimed_registrant(monkeypatch):
    # "dom" is klepper's in-game name; klepper's registered name never shows
    # up in the room, so the kill still credits him instead of dropping out.
    sb = _sb_comp(monkeypatch,
                  users=USERS + [{"game_username": "klepper", "discord_id": "M"}],
                  regs=[{"tourney_id": 7, "status": "accepted",
                         "member_ids": ["A", "M"]}])
    mid = _host_open(sb)
    proc._comp_seen.clear()
    room = _room({1: _player("cis6led", 1, True), 2: _player("dom", 2, True)},
                 [_kill("cis6led", "dom"), _kill("dom", "cis6led")],
                 me_name="cis6led")
    roster = proc._tourney_roster_sync(7, "bngc_comp_registrations")
    assert roster == {"A": "cis6led", "M": "klepper"}
    batch = proc._bank_open_events(table="bngc_comp_events", seen={}, mid=mid,
                                   room=room, tag="", roster=roster)
    # both kills land: cis6led (matched) and dom (paired with klepper)
    assert [(r["actor_name"], r["discord_id"]) for r in batch] == [
        ("cis6led", "A"), ("dom", "M")]


def test_killfeed_no_pairing_without_roster(monkeypatch):
    # no registrations to reason from: the unknown name stays unmapped
    sb = _sb_comp(monkeypatch,
                  users=USERS + [{"game_username": "klepper", "discord_id": "M"}])
    mid = _host_open(sb)
    proc._comp_seen.clear()
    room = _room({1: _player("cis6led", 1, True), 2: _player("dom", 2, True)},
                 [_kill("cis6led", "dom")], me_name="cis6led")
    batch = proc._bank_open_events(table="bngc_comp_events", seen={}, mid=mid,
                                   room=room, tag="",
                                   roster=proc._tourney_roster_sync(
                                       7, "bngc_comp_registrations"))
    assert [(r["actor_name"], r["discord_id"]) for r in batch] == [
        ("cis6led", "A")]


# --- spectating host must never be scored (regression) ---
#
# A host with host_playing False is a registrant who never enters the room.
# They must be absent from placements, kills and points. Three separate leaks
# used to let them back in: the alv filter was keyed on the capturing
# session's own in-game name (a no-op when the host never spawned an avatar),
# the contributors paired unknown names onto the host's did, and nothing ever
# told the scoring layer to ignore them.


def _sb_spectating(monkeypatch, **over):
    t = dict(TOURNEY)
    t["host_playing"] = False
    t.update(over)
    return _sb_comp(monkeypatch, tourney=t)


def test_spectating_host_is_marked_removed_on_the_match(monkeypatch):
    sb = _sb_spectating(monkeypatch)
    _host_open(sb)
    assert sb.t["bngc_comp_matches"][0].get("removed_discord_ids") == ["A"]


def test_playing_host_is_not_marked_removed(monkeypatch):
    sb = _sb_comp(monkeypatch)  # TOURNEY has host_playing True
    _host_open(sb)
    assert not sb.t["bngc_comp_matches"][0].get("removed_discord_ids")


def test_every_non_playing_host_is_marked_removed(monkeypatch):
    # co-hosting: both hosts are spectating, so both are out
    sb = _sb_spectating(monkeypatch, hosts=["A", "B"])
    _host_open(sb)
    assert sorted(sb.t["bngc_comp_matches"][0]["removed_discord_ids"]) == ["A", "B"]


def test_spectating_host_gets_no_alv_row_without_a_spawned_avatar(monkeypatch):
    # hosting-only host: room.me is unset, so the name-keyed filter no-oped
    # and the host kept an alv row, which is a placement on the board.
    sb = _sb_spectating(monkeypatch)
    _host_open(sb)  # stG 1 opens the match
    room = _room(PLAYERS, [_kill("chris7", "cis6led")], me_name="", last_stg=3)
    proc.seal_comp_match("ses-host", "A", room, "room-x", "zw_lg")
    alv = [e["actor_name"] for e in sb.t["bngc_comp_events"]
           if e.get("etype") == "alv"]
    assert "cis6led" not in alv
    assert "chris7" in alv


def test_contributor_never_pairs_a_stranger_onto_a_spectating_host(monkeypatch):
    # The host registered as "cis6led" but sits in the room under a different
    # in-game name, so the pairing tier saw an unclaimed registrant and handed
    # that name straight to the host's did - crediting a spectating host with
    # someone else's kill. With host_playing False there is no candidate.
    sb = _sb_spectating(
        monkeypatch,
        regs=[{"tourney_id": 7, "status": "accepted",
               "member_ids": ["A", "B"]}])
    _host_open(sb)
    proc._comp_seen.clear()
    room = _room({1: _player("dom", 1, True), 2: _player("chris7", 2, True)},
                 [_kill("dom", "chris7")], me_name="chris7")
    proc.seal_comp_match("ses-b", "B", room, "room-x", "zw_lg")
    dom_rows = [e for e in sb.t["bngc_comp_events"]
                if e.get("actor_name") == "dom"]
    assert dom_rows and all(r.get("discord_id") != "A" for r in dom_rows)


def test_rt_spectating_host_is_marked_removed(monkeypatch):
    sb = _sb_rt(monkeypatch)
    sb.t["bngc_ranked_tourneys"][0]["host_playing"] = False
    room = _room(PLAYERS, [_kill("cis6led", "victim1")], short_code="RCON1")
    proc.seal_ranked_tourney_match("ses-host", "A", room, "room-x", "zw_lg")
    m = sb.t["bngc_ranked_tourney_matches"][0]
    assert m.get("removed_discord_ids") == ["A"]


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
