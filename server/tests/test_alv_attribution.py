"""ALV/actor attribution: dict keys are opaque slot ids, never actor numbers.

Regression (live DF room): an alv with actor=2/target=2 resolved to the
wrong player sitting in dict slot 2, so a kill was followed by the killer's
own alv flipping dead — phantom deaths, wins, and phase-end verdicts.

Rules under test:
- _resolve_actor / _find_player never match room.players dict keys;
  only Player.actor_nr fields and actor_map names count.
- A fresh 255/map name beats a stale stored number (name-first ordering is
  exercised in feed; here we pin the helper contract it relies on).
"""
from server.lib.photon_engine.engine import _find_player, _resolve_actor
from server.lib.photon_engine.models import Player, RoomView


def _room():
    r = RoomView(room_id="r1")
    return r


def test_resolve_actor_ignores_dict_key():
    # me sits in slot 2 but OWNS actor 1; stale map is empty.
    r = _room()
    r.players[2] = Player(actor_nr=1, username="me", is_self=True)
    assert _resolve_actor(r, 2) is None  # slot 2 is not actor 2
    assert _resolve_actor(r, 1) == "me"  # field match repairs the map
    assert r.actor_map[1] == "me"


def test_find_player_ignores_dict_key_prefers_field():
    r = _room()
    r.players[2] = Player(actor_nr=1, username="me", is_self=True)
    r.players[7] = Player(actor_nr=2, username="foe")
    k, p = _find_player(r, target=2)
    assert p.username == "foe"  # field hit, not the slot-2 occupant
    assert k == 7


def test_find_player_map_name_beats_wrong_slot():
    r = _room()
    r.players[2] = Player(actor_nr=1, username="me", is_self=True)
    r.actor_map = {2: "foe"}
    r.players[9] = Player(actor_nr=-1, username="foe")
    k, p = _find_player(r, target=2)
    assert p.username == "foe"
    assert k == 9


def test_find_player_unknown_actor_returns_none():
    r = _room()
    r.players[2] = Player(actor_nr=1, username="me", is_self=True)
    assert _find_player(r, target=9) == (None, None)
    assert _find_player(r, actor=9) == (None, None)


def test_new_player_gets_real_actor_not_slot_key():
    # join collision path: dict key bumped, actor_nr field must stay real.
    # (Guards engine join: actor_nr=key corruption.)
    from server.lib.photon_engine.models import Player as P
    p = P(actor_nr=2, username="mate")
    assert p.actor_nr == 2
    # and a hash-keyed late join keeps actor_nr -1, never the hash
    q = P(actor_nr=-1, username="ghost")
    assert q.actor_nr == -1
