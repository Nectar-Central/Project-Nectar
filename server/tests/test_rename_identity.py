"""A player who renames mid-session must not vanish from tracking.

`view.me.username` is what every self-attribution compares against (the
processor's `_self_match_tier`). It is set once on the first join of a
session and never refreshed, because the first-join block is gated behind
`usernameChangeCount`. A player who renames after that keeps the old name
forever:

  - every killfeed names the NEW name, every comparison uses the OLD one, so
    `_self_match_tier` returns 0 and the processor writes no `kill`, `win` or
    `death` row for them again;
  - the JOIN handler does not recognise the new name as a self-join, so it
    adds a SECOND roster row for the same human (is_self=False) while
    `view.me` still carries the stale name.

Both are silent. On a duel wager it reads as "the tracker ignored me while
my opponent's kills counted", because the opponent's healthy side supplies
presence and the winner row for the group.

The name in an outbound CreateRoom/JoinRoom is this client's own
playerProps - the same authority the outbound killfeed echo is already
trusted with - so a name we do not recognise on our own join is a rename,
not another player.
"""
from server.lib.photon_engine import engine as engine_mod
from server.lib.photon_engine.enums import State


def _create(name, room="room-uuid-1", op="CreateRoom", pfid=None):
    props = {"255": name}
    if pfid:
        props["pfid"] = pfid
    return {
        "kind": "OperationRequest",
        "op": 227 if op == "CreateRoom" else 225,
        "opName": op,
        "gm": "zw_lg",
        "params": {"GameProperties": {"gm": "zw_lg", "pID": "19S9Z"},
                   "RoomName": room},
        "playerProps": props,
        "properties": {},
    }


def _engine(monkeypatch, mapping):
    e = engine_mod.Engine()
    monkeypatch.setattr(engine_mod, "parse_gpbinary", lambda h: mapping[h])
    return e


def test_a_rename_between_joins_updates_the_session_identity(monkeypatch):
    e = _engine(monkeypatch, {"J1": _create("cis6led", "room-a"),
                              "J2": _create("dom", "room-b")})
    e.feed("s_ren", "J1", "out")
    v = e.get_session("s_ren")
    assert v.me is not None and v.me.username == "cis6led"

    e.feed("s_ren", "J2", "out")
    # the name the game now uses is the name the session knows us by
    assert v.me.username == "dom"


def test_a_rename_leaves_exactly_one_roster_row_for_us(monkeypatch):
    # two rows for the same human make every twin guard ambiguous, which is
    # the other half of "nobody could credit my kills"
    e = _engine(monkeypatch, {"J1": _create("cis6led", "room-a"),
                              "J2": _create("dom", "room-a")})
    e.feed("s_ren", "J1", "out")
    e.feed("s_ren", "J2", "out")
    v = e.get_session("s_ren")
    room = v.rooms["room-a"]
    rows = [p for p in room.players.values() if p.username == "dom"]
    assert len(rows) == 1
    assert rows[0] is v.me
    assert room.me is v.me


def test_the_new_room_knows_us_by_the_new_name(monkeypatch):
    e = _engine(monkeypatch, {"J1": _create("cis6led", "room-a"),
                              "J2": _create("dom", "room-b")})
    e.feed("s_ren", "J1", "out")
    e.feed("s_ren", "J2", "out")
    v = e.get_session("s_ren")
    room = v.rooms["room-b"]
    assert v.me in list(room.players.values())
    assert any(p.username == "cis6led" for p in room.players.values()) is False


def test_an_inbound_join_from_someone_else_is_still_another_player(monkeypatch):
    # the rename rule is scoped to OUR OWN outbound join: an inbound join
    # carrying an unknown name must not hijack the session identity
    e = _engine(monkeypatch, {"J1": _create("cis6led", "room-a"),
                              "J2": _create("dom", "room-a", op="JoinRoom")})
    e.feed("s_ren", "J1", "out")
    e.feed("s_ren", "J2", "in")
    v = e.get_session("s_ren")
    assert v.me.username == "cis6led"


def test_the_same_name_is_not_a_rename(monkeypatch):
    e = _engine(monkeypatch, {"J1": _create("cis6led", "room-a"),
                              "J2": _create("CIS6LED", "room-b")})
    e.feed("s_ren", "J1", "out")
    e.feed("s_ren", "J2", "out")
    assert e.get_session("s_ren").me.username == "cis6led"


def test_a_rename_carries_the_current_game_id_over(monkeypatch):
    # pfid is the stable id: the latest one must win, or tier 4 keeps
    # comparing against a stale account
    e = _engine(monkeypatch, {"J1": _create("cis6led", "room-a", pfid="G1"),
                              "J2": _create("dom", "room-b", pfid="G2")})
    e.feed("s_ren", "J1", "out")
    e.feed("s_ren", "J2", "out")
    assert e.get_session("s_ren").me.game_id == "G2"


def test_rename_never_raises_on_a_blank_view(monkeypatch):
    e = engine_mod.Engine()
    assert e._adopt_own_rename(None, None, "dom", 1, "s") is False
    assert e._adopt_own_rename(object(), None, "", 1, "s") is False
