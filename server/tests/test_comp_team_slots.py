"""A team's colour must be derived from ONE slot, read in a stable order.

A team could change colour between syncs, which is not cosmetic: the colour is
frozen on the signup message as a reaction, so the board and the reaction
disagreed about who was who. The cause was the read order - the slot is the
row's position in the result, and Postgres guarantees no order without
ORDER BY, so the same team could land in a different slot every time.

So rows are read in registration order (id is a monotonic identity) and the
slot is that position. There are no team roles any more; the swap that used to
be visible as "blue reaction, red role" cannot happen without one.
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
