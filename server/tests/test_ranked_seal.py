"""Phase 2: ranked sealing — 10-tier milestones, exact per-kill ELO,
and seal_ranked_match gating (host/fresh/unsealed/placements/warnings).

DB is faked (chainable stub); points cache pre-seeded. No network.
"""
import importlib.util
import sys
import time
import types

import pytest

from server.lib import ranked as ranked_mod
from server.lib.photon_engine.models import KillInfo, Player, RoomView
import server.scripts.processor as proc


# ---------------------------------------------------------------------------
# Fake Supabase (chainable stub)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _static_economy(monkeypatch):
    """Legacy tests pin the static economy: most seals below were written
    against flat 8/50/-5 numbers. Dynamic-path tests opt back in with
    monkeypatch.setattr(ranked_mod, "RANKED_DYNAMIC", True)."""
    monkeypatch.setattr(ranked_mod, "RANKED_DYNAMIC", False)

class _FakeRes:
    def __init__(self, data):
        self.data = data


class _FakeQ:
    def __init__(self, fake, table, op=None, payload=None):
        self.fake = fake
        self.table = table
        self.op = op
        self.payload = payload
        self.filters = []

    def select(self, *a):
        self.op = "select"
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def neq(self, col, val):
        self.filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append((col, list(vals)))
        return self

    def ilike(self, col, pat):
        self.filters.append(("ilike", col, pat))
        return self

    def limit(self, n):
        return self

    def order(self, *a, **k):
        return self

    def upsert(self, payload, on_conflict=None):
        self.op = "upsert"
        self.payload = payload
        return self

    def insert(self, payload):
        self.op = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        return self

    def execute(self):
        return self.fake._run(self)


class _FakeDB:
    def __init__(self, users=(), results=()):
        self.users = list(users)      # bngc_users rows {game_username, discord_id}
        self.results = list(results)  # ranked_results rows
        self.points = {}              # ranked_points discord_id -> points
        self.rooms = {}               # ranked_rooms id -> fields
        self.ranked_events = []       # bngc_ranked_events rows
        self.calls = []

    def table(self, name):
        return _FakeQ(self, name)

    def _run(self, q):
        self.calls.append((q.table, q.op, q.payload, list(q.filters)))
        if q.table == "bngc_users" and q.op == "select":
            rows = list(self.users)
            for f in q.filters:
                if len(f) == 3 and f[0] == "ilike":
                    _, col, pat = f
                    sub = str(pat).strip("%").casefold()
                    rows = [r for r in rows if sub in str(r.get(col) or "").casefold()]
                elif len(f) == 2:
                    col, val = f
                    if isinstance(val, list):
                        rows = [r for r in rows if str(r.get(col)) in {str(v) for v in val}]
                    else:
                        rows = [r for r in rows if str(r.get(col)) == str(val)]
            return _FakeRes(rows)
        if q.table == "bngc_users" and q.op == "insert":
            payloads = q.payload if isinstance(q.payload, list) else [q.payload]
            for p in payloads:
                self.users.append(dict(p))
            return _FakeRes(payloads)
        if q.table == "bngc_users" and q.op == "update":
            touched = []
            for row in self.users:
                ok = True
                for f in q.filters:
                    if len(f) == 2:
                        c, v = f
                        if isinstance(v, list):
                            if str(row.get(c)) not in {str(x) for x in v}:
                                ok = False
                        elif str(row.get(c)) != str(v):
                            ok = False
                if ok:
                    row.update(dict(q.payload))
                    touched.append(dict(row))
            return _FakeRes(touched)
        if q.table == "ranked_results" and q.op == "select":
            rows = self.results
            for col, val in q.filters:
                if isinstance(val, list):
                    rows = [r for r in rows if str(r.get(col)) in {str(v) for v in val}]
                else:
                    rows = [r for r in rows if str(r.get(col)) == str(val)]
            return _FakeRes(rows)
        if q.table == "ranked_results" and q.op in ("upsert", "insert"):
            self.results.append(dict(q.payload))
            return _FakeRes([q.payload])
        if q.table == "ranked_results" and q.op == "update":
            for row in self.results:
                if all(str(row.get(c)) == str(v) for c, v in q.filters):
                    row.update(q.payload)
            return _FakeRes([q.payload])
        if q.table == "ranked_points" and q.op == "select":
            rows = [{"discord_id": d, "points": p} for d, p in self.points.items()]
            for col, val in q.filters:
                if isinstance(val, list):
                    rows = [r for r in rows if str(r.get(col)) in {str(v) for v in val}]
                else:
                    rows = [r for r in rows if str(r.get(col)) == str(val)]
            return _FakeRes(rows)
        if q.table == "ranked_points" and q.op == "upsert":
            payloads = q.payload if isinstance(q.payload, list) else [q.payload]
            for p in payloads:
                self.points[str(p["discord_id"])] = int(p["points"])
            return _FakeRes(payloads)
        if q.table == "ranked_rooms" and q.op == "update":
            for rid, fields in self.rooms.items():
                hit = all(str(fields.get(c)) == str(v) for c, v in q.filters if c == "id")
                if hit:
                    fields.update(q.payload)
            return _FakeRes([])
        if q.table == "ranked_rooms" and q.op == "select":
            rows = list(self.rooms.values())
            for f in q.filters:
                if len(f) == 3:
                    _, col, val = f
                    rows = [r for r in rows if str(r.get(col)) != str(val)]
                else:
                    col, val = f
                    rows = [r for r in rows if str(r.get(col)) == str(val)]
            return _FakeRes(rows)
        if q.table == "bngc_ranked_events" and q.op == "select":
            rows = list(self.ranked_events)
            for col, val in q.filters:
                rows = [r for r in rows if str(r.get(col)) == str(val)]
            return _FakeRes(rows)
        if q.table == "bngc_ranked_events" and q.op == "insert":
            payloads = q.payload if isinstance(q.payload, list) else [q.payload]
            for p in payloads:
                self.ranked_events.append(dict(p))
            return _FakeRes(payloads)
        if q.table == "bngc_ranked_events" and q.op == "update":
            touched = []
            for row in self.ranked_events:
                ok = True
                for f in q.filters:
                    if len(f) == 2:
                        c, v = f
                        if str(row.get(c)) != str(v):
                            ok = False
                if ok:
                    row.update(dict(q.payload))
                    touched.append(dict(row))
            return _FakeRes(touched)
        return _FakeRes([])


def _use_fake_for_ranked(monkeypatch, fake):
    """Point ranked._sb() at the fake (chainable stub adapter)."""
    class _Adapter:
        def table(self, name):
            return _FakeQ(fake, name)
    monkeypatch.setattr(ranked_mod, "_sb", lambda: _Adapter())


# ---------------------------------------------------------------------------
# 10-tier milestones (server)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pts,name", [
    (0, "Unranked"), (199, "Unranked"), (200, "Bronze"), (499, "Bronze"),
    (500, "Silver"), (999, "Silver"), (1000, "Gold"), (1499, "Gold"),
    (1500, "Platinum"), (1999, "Platinum"), (2000, "Diamond"), (2999, "Diamond"),
    (3000, "Emerald"), (3999, "Emerald"), (4000, "Champion"), (4999, "Champion"),
    (5000, "Master"), (7499, "Master"), (7500, "Grandmaster"), (99999, "Grandmaster"),
])
def test_tier_boundaries(pts, name):
    assert ranked_mod.milestone_for(pts)["name"] == name


def test_tier_icons_present():
    expected = {"Platinum": "platinium.png"}  # bucket filename has the typo
    for t in ranked_mod.MILESTONE_TIERS:
        slug = expected.get(t["name"], f"{t['name'].lower()}.png")
        assert t["icon_url"].endswith(f"/{slug}"), t["name"]


def test_next_milestone_edges():
    nxt, need, pct = ranked_mod.next_milestone(0)
    assert (nxt["name"], need, pct) == ("Bronze", 200, 0)
    assert ranked_mod.next_milestone(7500) == (None, 0, 100)
    nxt, need, pct = ranked_mod.next_milestone(1000)
    assert (nxt["name"], need) == ("Platinum", 500)


def test_arena_icon():
    assert ranked_mod.ARENA_ICON_URL.endswith("/arenaicon.png")


# ---------------------------------------------------------------------------
# Bot tier mirror (load file directly, stub `database`)
# ---------------------------------------------------------------------------

def _load_bot_ranked():
    stub = types.SimpleNamespace(USE_SUPABASE=False, supabase=None)
    sys.modules["database"] = stub
    try:
        spec = importlib.util.spec_from_file_location(
            "bot_ranked_mirror", "companion/bot/ranked.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.modules.pop("database", None)


def test_bot_tiers_mirror_server():
    bot_r = _load_bot_ranked()
    for pts, name in [(0, "Unranked"), (200, "Bronze"), (500, "Silver"),
                      (1000, "Gold"), (1500, "Platinum"), (2000, "Diamond"),
                      (3000, "Emerald"), (4000, "Champion"), (5000, "Master"),
                      (7500, "Grandmaster")]:
        assert bot_r.milestone_for(pts)[0] == ranked_mod.milestone_for(pts)["name"]
        assert bot_r.milestone_for(pts)[0] == name


# ---------------------------------------------------------------------------
# Bot ranked helpers: room_seals + session_standings (load file directly)
# ---------------------------------------------------------------------------

class _BotQ:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col)) == str(val)]
        return self

    def order(self, *a, **k):
        col = a[0] if a else None
        rev = bool(k.get("desc"))
        if col:
            self._rows.sort(key=lambda r: r.get(col) or 0, reverse=rev)
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def execute(self):
        return _FakeRes(self._rows)


class _BotDB:
    def __init__(self, results=(), rooms=()):
        self.results = list(results)
        self.rooms = list(rooms)

    def table(self, name):
        if name == "ranked_results":
            return _BotQ(self.results)
        if name == "ranked_rooms":
            return _BotQ(self.rooms)
        return _BotQ([])


def _load_bot_ranked_with(db):
    mod = _load_bot_ranked()
    mod._sb = lambda: db
    return mod


def test_bot_room_seals_newest_first():
    bot_r = _load_bot_ranked_with(_BotDB(results=[
        {"room_id": "R-X", "match_version": 1, "points": {"A": 10}},
        {"room_id": "R-X", "match_version": 2, "points": {"A": 20}},
        {"room_id": "R-Y", "match_version": 1, "points": {}},
    ]))
    seals = bot_r.room_seals("R-X")
    assert [s["match_version"] for s in seals] == [2, 1]


def test_bot_session_standings_totals():
    bot_r = _load_bot_ranked_with(_BotDB(results=[
        {"room_id": "R-X", "match_version": 1, "winner_discord_id": "A",
         "points": {"A": 58, "B": 25}},
        {"room_id": "R-X", "match_version": 2, "winner_discord_id": "B",
         "points": {"A": -5, "B": 66}},
    ]))
    board, n = bot_r.session_standings("R-X")
    assert n == 2
    # B: 91 gained 1W 2 played; A: 53 gained 1W 2 played
    assert board == [("B", [91, 1, 2]), ("A", [53, 1, 2])]


def test_bot_session_standings_empty():
    bot_r = _load_bot_ranked_with(_BotDB())
    assert bot_r.session_standings("R-X") == ([], 0)


def test_bot_list_live_rooms():
    bot_r = _load_bot_ranked_with(_BotDB(rooms=[
        {"id": "R-A", "status": "live"},
        {"id": "R-B", "status": "lobby"},
        {"id": "R-C", "status": "ended"},
    ]))
    assert [r["id"] for r in bot_r.list_live_rooms()] == ["R-A"]


# ---------------------------------------------------------------------------
# static reward units
# ---------------------------------------------------------------------------

def test_static_reward_table():
    assert ranked_mod.kill_reward() == 8
    # kills + win only: 1st pays 50, everything else 0
    assert [ranked_mod.placement_reward(p) for p in (1, 2, 3, 4, 5, 6, 99)] == [50, 0, 0, 0, 0, 0, 0]
    assert ranked_mod.placement_reward("bad") == 0


def test_defeat_penalty_matrix():
    # flat loss regardless of killer
    assert ranked_mod.defeat_penalty(2000, 500) == -5
    assert ranked_mod.defeat_penalty(500, 2000) == -5
    assert ranked_mod.defeat_penalty(500, 500) == -5
    assert ranked_mod.defeat_penalty(500, None) == -5
    assert ranked_mod.defeat_penalty("bad", None) == -5


def test_ranked_log_file_written(tmp_path, monkeypatch):
    from server.lib import logger as _log
    _log.log_ranked("[ranked] test line never-raises")
    from pathlib import Path
    assert (_log._ranked_dir / "ranked.log").exists()


# ---------------------------------------------------------------------------
# seal_match static math
# ---------------------------------------------------------------------------

def test_seal_match_kill_events_exact(monkeypatch):
    fake = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    monkeypatch.setitem(ranked_mod._points_cache, "K", (500, now))
    monkeypatch.setitem(ranked_mod._points_cache, "V", (500, now))
    res = ranked_mod.seal_match(rid="R-X", match_version=2, gm="zw_lg",
                                winner_discord_id="K", kills={"K": 1},
                                placements={"K": 1, "V": 2},
                                kill_events=[("K", "V")])
    assert res["ok"] is True
    # base table (avg 0): K = 1 kill (8) + win (50) + P1 (10) = 68; V = P2 (7) - 5 = 2
    assert res["points"] == {"K": 68, "V": 2}
    assert fake.points["K"] == 568
    assert fake.points["V"] == 502
    row = fake.results[0]
    assert (row["room_id"], row["match_version"], row["winner_discord_id"]) == ("R-X", 2, "K")
    assert row["placements"] == {"K": 1, "V": 2}  # stored for the toast + review


def test_seal_match_defeat_minus_lower_killer(monkeypatch):
    fake = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    monkeypatch.setitem(ranked_mod._points_cache, "K", (200, now))
    monkeypatch.setitem(ranked_mod._points_cache, "V", (2000, now))
    res = ranked_mod.seal_match(rid="R-X", match_version=1, gm="zw_lg",
                                winner_discord_id="V", kills={"K": 1},
                                placements={"K": 4, "V": 1},
                                kill_events=[("K", "V")],
                                death_killers={"V": "K"})
    assert res["ok"] is True
    # K = 1 kill (8) + P4 (3) - 5 = 6 (not winner); V = win (50) + P1 (10) = 60
    assert res["points"]["K"] == 6
    assert res["points"]["V"] == 60


def test_seal_match_defeat_minus_peer_and_zone(monkeypatch):
    fake = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    for did, pts in (("A", 1000), ("B", 1000), ("C", 1000), ("D", 1000)):
        monkeypatch.setitem(ranked_mod._points_cache, did, (pts, now))
    res = ranked_mod.seal_match(rid="R-Z", match_version=1, gm="zw_lg",
                                winner_discord_id="A",
                                kills={"A": 2},
                                placements={"A": 1, "B": 2, "C": 3, "D": 4},
                                death_killers={"B": "A", "C": None})
    assert res["ok"] is True
    # A = 2 kills (16) + win (50) + P1 (10) = 76; B = P2 (7) - 5 = 2;
    # C = P3 (5) - 5 = 0; D = P4 (3) - 5 = -2
    assert res["points"] == {"A": 76, "B": 2, "C": 0, "D": -2}


def test_seal_match_placements_capped_at_lobby_size(monkeypatch):
    fake = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    monkeypatch.setitem(ranked_mod._points_cache, "K", (0, now))
    monkeypatch.setitem(ranked_mod._points_cache, "V", (0, now))
    # anyone in the involved set who isn't the winner takes the flat loss
    res = ranked_mod.seal_match(rid="R-C", match_version=1, gm="zw_lg",
                                winner_discord_id="K", kills={},
                                placements={"K": 1, "V": 2, "X": 3},
                                players_count=2)
    assert res["ok"] is True
    assert res["points"] == {"K": 60, "V": 2, "X": 0}


def test_seal_match_flat_no_scaling_by_avg_points(monkeypatch):
    """No scaling by tier: same table for every lobby tier at equal avg."""
    fake = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    for did, pts in (("H1", 5000), ("H2", 5000)):
        monkeypatch.setitem(ranked_mod._points_cache, did, (pts, now))
    res = ranked_mod.seal_match(rid="R-S", match_version=1, gm="zw_lg",
                                winner_discord_id="H1", kills={},
                                placements={"H1": 1, "H2": 2})
    assert res["ok"] is True
    assert res["points"]["H1"] == 60
    assert res["points"]["H2"] == 2

    fake2 = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake2)
    for did, pts in (("V1", 10000), ("V2", 10000)):
        monkeypatch.setitem(ranked_mod._points_cache, did, (pts, now))
    res2 = ranked_mod.seal_match(rid="R-S2", match_version=1, gm="zw_lg",
                                 winner_discord_id="V1", kills={},
                                 placements={"V1": 1, "V2": 2})
    assert res2["ok"] is True
    assert res2["points"]["V1"] == 60
    assert res2["points"]["V2"] == 2


def test_seal_match_duplicate_is_noop(monkeypatch):
    """Re-sealing an already-sealed (rid, version) never double-awards."""
    fake = _FakeDB(results=[{"room_id": "R-D", "match_version": 1}])
    _use_fake_for_ranked(monkeypatch, fake)
    res = ranked_mod.seal_match(rid="R-D", match_version=1, gm="zw_lg",
                                winner_discord_id="K", kills={"K": 2})
    assert res["ok"] is True and res.get("duplicate") is True
    assert fake.points == {}  # no bumps written


def test_seal_match_bulk_single_read_and_upsert(monkeypatch):
    """Bulk award path: ONE IN read + ONE list upsert for N players."""
    fake = _FakeDB()
    fake.points = {"BK1": 100, "BK2": 200}
    _use_fake_for_ranked(monkeypatch, fake)
    res = ranked_mod.seal_match(rid="R-B", match_version=1, gm="zw_lg",
                                winner_discord_id="BK1", kills={"BK1": 1},
                                placements={"BK1": 1, "BK2": 2},
                                kill_events=[("BK1", "BK2")])
    assert res["ok"] is True
    sels = [c for c in fake.calls if c[0] == "ranked_points" and c[1] == "select"]
    assert len(sels) == 1  # one bulk IN read, not per-player selects
    inf = [f for f in sels[0][3] if f[0] == "discord_id"]
    assert inf and set(inf[0][1]) == {"BK1", "BK2"}
    ups = [c for c in fake.calls if c[0] == "ranked_points" and c[1] == "upsert"]
    assert len(ups) == 1 and isinstance(ups[0][2], list)  # one bulk upsert
    assert {r["discord_id"] for r in ups[0][2]} == {"BK1", "BK2"}
    assert fake.points["BK1"] == 100 + res["points"]["BK1"]
    assert fake.points["BK2"] == 200 + res["points"]["BK2"]
    assert res["points"] == {"BK1": 68, "BK2": 2}  # kill + win + P1 / P2 - loss


def test_seal_match_winner_fallback_gets_first(monkeypatch):
    fake = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    monkeypatch.setitem(ranked_mod._points_cache, "W", (0, now))
    # winner missing from placements (stale flags) still gets the 1st award
    res = ranked_mod.seal_match(rid="R-W", match_version=1, gm="zw_lg",
                                winner_discord_id="W", kills={},
                                placements={})
    assert res["ok"] is True
    assert res["points"] == {"W": 50}


# ---------------------------------------------------------------------------
# seal_ranked_match gating + placements + warnings
# ---------------------------------------------------------------------------

def _room():
    r = RoomView(room_id="r1")
    r.short_code = "ABCDE"
    r.match_version = 2
    r.last_packet_at = time.time()
    r.players[1] = Player(actor_nr=1, username="Killer", is_alive=True)
    r.players[2] = Player(actor_nr=2, username="Victim", is_alive=False)
    r.players[3] = Player(actor_nr=3, username="Outsider", is_alive=False)
    r.recent_kills.append(KillInfo(instigator="Killer", instigated="Victim", event_type="killed"))
    return r


_ROOM_DATA = {"id": "R-X", "short_code": "ABCDE", "host_discord_id": "K",
              "status": "live", "max_matches": 5, "gm": "zw_lg",
              "roster": [{"discord_id": "K"}, {"discord_id": "V"}]}
_NAME2DID = {"Killer": "K", "Victim": "V", "Outsider": "O"}


def _patch_ranked(monkeypatch, calls):
    monkeypatch.setattr(ranked_mod, "get_ranked_room", lambda code: dict(_ROOM_DATA))
    monkeypatch.setattr(proc, "_resolve_ranked_dids", lambda names: dict(_NAME2DID))

    def _seal(**kw):
        calls.append(kw)
        return {"ok": True, "points": {}, "match_version": kw["match_version"]}
    monkeypatch.setattr(ranked_mod, "seal_match", _seal)


def test_non_host_never_seals(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(proc, "supabase", fake)
    calls = []
    _patch_ranked(monkeypatch, calls)
    proc.seal_ranked_match("s", "NOTHOST", _room(), "r1", "zw_lg")
    assert calls == []


def test_unknown_code_never_seals(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(proc, "supabase", fake)
    monkeypatch.setattr(ranked_mod, "get_ranked_room", lambda code: None)
    calls = []
    monkeypatch.setattr(ranked_mod, "seal_match",
                        lambda **kw: calls.append(kw) or {"ok": True})
    proc.seal_ranked_match("s", "K", _room(), "r1", "zw_lg")
    assert calls == []


def test_stale_host_still_seals(monkeypatch):
    # freshness pause deleted: idempotent seal makes late seals harmless
    fake = _FakeDB()
    monkeypatch.setattr(proc, "supabase", fake)
    calls = []
    _patch_ranked(monkeypatch, calls)
    r = _room()
    r.last_packet_at = time.time() - 120
    proc.seal_ranked_match("s", "K", r, "r1", "zw_lg")
    assert len(calls) == 1


def test_no_processor_precheck_seal_owns_idempotency(monkeypatch):
    # processor fires freely; seal_match dedupes (covered by duplicate test)
    fake = _FakeDB(results=[{"room_id": "R-X", "match_version": 2}])
    monkeypatch.setattr(proc, "supabase", fake)
    calls = []
    _patch_ranked(monkeypatch, calls)
    proc.seal_ranked_match("s", "K", _room(), "r1", "zw_lg")
    assert len(calls) == 1


def test_happy_path_winner_kills_losses(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(proc, "supabase", fake)
    calls = []
    _patch_ranked(monkeypatch, calls)
    r = _room()
    proc.seal_ranked_match("s", "K", r, "r1", "zw_lg")
    assert len(calls) == 1
    kw = calls[0]
    assert kw["rid"] == "R-X" and kw["match_version"] == 2
    assert kw["winner_discord_id"] == "K"            # alive roster did
    assert kw["kill_events"] == [("K", "V")]
    assert kw["kills"] == {"K": 1}
    assert kw["death_killers"] == {"V": None}       # flat loss, no attribution
    # no bngc_users rows in this fake: Victim unmapped, winner still cohered P1
    assert kw["placements"] == {"K": 1}


def test_zone_death_takes_flat_loss(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(proc, "supabase", fake)
    calls = []
    _patch_ranked(monkeypatch, calls)
    r = _room()
    # Victim died to the zone: no killfeed entry, still dead in-room
    r.recent_kills.clear()
    proc.seal_ranked_match("s", "K", r, "r1", "zw_lg")
    assert len(calls) == 1
    kw = calls[0]
    assert kw["winner_discord_id"] == "K"
    assert kw["death_killers"] == {"V": None}
    assert kw["kill_events"] is None


def test_nobody_alive_last_to_die_wins(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(proc, "supabase", fake)
    calls = []
    _patch_ranked(monkeypatch, calls)
    r = _room()
    r.players[1].is_alive = False  # zone double-KO: nobody alive
    proc.seal_ranked_match("s", "K", r, "r1", "zw_lg")
    assert len(calls) == 1
    kw = calls[0]
    # most-recent death (Victim) survived longest => winner, not None
    assert kw["winner_discord_id"] == "V"
    assert kw["death_killers"] == {"K": None}


def test_hosting_only_host_not_counted_but_seals(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(proc, "supabase", fake)
    calls = []

    def _hosting_room(code):
        return dict(_ROOM_DATA, host_status="hosting_only")

    monkeypatch.setattr(ranked_mod, "get_ranked_room", _hosting_room)
    monkeypatch.setattr(proc, "_resolve_ranked_dids", lambda names: dict(_NAME2DID))

    def _seal(**kw):
        calls.append(kw)
        return {"ok": True, "points": {}, "match_version": kw["match_version"]}
    monkeypatch.setattr(ranked_mod, "seal_match", _seal)
    proc.seal_ranked_match("s", "K", _room(), "r1", "zw_lg")
    assert len(calls) == 1
    kw = calls[0]
    assert kw["winner_discord_id"] == "V"  # alive but skipped host K loses
    assert kw["kills"] == {}              # host's kills not counted
    assert kw["death_killers"] is None    # V won, K skipped entirely


# ---------------------------------------------------------------------------
# Duo team liveness: _team_alive_for_me edge cases (wins-only, no defeats)
# ---------------------------------------------------------------------------

def _duo_room(gv="duo"):
    """RoomView configured as a duo game."""
    r = RoomView(room_id="r-duo")
    r.short_code = "DUO01"
    r.match_version = 1
    r.gv = gv
    r.last_packet_at = time.time()
    return r


# --- _team_alive_for_me unit tests ---

def test_team_alive_returns_none_when_teams_empty():
    """Duo room with no _pt packets yet → None (no team data)."""
    r = _duo_room()
    assert proc._team_alive_for_me(r, None, "Alice") is None


def test_team_alive_returns_none_for_solo_room():
    """Solo room → None (solo rules)."""
    r = _duo_room(gv="solo")
    assert proc._team_alive_for_me(r, None, "Alice") is None


def test_team_alive_returns_true_when_teammate_alive():
    """Duo room with populated team, teammate alive → True."""
    from server.lib.photon_engine.models import Team
    r = _duo_room()
    r.teams["t1"] = Team(team_id="t1", members=["Alice", "Bob"], is_self_team=True)
    r.players[1] = Player(actor_nr=1, username="Alice", is_alive=False)
    r.players[2] = Player(actor_nr=2, username="Bob", is_alive=True)
    assert proc._team_alive_for_me(r, None, "Alice") is True


def test_team_alive_returns_false_when_whole_team_dead():
    """Duo room with both teammates dead → False."""
    from server.lib.photon_engine.models import Team
    r = _duo_room()
    r.teams["t1"] = Team(team_id="t1", members=["Alice", "Bob"], is_self_team=True)
    r.players[1] = Player(actor_nr=1, username="Alice", is_alive=False)
    r.players[2] = Player(actor_nr=2, username="Bob", is_alive=False)
    assert proc._team_alive_for_me(r, None, "Alice") is False


# --- Wins-only: no defeat guard, no deferral (deleted with _write_defeat) ---

def test_aligned_match_version_continuity():
    # fresh Photon room (v0) with seals behind it continues past them so the
    # next stG=3 seals a NEW version instead of colliding (skipped = no points)
    assert proc._aligned_match_version(0, 2) == 2
    assert proc._aligned_match_version(0, 0) == 0  # nothing sealed: untouched
    assert proc._aligned_match_version(1, 2) == 1  # mid-session room untouched
    assert proc._aligned_match_version(3, 2) == 3
    assert proc._aligned_match_version(None, 2) == 2
    assert proc._aligned_match_version("bad", 2) == "bad"  # non-numeric passes through


# ---------------------------------------------------------------------------
# Ranked event bank (comp-style rows per seal)
# ---------------------------------------------------------------------------

_BANK_USERS = [{"game_username": "Killer", "discord_id": "K"},
               {"game_username": "Victim", "discord_id": "V"}]


def _bank_seal(monkeypatch, fake, room_data=None):
    """Run a happy-path ranked seal with the real bank + mocked seal_match."""
    proc._comp_namemap.clear()
    monkeypatch.setattr(proc, "supabase", fake)
    monkeypatch.setattr(ranked_mod, "get_ranked_room",
                        lambda code: dict(room_data or _ROOM_DATA))
    monkeypatch.setattr(proc, "_resolve_ranked_dids", lambda names: dict(_NAME2DID))
    calls = []
    monkeypatch.setattr(ranked_mod, "seal_match",
                        lambda **kw: calls.append(kw) or {"ok": True})
    proc.seal_ranked_match("s", "K", _room(), "r1", "zw_lg")
    return calls


def test_ranked_bank_writes_kill_and_alv(monkeypatch):
    fake = _FakeDB(users=_BANK_USERS)
    calls = _bank_seal(monkeypatch, fake)
    assert len(calls) == 1  # seal still ran
    kills = [e for e in fake.ranked_events if e["etype"] == "kill"]
    alv = [e for e in fake.ranked_events if e["etype"] == "alv"]
    assert len(kills) == 1
    k = kills[0]
    assert (k["room_id"], k["match_version"]) == ("R-X", 2)
    assert k["actor_name"] == "Killer" and k["target_name"] == "Victim"
    assert k["discord_id"] == "K" and k["actor_nr"] == 1
    # dead in kill order first, survivor (winner) last; all read as dead
    assert [e["actor_name"] for e in alv] == ["Victim", "Outsider", "Killer"]
    assert all(e["alive"] is False for e in alv)
    assert {e["actor_name"]: e["discord_id"] for e in alv} == {
        "Victim": "V", "Outsider": None, "Killer": "K"}


def test_ranked_bank_idempotent(monkeypatch):
    fake = _FakeDB(users=_BANK_USERS)
    _bank_seal(monkeypatch, fake)
    n = len(fake.ranked_events)
    assert n > 0
    _bank_seal(monkeypatch, fake)  # retry: no new rows
    assert len(fake.ranked_events) == n


def test_ranked_bank_skips_spectating_host_alv(monkeypatch):
    fake = _FakeDB(users=_BANK_USERS)
    data = dict(_ROOM_DATA, host_status="hosting_only")
    calls = _bank_seal(monkeypatch, fake, room_data=data)
    assert len(calls) == 1  # feed still seals everyone else
    alv = [e for e in fake.ranked_events if e["etype"] == "alv"]
    assert "Killer" not in [e["actor_name"] for e in alv]
    kills = [e for e in fake.ranked_events if e["etype"] == "kill"]
    assert len(kills) == 1 and kills[0]["discord_id"] == "K"


def test_ranked_bank_failure_never_breaks_seal(monkeypatch):
    class _BoomDB(_FakeDB):
        def table(self, name):
            if name == "bngc_ranked_events":
                raise RuntimeError("db down")
            return super().table(name)

    fake = _BoomDB(users=_BANK_USERS)
    calls = _bank_seal(monkeypatch, fake)
    assert len(calls) == 1
    assert fake.ranked_events == []


# ---------------------------------------------------------------------------
# Banked-evidence detection (kills + placements from banked rows)
# ---------------------------------------------------------------------------

def _evs():
    return [
        {"etype": "kill", "actor_name": "Killer", "target_name": "Victim", "discord_id": "K"},
        {"etype": "kill", "actor_name": "Killer", "target_name": "Outsider", "discord_id": "K"},
        {"etype": "kill", "actor_name": "Ghost", "target_name": "Victim", "discord_id": None},
        {"etype": "alv", "actor_name": "Victim", "discord_id": "V", "alive": False},
        {"etype": "alv", "actor_name": "Outsider", "discord_id": None, "alive": False},
        {"etype": "alv", "actor_name": "Killer", "discord_id": "K", "alive": False},
    ]


def test_detect_ranked_kills_counts_and_pairs():
    kills, pairs = ranked_mod.detect_ranked_kills(_evs(), {"K", "V", "O"}, set(), _NAME2DID)
    assert kills == {"K": 2}                    # unmapped Ghost ignored
    assert pairs == [("K", "V"), ("K", "O")]    # Out via name2did


def test_detect_ranked_kills_victim_falls_back_to_killer():
    evs = [{"etype": "kill", "actor_name": "Killer", "target_name": "Nobody", "discord_id": "K"}]
    kills, pairs = ranked_mod.detect_ranked_kills(evs, {"K"}, set(), {})
    assert pairs == [("K", "K")]


def test_detect_ranked_kills_skip_and_non_roster_excluded():
    evs = [{"etype": "kill", "actor_name": "Killer", "target_name": "V", "discord_id": "K"},
           {"etype": "kill", "actor_name": "Stranger", "target_name": "V", "discord_id": "S"}]
    kills, pairs = ranked_mod.detect_ranked_kills(evs, {"K", "V"}, {"K"}, {})
    assert kills == {} and pairs == []          # skip excluded, S non-roster


def test_detect_ranked_placements_winner_cohered_p1():
    assert ranked_mod.detect_ranked_placements(_evs(), "K", {"K", "V", "O"}) == {"K": 1, "V": 2}
    # double-KO: snapshot winner (most-recent death) forced P1 even when the
    # last banked row belongs to a zone death
    assert ranked_mod.detect_ranked_placements(_evs(), "V", {"K", "V"}) == {"V": 1, "K": 2}
    # no winner (total wipe): pure alv order
    assert ranked_mod.detect_ranked_placements(_evs(), None, {"K", "V"}) == {"K": 1, "V": 2}
    # skip + unmapped never placed
    assert ranked_mod.detect_ranked_placements(_evs(), "V", {"K", "V"}, {"K"}) == {"V": 1}


def test_seal_double_ko_placements_agree_with_crown(monkeypatch):
    fake = _FakeDB(users=_BANK_USERS)
    proc._comp_namemap.clear()
    monkeypatch.setattr(proc, "supabase", fake)
    monkeypatch.setattr(ranked_mod, "get_ranked_room", lambda code: dict(_ROOM_DATA))
    monkeypatch.setattr(proc, "_resolve_ranked_dids", lambda names: dict(_NAME2DID))
    calls = []
    monkeypatch.setattr(ranked_mod, "seal_match",
                        lambda **kw: calls.append(kw) or {"ok": True})
    r = _room()
    r.players[1].is_alive = False  # zone double-KO: nobody alive
    proc.seal_ranked_match("s", "K", r, "r1", "zw_lg")
    assert calls[0]["winner_discord_id"] == "V"
    assert calls[0]["placements"] == {"V": 1, "K": 2}


# ---------------------------------------------------------------------------
# Comp-math scoring (banked evidence drives awards, loss preserved)
# ---------------------------------------------------------------------------

def _seed_banked(fake, rid="R-B2", mver=1):
    """Banked rows the legacy inputs deliberately disagree with."""
    fake.ranked_events = [
        {"room_id": rid, "match_version": mver, "etype": "kill",
         "actor_name": "Killer", "target_name": "Victim", "discord_id": "K", "alive": None},
        {"room_id": rid, "match_version": mver, "etype": "kill",
         "actor_name": "Killer", "target_name": "Victim", "discord_id": "K", "alive": None},
        {"room_id": rid, "match_version": mver, "etype": "kill",
         "actor_name": "Victim", "target_name": "Killer", "discord_id": "V", "alive": None},
        {"room_id": rid, "match_version": mver, "etype": "alv",
         "actor_name": "Victim", "discord_id": "V", "alive": False},
        {"room_id": rid, "match_version": mver, "etype": "alv",
         "actor_name": "Killer", "discord_id": "K", "alive": False},
    ]


def test_seal_banked_kills_drive_awards(monkeypatch):
    fake = _FakeDB()
    _seed_banked(fake)
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    monkeypatch.setitem(ranked_mod._points_cache, "K", (0, now))
    monkeypatch.setitem(ranked_mod._points_cache, "V", (0, now))
    # legacy inputs say 0 kills: banked evidence (K x2) must win -> 16 + 50 + P1 (10)
    res = ranked_mod.seal_match(rid="R-B2", match_version=1, gm="zw_lg",
                                winner_discord_id="K", kills={},
                                placements={"K": 1, "V": 2})
    assert res["ok"] is True
    assert res["points"]["K"] == 76


def test_seal_banked_loser_kill_keeps_flat_loss(monkeypatch):
    fake = _FakeDB()
    _seed_banked(fake)
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    monkeypatch.setitem(ranked_mod._points_cache, "K", (0, now))
    monkeypatch.setitem(ranked_mod._points_cache, "V", (0, now))
    res = ranked_mod.seal_match(rid="R-B2", match_version=1, gm="zw_lg",
                                winner_discord_id="K", kills={},
                                placements={"K": 1, "V": 2})
    assert res["ok"] is True
    # V's banked kill credits 8 + P2 (7), then the -5 non-winner loss
    assert res["points"]["V"] == 10


# ---------------------------------------------------------------------------
# GET /api/ranked/match (board + killfeed from banked rows)
# ---------------------------------------------------------------------------

def _call_match(monkeypatch, fake, params):
    import asyncio
    import json
    from server.routes import ranked_api as ra
    monkeypatch.setattr(ra, "supabase", fake)

    class _R:
        query_params = dict(params)

    resp = asyncio.run(ra.ranked_match_detail(_R()))
    assert resp.status_code in (200, 400, 500)
    return resp.status_code, json.loads(resp.body.decode())


_MATCH_USERS = [{"game_username": "Killer", "discord_username": "Killer",
                 "discord_id": "K"},
                {"game_username": "Victim", "discord_username": "Victim",
                 "discord_id": "V"}]


def _match_fake(sealed=True):
    fake = _FakeDB(users=_MATCH_USERS)
    _seed_banked(fake, rid="R-X", mver=2)
    if sealed:
        fake.results = [{"room_id": "R-X", "match_version": 2,
                         "winner_discord_id": "K",
                         "placements": {"K": 1, "V": 2},
                         "kills": {"K": 2, "V": 1},
                         "points": {"K": 76, "V": 10},
                         "points_table": {"kill": 8, "win": 50,
                                          "placement": [10, 7, 5, 3, 1]}}]
    return fake


def test_ranked_match_sealed_board(monkeypatch):
    code, body = _call_match(monkeypatch, _match_fake(sealed=True),
                             {"room_id": "R-X", "match_version": "2"})
    assert code == 200
    assert body["sealed"] is True and body["winner_discord_id"] == "K"
    assert body["placements"] == {"K": 1, "V": 2}
    assert body["points_table"] == {"kill": 8, "win": 50,
                                    "placement": [10, 7, 5, 3, 1]}
    assert [(b["discord_id"], b["points"]) for b in body["board"]] == [("K", 76), ("V", 10)]
    assert [b["name"] for b in body["board"]] == ["Killer", "Victim"]
    assert len(body["killfeed"]) == 5


def test_ranked_match_open_no_loss(monkeypatch):
    code, body = _call_match(monkeypatch, _match_fake(sealed=False),
                             {"room_id": "R-X", "match_version": "2"})
    assert code == 200
    assert body["sealed"] is False and body["winner_discord_id"] is None
    # live board crowns the current last-alive (comp-match semantics);
    # the -5 loss still waits for the seal
    assert [(b["discord_id"], b["points"]) for b in body["board"]] == [("K", 76), ("V", 15)]


def test_ranked_match_empty_and_bad_params(monkeypatch):
    code, body = _call_match(monkeypatch, _FakeDB(),
                             {"room_id": "R-Z", "match_version": "9"})
    assert code == 200
    assert body["sealed"] is False and body["board"] == [] and body["killfeed"] == []
    code, body = _call_match(monkeypatch, _FakeDB(), {"room_id": "R-Z"})
    assert code == 400
    code, body = _call_match(monkeypatch, _FakeDB(), {"room_id": "", "match_version": "1"})
    assert code == 400


# ---------------------------------------------------------------------------
# Ranked room lifecycle (host-only backout / end)
# ---------------------------------------------------------------------------

def _call_room(monkeypatch, fake, path, body):
    import asyncio
    import json
    from server.routes import ranked_api as ra
    monkeypatch.setattr(ra, "supabase", fake)

    class _R:
        async def json(self):
            return dict(body)

    fn = ra.ranked_room_backout if path == "backout" else ra.ranked_room_end
    resp = asyncio.run(fn(_R()))
    return resp.status_code, json.loads(resp.body.decode())


def _live_room_fake():
    fake = _FakeDB()
    fake.rooms = {"R-X": {"id": "R-X", "short_code": "ABCDE",
                          "status": "live", "host_discord_id": "K"}}
    return fake


def test_ranked_backout_live_to_lobby(monkeypatch):
    fake = _live_room_fake()
    code, body = _call_room(monkeypatch, fake, "backout",
                            {"room_id": "R-X", "discord_id": "K"})
    assert code == 200 and body["status"] == "lobby"
    assert fake.rooms["R-X"]["status"] == "lobby"


def test_ranked_backout_rejects(monkeypatch):
    fake = _live_room_fake()
    code, _ = _call_room(monkeypatch, fake, "backout",
                         {"room_id": "R-X", "discord_id": "NOTHOST"})
    assert code == 403
    code, _ = _call_room(monkeypatch, fake, "backout",
                         {"room_id": "NOPE", "discord_id": "K"})
    assert code == 404
    fake.rooms["R-X"]["status"] = "lobby"
    code, body = _call_room(monkeypatch, fake, "backout",
                            {"room_id": "R-X", "discord_id": "K"})
    assert code == 400  # not live


def test_ranked_end_live_and_lobby(monkeypatch):
    fake = _live_room_fake()
    code, body = _call_room(monkeypatch, fake, "end",
                            {"room_id": "R-X", "discord_id": "K"})
    assert code == 200 and body["status"] == "ended"
    fake.rooms["R-X"]["status"] = "lobby"
    code, _ = _call_room(monkeypatch, fake, "end",
                         {"room_id": "R-X", "discord_id": "K"})
    assert code == 200
    code, _ = _call_room(monkeypatch, fake, "end",
                         {"room_id": "R-X", "discord_id": "K"})
    assert code == 400  # already ended
    code, _ = _call_room(monkeypatch, fake, "end",
                         {"room_id": "R-X", "discord_id": "NOTHOST"})
    assert code == 403


# ---------------------------------------------------------------------------
# POST /api/ranked/match/recalc + /winner (differential ledger fix)
# ---------------------------------------------------------------------------

def _call_post(monkeypatch, fake, fn_name, body):
    import asyncio
    import json
    from server.routes import ranked_api as ra
    monkeypatch.setattr(ra, "supabase", fake)
    _use_fake_for_ranked(monkeypatch, fake)
    ranked_mod._points_cache.clear()

    class _R:
        async def json(self):
            return dict(body)

    resp = asyncio.run(getattr(ra, fn_name)(_R()))
    return resp.status_code, json.loads(resp.body.decode())


def _recalc_fake():
    fake = _FakeDB(users=_MATCH_USERS)
    fake.rooms = {"R-X": {"id": "R-X", "short_code": "ABCDE",
                          "status": "live", "host_discord_id": "K"}}
    fake.points = {"K": 500, "V": 500}
    # stale seal: K credited for 1 kill (8+50=58) but banked 2 -> 66 on recalc
    fake.results = [{"room_id": "R-X", "match_version": 2,
                     "winner_discord_id": "K",
                     "placements": {"K": 1, "V": 2},
                     "kills": {"K": 1, "V": 1},
                     "points": {"K": 58, "V": 3}}]
    _seed_banked(fake, rid="R-X", mver=2)
    return fake


def test_ranked_recalc_pays_diff_only(monkeypatch):
    fake = _recalc_fake()
    code, body = _call_post(monkeypatch, fake, "ranked_match_recalc",
                            {"room_id": "R-X", "match_version": 2,
                             "discord_id": "K"})
    assert code == 200 and body["ok"] is True
    assert body["points"] == {"K": 66, "V": 3}
    assert body["diffs"] == {"K": 8}           # +8 only, V unchanged
    assert fake.points == {"K": 508, "V": 500}  # ledger moved by the diff
    assert fake.results[0]["points"] == {"K": 66, "V": 3}
    assert fake.results[0]["kills"] == {"K": 2, "V": 1}
    # second run diffs to zero (never double-pays)
    code, body = _call_post(monkeypatch, fake, "ranked_match_recalc",
                            {"room_id": "R-X", "match_version": 2,
                             "discord_id": "K"})
    assert code == 200 and body["diffs"] == {}
    assert fake.points == {"K": 508, "V": 500}


def test_ranked_recalc_rejects(monkeypatch):
    fake = _recalc_fake()
    code, _ = _call_post(monkeypatch, fake, "ranked_match_recalc",
                         {"room_id": "R-X", "match_version": 9,
                          "discord_id": "K"})
    assert code == 404  # not sealed
    code, _ = _call_post(monkeypatch, fake, "ranked_match_recalc",
                         {"room_id": "R-X", "match_version": 2,
                          "discord_id": "NOTHOST"})
    assert code == 403


def test_ranked_winner_override_flips_crown(monkeypatch):
    fake = _recalc_fake()
    code, body = _call_post(monkeypatch, fake, "ranked_match_winner",
                            {"room_id": "R-X", "match_version": 2,
                             "discord_id": "K", "winner_discord_id": "V"})
    assert code == 200 and body["winner_discord_id"] == "V"
    # legacy row (no stored table): flat recompute - V wins (8 + 50),
    # K takes the loss (16 - 5)
    assert body["points"] == {"K": 11, "V": 58}
    assert fake.results[0]["winner_discord_id"] == "V"
    assert fake.results[0]["placements"] == {"K": 2, "V": 1}  # crown swapped, order kept
    code, _ = _call_post(monkeypatch, fake, "ranked_match_winner",
                         {"room_id": "NOPE", "match_version": 2,
                          "discord_id": "K", "winner_discord_id": "V"})
    assert code == 404


# ---------------------------------------------------------------------------
# live_match_board_sync (stats-poll freshness)
# ---------------------------------------------------------------------------

def _use_fake_sb(monkeypatch, fake):
    monkeypatch.setattr(ranked_mod, "_sb", lambda: fake)


def test_live_board_open_crowns_leader_no_loss(monkeypatch):
    fake = _FakeDB()
    _seed_banked(fake, rid="R-X", mver=2)
    _use_fake_sb(monkeypatch, fake)
    out = ranked_mod.live_match_board_sync("R-X", 2)
    assert out["sealed"] is False and out["winner_discord_id"] is None
    assert [(b["discord_id"], b["points"]) for b in out["board"]] == [("K", 76), ("V", 15)]


def test_live_board_sealed_keeps_loss(monkeypatch):
    fake = _FakeDB()
    _seed_banked(fake, rid="R-X", mver=2)
    fake.results = [{"room_id": "R-X", "match_version": 2,
                     "winner_discord_id": "K", "placements": {"K": 1, "V": 2},
                     "points_table": {"kill": 8, "win": 50,
                                      "placement": [10, 7, 5, 3, 1]}}]
    _use_fake_sb(monkeypatch, fake)
    out = ranked_mod.live_match_board_sync("R-X", 2)
    assert out["sealed"] is True and out["winner_discord_id"] == "K"
    assert [(b["discord_id"], b["points"]) for b in out["board"]] == [("K", 76), ("V", 10)]


def test_live_board_legacy_row_recomputes_flat(monkeypatch):
    # pre-purse seal (no stored table): display matches what was awarded
    fake = _FakeDB()
    _seed_banked(fake, rid="R-X", mver=2)
    fake.results = [{"room_id": "R-X", "match_version": 2,
                     "winner_discord_id": "K", "placements": {"K": 1, "V": 2}}]
    _use_fake_sb(monkeypatch, fake)
    out = ranked_mod.live_match_board_sync("R-X", 2)
    assert out["sealed"] is True
    # flat legacy table over 2 banked K-kills: 16 + 50 / 8 - 5
    assert [(b["discord_id"], b["points"]) for b in out["board"]] == [("K", 66), ("V", 3)]


def test_live_board_empty_and_top_n(monkeypatch):
    fake = _FakeDB()
    _use_fake_sb(monkeypatch, fake)
    assert ranked_mod.live_match_board_sync("R-X", 2)["board"] == []
    assert ranked_mod.live_match_board_sync("", 0)["board"] == []
    _seed_banked(fake, rid="R-X", mver=2)
    out = ranked_mod.live_match_board_sync("R-X", 2, top_n=1)
    assert [b["discord_id"] for b in out["board"]] == ["K"]
    # no DB at all degrades to empty, never raises
    monkeypatch.setattr(ranked_mod, "_sb", lambda: None)
    assert ranked_mod.live_match_board_sync("R-X", 2)["board"] == []


# ---------------------------------------------------------------------------
# Lobby-strength static table (avg-based kill/win/placement values)
# ---------------------------------------------------------------------------

def test_scaled_table_for():
    assert ranked_mod.scaled_table_for(0) == {"kill": 8, "win": 50,
                                              "placement": [10, 7, 5, 3, 1]}
    assert ranked_mod.scaled_table_for(None) == ranked_mod.scaled_table_for(0)
    assert ranked_mod.scaled_table_for(-99) == ranked_mod.scaled_table_for(0)
    # Champion avg (4000): exactly double
    assert ranked_mod.scaled_table_for(4000) == {"kill": 16, "win": 100,
                                                 "placement": [20, 14, 10, 6, 2]}
    t = ranked_mod.scaled_table_for(1000)
    assert t == {"kill": 10, "win": 62, "placement": [12, 9, 6, 4, 1]}


def test_dynamic_ranked_economy_is_bounded():
    # The feature flag controls rollout; these pure functions pin the new
    # economy independently of the legacy static-table compatibility path.
    assert ranked_mod.kill_delta(0, 4000, 0) == 12
    assert ranked_mod.kill_delta(4000, 0, 0) == 4
    assert 35 <= ranked_mod.win_delta(7500, 16) <= 95
    assert ranked_mod.win_delta(7500, 16) < 96
    assert ranked_mod.placement_delta(1, 0, 8) <= 22
    assert ranked_mod.placement_delta(1, 999999, 99) == 22


def test_dynamic_seal_freezes_per_kill_awards(monkeypatch):
    fake = _FakeDB()
    fake.points = {"K": 0, "V": 4000}
    _use_fake_for_ranked(monkeypatch, fake)
    ranked_mod._points_cache.clear()
    monkeypatch.setattr(ranked_mod, "RANKED_DYNAMIC", True)
    res = ranked_mod.seal_match(rid="R-D", match_version=1, gm="zw_lg",
                                winner_discord_id="K", kills={"K": 1},
                                placements={"K": 1, "V": 2},
                                kill_events=[("K", "V")], players_count=2)
    assert res["ok"] is True
    table = fake.results[0]["points_table"]
    assert table["mode"] == "dynamic-v1"
    assert table["kill_awards"] == [{"killer": "K", "victim": "V", "nth": 1, "points": 13}]


def test_dynamic_rows_exclude_a_removed_player():
    table = ranked_mod.dynamic_table_for(0, 3, [
        {"killer": "K", "victim": "V", "points": 8},
        {"killer": "L", "victim": "V", "points": 8},
    ])
    events = [
        {"etype": "kill", "discord_id": "K", "actor_name": "K"},
        {"etype": "kill", "discord_id": "L", "actor_name": "L"},
        {"etype": "alv", "discord_id": "V", "alive": False},
    ]
    rows = ranked_mod._ranked_rows(events, table, "K", {"K": 1, "V": 2},
                                   table["kill_awards"], {"L"})
    assert [r["discord_id"] for r in rows] == ["K", "V"]


def test_kill_taper_schedule():
    assert ranked_mod.taper_for(1) == 1.0
    assert ranked_mod.taper_for(2) == 1.0
    assert ranked_mod.taper_for(3) == 0.8
    assert ranked_mod.taper_for(5) == 0.5
    assert ranked_mod.taper_for(6) == 0.5
    assert ranked_mod.taper_for(99) == 0.5
    assert ranked_mod.taper_for(0) == 1.0
    assert ranked_mod.taper_for(None) == 1.0
    assert ranked_mod.taper_for("junk") == 1.0


def test_kill_awards_taper_within_match():
    awards = ranked_mod._kill_awards_for(
        [("K", "V")] * 4, {"K": 0, "V": 0}, 0)
    assert [a["nth"] for a in awards] == [1, 2, 3, 4]
    base = ranked_mod.kill_delta(0, 0, 0)
    assert awards[0]["points"] == base
    assert awards[1]["points"] == base
    assert awards[2]["points"] == max(1, round(base * 0.8))
    assert awards[3]["points"] == max(1, round(base * 0.65))
    # per-killer counting: a second killer starts at full again
    mixed = ranked_mod._kill_awards_for(
        [("K", "V"), ("L", "V"), ("K", "V")], {"K": 0, "L": 0, "V": 0}, 0)
    assert [a["nth"] for a in mixed] == [1, 1, 2]


def test_tier_index_and_scaled_loss():
    assert ranked_mod.tier_index_for(0) == 0
    assert ranked_mod.tier_index_for(199) == 0
    assert ranked_mod.tier_index_for(200) == 1
    assert ranked_mod.tier_index_for(7500) == 9
    assert ranked_mod.tier_index_for(99999) == 9
    assert ranked_mod.tier_index_for(None) == 0
    assert ranked_mod.loss_for(0) == -5
    assert ranked_mod.loss_for(199) == -5
    assert ranked_mod.loss_for(200) == -8
    assert ranked_mod.loss_for(1000) == -14
    assert ranked_mod.loss_for(7500) == -32
    assert ranked_mod.loss_for(99999) == -32


def test_dynamic_table_freezes_loss_awards():
    t = ranked_mod.dynamic_table_for(0, 3, [], {"K": -5, "V": -8})
    assert t["loss_awards"] == {"K": -5, "V": -8}
    # default: empty map, legacy readers unaffected
    assert ranked_mod.dynamic_table_for(0, 3)["loss_awards"] == {}


def test_dynamic_seal_freezes_tiered_loss_and_taper(monkeypatch):
    fake = _FakeDB()
    fake.points = {"K": 0, "V": 1000}  # Unranked killer, Gold victim
    _use_fake_for_ranked(monkeypatch, fake)
    ranked_mod._points_cache.clear()
    monkeypatch.setattr(ranked_mod, "RANKED_DYNAMIC", True)
    res = ranked_mod.seal_match(rid="R-D", match_version=1, gm="zw_lg",
                                winner_discord_id="K", kills={"K": 3},
                                placements={"K": 1, "V": 2},
                                kill_events=[("K", "V")] * 3, players_count=2)
    assert res["ok"] is True
    row = fake.results[0]
    table = row["points_table"]
    # tier-scaled losses frozen for everyone involved (winner included,
    # so a later crown swap finds the dethroned loss)
    assert table["loss_awards"] == {"K": -5, "V": -14}
    assert [a["nth"] for a in table["kill_awards"]] == [1, 2, 3]
    base = ranked_mod.kill_delta(0, 1000, 500.0)
    assert [a["points"] for a in table["kill_awards"]] == [
        base, base, max(1, round(base * 0.8))]
    # final ledger: K = tapered kills + win + P1, V = P2 + frozen -14
    win, place = table["win"], table["placement"]
    assert row["points"]["K"] == sum(a["points"] for a in table["kill_awards"]) + win + place[0]
    assert row["points"]["V"] == place[1] - 14


def test_recalc_replays_frozen_loss_and_crown_swap(monkeypatch):
    fake = _FakeDB()
    fake.rooms = {"R-X": {"id": "R-X", "short_code": "ABCDE",
                          "status": "live", "host_discord_id": "K"}}
    fake.points = {"K": 500, "V": 500}
    table = {"mode": "dynamic-v1", "avg_points": 500.0, "players": 2,
             "kill": None, "win": 60, "placement": [15, 10, 7, 4, 2],
             "kill_awards": [{"killer": "K", "victim": "V", "nth": 1, "points": 12}],
             "loss_awards": {"K": -5, "V": -20}}
    fake.results = [{"room_id": "R-X", "match_version": 2,
                     "winner_discord_id": "K",
                     "placements": {"K": 1, "V": 2},
                     "kills": {"K": 1, "V": 0},
                     "points": {"K": 0, "V": 0},  # stale: forces full recompute
                     "points_table": dict(table)}]
    _seed_banked(fake, rid="R-X", mver=2)
    code, body = _call_post(monkeypatch, fake, "ranked_match_recalc",
                            {"room_id": "R-X", "match_version": 2,
                             "discord_id": "K"})
    assert code == 200
    # K: 12 (kill) + 60 (win) + 15 (P1), no loss as winner.
    # V: banked alv death -> P2 (10) + frozen -20.
    assert body["points"] == {"K": 87, "V": -10}
    # second recalc diffs to zero (frozen replay, never double-pays)
    code, body2 = _call_post(monkeypatch, fake, "ranked_match_recalc",
                             {"room_id": "R-X", "match_version": 2,
                              "discord_id": "K"})
    assert code == 200 and body2["diffs"] == {}
    # crown swap: V wins -> V pays 0 (its banked kill has no frozen award
    # row, so 0 + win + P1), K pays its frozen -5 on top of P2 + award.
    code, body3 = _call_post(monkeypatch, fake, "ranked_match_winner",
                             {"room_id": "R-X", "match_version": 2,
                              "discord_id": "K", "winner_discord_id": "V"})
    assert code == 200
    assert body3["points"]["V"] == 60 + 15
    assert body3["points"]["K"] == 12 + 10 - 5


def test_table_of_result():
    t = {"kill": 16, "win": 100, "placement": [20, 14, 10, 6, 2]}
    assert ranked_mod.table_of_result({"points_table": t}) == t
    # legacy rows (no stored table): flat recompute, no purse
    assert ranked_mod.table_of_result({}) == {"kill": 8, "win": 50, "placement": []}
    assert ranked_mod.table_of_result(None) == {"kill": 8, "win": 50, "placement": []}
    assert ranked_mod.table_of_result({"points_table": "junk"}) == {
        "kill": 8, "win": 50, "placement": []}


def test_lobby_avg_points_sync(monkeypatch):
    fake = _FakeDB()
    fake.points = {"K": 750, "V": 100}
    _use_fake_for_ranked(monkeypatch, fake)
    ranked_mod._points_cache.clear()
    assert ranked_mod.lobby_avg_points_sync(["K", "V"]) == 425.0
    assert ranked_mod.lobby_avg_points_sync([]) == 0.0
    # no DB and cold cache degrades to 0.0, never raises
    ranked_mod._points_cache.clear()
    monkeypatch.setattr(ranked_mod, "_sb", lambda: None)
    assert ranked_mod.lobby_avg_points_sync(["K"]) == 0.0


def test_seal_scaled_by_lobby_avg(monkeypatch):
    fake = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    monkeypatch.setitem(ranked_mod._points_cache, "K", (0, now))
    monkeypatch.setitem(ranked_mod._points_cache, "V", (0, now))
    # Champion lobby (avg 4000): double table -> K = 2 kills (32) + win (100) + P1 (20)
    res = ranked_mod.seal_match(rid="R-T", match_version=1, gm="zw_lg",
                                winner_discord_id="K", kills={"K": 2},
                                placements={"K": 1, "V": 2},
                                kill_events=[("K", "V"), ("K", "V")],
                                avg_points=4000)
    assert res["ok"] is True
    assert res["points"] == {"K": 152, "V": 9}
    row = fake.results[0]
    assert row["points_table"] == {"kill": 16, "win": 100,
                                   "placement": [20, 14, 10, 6, 2]}


def test_recalc_uses_stored_table(monkeypatch):
    fake = _FakeDB()
    fake.rooms = {"R-X": {"id": "R-X", "short_code": "ABCDE",
                          "status": "live", "host_discord_id": "K"}}
    fake.points = {"K": 500, "V": 500}
    # stale seal under a stored double table: recompute honors it, not base
    fake.results = [{"room_id": "R-X", "match_version": 2,
                     "winner_discord_id": "K",
                     "placements": {"K": 1, "V": 2},
                     "kills": {"K": 1, "V": 1},
                     "points": {"K": 58, "V": 3},
                     "points_table": {"kill": 16, "win": 100,
                                      "placement": [20, 14, 10, 6, 2]}}]
    _seed_banked(fake, rid="R-X", mver=2)
    code, body = _call_post(monkeypatch, fake, "ranked_match_recalc",
                            {"room_id": "R-X", "match_version": 2,
                             "discord_id": "K"})
    assert code == 200
    # K: 2 banked kills (32) + win (100) + P1 (20) = 152; V: 16 + P2 (14) - 5 = 25
    assert body["points"] == {"K": 152, "V": 25}
    assert body["diffs"] == {"K": 94, "V": 22}
    assert fake.points == {"K": 594, "V": 522}


# ---------------------------------------------------------------------------
# GET /api/ranked/table (optimistic client values)
# ---------------------------------------------------------------------------

def _call_table_get(monkeypatch, fake, fn_name, params):
    import asyncio
    import json
    from server.routes import ranked_api as ra
    monkeypatch.setattr(ra, "supabase", fake)

    class _R:
        query_params = dict(params)

    resp = asyncio.run(getattr(ra, fn_name)(_R()))
    return resp.status_code, json.loads(resp.body.decode())


def _table_rooms_fake():
    fake = _FakeDB(users=_MATCH_USERS)
    fake.rooms = {
        "R-X": {"id": "R-X", "short_code": "ABCDE", "status": "live",
                "host_discord_id": "K",
                "roster": [{"discord_id": "K"}, {"discord_id": "V"}]},
    }
    fake.points = {"K": 750, "V": 100}
    return fake


def test_ranked_table_scaled_by_lobby(monkeypatch):
    fake = _table_rooms_fake()
    ranked_mod._points_cache.clear()
    _use_fake_for_ranked(monkeypatch, fake)
    code, body = _call_table_get(monkeypatch, fake, "ranked_table",
                           {"room_id": "R-X"})
    assert code == 200
    # roster avg (750 + 100) / 2 = 425 -> factor 1.10625
    assert body["avg_points"] == 425.0
    assert body["table"] == {"kill": 9, "win": 55,
                             "placement": [11, 8, 6, 3, 1]}
    assert body["loss"] == -5
    assert body["short_code"] == "ABCDE"


def test_ranked_table_missing_and_bad(monkeypatch):
    code, _ = _call_table_get(monkeypatch, _FakeDB(), "ranked_table",
                        {"room_id": "NOPE"})
    assert code == 404
    code, _ = _call_table_get(monkeypatch, _FakeDB(), "ranked_table", {})
    assert code == 400


# ---------------------------------------------------------------------------
# GET /api/ranked/room/by-code (join detection)
# ---------------------------------------------------------------------------

def _bycode_fake():
    fake = _FakeDB()
    fake.rooms = {
        "R-X": {"id": "R-X", "short_code": "ABCDE", "status": "live"},
        "R-Y": {"id": "R-Y", "short_code": "FGHIJ", "status": "ended"},
        "R-Z1": {"id": "R-Z1", "short_code": "DUP11", "status": "live"},
        "R-Z2": {"id": "R-Z2", "short_code": "DUP11", "status": "lobby"},
    }
    return fake


def test_ranked_room_by_code(monkeypatch):
    import asyncio
    import json
    from server.routes import ranked_api as ra
    fake = _bycode_fake()
    monkeypatch.setattr(ra, "supabase", fake)

    class _R:
        def __init__(self, params):
            self.query_params = dict(params)

    def _get(params):
        return json.loads(asyncio.run(ra.ranked_room_by_code(_R(params))).body.decode())

    assert _get({"code": "abcde"}) == {
        "room": {"id": "R-X", "short_code": "ABCDE", "status": "live"}}
    assert _get({"code": "NOPE1"}) == {"room": None}   # unknown
    assert _get({"code": "FGHIJ"}) == {"room": None}   # ended
    assert _get({"code": "DUP11"}) == {"room": None}   # ambiguous
    assert _get({"code": "ab"}) == {"room": None}      # malformed
    assert _get({}) == {"room": None}                  # missing
