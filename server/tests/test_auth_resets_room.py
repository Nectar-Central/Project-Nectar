"""Regression: a fresh authenticate (reload / disconnect / reconnect) must drop
the stale current room — otherwise stats keeps serving the previous (possibly
ranked) room as live and the drawer/UI shows a ghost ranked session.

Feeds a minimal real auth op (f302e600 -> opName Authenticate) through the
engine against a session parked in a ranked room.
"""
import pytest

from server.lib.photon_engine.engine import Engine
from server.lib.photon_engine.models import RoomView, SessionView

AUTH_HEX = "f302e600"


def _seeded(current="R-AAA"):
    eng = Engine()
    v = SessionView(session_id="s")
    r = RoomView(room_id=current, code=current)
    r.short_code = "A5337"
    v.rooms[current] = r
    v.current_room_id = current
    eng.put_session("s", v)
    return eng


def test_auth_resets_stale_current_room_to_lobby():
    eng = _seeded()
    eng.feed("s", AUTH_HEX)
    v = eng.get_session("s")
    assert v.current_room_id == "lobby"
    assert "lobby" in v.rooms


def test_auth_when_already_lobby_is_noop():
    eng = Engine()
    v = SessionView(session_id="s")
    eng.put_session("s", v)
    eng.feed("s", AUTH_HEX)
    assert eng.get_session("s").current_room_id == "lobby"


def test_auth_keeps_ranked_room_record():
    # Only the pointer moves — the old room (seals/history) is untouched.
    eng = _seeded()
    eng.feed("s", AUTH_HEX)
    v = eng.get_session("s")
    assert "R-AAA" in v.rooms
    assert v.rooms["R-AAA"].short_code == "A5337"
