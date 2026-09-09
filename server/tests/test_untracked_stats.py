"""Untracked-stats identity: name match OR stable game_id (pfid).

Regression tests for players whose killfeed names drift from the join-time
username (casing/tags) or whose view.me never resolved — playtime kept
counting (session-scoped) while kills/skins silently missed.
"""
from server.lib.photon_engine.engine import Engine
from server.lib.photon_engine.models import Player, RoomView, SessionView
from server.scripts.processor import _is_self


def _room(*players):
    r = RoomView(room_id="r1")
    for i, p in enumerate(players):
        r.players[100 + i] = p
    return r


def _user(name=None, gid=None):
    return {"game_username": name, "game_id": gid}


class _Me:
    def __init__(self, username):
        self.username = username


class _View:
    def __init__(self, me):
        self.me = me


def test_name_match_exact_and_decorated():
    room = _room()
    user = _user("Alice")
    view = _View(_Me("Alice"))
    assert _is_self(room, view, user, "Alice") is True
    assert _is_self(room, view, user, "dc: Alice") is True
    assert _is_self(room, view, user, "<color=red>Alice</color>") is True
    assert _is_self(room, view, user, "Bob") is False
    assert _is_self(room, view, user, None) is False


def test_gid_match_when_name_drifts():
    # killfeed casing drift + view.me missing entirely
    room = _room(Player(actor_nr=7, username="ALICE", game_id="G1"))
    user = _user("alice", "G1")
    assert _is_self(room, None, user, "ALICE") is True
    # totally different display name, same stable account id
    room2 = _room(Player(actor_nr=7, username="xX_sniper_Xx", game_id="G1"))
    assert _is_self(room2, None, user, "xX_sniper_Xx") is True


def test_gid_mismatch_is_not_self():
    room = _room(Player(actor_nr=7, username="ALICE", game_id="G2"))
    user = _user("alice", "G1")
    assert _is_self(room, None, user, "ALICE") is False


def test_no_gid_no_match():
    room = _room(Player(actor_nr=7, username="ALICE", game_id="G9"))
    user = _user("alice", None)
    assert _is_self(room, None, user, "ALICE") is False
    assert _is_self(None, None, user, "alice") is True  # name fallback, no room
    assert _is_self(None, None, _user(None, None), "alice") is False


def test_never_raises():
    assert _is_self(None, None, None, None) is False
    assert _is_self("garbage", "garbage", "garbage", 123) is False


def _adopt_view():
    eng = Engine()
    v = SessionView(session_id="s-echo")
    r = RoomView(room_id="r1", code="r1")
    v.rooms["r1"] = r
    v.current_room_id = "r1"
    return eng, v, r


def test_echo_adopt_sets_me_and_registers():
    eng, v, r = _adopt_view()
    assert eng._adopt_me_from_echo(v, r, "itzshirooo", 7, "s-echo") is True
    assert v.me is not None and v.me.username == "itzshirooo"
    assert v.me.is_self is True and v.me.actor_nr == 7
    assert v.me in list(r.players.values())
    assert r.me is v.me


def test_echo_adopt_never_overwrites():
    eng, v, r = _adopt_view()
    v.me = Player(actor_nr=3, username="RealMe", is_self=True)
    assert eng._adopt_me_from_echo(v, r, "SomeoneElse", 9, "s-echo") is False
    assert v.me.username == "RealMe"


def test_echo_adopt_strips_tags_and_rejects_empty():
    eng, v, r = _adopt_view()
    assert eng._adopt_me_from_echo(v, r, "<color=red>Bob</color>", None, "s-echo") is True
    assert v.me.username == "Bob"
    eng2, v2, r2 = _adopt_view()
    assert eng2._adopt_me_from_echo(v2, r2, "", 1, "s-echo") is False
    assert eng2._adopt_me_from_echo(v2, r2, None, 1, "s-echo") is False
    assert eng2._adopt_me_from_echo(None, r2, "Bob", 1, "s-echo") is False
    assert v2.me is None
