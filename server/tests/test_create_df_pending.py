"""CreateRoom DF must survive the request->response hop.

A private DF party's gm=DF lives only in the outbound CreateRoom request
(GameProperties); the inbound response carries RoomName but no gm. DF is
otherwise treated as a client placeholder and never stashed — so without
source-aware pending, the born room ends up gm=None (presence shows "-",
processor rows lose their mode) until a MainWorld scene repairs it.

Also guards the reverse: a stale matchmaking gm pending while a private
CreateRoom response arrives must be dropped, never applied.
"""
import time

from server.lib.photon_engine import engine as engine_mod
from server.lib.photon_engine.enums import GameMode, State


def _req_df():
    return {
        "kind": "OperationRequest",
        "op": 227,
        "opName": "CreateRoom",
        "gm": "DF",
        "params": {
            "GameProperties": {"gm": "DF", "pID": "19S9Z", "Party": True},
            "RoomName": None,
        },
        "playerProps": {"255": "cis6led"},
        "properties": {},
    }


def _resp_room(room="room-uuid-1"):
    return {
        "kind": "OperationResponse",
        "op": 227,
        "opName": "CreateRoom",
        "returnCode": 0,
        "params": {
            "RoomName": {"type": "String", "value": room},
            "Address": {"type": "String", "value": "wss://example:19091/game"},
        },
        "properties": {},
    }


def _req_matchmaking_zwlg():
    return {
        "kind": "OperationRequest",
        "op": 225,
        "opName": "JoinRandomRoom",
        "gm": "zw_lg",
        "params": {"GameProperties": {"gm": "zw_lg"}},
        "properties": {},
    }


def test_create_df_survives_request_response_hop(monkeypatch):
    e = engine_mod.Engine()
    mapping = {"REQ": _req_df(), "RESP": _resp_room()}
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    e.feed("s_df", "REQ", "out")
    # DF from an explicit CreateRoom is stashed (with its source op + ts)
    assert e._pending_gm.get("s_df")[:2] == (GameMode.DF, "CreateRoom")
    pkt = e.feed("s_df", "RESP", "in")
    room = e.get_session("s_df").rooms[pkt.room_id]
    assert room.game_mode == GameMode.DF
    assert room.short_code == "19S9Z"


def test_stale_matchmaking_gm_dropped_for_private_create(monkeypatch):
    e = engine_mod.Engine()
    mapping = {"MM": _req_matchmaking_zwlg(), "RESP": _resp_room(room="priv-uuid-9")}
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    e.feed("s_mm", "MM", "out")
    assert e._pending_gm.get("s_mm")[:2] == (GameMode.ZW_LG, "JoinRandomRoom")
    pkt = e.feed("s_mm", "RESP", "in")
    room = e.get_session("s_mm").rooms[pkt.room_id]
    assert room.game_mode is None  # never stamped by stale matchmaking gm
    assert "s_mm" not in e._pending_gm  # consumed (dropped), not lingering


def _resp_nogp(room="late-uuid-1"):
    """Response carrying RoomName but no GameProperties (the reconnect case)."""
    return {
        "kind": "OperationResponse",
        "op": 227,
        "opName": "CreateRoom",
        "returnCode": 0,
        "params": {
            "RoomName": {"type": "String", "value": room},
            "Address": {"type": "String", "value": "wss://example:19091/game"},
        },
        "properties": {},
    }


def _req_df_full():
    """Late room-less CreateRoom request: gm + pID + Party, no RoomName."""
    return {
        "kind": "OperationRequest",
        "op": 227,
        "opName": "CreateRoom",
        "gm": "DF",
        "Party": True,
        "params": {
            "GameProperties": {"gm": "DF", "pID": "19S9Z", "Party": True},
            "RoomName": None,
        },
        "playerProps": {"255": "cis6led"},
        "properties": {},
    }


def test_retrofill_late_create_request_heals_born_room(monkeypatch):
    # Response first (room born blank), request seconds later — the room
    # must still end up DF + coded + party-flagged.
    e = engine_mod.Engine()
    mapping = {"RESP": _resp_nogp(), "REQ": _req_df_full()}
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    pkt = e.feed("s_retro", "RESP", "in")
    room = e.get_session("s_retro").rooms[pkt.room_id]
    assert room.game_mode is None and room.short_code is None
    assert room.created_by_me is True
    e.feed("s_retro", "REQ", "out")
    assert room.game_mode == GameMode.DF
    assert room.short_code == "19S9Z"
    assert room.party.is_party is True


def test_pending_gm_expires(monkeypatch):
    # A stash older than the window must not stamp a later room.
    e = engine_mod.Engine()
    mapping = {"RESP": _resp_room(room="later-uuid-2")}
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    e._pending_gm["s_old"] = (GameMode.DF, "CreateRoom", time.time() - 500)
    pkt = e.feed("s_old", "RESP", "in")
    room = e.get_session("s_old").rooms[pkt.room_id]
    assert room.game_mode is None
    assert "s_old" not in e._pending_gm  # expired entry dropped, not kept


def _scene_mainworld():
    return {
        "kind": "EventData",
        "evCode": 253,
        "evName": "PropertiesChanged",
        "properties": {"curScn": "MainWorld"},
        "actorNr": 1,
    }


def test_live_lobby_match_keeps_gm(monkeypatch):
    # Game running in the lobby bucket (lost CreateRoom response): the FSM
    # sets DF on MainWorld enter and later packets must not scrub it while
    # the lobby is fresh.
    e = engine_mod.Engine()
    mapping = {"SCN": _scene_mainworld(), "UNK": {"kind": "Unknown"}}
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    e.feed("s_lob", "SCN", "in")
    lobby = e.get_session("s_lob").rooms["lobby"]
    assert lobby.state == State.MATCH
    assert lobby.game_mode == GameMode.DF
    e.feed("s_lob", "UNK", "in")
    e.feed("s_lob", "UNK", "in")
    assert lobby.game_mode == GameMode.DF  # fresh lobby: scrub must not erase
    assert lobby.state == State.MATCH


def test_stale_lobby_is_scrubbed(monkeypatch):
    # Restored snapshot lobby (old last_packet_at) with a mode: first live
    # packet scrubs it instead of leaking it into fallbacks.
    e = engine_mod.Engine()
    mapping = {"UNK": {"kind": "Unknown"}}
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    e.feed("s_stale", "UNK", "in")
    lobby = e.get_session("s_stale").rooms["lobby"]
    lobby.game_mode = GameMode.ZW_LG
    lobby.short_code = "ABCDE"
    lobby.last_packet_at = time.time() - 500
    e.feed("s_stale", "UNK", "in")
    assert lobby.game_mode is None
    assert lobby.short_code is None


def _auth():
    return {"kind": "OperationRequest", "op": 230, "opName": "Authenticate", "params": {}}


def test_auth_keeps_nascent_created_room(monkeypatch):
    # Reconnect-handshake auth seconds after a CreateRoom response must not
    # yank current back to lobby nor clear the room's code — otherwise every
    # later gameplay packet lands in the lobby bucket and the room sits
    # ver=0 forever (duel seals + ranked drains starve).
    e = engine_mod.Engine()
    mapping = {"RESP": _resp_nogp(room="nascent-1"), "AUTH": _auth()}
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    pkt = e.feed("s_auth", "RESP", "in")
    room = e.get_session("s_auth").rooms[pkt.room_id]
    room.short_code = "CJ1HC"
    e.feed("s_auth", "AUTH", "out")
    assert e.get_session("s_auth").current_room_id == pkt.room_id
    assert room.short_code == "CJ1HC"


def test_auth_keeps_live_match_room(monkeypatch):
    # Mid-match reconnect auth: MATCH room with fresh packets stays current.
    e = engine_mod.Engine()
    mapping = {"RESP": _resp_nogp(room="live-1"), "AUTH": _auth()}
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    pkt = e.feed("s_authlive", "RESP", "in")
    room = e.get_session("s_authlive").rooms[pkt.room_id]
    room.state = State.MATCH
    room.short_code = "CJ1HC"
    e.feed("s_authlive", "AUTH", "out")
    assert e.get_session("s_authlive").current_room_id == pkt.room_id
    assert room.short_code == "CJ1HC"


def test_auth_yanks_stale_room(monkeypatch):
    # Back-to-menu auth with no nascent/live room: old behavior preserved —
    # current drops to lobby and the stale code is cleared (ranked ghost
    # protection).
    e = engine_mod.Engine()
    mapping = {"RESP": _resp_nogp(room="stale-1"), "AUTH": _auth()}
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    pkt = e.feed("s_authstale", "RESP", "in")
    room = e.get_session("s_authstale").rooms[pkt.room_id]
    room.short_code = "CJ1HC"
    room.created_at = "2020-01-01T00:00:00+00:00"
    e.feed("s_authstale", "AUTH", "out")
    assert e.get_session("s_authstale").current_room_id == "lobby"
    assert room.short_code is None


def test_gameplay_after_create_auth_lands_in_room(monkeypatch):
    # Full fixed flow: CreateRoom response births the room, handshake auth
    # keeps it current, then room-less kill + scene packets land IN the room
    # (not the lobby bucket) and flip it to a DF match — duel-sealable.
    e = engine_mod.Engine()
    mapping = {
        "RESP": _resp_nogp(room="game-1"),
        "AUTH": _auth(),
        "KILL": {"kind": "EventData", "evCode": 101, "evName": "Killfeed",
                 "killfeed": "cis6led killed chris7"},
        "SCN": _scene_mainworld(),
    }
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    pkt = e.feed("s_game", "RESP", "in")
    rid = pkt.room_id
    e.feed("s_game", "AUTH", "out")
    e.feed("s_game", "AUTH", "out")
    kpkt = e.feed("s_game", "KILL", "in")
    assert kpkt.room_id == rid  # not lobby
    room = e.get_session("s_game").rooms[rid]
    assert len(room.recent_kills) == 1
    e.feed("s_game", "SCN", "in")
    assert room.state == State.MATCH
    assert room.game_mode == GameMode.DF
    _lob = e.get_session("s_game").rooms.get("lobby")
    assert _lob is None or _lob.recent_kills == []


def test_mm_room_code_cleared_even_when_fresh(monkeypatch):
    # mm-* rooms stay codeless unconditionally (ranked/comp are code-gated).
    e = engine_mod.Engine()
    mapping = {
        "RESP": {
            "kind": "OperationResponse",
            "op": 225,
            "opName": "JoinRandomRoom",
            "returnCode": 0,
            "params": {"RoomName": {"type": "String", "value": "mm-test-99"}},
            "properties": {},
        }
    }
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    pkt = e.feed("s_mm2", "RESP", "in")
    mm_id = pkt.room_id
    assert mm_id.startswith("mm-")
    room = e.get_session("s_mm2").rooms[mm_id]
    room.short_code = "N69A8"  # plant: stale-code leak simulation
    e.feed("s_mm2", "RESP", "in")
    assert room.short_code is None
