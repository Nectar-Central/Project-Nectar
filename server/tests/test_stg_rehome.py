"""stG re-home: lobby-bound match markers land on the last live match room.

Death sends Leave -> current switches to the lobby pseudo-room -> room-less
stG=3 would arm lobby (version never bumps, drain always skips) and the win
is silently lost while the drawer flaps lobby->match on the next kill.
_last_live_room picks the re-home target; wins then drain regardless of
lobby/match state (leaver/death guards downstream still deny false wins).
"""
import time

from server.lib.photon_engine.engine import _last_live_room
from server.lib.photon_engine.enums import GameMode, State
from server.lib.photon_engine.models import RoomView, SessionView


def _view(now):
    v = SessionView(session_id="s")
    v.rooms["lobby"] = RoomView(room_id="lobby")
    v.current_room_id = "lobby"
    return v, now


def _room(v, rid, age, state=State.LOBBY, version=1, gm=GameMode.ZW_LG, retired=False):
    r = RoomView(room_id=rid)
    r.state = state
    r.match_version = version
    r.game_mode = gm
    r.retired = retired
    r.last_packet_at = time.time() - age
    v.rooms[rid] = r
    return r


def test_rehome_picks_most_recent_real_room():
    v, now = _view(time.time())
    _room(v, "old", 60)
    new = _room(v, "new", 5)
    assert _last_live_room(v, now) is new


def test_rehome_skips_lobby_and_stale():
    v, now = _view(time.time())
    _room(v, "stale", 500)
    assert _last_live_room(v, now) is None


def test_rehome_skips_virgin_mm_room_but_keeps_combat_mm():
    v, now = _view(time.time())
    _room(v, "mm-sess_1a08", 5, state=State.LOBBY, version=0, gm=None)
    assert _last_live_room(v, now) is None
    combat = _room(v, "mm-sess_9z99", 5, state=State.MATCH, version=1, gm=GameMode.ZW_LG)
    assert _last_live_room(v, now) is combat


def test_rehome_accepts_flapped_lobby_room():
    # Death flapped the real room to LOBBY: still the right target (the
    # version bump no longer requires MATCH state).
    v, now = _view(time.time())
    r = _room(v, "13a086ea", 10, state=State.LOBBY, version=1)
    assert _last_live_room(v, now) is r


def test_rehome_nothing_live_returns_none():
    v, now = _view(time.time())
    assert _last_live_room(v, now) is None
