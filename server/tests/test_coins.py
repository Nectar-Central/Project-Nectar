"""Nectar Coins economy: pure math + route integration (fake Supabase)."""
from datetime import date
from types import SimpleNamespace

from server.lib import coins as c
from server.routes import coins as routes


def test_daily_coins_random_range():
    import random
    # payout is always 1-5 regardless of streak (streak tracked, no bonus)
    for streak in (1, 2, 3, 7, 30, "bad"):
        for _ in range(25):
            assert 1 <= c.daily_coins_for(streak) <= 5
    # injectable rng stays deterministic for tests
    assert c.daily_coins_for(1, rng=random.Random(7)) == \
        c.daily_coins_for(30, rng=random.Random(7))


def test_validate_bet():
    assert c.validate_bet(1000, 100) == (True, None)
    assert c.validate_bet(1000, 1) == (True, None)  # min is 1
    assert c.validate_bet(50, 100)[0] is False  # insufficient
    assert c.validate_bet(1000, 0)[0] is False  # under min
    assert c.validate_bet(99999, 20000)[0] is False  # over max
    assert c.validate_bet(1000, "bad")[0] is False


def test_cap_wager_profit():
    assert c.cap_wager_profit(100, 0) == 50  # default cap 50
    assert c.cap_wager_profit(30, 0) == 30  # under cap untouched
    assert c.cap_wager_profit(100, 40) == 10  # partial room
    assert c.cap_wager_profit(100, 50) == 0  # exhausted
    assert c.cap_wager_profit(100, 999) == 0
    assert c.cap_wager_profit(0, 0) == 0
    assert c.cap_wager_profit(-5, 0) == 0
    assert c.cap_wager_profit(100, 0, cap=200) == 100  # env-tunable
    assert c.WAGER_PROFIT_REASONS == frozenset({"bet_won", "duel_win"})


def test_normalize_and_resolve_flip():
    assert c.normalize_choice("heads") == "Heads"
    assert c.normalize_choice("T") == "Tails"
    assert c.normalize_choice("edge") is None
    assert c.resolve_flip(100, "Heads", "Heads") == 100
    assert c.resolve_flip(100, "Heads", "Tails") == -100


def test_validate_transfer():
    assert c.validate_transfer(500, 100, "A", "B") == (True, None)
    assert c.validate_transfer(500, 100, "A", "A")[0] is False
    assert c.validate_transfer(50, 100, "A", "B")[0] is False
    assert c.validate_transfer(500, 0, "A", "B")[0] is False


def test_slots_kind_and_delta():
    assert c.slots_kind(["7️⃣", "7️⃣", "7️⃣"]) == "trips"
    assert c.slots_kind(["🍒", "🍒", "⭐"]) == "pair"
    assert c.slots_kind(["🍒", "⭐", "🪙"]) == "loss"
    assert c.slots_kind(["🍒"]) == "loss"
    # trips pay profit multiples per symbol
    assert c.slots_delta(100, ["7️⃣", "7️⃣", "7️⃣"]) == 2500
    assert c.slots_delta(100, ["💎", "💎", "💎"]) == 1000
    assert c.slots_delta(100, ["🪙", "🪙", "🪙"]) == 500
    assert c.slots_delta(100, ["⭐", "⭐", "⭐"]) == 300
    assert c.slots_delta(100, ["🍒", "🍒", "🍒"]) == 200
    # pair refunds, loss takes the bet
    assert c.slots_delta(100, ["🍒", "🍒", "⭐"]) == 0
    assert c.slots_delta(100, ["🍒", "⭐", "🪙"]) == -100
    assert c.slots_delta("bad", ["🍒", "⭐", "🪙"]) == 0


def test_spin_slots_shape():
    class _Rng:
        def choice(self, seq):
            return seq[0]
    assert c.spin_slots(_Rng()) == ["🍒", "🍒", "🍒"]
    live = c.spin_slots()
    assert len(live) == 3 and all(s in c.SLOT_SYMBOLS for s in live)


def test_dice_normalize_and_delta():
    assert c.normalize_dice_choice("over") == "Over"
    assert c.normalize_dice_choice("U") == "Under"
    assert c.normalize_dice_choice("seven") is None
    assert c.dice_delta(100, "Over", 75) == 100
    assert c.dice_delta(100, "Over", 25) == -100
    assert c.dice_delta(100, "Under", 25) == 100
    assert c.dice_delta(100, "Under", 75) == -100
    assert c.dice_delta(100, "Over", 50) == 0  # push refunds
    assert c.dice_delta(100, "Under", 50) == 0
    assert c.dice_delta("bad", "Over", 75) == 0


def test_roll_dice_bounds():
    for _ in range(50):
        assert 1 <= c.roll_dice() <= 100


# --- Route integration with in-memory fake Supabase ------------------------

class _FakeTable:
    def __init__(self, db, name):
        self._db = db
        self._name = name
        self._filters = []
        self._order = None
        self._limit = None
        self._select_cols = "*"
        self._update = None
        self._insert = None

    def select(self, cols="*"):
        self._select_cols = cols
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def single(self):
        return self

    def update(self, payload):
        self._update = payload
        return self

    def insert(self, payload):
        self._insert = payload
        return self

    def _rows(self):
        rows = list(self._db.get(self._name, []))
        for col, val in self._filters:
            rows = [r for r in rows if str(r.get(col)) == str(val)]
        if self._order:
            col, desc = self._order
            rows = sorted(rows, key=lambda r: str(r.get(col) or ""), reverse=bool(desc))
        if self._limit is not None:
            rows = rows[:self._limit]
        return rows

    def execute(self):
        if self._insert is not None:
            row = dict(self._insert)
            self._db.setdefault(self._name, []).append(row)
            return SimpleNamespace(data=[row])
        if self._update is not None:
            out = []
            for r in self._rows():
                r.update(self._update)
                out.append(r)
            return SimpleNamespace(data=out)
        rows = self._rows()
        if "single" in ():  # never; single() handled by caller shape
            pass
        # emulate supabase-py single(): tests call limit(1), not single()
        return SimpleNamespace(data=rows)


class _FakeSupabase:
    def __init__(self, db):
        self._db = db

    def table(self, name):
        return _FakeTable(self._db, name)


def _patch_supabase(monkeypatch, db):
    fake = _FakeSupabase(db)
    monkeypatch.setattr(routes, "supabase", fake)

    async def _nowait(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr(routes, "db_io", _nowait)
    return fake


def _user(did="U1", **kw):
    row = {"discord_id": did, "discord_username": "tester",
           "nectar_coins": 500, "nectar_total_earned": 500,
           "nectar_total_wagered": 0, "daily_streak": 0,
           "last_daily_claim": None}
    row.update(kw)
    return row


def test_apply_delta_and_ledger(monkeypatch):
    db = {"bngc_users": [_user()], "bngc_coin_ledger": []}
    _patch_supabase(monkeypatch, db)
    out = routes._apply_delta_sync("U1", 100, "daily", {"streak": 1})
    assert out["balance"] == 600
    assert db["bngc_users"][0]["nectar_coins"] == 600
    assert len(db["bngc_coin_ledger"]) == 1
    assert db["bngc_coin_ledger"][0]["reason"] == "daily"


def test_apply_delta_insufficient(monkeypatch):
    db = {"bngc_users": [_user()], "bngc_coin_ledger": []}
    _patch_supabase(monkeypatch, db)
    try:
        routes._apply_delta_sync("U1", -9999, "flip_loss", {"bet": 9999})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    assert db["bngc_users"][0]["nectar_coins"] == 500  # untouched


def test_gamble_wager_accounting(monkeypatch):
    db = {"bngc_users": [_user()], "bngc_coin_ledger": []}
    _patch_supabase(monkeypatch, db)
    # slots trips win: stake counted as wagered, profit counted as earned
    out = routes._apply_delta_sync("U1", 500, "slots_win",
                                   {"bet": 100, "kind": "trips"})
    assert out["balance"] == 1000
    assert out["earned"] == 1000  # 500 starting + 500 profit
    assert out["wagered"] == 100
    # dice push: no balance change, stake still counted as wagered
    out = routes._apply_delta_sync("U1", 0, "dice_push",
                                   {"bet": 100, "roll": 50})
    assert out["balance"] == 1000
    assert out["wagered"] == 200
    assert len(db["bngc_coin_ledger"]) == 2
