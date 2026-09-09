"""Unit tests for presence computation (pure, no DB/cache)."""
import time

from server.lib.presence import presence_for


def _room(room_id="r1", state="match", gm="zw_lg", code=None, count=10, maxp=12, lp=None):
    return {"room_id": room_id, "state": state, "game_mode": gm,
            "short_code": code, "players_count": count, "max_players": maxp,
            "last_packet_at": lp if lp is not None else time.time()}


def _live(room, cached_at=None):
    return {"rooms": {"r1": room}, "current_room_id": "r1",
            "_cached_at": cached_at if cached_at is not None else time.time()}


def test_offline_no_session_or_stale():
    assert presence_for(None)["status"] == "offline"
    assert presence_for({})["status"] == "offline"
    old = _live(_room(lp=time.time() - 500), cached_at=time.time() - 500)
    assert presence_for(old)["status"] == "offline"


def test_lobby_hides_code_and_gm():
    p = presence_for(_live(_room(room_id="lobby", state="lobby", gm=None, code="PBS05")))
    assert (p["status"], p["detail"], p["gm"], p["code"]) == ("lobby", "lobby", None, None)
    p2 = presence_for(_live(_room(state="lobby", gm="zw_lg")))
    assert p2["detail"] == "lobby" and p2["code"] is None


def test_plain_match_no_code_leak():
    p = presence_for(_live(_room(code=None, count=5, maxp=12)))
    assert p["detail"] == "match" and p["code"] is None and p["gm"] == "zw_lg"


def test_tagged_tourney_and_scrim():
    tags = {"PBS05": {"kind": "tourney", "name": "Friday Cup"}}
    p = presence_for(_live(_room(code="PBS05", count=12)), tags)
    assert p["detail"] == "tourney" and p["tag"] == {"kind": "tourney", "name": "Friday Cup"}
    assert p["code"] == "PBS05"
    tags2 = {"ABCDE": {"kind": "scrim", "name": "Zone scrims"}}
    p2 = presence_for(_live(_room(code="ABCDE", count=10)), tags2)
    assert p2["detail"] == "scrim"


def test_scrim_heuristic_private_big_room():
    p = presence_for(_live(_room(code="ZZZ99", count=9, maxp=16, gm="bf")))
    assert p["detail"] == "scrim" and p["tag"] is None


def test_small_private_room_is_plain_match():
    p = presence_for(_live(_room(code="ZZZ99", count=3, maxp=4)))
    assert p["detail"] == "match"


def test_never_raises():
    assert presence_for({"rooms": None}, None)["status"] in ("offline", "lobby")
