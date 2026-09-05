"""Unit tests for the stG new-life rule (double-defeat regression).

Bug: resetting per-life flags at the stG=3 version bump wiped the just-ended
match's guards before its drain ran, so the sweep re-marked and the drain
re-wrote its death+defeat (double defeat in zw_lg after a killfeed elim).
Flags must reset only on a fresh stG=1 (match start), edge-triggered.
"""
import pytest

from server.lib.photon_engine.engine import _stg_starts_new_life, _stg_bumps_version
from server.lib.photon_engine.models import RoomView


@pytest.mark.parametrize("stg,last,expected", [
    (1, None, True),    # first match start
    (1, 0, True),
    (1, 2, True),
    (1, 3, True),       # normal 3 -> 1 new match
    (1, 1, False),      # repeat must not wipe a life mid-flight
    (2, 1, False),
    (3, 2, False),      # match end: never reset here
    (3, None, False),
    (0, None, False),
    ("1", 3, True),
    ("bad", None, False),
    (None, None, False),
])
def test_new_life_matrix(stg, last, expected):
    assert _stg_starts_new_life(stg, last) is expected


@pytest.mark.parametrize("stg,last,armed,expected", [
    (3, 2, True, True),      # normal match end
    (3, 1, True, True),
    (3, None, True, True),   # first bump without a prior stG
    (3, 2, False, False),    # 3->2->3 flap: arm consumed, no double bump
    (3, None, False, False),
    (3, 3, True, False),     # timer ticking through 3
    (3, 3, False, False),
    (2, 1, True, False),     # not a 3
    (1, 3, True, False),
    (3, 30, True, False),    # zone_time-ish value, not a phase enter
    ("3", "2", True, True),
    ("bad", 2, True, False),
    (None, 2, True, False),
])
def test_bump_matrix(stg, last, armed, expected):
    assert _stg_bumps_version(stg, last, armed) is expected


def test_room_armed_by_default():
    # new rooms can bump once (join-mid-match seals still work); the bump
    # consumes the arm and stG=1 re-arms it.
    assert RoomView(room_id="r1").stg3_armed is True
