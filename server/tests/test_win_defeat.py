"""Win integrity (wins-only): solid twins guard, open casing fallback, win idempotency.

- exact raw matches credit even with twins (unambiguous);
- strict/fold normalized matches with a genuine twin conflict and no gid
  stay blocked (solid) — twins resolve via dashboard /link;
- casing drift with NO twin conflict credits (open) so players never sit at
  0 kills for hours while playtime counts;
- wins record once per version (idempotent across batches/workers).
"""
from server.lib.photon_engine.models import Player, RoomView
from server.scripts import processor as proc
from server.scripts.processor import _is_self, _self_match_tier, _twin_ambiguous


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
    # genuine twin conflict, gid unknown -> solid block (all tiers guarded)
    assert _is_self(room, None, {"game_username": "bob"}, "dc: Bob") is False
    assert _twin_ambiguous(room, {"game_id": None}, "Bob") is True


def test_casing_drift_without_twins_credits():
    room = _room(Player(actor_nr=1, username="Alice", game_id="G1"))
    # no twin conflict -> fold fallback credits ("ALICE" vs "alice")
    assert _is_self(room, None, {"game_username": "alice"}, "ALICE") is True


def test_match_tier_ordering():
    room = _room(Player(actor_nr=1, username="Alice", game_id="G1"))
    assert _self_match_tier(room, None, {"game_username": "Alice"}, "Alice") == 1
    assert _self_match_tier(room, None, {"game_username": "alice"}, "ALICE") == 3
    assert _self_match_tier(room, None, {"game_username": "bob"}, "ALICE") == 0
    twins = _room(Player(actor_nr=1, username="Bob", game_id="G1"),
                  Player(actor_nr=2, username="dc: Bob", game_id="G2"))
    assert _self_match_tier(twins, None, {"game_username": "bob"}, "dc: Bob") == 0


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


def test_win_writes_once_per_version(monkeypatch):
    monkeypatch.setattr(proc, "_result_event_exists", lambda sid, et, rid, mv, buf=None: False)
    monkeypatch.setattr(proc, "_precision", type("P", (), {"schedule": staticmethod(lambda *a, **k: None)})())
    room = RoomView(room_id="r1")
    room.match_version = 2
    user = {"wins": 1}
    buf, dirty = [], set()
    out = proc._write_win("did1", "sess1", user, "zw_lg", "r1", None, 2, "did1", room, buf=buf, dirty=dirty)
    assert out == 2
    assert user["wins"] == 2
    assert room.last_win_version == 2
    assert any(r.get("event_type") == "win" for r in buf)


def test_win_skips_when_already_recorded(monkeypatch):
    # DB says a win exists for this (session, room, version): no double write.
    monkeypatch.setattr(proc, "_result_event_exists",
                        lambda sid, et, rid, mv, buf=None: et == "win")
    room = RoomView(room_id="r1")
    room.last_win_version = -1  # marker lost (restore)
    user = {"wins": 3}
    out = proc._write_win("did1", "sess1", user, "zw_lg", "r1", None, 2, "did1", room, buf=[], dirty=set())
    assert out == 3
    assert user["wins"] == 3
    assert room.last_win_version == 2  # healed so it isn't retried
