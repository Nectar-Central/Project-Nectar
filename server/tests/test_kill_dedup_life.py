"""A legitimate re-kill must not be eaten by the retransmit guard.

BuildNow re-sends a killfeed broadcast a few hundred ms later, so the engine
and processor suppress a same killer->victim pair inside a 5s window. In Free
Mode (df) `stG` never advances, so `match_version` never changes and a DF
respawn cycle is ~2-4s: the SECOND kill of the same victim by the same killer
landed inside that window and was dropped as a "retransmit". The player's kill
counter moved but no `kill` event row was written, so duel wagers never saw it
(the second kill silently did not count).

Both guards now consult the victim's last revive: a kill after the victim was
seen alive again is a new life, not a retransmit.
"""
import time

from server.lib.photon_engine.engine import _kill_content_dup
from server.lib.photon_engine.models import KillInfo, RoomView
from server.scripts import processor as proc


def _room():
    r = RoomView(room_id="r1")
    r.match_version = 0  # df: stG pinned at 1, so the version never moves
    r.proc_kill_seen = {}
    return r


def _norm(n):
    return str(n or "").strip().casefold()


# --- engine guard ----------------------------------------------------------

def test_engine_still_suppresses_a_real_retransmit():
    r = _room()
    now = time.time() * 1000.0
    r.recent_kills = [KillInfo(instigator="cis6led", instigated="chris7",
                               event_type="killed", t=now)]
    # Same kill 0.4s later, victim never respawned: a retransmit.
    assert _kill_content_dup(r.recent_kills, "cis6led", "chris7", "killed",
                             now + 400) is True


def test_engine_allows_a_rekill_after_the_victim_respawned():
    r = _room()
    now = time.time() * 1000.0
    r.recent_kills = [KillInfo(instigator="cis6led", instigated="chris7",
                               event_type="killed", t=now)]
    # chris7 respawned 1s later, then was killed again 2s after that.
    r.proc_alive_at = {_norm("chris7"): now + 1000}
    assert _kill_content_dup(r.recent_kills, "cis6led", "chris7", "killed",
                             now + 3000, r.proc_alive_at) is False


def test_engine_still_suppresses_a_retransmit_after_a_revive():
    r = _room()
    now = time.time() * 1000.0
    r.recent_kills = [KillInfo(instigator="cis6led", instigated="chris7",
                               event_type="killed", t=now + 3000)]
    # The respawn happened BEFORE the kill being echoed, so this is still the
    # same kill arriving twice.
    r.proc_alive_at = {_norm("chris7"): now + 1000}
    assert _kill_content_dup(r.recent_kills, "cis6led", "chris7", "killed",
                             now + 3400, r.proc_alive_at) is True


# --- processor guard -------------------------------------------------------

def test_processor_still_suppresses_a_real_retransmit():
    r = _room()
    now = time.time() * 1000.0
    proc._kill_count_stamp(r, "cis6led", "chris7", "killed", now)
    assert proc._kill_count_dup(r, "cis6led", "chris7", "killed", now + 400) is True


def test_processor_allows_a_rekill_after_the_victim_respawned():
    r = _room()
    now = time.time() * 1000.0
    proc._kill_count_stamp(r, "cis6led", "chris7", "killed", now)
    r.proc_alive_at = {_norm("chris7"): now + 1000}
    assert proc._kill_count_dup(r, "cis6led", "chris7", "killed", now + 3000) is False


def test_processor_guards_stay_scoped_to_the_pair():
    r = _room()
    now = time.time() * 1000.0
    proc._kill_count_stamp(r, "cis6led", "chris7", "killed", now)
    r.proc_alive_at = {_norm("chris7"): now + 1000}
    # A different victim, or a different killer, is never a dup.
    assert proc._kill_count_dup(r, "cis6led", "someone-else", "killed", now + 300) is False
    assert proc._kill_count_dup(r, "other", "chris7", "killed", now + 300) is False


def test_guards_never_raise_on_missing_state():
    r = RoomView(room_id="r1")
    assert proc._kill_count_dup(r, "a", "b", "killed", None) is False
    assert _kill_content_dup([], None, None, "killed", 0.0) is False
    r.proc_kill_seen = "not-a-dict"
    assert proc._kill_count_dup(r, "a", "b", "killed", 1.0) is False
