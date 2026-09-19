"""Null-identity healing + leet fallback (the "registered but ignored" class).

- view.me None + DB game_username set + roster carries that name -> _heal_view_me
  adopts it so kills/deaths/wins credit (Void Eight shape).
- "Vo1d Eight" (killfeed) vs registered "Void Eight" credits via the leet tier
  (1->i / 0->o) when no twin conflict exists; a genuine leet-level twin still
  blocks, and unrelated names still miss.
- Healing never overwrites an existing view.me and never fires when the DB
  identity itself is blank.
"""
from server.lib.photon_engine.models import Player, RoomView, SessionView
from server.scripts.processor import (
    _heal_view_me,
    _is_self,
    _names_equal_leet,
    _self_match_tier,
    _twin_ambiguous_leet,
)


def _view_with_roster(*names, me=None, gid_by_name=None):
    v = SessionView(session_id="s")
    r = RoomView(room_id="r1")
    for i, n in enumerate(names):
        gid = (gid_by_name or {}).get(n)
        r.players[100 + i] = Player(actor_nr=100 + i, username=n, game_id=gid)
    v.rooms["r1"] = r
    v.current_room_id = "r1"
    v.me = me
    return v, r


def test_heal_adopts_roster_row_by_db_name():
    v, r = _view_with_roster("Vo1d Eight", "Player 21545")
    user = {"game_username": "Vo1d Eight", "game_id": None}
    assert v.me is None
    assert _heal_view_me(v, user) is True
    assert v.me is not None and v.me.username == "Vo1d Eight"
    # kill now attributes to self
    assert _is_self(r, v, user, "Vo1d Eight") is True


def test_heal_via_game_id_when_name_drifted():
    v, r = _view_with_roster("Vo1d Eight", gid_by_name={"Vo1d Eight": "G9"})
    user = {"game_username": "someone else", "game_id": "G9"}
    # name path misses, gid path hits the roster row
    assert _heal_view_me(v, user) is True
    assert v.me is not None and v.me.username == "Vo1d Eight"


def test_heal_never_overwrites_existing_me():
    keep = Player(actor_nr=1, username="Real Me")
    v, _ = _view_with_roster("Vo1d Eight", me=keep)
    assert _heal_view_me(v, {"game_username": "Vo1d Eight"}) is False
    assert v.me is keep


def test_heal_noop_when_db_identity_blank():
    v, _ = _view_with_roster("Vo1d Eight")
    assert _heal_view_me(v, {"game_username": None, "game_id": None}) is False
    assert v.me is None


def test_leet_vo1d_matches_void_without_twins():
    assert _names_equal_leet("Vo1d Eight", "Void Eight") is True
    assert _names_equal_leet("Player 21545", "Player 21545") is True
    assert _names_equal_leet("bob", "ALICE") is False
    room_rv, _ = _view_with_roster("Alice")
    assert _twin_ambiguous_leet(room_rv, {"game_id": None}, "ALICE") is False
    assert _self_match_tier(room_rv, None, {"game_username": "Void Eight"},
                            "Vo1d Eight") == 5
    assert _is_self(room_rv, None, {"game_username": "Void Eight"},
                    "Vo1d Eight") is True


def test_leet_twin_conflict_still_blocks():
    room = RoomView(room_id="r1")
    room.players[1] = Player(actor_nr=1, username="Void Eight", game_id="G1")
    room.players[2] = Player(actor_nr=2, username="Vo1d Eight", game_id="G2")
    assert _twin_ambiguous_leet(room, {"game_id": None}, "Vo1d Eight") is True
    assert _is_self(room, None, {"game_username": "Void Eight"},
                    "Vo1d Eight") is False
    # stable game_id disambiguates the twins
    assert _is_self(room, None, {"game_username": "Void Eight", "game_id": "G1"},
                    "Void Eight") is True
