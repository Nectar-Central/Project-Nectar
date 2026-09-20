"""Private party continuous series: stG cycles 3->2->1 with no lobby return.

Without the stG=1 re-enter, a room parked in LOBBY by an intermission blip
stays there and every later phase-end drain starves (gated on MATCH) — wins
silently lost after the first match. Leavers must never win via phase end.

Same starvation when no scene packet ever arrives (Create/Join carry no
curScn): first combat in a LOBBY-state real room enters MATCH.
"""
from server.lib.photon_engine.engine import room_enter_on_combat
from server.lib.photon_engine.enums import GameMode, State
from server.lib.photon_engine.fsm import RoomFSM
from server.lib.photon_engine.models import KillInfo, Player, RoomView, SessionView
from server.scripts.processor import _phase_end_result


def _room(state=State.LOBBY, code="ABCDE", version=2, me="me", others=("foe",)):
    r = RoomView(room_id="r1")
    r.short_code = code
    r.state = state
    r.match_version = version
    r.stg_is_phase_end = False
    r.last_win_version = 5
    r.actor_map = {"old": "stale"}
    if me is not None:
        r.players[1] = Player(actor_nr=1, username=me, is_alive=True)
    for i, o in enumerate(others, start=2):
        r.players[i] = Player(actor_nr=i, username=o, is_alive=True)
    v = SessionView(session_id="s")
    if me is not None:
        v.me = Player(actor_nr=1, username=me)
    v.rooms["r1"] = r
    v.current_room_id = "r1"
    return r, v


def test_reenter_cycled_pid_room():
    r, v = _room()
    assert RoomFSM.reenter_match_for_series(r, v, "sess_test") is True
    assert r.state == State.MATCH
    assert r.last_win_version == -1  # on_enter reset for the new match
    assert r.actor_map.get(1) == "me"  # self reseeded, stale dropped
    assert "old" not in r.actor_map


def test_reenter_keeps_scene():
    r, v = _room()
    r.cur_scn = "ZoneWars_LateGame_12Players"
    assert RoomFSM.reenter_match_for_series(r, v, "sess_test") is True
    assert r.cur_scn == "ZoneWars_LateGame_12Players"


def test_no_reenter_public_room_without_prior_match():
    # First public match (v0) with me on roster: stG=1 means the match is
    # starting even though no scene ever arrived (JoinRoom carries no curScn).
    # Without this, public rooms sit in LOBBY and wins are lost (cis6led zw_lg).
    r, v = _room(code=None, version=0)
    assert RoomFSM.reenter_match_for_series(r, v, "sess_test") is True
    assert r.state == State.MATCH
    assert r.match_version == 1


def test_no_reenter_public_room_without_me_or_history():
    # Still no false-enter: unknown identity + thin room + v0 stays lobby.
    r, v = _room(code=None, version=0, me=None, others=("foe",))
    v.me = None
    assert RoomFSM.reenter_match_for_series(r, v, "sess_test") is False
    assert r.state == State.LOBBY


def test_reenter_public_room_with_prior_match():
    # same symptom in public matches: parked in LOBBY after a proven match
    # here, series cycles -> re-enter like party rooms
    r, v = _room(code=None, version=2)
    assert RoomFSM.reenter_match_for_series(r, v, "sess_test") is True
    assert r.state == State.MATCH


def test_no_reenter_lobby_pseudo_room():
    r, v = _room(code=None, version=0)
    r.room_id = "lobby"
    assert RoomFSM.reenter_match_for_series(r, v, "sess_test") is False


def test_no_reenter_when_already_match():
    r, v = _room(state=State.MATCH)
    assert RoomFSM.reenter_match_for_series(r, v, "sess_test") is False


def test_no_reenter_for_leaver():
    # my row pruned on leave: no re-enter even as the series cycles
    r, v = _room(me=None, others=("foe", "mate"))
    v.me = Player(actor_nr=1, username="me")
    assert RoomFSM.reenter_match_for_series(r, v, "sess_test") is False
    assert r.state == State.LOBBY


def test_unknown_identity_needs_populated_room():
    r, v = _room(me=None, others=("foe",))
    v.me = None
    assert RoomFSM.reenter_match_for_series(r, v, "sess_test") is False
    r.players[9] = Player(actor_nr=9, username="mate", is_alive=True)
    assert RoomFSM.reenter_match_for_series(r, v, "sess_test") is True


def test_phase_end_leaver_never_wins():
    r, v = _room(state=State.MATCH, me=None, others=("foe",))
    r.recent_kills.append(KillInfo(instigator="me", instigated="", event_type="left"))
    assert _phase_end_result(r, v, "me") is None


def test_phase_end_absent_without_leave_keeps_fallback():
    r, v = _room(state=State.MATCH, me=None, others=())
    assert _phase_end_result(r, v, "me") == "win"


def _combat_room(**kw):
    r, v = _room(**kw)
    return r, v


def test_combat_enters_sceneless_lobby_room():
    r, v = _combat_room()
    assert room_enter_on_combat(r, v, "sess_test", "killer", "victim", GameMode.ZW_LG) is True
    assert r.state == State.MATCH
    assert r.game_mode == GameMode.ZW_LG
    assert r.match_playtime.last_state == State.MATCH.value


def test_combat_enter_idempotent_when_matched():
    r, v = _combat_room(state=State.MATCH)
    assert room_enter_on_combat(r, v, "sess_test", "killer", "victim") is False


def test_combat_enter_skips_lobby_pseudo_room():
    r, v = _combat_room()
    r.room_id = "lobby"
    assert room_enter_on_combat(r, v, "sess_test", "killer", "victim") is False
    assert r.state == State.LOBBY


def test_combat_enter_rejects_garbage_names():
    r, v = _combat_room()
    assert room_enter_on_combat(r, v, "sess_test", "<color=killer>", "victim") is False
    assert room_enter_on_combat(r, v, "sess_test", "", "victim") is False
    assert r.state == State.LOBBY


def test_combat_enter_never_adopts_df_placeholder():
    r, v = _combat_room()
    assert room_enter_on_combat(r, v, "sess_test", "killer", "victim", GameMode.DF) is True
    assert r.state == State.MATCH
    assert r.game_mode is None


def test_combat_enter_keeps_existing_gm():
    r, v = _combat_room()
    r.game_mode = GameMode.ZW_LG
    assert room_enter_on_combat(r, v, "sess_test", "killer", "victim", GameMode.DF) is True
    assert r.game_mode == GameMode.ZW_LG


class _Pkt:
    """Minimal stand-in for EnginePacket (only what the FSM reads)."""

    def __init__(self, ptype, name=None):
        self.packet_type = ptype
        self.player_name = name
        self.cur_scn = None
        self.game_mode = None


def _scene_transition(r, v, cur_scn):
    from server.lib.photon_engine.enums import PacketType as _PT
    return RoomFSM.transition(r, v, _Pkt(_PT.SCENE), {}, cur_scn, "sess_test")


def _endless_room(gm, code="ABCDE"):
    r, v = _room(state=State.MATCH, code=code)
    r.game_mode = gm
    return r, v


def test_transition_executes_lobby_to_match():
    # Regression: the transition body was once stranded as dead code after
    # reenter_match_for_series, so transition() returned None on every real
    # flip (engine.py swallowed the unpack TypeError as "fsm error").
    r, v = _room(state=State.LOBBY)
    did, old, new, via = _scene_transition(r, v, "Game")
    assert did is True
    assert (old, new) == (State.LOBBY, State.MATCH)
    assert r.state == State.MATCH


def test_transition_executes_match_to_lobby_on_auth():
    from server.lib.photon_engine.enums import PacketType as _PT
    r, v = _room(state=State.MATCH)
    did, old, new, via = RoomFSM.transition(
        r, v, _Pkt(_PT.UNKNOWN), {"opName": "Authenticate"}, None, "sess_test")
    assert did is True
    assert (old, new) == (State.MATCH, State.LOBBY)
    assert via == "authenticate"
    assert r.state == State.LOBBY


def test_transition_no_trigger_returns_false_tuple():
    from server.lib.photon_engine.enums import PacketType as _PT
    r, v = _room(state=State.MATCH)
    assert RoomFSM.transition(r, v, _Pkt(_PT.UNKNOWN), {}, None, "sess_test") == (
        False, State.MATCH, State.MATCH, None)


def test_endless_hold_private_zwlg_ignores_lobby_scene():
    # Private zw_lg room, infinite matches, stG=3 intermission reads as a
    # MainLobby scene: state must stay MATCH (details lines keep "In Match").
    r, v = _endless_room(GameMode.ZW_LG)
    did, old, new, via = _scene_transition(r, v, "MainLobby")
    assert did is False
    assert r.state == State.MATCH


def test_endless_hold_private_wwzw_ignores_lobby_scene():
    r, v = _endless_room(GameMode.WWZW)
    did, old, new, via = _scene_transition(r, v, "MainLobby")
    assert did is False
    assert r.state == State.MATCH


def test_endless_hold_public_room_still_exits():
    # No code (public matchmaking): unchanged behavior, scene exits to lobby.
    r, v = _endless_room(GameMode.ZW_LG, code=None)
    did, old, new, via = _scene_transition(r, v, "MainLobby")
    assert did is True
    assert r.state == State.LOBBY


def test_endless_hold_other_mode_still_exits():
    # Private but not an endless-series mode: unchanged behavior.
    r, v = _endless_room(GameMode.BF)
    did, old, new, via = _scene_transition(r, v, "MainLobby")
    assert did is True
    assert r.state == State.LOBBY


def test_endless_hold_auth_still_exits():
    # Authenticate is the series end: the hold must never trap it.
    from server.lib.photon_engine.enums import PacketType as _PT
    r, v = _endless_room(GameMode.ZW_LG)
    did, old, new, via = RoomFSM.transition(
        r, v, _Pkt(_PT.UNKNOWN), {"opName": "Authenticate"}, None, "sess_test")
    assert did is True
    assert r.state == State.LOBBY
    assert via == "authenticate"


def test_endless_hold_self_leave_still_exits():
    # Proven self-leave (Leave naming me) ends the series like an auth.
    from server.lib.photon_engine.enums import PacketType as _PT
    r, v = _endless_room(GameMode.ZW_LG)
    did, old, new, via = RoomFSM.transition(
        r, v, _Pkt(_PT.LEAVE, "me"), {}, None, "sess_test")
    assert did is True
    assert r.state == State.LOBBY
