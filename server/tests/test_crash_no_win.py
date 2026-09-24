"""A player who CRASHED must not be credited a win at the phase end.

The roster row of a crashed player keeps `is_alive=True` forever — they simply
stop sending packets, so nothing clears it. At stG=3 `_phase_end_result` saw
"alive" and wrote a win row for someone who was no longer in the game, which
in a duel hands a free round to the player who crashed.

Players now carry `last_seen_at`, stamped whenever a packet mentions them, and
"alive at the phase end" additionally requires that stamp to be fresh. Unknown
(None) is trusted — an old cache must never cost somebody a real win, and a
dead engine feed must not rewrite history either.
"""
import time

import pytest

from server.lib.photon_engine.enums import GameMode, State
from server.lib.photon_engine.models import Player, RoomView, SessionView
from server.scripts.processor import _phase_end_debug, _phase_end_result


def _room(*, alive=True, seen_ago=None, host_stale=False):
    now = time.time()
    r = RoomView(room_id="r1")
    r.game_mode = GameMode.ZW_LG
    r.state = State.MATCH
    r.stg_is_phase_end = True
    r.match_version = 1
    p = Player(actor_nr=1, username="cis6led", is_alive=alive)
    p.last_seen_at = None if seen_ago is None else now - seen_ago
    r.players[1] = p
    # the stayer: present and talking
    foe = Player(actor_nr=2, username="chris7", is_alive=True)
    foe.last_seen_at = now
    r.players[2] = foe
    r.last_packet_at = now - 3600 if host_stale else now
    v = SessionView(session_id="s")
    v.me = p
    v.rooms["r1"] = r
    v.current_room_id = "r1"
    return r, v


def test_a_live_recent_player_still_wins():
    r, v = _room(alive=True, seen_ago=1)
    assert _phase_end_result(r, v, "cis6led") == "win"


def test_a_crashed_player_is_not_credited_a_win():
    # alive=True in the roster, but no packet for 10 minutes: the crash.
    r, v = _room(alive=True, seen_ago=600)
    assert _phase_end_result(r, v, "cis6led") is None
    assert "stale_alive=True" in _phase_end_debug(r, v, "cis6led")


def test_unknown_last_seen_is_trusted():
    # Old caches have no stamp: never take a real win away.
    r, v = _room(alive=True, seen_ago=None)
    assert _phase_end_result(r, v, "cis6led") == "win"


def test_a_dead_player_is_still_no_win():
    r, v = _room(alive=False, seen_ago=1)
    assert _phase_end_result(r, v, "cis6led") is None


def test_stale_alive_does_not_apply_when_the_host_feed_is_dead():
    # Nobody is talking at all (restart / closed laptop): no rewrite.
    r, v = _room(alive=True, seen_ago=600, host_stale=True)
    assert _phase_end_result(r, v, "cis6led") == "win"


def test_stale_window_is_configurable(monkeypatch):
    from server.scripts import processor as proc
    monkeypatch.setenv("PHASE_STALE_ALIVE_SEC", "30")
    import importlib
    reloaded = importlib.reload(proc)
    try:
        r, v = _room(alive=True, seen_ago=60)
        assert reloaded._phase_end_result(r, v, "cis6led") is None
        r2, v2 = _room(alive=True, seen_ago=5)
        assert reloaded._phase_end_result(r2, v2, "cis6led") == "win"
    finally:
        monkeypatch.delenv("PHASE_STALE_ALIVE_SEC", raising=False)
        importlib.reload(proc)


def test_player_last_seen_round_trips_through_the_cache():
    r = RoomView(room_id="r1")
    p = Player(actor_nr=1, username="cis6led")
    p.last_seen_at = 1234.5
    r.players[1] = p
    back = RoomView.from_dict(r.to_dict())
    assert back.players[1].last_seen_at == 1234.5
    # An old cache without the key must not explode.
    old = Player.from_dict({"actor_nr": 1, "username": "x"})
    assert old.last_seen_at is None
