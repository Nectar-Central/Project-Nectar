"""Win/defeat integrity: twins fail closed, sealed wins suppress defeats.

Covers the intermittent "win counted as defeat" class in solo zw_lg:
- two roster entries sharing a normalized name must not cross-credit
  unless the raw names match exactly or the game_id disambiguates;
- a defeat drain arriving after the win for the same version (cross-batch
  or cross-worker, markers lost) must not write: win stands, always.
"""
from server.lib.photon_engine.models import Player, RoomView
from server.scripts import processor as proc
from server.scripts.processor import _is_self, _twin_ambiguous, _write_defeat


def _room(*players):
    r = RoomView(room_id="r1")
    for i, p in enumerate(players):
        r.players[100 + i] = p
    return r


def test_exact_raw_match_credits_despite_twin():
    room = _room(Player(actor_nr=1, username="Bob", game_id="G1"),
                 Player(actor_nr=2, username="dc: Bob", game_id="G2"))
    assert _is_self(room, None, {"game_username": "Bob", "game_id": "G1"}, "Bob") is True


def test_normalized_only_match_with_twins_skips():
    room = _room(Player(actor_nr=1, username="Bob", game_id="G1"),
                 Player(actor_nr=2, username="dc: Bob", game_id="G2"))
    # same normalized name, gid unknown -> ambiguous, fail closed
    assert _is_self(room, None, {"game_username": "bob"}, "dc: Bob") is False
    assert _twin_ambiguous(room, {"game_id": None}, "Bob") is True


def test_gid_disambiguates_twins():
    room = _room(Player(actor_nr=1, username="Bob", game_id="G1"),
                 Player(actor_nr=2, username="dc: Bob", game_id="G2"))
    assert _twin_ambiguous(room, {"game_id": "G2"}, "Bob") is False
    assert _is_self(room, None, {"game_username": "robert", "game_id": "G2"}, "dc: Bob") is True


def test_single_name_still_credits():
    room = _room(Player(actor_nr=1, username="Alice", game_id="G1"))
    assert _twin_ambiguous(room, {"game_id": None}, "Alice") is False
    assert _is_self(room, None, {"game_username": "Alice"}, "Alice") is True
    # normalized-only (dc: prefix drift), no twin in room -> still credits
    assert _is_self(room, None, {"game_username": "dc: Alice"}, "Alice") is True


def test_defeat_skipped_when_win_sealed_for_version(monkeypatch):
    # DB says a win exists for this (session, room, version): the defeat
    # drain must stand down even with no in-memory marker (serverless).
    monkeypatch.setattr(proc, "_result_event_exists",
                        lambda sid, et, rid, mv, buf=None: et == "win")
    room = RoomView(room_id="r1")
    room.last_win_version = -1  # marker lost (restore)
    user = {"defeats": 3}
    out = _write_defeat("did1", "sess1", user, "zw_lg", "r1", None, 2, room, buf=[], dirty=set())
    assert out == 3
    assert user["defeats"] == 3
    assert room.last_win_version == 2  # healed so it isn't retried


def test_defeat_writes_when_no_win(monkeypatch):
    monkeypatch.setattr(proc, "_result_event_exists", lambda sid, et, rid, mv, buf=None: False)
    room = RoomView(room_id="r1")
    user = {"defeats": 3}
    buf, dirty = [], set()
    out = _write_defeat("did1", "sess1", user, "zw_lg", "r1", None, 2, room, buf=buf, dirty=dirty)
    assert out == 4
    assert user["defeats"] == 4
    assert any(r.get("event_type") == "defeat" for r in buf)
    assert room.had_self_defeat is True
