"""Regression: phase-end (stG=3) must resolve wins even when the room has
emptied (no live players to judge) — the duo zw_lg case where matches ended
with an undrained arm and nothing was ever recorded. Wins-only: non-wins
resolve to None (seal-only drain still seals ranked).

Uses the real helpers against constructed RoomViews (no DB, no engine feed).
"""
import pytest

from server.lib.photon_engine.enums import GameMode, State
from server.lib.photon_engine.models import (
    KillInfo,
    Player,
    RoomView,
    SessionView,
    Team,
)
from server.scripts.processor import _phase_end_debug, _phase_end_result, _self_death_evidence


def _room(me_name="me", teammates=(), dead=(), feed_killed_me=False,
          flag=False, players=True):
    r = RoomView(room_id="r1")
    r.game_mode = GameMode.ZW_LG
    r.state = State.MATCH
    r.stg_is_phase_end = True
    r.match_version = 1
    r.had_self_death = flag
    if players:
        r.players[1] = Player(actor_nr=1, username=me_name,
                              is_alive=("me" not in dead and me_name not in dead))
        for i, t in enumerate(teammates, start=2):
            r.players[i] = Player(actor_nr=i, username=t,
                                  is_alive=(t not in dead))
    if teammates:
        r.teams["t1"] = Team(team_id="t1",
                             members=[me_name, *teammates])
    if feed_killed_me:
        r.recent_kills.append(KillInfo(instigator="foe", instigated=me_name,
                                       event_type="killed"))
    v = SessionView(session_id="s")
    v.me = Player(actor_nr=1, username=me_name)
    v.rooms["r1"] = r
    v.current_room_id = "r1"
    return r, v


def test_solo_alive_is_win():
    r, v = _room()
    assert _phase_end_result(r, v, "me") == "win"


def test_solo_dead_tracked_is_no_win():
    r, v = _room(dead=("me",))
    assert _phase_end_result(r, v, "me") is None


def test_duo_teammate_alive_carries_win():
    r, v = _room(teammates=("mate",), dead=("me",))
    assert _phase_end_result(r, v, "me") == "win"


def test_duo_both_dead_is_no_win():
    r, v = _room(teammates=("mate",), dead=("me", "mate"),
                 feed_killed_me=True)
    assert _phase_end_result(r, v, "me") is None


def test_emptied_room_no_death_evidence_is_win():
    # the 1d387896 shape: room drained of players, survived -> win, not silence
    r, v = _room(players=False)
    r.me = None
    v.me = Player(actor_nr=-1, username="me")
    assert _self_death_evidence(r, v, "me") is False
    assert _phase_end_result(r, v, "me") == "win"


def test_emptied_room_with_death_flag_is_no_win():
    r, v = _room(players=False, flag=True)
    assert _phase_end_result(r, v, "me") is None


def test_emptied_room_with_killfeed_death_is_no_win():
    r, v = _room(players=False, feed_killed_me=True)
    r.me = None
    assert _phase_end_result(r, v, "me") is None


def test_no_info_at_all_falls_back_to_win():
    # phase end seen while connected + zero death evidence = survived.
    # (None only when there is no room/me to judge at all.)
    r = RoomView(room_id="r1")
    r.state = State.MATCH
    v = SessionView(session_id="s")
    assert _phase_end_result(r, v, "me") == "win"
    assert _phase_end_result(r, v, None) is None
    assert _phase_end_result(None, v, "me") is None


def test_debug_names_deny_reason():
    r, v = _room(dead=("me",))
    assert _phase_end_result(r, v, "me") is None
    d = _phase_end_debug(r, v, "me")
    assert "death_ev=True" in d
    assert "alive_in_roster=False" in d


def test_debug_no_identity():
    r, v = _room()
    assert _phase_end_debug(r, v, None) == "no-identity(view.me null + game_username null)"
    assert _phase_end_debug(None, v, "me") == "no-room"


def test_debug_leaver_shape():
    r, v = _room(players=False)
    r.recent_kills.append(KillInfo(instigator="me", instigated="",
                                   event_type="left"))
    assert _phase_end_result(r, v, "me") is None
    d = _phase_end_debug(r, v, "me")
    assert "left_row=True" in d
