"""Duo team colors + per-team Discord roles (pure helpers, no Discord/DB).

Colors are keyed by team ORDER - the same intake order that froze them on the
registration row - and the role ids are configuration, so team 1..6 map to
TEAM_COLORS / COMP_TEAM_ROLE_1..6 in lockstep and both cycle together past
six teams.

Lives in companion/bot/teams.py on purpose: it must be testable without
importing bot.py, which registers the whole command tree and would blow
Discord's 100-command limit on a second import.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _load_teams(env=None, clear=False):
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    for i in range(1, 7):
        k = f"COMP_TEAM_ROLE_{i}"
        if clear:
            os.environ.pop(k, None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    spec = importlib.util.spec_from_file_location(
        "teams_under_test", Path(bot_dir) / "teams.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The six team roles as they exist on the Nectar server, in slot order. Colour
# and role must stay in lockstep: slot 3 is the GREEN role, not the orange one.
SERVER_ROLES = [
    "1553079575452450977",  # 1 blue
    "1553079824124223690",  # 2 red
    "1553079938599358605",  # 3 green
    "1553080070019481750",  # 4 yellow
    "1553080214240624750",  # 5 orange
    "1553080641502056458",  # 6 purple
]
SERVER_COLORS = ["\U0001f535", "\U0001f534", "\U0001f7e2", "\U0001f7e1",
                 "\U0001f7e0", "\U0001f7e3"]


def test_shipped_defaults_are_the_servers_actual_roles():
    t = _load_teams(clear=True)
    assert [t.team_role_id(i) for i in range(6)] == SERVER_ROLES
    assert [t.team_color(i) for i in range(6)] == SERVER_COLORS


def test_colour_order_stays_in_lockstep_with_the_roles():
    # blue, red, green, yellow, orange, purple - swapping 3 and 5 back would
    # hand a team the wrong role for the colour the board shows
    t = _load_teams(clear=True)
    assert t.TEAM_COLORS == SERVER_COLORS
    assert len(t.TEAM_COLORS) == 6


def test_env_overrides_a_default_slot():
    t = _load_teams({"COMP_TEAM_ROLE_3": "999"}, clear=True)
    assert t.team_role_id(2) == "999"
    assert t.team_role_id(0) == SERVER_ROLES[0]  # others still default


def test_explicitly_empty_env_disables_a_default_slot():
    # set-but-empty means "no role here", distinct from "not configured"
    t = _load_teams({f"COMP_TEAM_ROLE_{i}": "" for i in range(1, 7)},
                    clear=True)
    assert t.team_role_id(2) == ""
    assert t.configured_team_role_ids() == set()


ROLES = {f"COMP_TEAM_ROLE_{i}": str(1000 + i) for i in range(1, 7)}


def test_roles_map_to_the_same_slots_as_the_colors():
    t = _load_teams(ROLES)
    assert len(t.TEAM_COLORS) == 6
    for i in range(6):
        assert t.team_role_id(i) == str(1001 + i)
    # slot 0 is team 1
    assert t.team_color(0) == t.TEAM_COLORS[0]
    assert t.team_role_id(0) == "1001"


def test_teams_past_six_cycle_both_color_and_role():
    t = _load_teams(ROLES)
    assert t.team_color(6) == t.TEAM_COLORS[0]
    assert t.team_role_id(6) == "1001"
    assert t.team_color(7) == t.TEAM_COLORS[1]
    assert t.team_role_id(7) == "1002"


def test_unset_role_slots_grant_nothing_but_never_crash():
    t = _load_teams({k: "" for k in ROLES})
    assert t.team_role_id(0) == ""
    assert t.configured_team_role_ids() == set()
    assert t.team_color(3) == t.TEAM_COLORS[3]  # colours still work


def test_only_configured_roles_are_stripped():
    t = _load_teams({**ROLES, "COMP_TEAM_ROLE_4": "", "COMP_TEAM_ROLE_5": ""})
    assert t.configured_team_role_ids() == {"1001", "1002", "1003", "1006"}


def test_role_lookup_never_raises_on_junk():
    t = _load_teams(ROLES)
    assert t.team_role_id(-1) == str(1006)          # -1 % 6 == 5
    # an unparsable index grants nothing rather than raising into a command
    assert t.team_role_id("bad") == ""
    assert t.team_color("bad") == t.TEAM_COLORS[0]
    assert t.configured_team_role_ids() is not None
