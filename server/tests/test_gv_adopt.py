"""gv adoption on live rooms (wwzw->solo lobby change regression).

A room created in wwzw (gv=duo) then switched to zw_lg+solo kept gv=duo
forever (write-once): solo matches ran duo rules, defeat markers held
silently, and stale _pt teams renamed solo winners to team_ids.
adopt_gv updates on change and purges duo remnants on a flip to solo.
"""
from server.lib.photon_engine.models import Player, RoomView, Team


def _duo_room():
    r = RoomView(room_id="r-gv")
    r.gv = "duo"
    r.players[1] = Player(actor_nr=1, username="cis6led", is_alive=True)
    r.players[1].pt = "t1"
    r.players[2] = Player(actor_nr=2, username="chris7", is_alive=False)
    r.players[2].pt = "t1"
    r.teams["t1"] = Team(team_id="t1", members=["cis6led", "chris7"], is_self_team=True)
    r.pending_pt[9] = "t1"
    return r


def test_init_sets_gv():
    r = RoomView(room_id="r-new")
    assert r.adopt_gv("duo") is True
    assert r.gv == "duo" and r.is_duo is True


def test_same_value_noop_case_insensitive():
    r = _duo_room()
    assert r.adopt_gv("DUO") is False
    assert r.gv == "duo"  # original casing kept
    assert r.teams  # untouched


def test_blank_rejected():
    r = _duo_room()
    assert r.adopt_gv(None) is False
    assert r.adopt_gv("") is False
    assert r.adopt_gv("  ") is False
    assert r.gv == "duo"


def test_flip_to_solo_purges_duo_state():
    r = _duo_room()
    assert r.adopt_gv("solo") is True
    assert r.gv == "solo" and r.is_duo is False
    assert r.teams == {}
    assert all(p.pt is None for p in r.players.values())
    assert r.pending_pt == {}


def test_flip_to_duo_keeps_teams_for_rebuild():
    r = RoomView(room_id="r-s")
    r.gv = "solo"
    assert r.adopt_gv("duo") is True
    assert r.is_duo is True


def test_stale_team_no_longer_overrides_solo():
    """After the flip, team logic is gone: solo rules apply."""
    import server.scripts.processor as proc

    r = _duo_room()
    assert proc._team_alive_for_me(r, None, "cis6led") is True  # stale team: alive
    r.adopt_gv("solo")
    assert proc._team_alive_for_me(r, None, "cis6led") is None  # solo rules
