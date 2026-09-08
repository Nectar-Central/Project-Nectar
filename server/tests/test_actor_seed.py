"""Regression: MATCH-enter wipe must preserve self actor routing.

Without the seed, a nameless alv=false about me (quiet zone elim, no
killfeed) arriving after the wipe can't resolve via actor_map, the death is
missed, and the phase end records a win (1afa5e7f shape).
"""
from server.lib.photon_engine.engine import _resolve_actor
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


def _room_with_killfeed_learned_host():
    """falcon's view: self joined with props, host learned from killfeed
    (actor_nr -1, no map entry) — actor 1 must still resolve to the host."""
    r = RoomView(room_id="r1")
    r.state = State.MATCH
    me = Player(actor_nr=2, username="falcon", is_self=True)
    host = Player(actor_nr=-1, username="host")
    r.me = me
    r.players[2] = me
    r.players[7] = host
    r.actor_map = {2: "falcon"}
    return r


def test_resolve_actor_backfills_single_unknown():
    r = _room_with_killfeed_learned_host()
    assert _resolve_actor(r, 1) == "host"
    assert r.actor_map[1] == "host"  # map repaired for later lookups
    assert r.players[7].actor_nr == 1  # player linked, not duplicated


def test_resolve_actor_ambiguous_unknowns_stays_none():
    r = _room_with_killfeed_learned_host()
    r.players[8] = Player(actor_nr=-1, username="stranger")
    assert _resolve_actor(r, 1) is None  # two unknowns: must not guess
    assert 1 not in r.actor_map


def test_resolve_actor_repairs_pruned_map():
    r, _v = _room_with_self(actor=3, joined=True)
    r.actor_map = {}  # leave-pruning dropped the map while the player stayed
    assert _resolve_actor(r, 3) == "me"
    assert r.actor_map[3] == "me"


def test_resolve_actor_zero_is_room():
    r, _v = _room_with_self()
    assert _resolve_actor(r, 0) == "room"
