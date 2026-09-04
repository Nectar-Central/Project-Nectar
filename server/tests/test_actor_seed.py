"""Regression: MATCH-enter wipe must preserve self actor routing.

Without the seed, a nameless alv=false about me (quiet zone elim, no
killfeed) arriving after the wipe can't resolve via actor_map, the death is
missed, and the phase end records a win (1afa5e7f shape).
"""
from server.lib.photon_engine.enums import State
from server.lib.photon_engine.fsm import RoomFSM
from server.lib.photon_engine.models import Player, RoomView, SessionView


def _room_with_self(actor=3, joined=True):
    r = RoomView(room_id="r1")
    r.state = State.LOBBY
    r.actor_map = {9: "ghost"}
    me = Player(actor_nr=actor, username="me", is_self=True)
    r.me = me
    if joined:
        r.players[100] = me
    v = SessionView(session_id="s")
    v.me = me
    return r, v


def test_match_enter_preserves_self_actor():
    r, v = _room_with_self(actor=3, joined=True)
    RoomFSM._on_enter(r, v, State.LOBBY, State.MATCH, "scene:Game",
                      "sess_test", None, None, "Game")
    assert r.actor_map == {3: "me"}
    assert r.recent_kills == [] and r.recent_knocks == []


def test_match_enter_before_my_join_seeds_nothing():
    # scene packet arriving before my join (stale actor from a previous room
    # must not linger and misattribute others to me)
    r, v = _room_with_self(actor=3, joined=False)
    RoomFSM._on_enter(r, v, State.LOBBY, State.MATCH, "scene:Game",
                      "sess_test", None, None, "Game")
    assert r.actor_map == {}


def test_match_enter_without_known_actor_clears_fully():
    r, v = _room_with_self(actor=-1, joined=True)
    RoomFSM._on_enter(r, v, State.LOBBY, State.MATCH, "scene:Game",
                      "sess_test", None, None, "Game")
    assert r.actor_map == {}


def test_later_join_overwrites_seed():
    r, v = _room_with_self(actor=3, joined=True)
    RoomFSM._on_enter(r, v, State.LOBBY, State.MATCH, "scene:Game",
                      "sess_test", None, None, "Game")
    r.actor_map[3] = "someone-else"  # what a Join re-resolving would do
    assert r.actor_map[3] == "someone-else"
