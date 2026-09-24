"""Match betting: pure pool math + settle integration (fake Supabase)."""
from types import SimpleNamespace

from server.lib import bets as b
from server.routes import bets as routes
from server.routes import coins as coins_routes


# --- pure ---------------------------------------------------------------

def test_parse_and_make_key():
    assert b.parse_key("comp:3:2") == {"kind": "comp", "tourney_id": 3, "version": 2}
    assert b.parse_key("rt:5:0") == {"kind": "rt", "tourney_id": 5, "version": 0}
    assert b.parse_key("room:R-ABC:1") == {"kind": "room", "room_id": "R-ABC",
                                           "version": 1}
    assert b.parse_key("nope") is None
    assert b.parse_key("comp:x:2") is None
    assert b.make_key("comp", 3, 2) == "comp:3:2"
    assert b.make_key("bogus", 1, 0) is None


def test_validate_stake_limits():
    assert b.validate_stake(1000, 100) == (True, None)
    assert b.validate_stake(1000, 1) == (True, None)  # min is 1
    assert b.validate_stake(50, 100)[0] is False
    assert b.validate_stake(1000, 0)[0] is False
    assert b.validate_stake(99999, 20000)[0] is False


def test_winners_with_teammates():
    regs = [{"member_ids": ["A", "B"]}, {"member_ids": ["C"]}]
    assert b.winners_with_teammates("B", regs) == {"A", "B"}
    assert b.winners_with_teammates("C", regs) == {"C"}
    assert b.winners_with_teammates("Z", regs) == {"Z"}
    assert b.winners_with_teammates(None, regs) == set()
    assert b.winners_with_teammates("", regs) == set()


def _bet(i, pick, amount):
    return {"id": i, "bettor": f"U{i}", "pick": pick, "amount": amount}


def test_settle_pool_decisive_split():
    out = b.settle_pool([_bet(1, "B", 100), _bet(2, "B", 100),
                         _bet(3, "C", 100)], {"B"})
    assert out["refunded"] is False
    assert out["pool"] == 300
    assert out["rake"] == 15  # 5% burned
    assert out["credits"] == {1: 142, 2: 142, 3: 0}  # 285 pot // 200 stakes


def test_settle_pool_refunds():
    # sealed with no winner -> everybody refunded whole, no rake
    out = b.settle_pool([_bet(1, "B", 100), _bet(2, "C", 50)], set())
    assert out == {"credits": {1: 100, 2: 50}, "rake": 0,
                   "pool": 150, "refunded": True}
    # nobody picked the winner -> same full refund
    out = b.settle_pool([_bet(1, "C", 100)], {"B"})
    assert out["refunded"] is True and out["credits"] == {1: 100}
    assert b.settle_pool([], {"B"})["refunded"] is True


# --- fake supabase -------------------------------------------------------

class _FakeTable:
    def __init__(self, store, name):
        self._s = store
        self._n = name
        self._eq = []
        self._in = []
        self._order = None
        self._limit = None
        self._update = None
        self._insert = None

    def select(self, cols="*"):
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def in_(self, col, vals):
        self._in.append((col, list(vals)))
        return self

    def in_(self, col, vals):
        self._in.append((col, list(vals)))
        return self

    def order(self, col, desc=False):
        self._order = (col, bool(desc))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def update(self, payload):
        self._update = dict(payload)
        return self

    def insert(self, payload):
        self._insert = dict(payload)
        return self

    def _rows(self):
        rows = list(self._s.get(self._n, []))
        for col, val in self._eq:
            rows = [r for r in rows if str(r.get(col)) == str(val)]
        for col, vals in self._in:
            want = {str(v) for v in vals}
            rows = [r for r in rows if str(r.get(col)) in want]
        if self._order:
            col, desc = self._order
            rows = sorted(rows, key=lambda r: str(r.get(col) or ""),
                          reverse=desc)
        if self._limit is not None:
            rows = rows[:self._limit]
        return rows

    def execute(self):
        if self._insert is not None:
            row = dict(self._insert)
            row.setdefault("id", len(self._s.get(self._n, [])) + 1)
            self._s.setdefault(self._n, []).append(row)
            return SimpleNamespace(data=[row])
        if self._update is not None:
            out = []
            for r in self._rows():
                r.update(self._update)
                out.append(r)
            return SimpleNamespace(data=out)
        return SimpleNamespace(data=self._rows())


class _FakeSupabase:
    def __init__(self, store):
        self._s = store

    def table(self, name):
        return _FakeTable(self._s, name)


def _patch(monkeypatch, store):
    fake = _FakeSupabase(store)
    monkeypatch.setattr(routes, "supabase", fake)
    monkeypatch.setattr(coins_routes, "supabase", fake)

    async def _nowait(fn, *a, **k):
        return fn(*a, **k)
    monkeypatch.setattr(routes, "db_io", _nowait)
    return fake


def _comp_store(**kw):
    store = {
        "bngc_comp_tourneys": [{"id": 3, "name": "Cup", "status": "open"}],
        "bngc_comp_matches": [{"id": 11, "tourney_id": 3, "version": 2,
                               "room_id": "R1",
                               "status": kw.get("status", "sealed"),
                               "winner_discord_id": kw.get("winner", "B")}],
        "bngc_comp_registrations": [{"member_ids": ["A", "B"], "status": "accepted",
                                     "tourney_id": 3},
                                    {"member_ids": ["C"], "status": "accepted",
                                     "tourney_id": 3}],
        "bngc_users": [
            {"discord_id": "A", "game_username": "Ann", "discord_username": "ann",
             "nectar_coins": 1000, "nectar_total_earned": 0, "nectar_total_wagered": 0},
            {"discord_id": "B", "game_username": "Bo", "discord_username": "bo",
             "nectar_coins": 1000, "nectar_total_earned": 0, "nectar_total_wagered": 0},
            {"discord_id": "C", "game_username": "Cy", "discord_username": "cy",
             "nectar_coins": 1000, "nectar_total_earned": 0, "nectar_total_wagered": 0},
            {"discord_id": "D", "game_username": "Dee", "discord_username": "dee",
             "nectar_coins": 900, "nectar_total_earned": 0, "nectar_total_wagered": 100},
            {"discord_id": "E", "game_username": "Eli", "discord_username": "eli",
             "nectar_coins": 900, "nectar_total_earned": 0, "nectar_total_wagered": 100},
            {"discord_id": "F", "game_username": "Fay", "discord_username": "fay",
             "nectar_coins": 900, "nectar_total_earned": 0, "nectar_total_wagered": 100},
        ],
        "bngc_match_bets": [
            {"id": 1, "match_key": "comp:3:2", "kind": "comp",
             "bettor_discord_id": "D", "pick_discord_id": "B",
             "amount": 100, "status": "open", "payout": 0},
            {"id": 2, "match_key": "comp:3:2", "kind": "comp",
             "bettor_discord_id": "E", "pick_discord_id": "B",
             "amount": 100, "status": "open", "payout": 0},
            {"id": 3, "match_key": "comp:3:2", "kind": "comp",
             "bettor_discord_id": "F", "pick_discord_id": "C",
             "amount": 100, "status": "open", "payout": 0},
        ],
        "bngc_coin_ledger": [],
    }
    return store


def _bal(store, did):
    return next(r for r in store["bngc_users"] if r["discord_id"] == did)


def test_settle_decisive_duo_win(monkeypatch):
    store = _comp_store()
    _patch(monkeypatch, store)
    out = routes._settle_match_sync("comp:3:2", [
        {"id": 1, "bettor": "D", "pick": "B", "amount": 100},
        {"id": 2, "bettor": "E", "pick": "B", "amount": 100},
        {"id": 3, "bettor": "F", "pick": "C", "amount": 100},
    ])
    assert out == {"settled": 3, "refunded": False, "paid": 284}
    by = {r["id"]: r for r in store["bngc_match_bets"]}
    assert (by[1]["status"], by[1]["payout"]) == ("won", 142)
    assert (by[2]["status"], by[2]["payout"]) == ("won", 142)
    assert (by[3]["status"], by[3]["payout"]) == ("lost", 0)
    # D got stake back + 42 profit; only profit counts as earned
    assert _bal(store, "D")["nectar_coins"] == 1042
    assert _bal(store, "D")["nectar_total_earned"] == 42
    assert _bal(store, "D")["nectar_total_wagered"] == 200
    assert _bal(store, "F")["nectar_coins"] == 900  # loser untouched


def test_settle_refund_no_winner(monkeypatch):
    store = _comp_store(winner=None)
    _patch(monkeypatch, store)
    out = routes._settle_match_sync("comp:3:2", [
        {"id": 1, "bettor": "D", "pick": "B", "amount": 100},
    ])
    assert out["refunded"] is True
    assert _bal(store, "D")["nectar_coins"] == 1000  # stake back
    assert _bal(store, "D")["nectar_total_earned"] == 0  # refund isn't earnings
    assert store["bngc_match_bets"][0]["status"] == "refunded"


def test_settle_skips_open_match(monkeypatch):
    store = _comp_store(status="open")
    _patch(monkeypatch, store)
    out = routes._settle_match_sync("comp:3:2", [
        {"id": 1, "bettor": "D", "pick": "B", "amount": 100},
    ])
    assert out == {"settled": 0, "refunded": False, "paid": 0}
    assert store["bngc_match_bets"][0]["status"] == "open"


def test_load_match_closed_tourney_hidden(monkeypatch):
    store = _comp_store()
    store["bngc_comp_tourneys"][0]["status"] = "closed"
    _patch(monkeypatch, store)
    assert routes._load_match_sync("comp", 3, 2) is None


# --- live rooms ----------------------------------------------------------

def test_live_result_win_row_first():
    r = b.live_result({"A": 9, "B": 2}, set(), set(), "B", {"A", "B"})
    assert r == {"status": "won", "winner": "B"}  # banked win beats kill count


def test_live_result_kill_target():
    r = b.live_result({"A": 10, "B": 2}, set(), set(), None, {"A", "B"})
    assert r == {"status": "won", "winner": "A"}
    # tie at/above target stays pending
    r = b.live_result({"A": 10, "B": 10}, set(), set(), None, {"A", "B"})
    assert r["status"] == "pending"


def test_live_result_last_standing():
    r = b.live_result({"A": 1}, {"B"}, {"C"}, None, {"A", "B", "C"})
    assert r == {"status": "won", "winner": "A"}
    # everyone out -> no winner by standing
    r = b.live_result({}, {"A"}, {"B"}, None, {"A", "B"})
    assert r["status"] == "pending"


def test_live_result_pending_and_solo():
    assert b.live_result({"A": 3}, set(), set(), None, {"A", "B"})["status"] == "pending"
    assert b.live_result({"A": 30}, set(), set(), None, {"A"})["status"] == "pending"
    assert b.live_result({}, set(), set(), "Z", {"A", "B"})["status"] == "pending"  # unknown winner ignored


# --- live settle integration ---------------------------------------------

def _ev(did, et, room="ROOM1", ver=1, ts=None, gm="zw_lg", code="PBS05"):
    from datetime import datetime, timezone as _tz
    return {"discord_id": did, "session_id": "S1", "event_type": et,
            "event_data": {"room_id": room, "match_version": ver,
                           "gm": gm, "short_code": code},
            "created_at": ts or datetime.now(_tz.utc).isoformat()}


def _live_store(rows, bettor="D", pick="A", stake=100):
    return {
        "bngc_users": [
            {"discord_id": "A", "game_username": "Ann", "discord_username": "ann",
             "nectar_coins": 1000, "nectar_total_earned": 0, "nectar_total_wagered": 0},
            {"discord_id": "B", "game_username": "Bo", "discord_username": "bo",
             "nectar_coins": 1000, "nectar_total_earned": 0, "nectar_total_wagered": 0},
            {"discord_id": bettor, "game_username": "Dee", "discord_username": "dee",
             "nectar_coins": 900, "nectar_total_earned": 0, "nectar_total_wagered": 100},
        ],
        "bngc_game_events": list(rows),
        "bngc_match_bets": [
            {"id": 1, "match_key": "live:ROOM1:1", "kind": "live",
             "bettor_discord_id": bettor, "pick_discord_id": pick,
             "amount": stake, "status": "open", "payout": 0},
        ],
        "bngc_coin_ledger": [],
    }


def _settle_live(store):
    bet = dict(store["bngc_match_bets"][0])
    return routes._settle_match_sync("live:ROOM1:1", [{
        "id": bet["id"], "bettor": bet["bettor_discord_id"],
        "pick": bet["pick_discord_id"], "amount": bet["amount"]}])


def test_live_settle_win_row(monkeypatch):
    rows = [_ev("A", "kill"), _ev("A", "kill"), _ev("B", "death"),
            _ev("B", "kill"), _ev("A", "win")]
    store = _live_store(rows)
    _patch(monkeypatch, store)
    view = routes._load_match_sync("live", "ROOM1", 1)
    assert view["status"] == "sealed" and view["winner"] == "A"
    assert set(view["participants"]) == {"A", "B"}
    out = _settle_live(store)
    assert out == {"settled": 1, "refunded": False, "paid": 95}  # 100 - 5 rake
    assert store["bngc_match_bets"][0]["status"] == "won"
    assert _bal(store, "D")["nectar_coins"] == 995  # 900 + 95 (5 lost to rake)


def test_live_settle_kill_target(monkeypatch):
    rows = [_ev("A", "kill") for _ in range(10)] + [_ev("B", "kill")]
    store = _live_store(rows)
    _patch(monkeypatch, store)
    view = routes._load_match_sync("live", "ROOM1", 1)
    assert view["status"] == "sealed" and view["winner"] == "A"
    out = _settle_live(store)
    assert out["refunded"] is False and store["bngc_match_bets"][0]["status"] == "won"


def test_live_settle_last_standing(monkeypatch):
    rows = [_ev("A", "kill"), _ev("B", "death"), _ev("B", "leave")]
    store = _live_store(rows)
    _patch(monkeypatch, store)
    view = routes._load_match_sync("live", "ROOM1", 1)
    assert view["status"] == "sealed" and view["winner"] == "A"


def test_live_settle_stale_refund(monkeypatch):
    from datetime import datetime, timedelta, timezone as _tz
    old = (datetime.now(_tz.utc) - timedelta(hours=2)).isoformat()
    rows = [_ev("A", "kill", ts=old), _ev("B", "kill", ts=old)]
    store = _live_store(rows)
    _patch(monkeypatch, store)
    view = routes._load_match_sync("live", "ROOM1", 1)
    assert view["status"] == "sealed" and view["winner"] is None  # abandoned
    out = _settle_live(store)
    assert out["refunded"] is True
    assert _bal(store, "D")["nectar_coins"] == 1000  # stake back
    assert store["bngc_match_bets"][0]["status"] == "refunded"


def test_live_open_while_running(monkeypatch):
    rows = [_ev("A", "kill"), _ev("B", "kill")]
    store = _live_store(rows)
    _patch(monkeypatch, store)
    view = routes._load_match_sync("live", "ROOM1", 1)
    assert view["status"] == "open"
    assert view["label"].startswith("Room PBS05 M1")
    out = _settle_live(store)
    assert out == {"settled": 0, "refunded": False, "paid": 0}
    assert store["bngc_match_bets"][0]["status"] == "open"


# --- fixed odds ----------------------------------------------------------

def test_odds_math_sanity():
    assert b.normalize_bet_type("OUT") == "out"
    assert b.normalize_bet_type("bogus") == "win"
    assert b.validate_line(5) == (True, None)
    assert b.validate_line(99)[0] is False
    # demon vs novice pricing on the same line
    assert b.kills_odds(6.0, 5) < b.kills_odds(1.0, 5)
    assert b.kills_odds(6.0, 3) < b.kills_odds(6.0, 10)  # harder line pays more
    assert b.kills_odds(0.0, 5) == b.ODDS_MAX  # impossible pays max
    assert b.kills_odds(50.0, 2) == b.ODDS_MIN  # certain pays min
    # elimination pricing: bad players are near-certain outs (pay ~min),
    # good players might survive (pay more)
    assert b.out_odds(0.05, 8) < b.out_odds(0.5, 8)
    assert b.out_odds(0.0, 8) == b.ODDS_MIN  # never wins -> always out
    assert b.payout_for(100, 2.48) == 248
    assert b.form_from_ledger(12, 3, 4) == {"lam": 3.0, "win_rate": 0.75,
                                            "provisional": False}
    prov = b.form_from_ledger(0, 0, 0)
    assert prov["provisional"] is True and prov["lam"] == b.PRIOR_LAM


def _side_bet(store, btype, line=None, odds=2.0, pick="A", stake=100,
              bettor="D"):
    store["bngc_match_bets"].append(
        {"id": len(store["bngc_match_bets"]) + 1, "match_key": "live:ROOM1:1",
         "kind": "live", "bettor_discord_id": bettor, "pick_discord_id": pick,
         "amount": stake, "status": "open", "payout": 0,
         "bet_type": btype, "line": line, "odds": odds})
    bet = store["bngc_match_bets"][-1]
    return [{"id": bet["id"], "bettor": bettor, "pick": pick,
             "amount": stake, "type": btype, "line": line, "odds": odds}]


def test_side_kills_win_and_loss(monkeypatch):
    rows = ([_ev("A", "kill") for _ in range(5)] + [_ev("B", "kill")]
            + [_ev("A", "win")])
    store = _live_store(rows, pick="A")
    store["bngc_match_bets"] = []  #_side bets only
    _patch(monkeypatch, store)
    out = routes._settle_match_sync("live:ROOM1:1",
                                    _side_bet(store, "kills", line=5, odds=2.0))
    # profit 100 exceeds the 50/day wager cap -> stake 100 + 50 profit
    assert out == {"settled": 1, "refunded": False, "paid": 150}
    assert _bal(store, "D")["nectar_coins"] == 1050  # 900 + 150
    assert _bal(store, "D")["nectar_total_earned"] == 50  # capped profit only
    assert store["bngc_coin_ledger"][0]["meta"]["capped"] is True
    # same evidence, higher line -> loss
    store2 = _live_store(rows, pick="A")
    store2["bngc_match_bets"] = []
    _patch(monkeypatch, store2)
    out = routes._settle_match_sync("live:ROOM1:1",
                                    _side_bet(store2, "kills", line=10, odds=5.0))
    assert store2["bngc_match_bets"][0]["status"] == "lost"
    assert _bal(store2, "D")["nectar_coins"] == 900


def test_side_out_win_loss_refund(monkeypatch):
    rows = [_ev("A", "kill"), _ev("B", "death"), _ev("A", "win")]
    # out on B (dead, not winner) -> won
    store = _live_store(rows, pick="B")
    store["bngc_match_bets"] = []
    _patch(monkeypatch, store)
    out = routes._settle_match_sync("live:ROOM1:1",
                                    _side_bet(store, "out", odds=1.5, pick="B"))
    assert out == {"settled": 1, "refunded": False, "paid": 150}
    # out on A (the winner) -> lost
    store2 = _live_store(rows, pick="A")
    store2["bngc_match_bets"] = []
    _patch(monkeypatch, store2)
    routes._settle_match_sync("live:ROOM1:1",
                              _side_bet(store2, "out", odds=1.5, pick="A"))
    assert store2["bngc_match_bets"][0]["status"] == "lost"
    # out on Z (never played) -> refund
    store3 = _live_store(rows, pick="Z")
    store3["bngc_match_bets"] = []
    _patch(monkeypatch, store3)
    out = routes._settle_match_sync("live:ROOM1:1",
                                    _side_bet(store3, "out", odds=3.0, pick="Z"))
    assert out["refunded"] is False  # overall: win bets none, side refund
    assert store3["bngc_match_bets"][0]["status"] == "refunded"
    assert _bal(store3, "D")["nectar_coins"] == 1000


def test_side_kills_on_comp_standings(monkeypatch):
    store = _comp_store()
    store["bngc_comp_results"] = [{"match_id": 11, "standings": [
        {"discord_id": "A", "kills": 5}, {"discord_id": "B", "kills": 2}]}]
    store["bngc_match_bets"] = [
        {"id": 1, "match_key": "comp:3:2", "kind": "comp",
         "bettor_discord_id": "D", "pick_discord_id": "A",
         "amount": 100, "status": "open", "payout": 0,
         "bet_type": "kills", "line": 3, "odds": 2.0}]
    _patch(monkeypatch, store)
    out = routes._settle_match_sync("comp:3:2", [{
        "id": 1, "bettor": "D", "pick": "A", "amount": 100,
        "type": "kills", "line": 3, "odds": 2.0}])
    # profit 100 exceeds the 50/day wager cap -> stake 100 + 50 profit
    assert out == {"settled": 1, "refunded": False, "paid": 150}
    assert _bal(store, "D")["nectar_coins"] == 1050
    assert _bal(store, "D")["nectar_total_earned"] == 50
    # missing standings -> refund, never guessed
    store2 = _comp_store()
    store2["bngc_match_bets"] = [
        {"id": 1, "match_key": "comp:3:2", "kind": "comp",
         "bettor_discord_id": "D", "pick_discord_id": "A",
         "amount": 100, "status": "open", "payout": 0,
         "bet_type": "kills", "line": 3, "odds": 2.0}]
    _patch(monkeypatch, store2)
    routes._settle_match_sync("comp:3:2", [{
        "id": 1, "bettor": "D", "pick": "A", "amount": 100,
        "type": "kills", "line": 3, "odds": 2.0}])
    assert store2["bngc_match_bets"][0]["status"] == "refunded"


def test_win_pool_ignores_side_bets(monkeypatch):
    store = _comp_store()
    store["bngc_match_bets"].append(
        {"id": 9, "match_key": "comp:3:2", "kind": "comp",
         "bettor_discord_id": "D", "pick_discord_id": "C",
         "amount": 1000, "status": "open", "payout": 0,
         "bet_type": "kills", "line": 3, "odds": 2.0})
    _patch(monkeypatch, store)
    # only the 3 win bets settle through the pool; side bet stays open
    # (no standings rows seeded -> would refund, but comp sealed + no
    # standings means kills unevaluable -> refund path)
    out = routes._settle_match_sync("comp:3:2", [
        {"id": 1, "bettor": "D", "pick": "B", "amount": 100, "type": "win",
         "line": None, "odds": None},
        {"id": 9, "bettor": "D", "pick": "C", "amount": 1000,
         "type": "kills", "line": 3, "odds": 2.0},
    ])
    by = {r["id"]: r for r in store["bngc_match_bets"]}
    assert by[1]["status"] == "won" and by[1]["payout"] == 95  # pool of 100 only
    assert by[9]["status"] == "refunded"  # no standings -> refund


# --- daily wager-earnings cap (50/day profit) ------------------------------

def _ledger_win(did, earned, days_ago=0, reason="bet_won"):
    from datetime import datetime, timedelta, timezone as _tz
    ts = (datetime.now(_tz.utc) - timedelta(days=days_ago)).isoformat()
    return {"discord_id": did, "delta": earned + 100, "balance_after": 1000,
            "reason": reason, "meta": {"earned": earned}, "created_at": ts}


def test_wager_cap_partial_and_exhausted(monkeypatch):
    rows = ([_ev("A", "kill") for _ in range(5)] + [_ev("B", "kill")]
            + [_ev("A", "win")])
    # 40 already earned today -> 100-profit win pays 100 stake + 10
    store = _live_store(rows, pick="A")
    store["bngc_match_bets"] = []
    store["bngc_coin_ledger"] = [_ledger_win("D", 40)]
    _patch(monkeypatch, store)
    out = routes._settle_match_sync("live:ROOM1:1",
                                    _side_bet(store, "kills", line=5, odds=2.0))
    assert out["paid"] == 110
    assert _bal(store, "D")["nectar_coins"] == 1010  # 900 + 110
    assert _bal(store, "D")["nectar_total_earned"] == 10
    assert store["bngc_coin_ledger"][-1]["meta"]["capped"] is True
    # exhausted (50 banked) -> stake back only, still marked won
    store2 = _live_store(rows, pick="A")
    store2["bngc_match_bets"] = []
    store2["bngc_coin_ledger"] = [_ledger_win("D", 50)]
    _patch(monkeypatch, store2)
    out = routes._settle_match_sync("live:ROOM1:1",
                                    _side_bet(store2, "kills", line=5, odds=2.0))
    assert out["paid"] == 100
    assert _bal(store2, "D")["nectar_coins"] == 1000  # stake back, no profit
    assert store2["bngc_match_bets"][0]["status"] == "won"
    # yesterday's 100 does not count toward today
    store3 = _live_store(rows, pick="A")
    store3["bngc_match_bets"] = []
    store3["bngc_coin_ledger"] = [_ledger_win("D", 100, days_ago=2)]
    _patch(monkeypatch, store3)
    out = routes._settle_match_sync("live:ROOM1:1",
                                    _side_bet(store3, "kills", line=5, odds=2.0))
    assert out["paid"] == 150  # full 100 profit (50 cap, 0 banked)
    assert _bal(store3, "D")["nectar_coins"] == 1050


# --- session-scoped lobby matches (room-less pubs) ------------------------

def _sess_ev(did, et, session="S9", room="lobby", ver=0, gm="zw_lg", ts=None):
    from datetime import datetime, timezone as _tz
    return {"discord_id": did, "session_id": session, "event_type": et,
            "event_data": {"room_id": room, "match_version": ver,
                           "gm": gm, "short_code": None, "code": None},
            "created_at": ts or datetime.now(_tz.utc).isoformat()}


def _sess_kf(instigator, instigated, session="S9", ver=0, ktype="killed"):
    from datetime import datetime, timezone as _tz
    return {"discord_id": "X", "session_id": session, "event_type": "killfeed",
            "event_data": {"room_id": "lobby", "match_version": ver,
                           "gm": "zw_lg", "short_code": None, "code": None,
                           "kill_type": ktype, "instigator": instigator,
                           "instigated": instigated},
            "created_at": datetime.now(_tz.utc).isoformat()}


def _sess_store():
    return {
        "bngc_users": [
            {"discord_id": "X", "game_username": "Xeno", "discord_username": "x",
             "nectar_coins": 1000, "nectar_total_earned": 0, "nectar_total_wagered": 0},
            {"discord_id": "Y", "game_username": "Yara", "discord_username": "y",
             "nectar_coins": 1000, "nectar_total_earned": 0, "nectar_total_wagered": 0},
            {"discord_id": "Z", "game_username": "Zed", "discord_username": "z",
             "nectar_coins": 1000, "nectar_total_earned": 0, "nectar_total_wagered": 0},
        ],
        "bngc_game_events": [
            _sess_ev("X", "kill"), _sess_ev("X", "kill"), _sess_ev("X", "kill"),
            _sess_kf("Yara", "SomeGuy"),
            _sess_kf("Xeno", "Yara"),
        ],
        "bngc_match_bets": [],
        "bngc_coin_ledger": [],
    }


def test_room_key_lobby_scoped():
    r = {"session_id": "S9",
         "event_data": {"room_id": "lobby", "match_version": 0}}
    assert routes._room_key_of(r) == ("sess-S9", 0)
    r2 = {"session_id": "S9",
          "event_data": {"room_id": "R1", "match_version": 0}}
    assert routes._room_key_of(r2) == ("R1", 0)  # v0 real rooms count now
    r3 = {"session_id": "S9",
          "event_data": {"room_id": "R1", "match_version": 2}}
    assert routes._room_key_of(r3) == ("R1", 2)
    assert routes._room_key_of({"event_data": {}}) is None


def test_sess_match_loads(monkeypatch):
    store = _sess_store()
    _patch(monkeypatch, store)
    v = routes._load_match_sync("live", "sess-S9", 0)
    assert v and v["status"] == "open"
    assert set(v["participants"]) == {"X", "Y"}
    assert v["_row_holders"] == ["X"]
    assert "Xeno" in v["label"]
    assert v["evidence"]["kills"] == {"X": 3, "Y": 1}  # mapped fold
    assert v["key"] == "live:sess-S9:0"


def test_sess_match_sealed_mapped_winner(monkeypatch):    # X dies -> mapped Yara wins by last standing (sealed, no pool anyway)
    store = _sess_store()
    store["bngc_game_events"].append(_sess_ev("X", "death"))
    _patch(monkeypatch, store)
    v = routes._load_match_sync("live", "sess-S9", 0)
    assert v and v["status"] == "sealed" and v["winner"] == "Y"


def test_v0_room_listed(monkeypatch):
    store = _sess_store()
    _patch(monkeypatch, store)
    v = routes._load_match_sync("live", "R9", 0)
    assert v is not None and v["status"] == "sealed"  # no rows: sealed-empty, never listed
    store["bngc_game_events"].extend([
        _sess_ev("X", "kill", room="R9", session="S1"),
        _sess_ev("Y", "kill", room="R9", session="S2"),
    ])
    v = routes._load_match_sync("live", "R9", 0)
    assert v and v["status"] == "open"
    assert set(v["participants"]) == {"X", "Y"}


def _place_req(payload):
    import json as _json
    from starlette.requests import Request

    async def _recv():
        return {"type": "http.request", "body": _json.dumps(payload).encode(),
                "more_body": False}
    return Request({"type": "http", "method": "POST",
                    "headers": [(b"content-type", b"application/json")]},
                   _recv)


def _place(monkeypatch, store, payload):
    import asyncio
    import json as _json
    _patch(monkeypatch, store)
    resp = asyncio.run(routes.bets_place(_place_req(payload)))
    assert resp.status_code in (200, 400), resp.status_code
    return resp.status_code, _json.loads(resp.body.decode())


def test_sess_win_blocked_kills_settle(monkeypatch):
    store = _sess_store()
    # win pools are blocked on session matches
    code, body = _place(monkeypatch, store, {
        "discord_id": "Z", "match_key": "live:sess-S9:0",
        "pick_discord_id": "X", "amount": 100, "bet_type": "win"})
    assert code == 400 and "side bets only" in body["error"]
    # out bet on a mapped-but-rowless pick is refused while open
    code, body = _place(monkeypatch, store, {
        "discord_id": "Z", "match_key": "live:sess-S9:0",
        "pick_discord_id": "Y", "amount": 50, "bet_type": "out"})
    assert code == 400 and "no tracked rows" in body["error"]
    # kills line on the tracked capturer (3 kills >= line 2)
    code, kbody = _place(monkeypatch, store, {
        "discord_id": "Z", "match_key": "live:sess-S9:0",
        "pick_discord_id": "X", "amount": 100, "bet_type": "kills", "line": 2})
    assert code == 200, kbody
    assert kbody.get("odds")
    # seal it: banked win for X, then the kills line settles won
    # (odds are poisson-priced off X's history; cap math is covered above)
    store["bngc_game_events"].append(_sess_ev("X", "win"))
    _odds = float(kbody["odds"])
    _profit = int(round(100 * _odds)) - 100
    _pay = 100 + max(0, min(_profit, 50))
    out = routes._settle_match_sync("live:sess-S9:0", [{
        "id": kbody["bet_id"], "bettor": "Z", "pick": "X", "amount": 100,
        "type": "kills", "line": 2, "odds": kbody["odds"]}])
    assert out == {"settled": 1, "refunded": False, "paid": _pay}
    row = next(r for r in store["bngc_match_bets"]
               if r.get("id") == kbody["bet_id"])
    assert row["status"] == "won"


def test_sess_solo_grinder_lists(monkeypatch):
    # Tracked player alone (opponents unmapped pubs): still listable,
    # kills/out lines work on the holder.
    store = _sess_store()
    store["bngc_game_events"] = [
        _sess_ev("X", "kill"), _sess_ev("X", "kill"),
        _sess_kf("Xeno", "RandomPub"),
    ]
    _patch(monkeypatch, store)
    v = routes._load_match_sync("live", "sess-S9", 0)
    assert v and v["status"] == "open"
    assert v["participants"] == ["X"]
    code, body = _place(monkeypatch, store, {
        "discord_id": "Z", "match_key": "live:sess-S9:0",
        "pick_discord_id": "X", "amount": 20, "bet_type": "kills", "line": 2})
    assert code == 200, body
