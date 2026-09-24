"""Lobby-bucket mode reset on match enter.

A stale concrete gm on the lobby pseudo-room (previous match, or a restored
Upstash snapshot) used to block DF forever: adopt_game_mode never lets DF
clobber concrete, so DF matches stayed invisible (wrong mode on playtime,
kills and drawer, flicker-suppressed logs). Real rooms must keep their gm.
"""
from types import SimpleNamespace

from server.lib.photon_engine.enums import GameMode, State
from server.lib.photon_engine.fsm import RoomFSM
from server.lib.photon_engine.models import RoomView, SessionView


def _lobby_room(gm):
    r = RoomView(room_id="lobby", code=None)
    r.state = State.LOBBY
    r.game_mode = gm
    v = SessionView(session_id="s")
    v.rooms["lobby"] = r
    v.current_room_id = "lobby"
    return r, v


def _scene_packet():
    return SimpleNamespace(packet_type="scene", game_mode=None, cur_scn=None)


def test_lobby_gm_reset_allows_df():
    r, v = _lobby_room(GameMode.ZW_LG)  # stale previous match
    changed, old, new, via = RoomFSM.transition(
        r, v, _scene_packet(), None, "MainWorld", "sess_test")
    assert changed is True
    assert new == State.MATCH
    assert r.game_mode == GameMode.DF  # fresh match, fresh mode


def test_lobby_gm_reset_then_upgrades():
    # Same entry, then concrete evidence upgrades the DF placeholder.
    r, v = _lobby_room(GameMode.ZW_LG)
    RoomFSM.transition(r, v, _scene_packet(), None, "MainWorld", "sess_test")
    assert r.game_mode == GameMode.DF
    assert r.adopt_game_mode(GameMode.ZW_LG) is True
    assert r.game_mode == GameMode.ZW_LG


def test_real_room_keeps_gm():
    # Private/series rooms are NOT reset: mode continuity belongs to them.
    r = RoomView(room_id="abc123", code="ABCDE")
    r.state = State.LOBBY
    r.game_mode = GameMode.ZW_LG
    v = SessionView(session_id="s")
    v.rooms["abc123"] = r
    v.current_room_id = "abc123"
    changed, old, new, via = RoomFSM.transition(
        r, v, _scene_packet(), None, "MainWorld", "sess_test")
    assert changed is True
    assert new == State.MATCH
    assert r.game_mode == GameMode.ZW_LG  # DF placeholder must not clobber


def _match_room(gm):
    # A room sitting in MATCH (e.g. after a zw_lg series in the bucket).
    r = RoomView(room_id="lobby", code=None)
    r.state = State.MATCH
    r.game_mode = gm
    v = SessionView(session_id="s")
    v.rooms["lobby"] = r
    v.current_room_id = "lobby"
    return r, v


def test_lobby_gm_cleared_on_lobby_return():
    # Leaving to MainLobby clears the bucket mode (no drain reads it —
    # the processor skips the pseudo-room unconditionally).
    r, v = _match_room(GameMode.ZW_LG)
    changed, old, new, via = RoomFSM.transition(
        r, v, _scene_packet(), None, "MainLobby", "sess_test")
    assert changed is True
    assert new == State.LOBBY
    assert r.game_mode is None


def test_real_room_gm_kept_on_lobby_return():
    # Real rooms keep gm across the exit (post-match drains need it).
    r = RoomView(room_id="abc123", code="ABCDE")
    r.state = State.MATCH
    r.game_mode = GameMode.ZW_LG
    v = SessionView(session_id="s")
    v.rooms["abc123"] = r
    v.current_room_id = "abc123"
    changed, old, new, via = RoomFSM.transition(
        r, v, _scene_packet(), None, "MainLobby", "sess_test")
    assert changed is True
    assert new == State.LOBBY
    assert r.game_mode == GameMode.ZW_LG
