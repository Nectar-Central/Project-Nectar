"""Unit tests for the play-points economy (ranked.hourly_play_points)."""
from server.lib.ranked import (
    PLAY_HOUR,
    PLAY_KILL,
    PLAY_KNOCK,
    PLAY_WIN,
    hourly_play_points,
    play_points_for,
)
from server.routes.insights import _match_room_key


def test_economy_values():
    assert PLAY_KILL == 8
    assert PLAY_KNOCK == 2
    assert PLAY_WIN == 50
    assert PLAY_HOUR == 20


def test_hourly_no_full_hour_yet():
    assert hourly_play_points(3599, 0) == (0, 0)
    assert hourly_play_points(0, 0) == (0, 0)


def test_hourly_first_hour():
    owed, paid = hourly_play_points(3600, 0)
    assert (owed, paid) == (PLAY_HOUR, 1)


def test_hourly_multiple_hours_catch_up():
    owed, paid = hourly_play_points(3 * 3600 + 100, 1)
    assert (owed, paid) == (2 * PLAY_HOUR, 3)


def test_hourly_never_rewinds_or_double_pays():
    assert hourly_play_points(7200, 2) == (0, 2)
    assert hourly_play_points(100, 5) == (0, 5)  # clock skew safe


def test_hourly_bad_input_fail_open():
    assert hourly_play_points(None, None) == (0, 0)


def test_play_points_for():
    assert play_points_for(10, 2, 3, 3700) == 10 * 8 + 2 * 50 + 3 * 2 + 1 * 20
    assert play_points_for(0, 0, 0, 0) == 0
    assert play_points_for(0, 0, 0, 3599) == 0
    assert play_points_for(None, None, None, None) == 0


def test_match_room_key():
    assert _match_room_key({"room_id": "abc", "match_version": 2}) == ("abc", 2)
    assert _match_room_key({"code": "PBS05", "match_version": 1}) == ("PBS05", 1)
    assert _match_room_key({}) is None
    assert _match_room_key(None) is None
