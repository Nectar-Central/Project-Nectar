"""Team colour and team role must be derived from ONE slot, not two.

A team that shows a blue reaction while holding the red team role is not a
cosmetic bug: the board, the signup reaction and the member list all disagree
about who is who. It happened because the two were derived from different
things - the frozen colour from a COUNT taken at registration, the role from
the row's POSITION in an unordered SELECT. Postgres guarantees no order
without ORDER BY, so the same team could land in a different slot on every
sync, and the colour would disagree with the role.

So: rows are read in a stable order (registration id, which is a monotonic
identity), and colour + role + slot all come from one pass over that order.

The colour is still frozen at intake - it is the reaction the players see -
but roles are no longer granted there. They land at /tourney-start, when the
roster is final, via the same pass.
"""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BOT_DIR = str(Path("companion/bot").resolve())
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)


def _teams():
    spec = importlib.util.spec_from_file_location(
        "teams_slot_under_test", Path(BOT_DIR) / "teams.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- one pass, one slot ----------------------------------------------------

def test_slots_are_positions_in_the_given_order():
    t = _teams()
    rows = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert [slot for _r, slot in t.team_slots(rows)] == [0, 1, 2]


def test_a_banned_team_does_not_consume_a_slot():
    t = _teams()
    rows = [{"id": 1, "status": "accepted"},
            {"id": 2, "status": "banned"},
            {"id": 3, "status": "accepted"}]
    got = t.team_slots(rows)
    assert [(r["id"], slot) for r, slot in got] == [(1, 0), (3, 1)]


def test_pending_and_rejected_still_hold_their_slot():
    # only 'banned' frees a slot, so a pending duo keeps its colour
    t = _teams()
    rows = [{"id": 1, "status": "accepted"},
            {"id": 2, "status": "pending"},
            {"id": 3, "status": "rejected"}]
    assert [slot for _r, slot in t.team_slots(rows)] == [0, 1, 2]


def test_slots_survive_junk():
    t = _teams()
    assert t.team_slots(None) == []
    assert t.team_slots([None, {}]) != []
    assert [slot for _r, slot in t.team_slots([{"status": "banned"}])] == []


def test_colour_and_role_come_from_the_same_slot():
    # the invariant the swap broke: one slot -> one colour AND one role
    t = _teams()
    for slot in range(6):
        rows = [{"id": i} for i in range(slot + 1)]
        _row, got = t.team_slots(rows)[-1]
        assert got == slot
        assert t.team_color(got) == t.TEAM_COLORS[got]
        assert t.team_role_id(got) == t.TEAM_ROLE_IDS[got]


def test_no_team_gets_another_teams_colour():
    t = _teams()
    seen = {}
    for slot in range(6):
        seen[t.team_color(slot)] = t.team_role_id(slot)
    # each colour maps to exactly one role, and vice versa
    assert len(seen) == 6
    assert len(set(seen.values())) == 6


def test_the_colour_frozen_at_intake_matches_the_role_granted_at_start():
    # Intake freezes a colour from a COUNT of the live teams that exist at that
    # moment; /tourney-start derives the slot from the row's position in
    # registration order. For the team that just signed up those two must be
    # the same slot - it is the reaction the players see vs the role they are
    # given, and they were swapped when they were not.
    t = _teams()
    for existing in range(6):
        rows = [{"id": i + 1, "status": "accepted"} for i in range(existing)]
        frozen = t.team_color(len(rows))          # what intake writes
        rows.append({"id": existing + 1, "status": "accepted"})   # the new team
        _row, slot = t.team_slots(rows)[-1]
        assert slot == existing
        assert t.team_color(slot) == frozen
        assert t.team_role_id(slot) == t.TEAM_ROLE_IDS[slot]


def test_a_banned_team_does_not_shift_the_next_teams_colour():
    # the slot is the position among LIVE teams, so a banned duo leaves a gap
    # that the teams after it move up into - on both sides of the agreement
    t = _teams()
    rows = [{"id": 1, "status": "accepted"},
            {"id": 2, "status": "banned"},
            {"id": 3, "status": "accepted"}]
    live = t.team_slots(rows)
    assert t.team_color(live[1][1]) == t.team_color(1)   # id 3 -> slot 1


# --- the read order has to be stable ---------------------------------------

class _Q:
    def __init__(self):
        self.calls = []

    def select(self, *a):
        self.calls.append(("select",) + a)
        return self

    def eq(self, c, v):
        self.calls.append(("eq", c, v))
        return self

    def neq(self, c, v):
        self.calls.append(("neq", c, v))
        return self

    def order(self, c, desc=False):
        self.calls.append(("order", c, desc))
        return self

    def limit(self, n):
        self.calls.append(("limit", n))
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class _SB:
    def __init__(self):
        self.q = _Q()
        self.name = None

    def table(self, name):
        self.name = name
        return self.q


@pytest.fixture()
def bot_mod():
    import bot as bm
    return bm


def test_registration_rows_are_read_in_a_stable_order(monkeypatch, bot_mod):
    # WITHOUT this, the slot is the row's position in an arbitrary result and
    # the same team can be handed another team's colour and role.
    sb = _SB()

    async def _sb_call(fn):
        return fn()

    monkeypatch.setattr(bot_mod, "_comp_sb", lambda: sb)
    monkeypatch.setattr(bot_mod.db, "_sb_call", _sb_call)
    assert asyncio.run(bot_mod._comp_reg_rows(7)) == []
    orders = [c for c in sb.q.calls if c[0] == "order"]
    assert orders, f"no ORDER BY: {sb.q.calls}"
    assert orders[0][1] == "id"


def test_reg_rows_selects_the_columns_the_slot_pass_needs(monkeypatch, bot_mod):
    sb = _SB()

    async def _sb_call(fn):
        return fn()

    monkeypatch.setattr(bot_mod, "_comp_sb", lambda: sb)
    monkeypatch.setattr(bot_mod.db, "_sb_call", _sb_call)
    asyncio.run(bot_mod._comp_reg_rows(7))
    sel = next(c for c in sb.q.calls if c[0] == "select")
    cols = sel[1]
    for needed in ("id", "member_ids", "status", "team_color"):
        assert needed in cols
