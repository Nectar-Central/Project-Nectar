"""Duo team colors (pure helpers, no Discord/DB).

Colours are keyed by team ORDER - the same signup order that froze them on the
registration row - and there are six slots, so the colour cycles every six
teams.

There are no Discord team roles any more: a role per slot had to agree with a
colour the roster can reassign at any time, and the two kept disagreeing.

Lives in companion/bot/teams.py on purpose: it must be testable without
importing bot.py, which registers the whole command tree and would blow
Discord's 100-command limit on a second import.
"""
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_teams():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "teams_under_test", Path(bot_dir) / "teams.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# blue, red, green, yellow, orange, purple - the order the server's team
# channels are named in. Reordering this silently repaints every team.
SERVER_COLORS = ["\U0001f535", "\U0001f534", "\U0001f7e2", "\U0001f7e1",
                 "\U0001f7e0", "\U0001f7e3"]


def test_the_palette_is_the_six_team_colours():
    t = _load_teams()
    assert t.TEAM_COLORS == SERVER_COLORS
    assert len(t.TEAM_COLORS) == 6
    assert t.TEAM_SLOTS == 6


def test_slot_zero_is_team_one():
    t = _load_teams()
    assert t.team_color(0) == t.TEAM_COLORS[0]


def test_colours_cycle_every_six_teams():
    t = _load_teams()
    for i in range(6):
        assert t.team_color(i) == t.TEAM_COLORS[i]
    assert t.team_color(6) == t.TEAM_COLORS[0]
    assert t.team_color(7) == t.TEAM_COLORS[1]
    assert t.team_color(12) == t.TEAM_COLORS[0]


def test_colour_lookup_never_raises_on_junk():
    t = _load_teams()
    assert t.team_color("bad") == t.TEAM_COLORS[0]
    assert t.team_color(None) == t.TEAM_COLORS[0]
    assert t.team_color(-1) == t.TEAM_COLORS[5]      # -1 % 6 == 5


def test_no_role_machinery_is_left_behind():
    # the roles were removed on purpose; a lingering accessor is a landmine
    t = _load_teams()
    for gone in ("team_role_id", "configured_team_role_ids",
                 "team_role_strip_targets", "TEAM_ROLE_IDS",
                 "DEFAULT_TEAM_ROLE_IDS"):
        assert not hasattr(t, gone), f"{gone} should be gone"


def test_slots_are_positions_among_live_teams():
    t = _load_teams()
    rows = [{"id": 1, "status": "accepted"},
            {"id": 2, "status": "banned"},
            {"id": 3, "status": "accepted"}]
    got = t.team_slots(rows)
    assert [(r["id"], slot) for r, slot in got] == [(1, 0), (3, 1)]
    assert t.team_color(got[1][1]) == t.TEAM_COLORS[1]


def test_no_team_gets_another_teams_colour():
    t = _load_teams()
    assert len({t.team_color(i) for i in range(6)}) == 6
