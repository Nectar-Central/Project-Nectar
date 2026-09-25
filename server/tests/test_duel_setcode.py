"""/wager set-code - force the resolver onto the room you are actually in.

A duelist can already type the code in the duel thread, but that only sets
`room_code`: if the wager is still bound to an abandoned room, every row of
the real room is filtered as `room != bound` and the duel tracks nothing
while looking alive. The escape hatch therefore has to move the BINDING too,
which is the whole point of the command.

Pure helpers, no Discord, no DB.
"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_setcode_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


OLD = "e26945f4-0000"
NEW = "b6864d75-0000"


def _rooms():
    return [{"room_id": OLD, "code": "OLDCD", "gm": "zw_lg",
             "last_ts": _iso(minutes=20)},
            {"room_id": NEW, "code": "REAL1", "gm": "zw_lg",
             "last_ts": _iso(seconds=3)}]


def test_setting_the_code_pulls_the_binding_to_that_room():
    duels = _load_duels()
    # the abandoned room is what the resolver is currently pinned to
    patch = duels.set_code_patch(_rooms(), "REAL1", bound_room=OLD)
    assert patch == {"room_code": "REAL1", "room_id": NEW}


def test_setting_an_unknown_code_clears_the_binding():
    # no room is running under it yet: drop the stale binding and let the
    # resolver bind when that room produces rows
    duels = _load_duels()
    assert duels.set_code_patch(_rooms(), "LATER", bound_room=OLD) == {
        "room_code": "LATER", "room_id": None}


def test_codes_are_case_and_space_insensitive():
    duels = _load_duels()
    assert duels.set_code_patch(_rooms(), "  real1 ")["room_id"] == NEW


def test_the_freshest_room_wins_when_two_carry_the_code():
    duels = _load_duels()
    rooms = [{"room_id": "a", "code": "SAME", "last_ts": _iso(minutes=9)},
             {"room_id": "b", "code": "SAME", "last_ts": _iso(seconds=2)}]
    assert duels.set_code_patch(rooms, "SAME")["room_id"] == "b"


def test_clearing_the_code_releases_everything():
    duels = _load_duels()
    assert duels.set_code_patch(_rooms(), "", bound_room=NEW) == {
        "room_code": None, "room_id": None}


def test_set_code_never_raises_on_junk():
    duels = _load_duels()
    assert duels.set_code_patch(None, None) == {"room_code": None, "room_id": None}
    assert duels.set_code_patch([{"room_id": None}], "X")["room_id"] is None
    assert duels.set_code_patch([{"code": "X"}], "X")["room_id"] is None


def test_validate_room_code_still_gates_the_command():
    duels = _load_duels()
    assert duels.validate_room_code("REAL1")[0] is True
    assert duels.validate_room_code("")[0] is True
    assert duels.validate_room_code("TOOLONG")[0] is False
