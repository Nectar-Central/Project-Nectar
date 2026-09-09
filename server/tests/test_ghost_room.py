"""Ghost-room healer: a MATCH room gone quiet while the session lives on must
park back to lobby (self-leave missed, e.g. null identity) instead of
freezing the drawer, presence and room tag on a dead match forever."""
import time

from server.lib.photon_engine.engine import Engine
from server.lib.photon_engine.enums import GameMode, State
from server.lib.photon_engine.models import RoomView, SessionView


def _view_with_match(age_s, state=State.MATCH, gm=GameMode.ZW_LG):
    eng = Engine()
    v = SessionView(session_id="s-ghost")
    r = RoomView(room_id="R-AAA", code="R-AAA")
    r.short_code = "PBS05"
    r.state = state
    r.game_mode = gm
    r.players_count = 8
    r.last_packet_at = time.time() - age_s if age_s is not None else None
    v.rooms["R-AAA"] = r
    v.current_room_id = "R-AAA"
    eng.put_session("s-ghost", v)
    return eng, v, r


def test_silent_match_heals_to_lobby():
    eng, v, r = _view_with_match(500)
    assert eng._heal_ghost_room(v, "s-ghost") is True
    assert v.current_room_id == "lobby"
    assert "lobby" in v.rooms
    assert r.retired is True
    # history preserved for drains: seals, versions, code all intact
    assert r.short_code == "PBS05"
    assert r.state == State.MATCH


def test_fresh_match_untouched():
    eng, v, r = _view_with_match(5)
    assert eng._heal_ghost_room(v, "s-ghost") is False
    assert v.current_room_id == "R-AAA"
    assert r.retired is False


def test_no_timestamp_never_heals():
    eng, v, r = _view_with_match(None)
    assert eng._heal_ghost_room(v, "s-ghost") is False


def test_lobby_and_non_match_untouched():
    eng, v, r = _view_with_match(500, state=State.LOBBY)
    assert eng._heal_ghost_room(v, "s-ghost") is False
    v2 = SessionView(session_id="s2")
    assert eng._heal_ghost_room(v2, "s2") is False
    assert eng._heal_ghost_room(None, "s2") is False


def test_retired_survives_serverless_restore():
    _, v, r = _view_with_match(500)
    r.retired = True
    d = v.to_dict()
    assert d["rooms"]["R-AAA"]["retired"] is True
    v2 = SessionView.from_dict(d)
    assert v2.rooms["R-AAA"].retired is True
    # old caches without the key default open (not retired)
    d["rooms"]["R-AAA"].pop("retired", None)
    v3 = SessionView.from_dict(d)
    assert v3.rooms["R-AAA"].retired is False
