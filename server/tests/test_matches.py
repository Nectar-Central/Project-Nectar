"""Unit tests for match grouping behind GET /api/matches."""
from server.routes.discovery import _group_match_events, _match_key


def _ev(etype, room="abc123", ver=1, gm="zw_lg", ts="2026-09-04T10:00:00"):
    return {"session_id": "sess1", "event_type": etype,
            "event_data": {"room_id": room, "match_version": ver, "gm": gm},
            "created_at": ts}


def test_groups_by_room_and_version():
    evs = [_ev("kill", ver=1), _ev("death", ver=1), _ev("kill", ver=2),
           _ev("win", ver=2, ts="2026-09-04T11:00:00")]
    out = _group_match_events(evs)
    assert len(out) == 2
    assert out[0]["match_version"] == 2  # newest first
    assert out[0]["kills"] == 1 and out[0]["win"] is True
    assert out[1]["deaths"] == 1 and out[1]["win"] is False


def test_lobby_noise_skipped():
    evs = [{"session_id": "s", "event_type": "join",
            "event_data": {"room_id": "lobby"}, "created_at": "2026-09-04T10:00:00"}]
    assert _group_match_events(evs) == []


def test_match_key_falls_back_to_session():
    assert _match_key({}, "sess9") == "sess9"
    assert _match_key({"room_id": "r", "match_version": 3}, "") == "r_3"
