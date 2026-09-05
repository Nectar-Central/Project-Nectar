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

    def in_(self, col, vals):
        self.filters.append((col, list(vals)))
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
        self.calls = []

    def table(self, name):
        return _FakeQ(self, name)

    def _run(self, q):
        self.calls.append((q.table, q.op, q.payload, list(q.filters)))
        if q.table == "bngc_users" and q.op == "select":
            names = None
            for col, val in q.filters:
                if col == "game_username":
                    names = set(val if isinstance(val, list) else [val])
            rows = [r for r in self.users if names is None or r.get("game_username") in names]
            return _FakeRes(rows)
        if q.table == "ranked_results" and q.op == "select":
            rows = self.results
            for col, val in q.filters:
                rows = [r for r in rows if str(r.get(col)) == str(val)]
            return _FakeRes(rows)
        if q.table == "ranked_results" and q.op in ("upsert", "insert"):
            self.results.append(dict(q.payload))
            return _FakeRes([q.payload])
        if q.table == "ranked_points" and q.op == "upsert":
            self.points[str(q.payload["discord_id"])] = int(q.payload["points"])
            return _FakeRes([q.payload])
        if q.table == "ranked_rooms" and q.op == "update":
            for rid, fields in self.rooms.items():
                hit = all(str(fields.get(c)) == str(v) for c, v in q.filters if c == "id")
                if hit:
                    fields.update(q.payload)
            return _FakeRes([])
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
    assert [ranked_mod.placement_reward(p) for p in (1, 2, 3, 4, 5, 6, 99)] == [50, 30, 20, 10, 5, 0, 0]
    # lobby-size cap: 2 players => only 1st/2nd pay
    assert ranked_mod.placement_reward(2, 2) == 30
    assert ranked_mod.placement_reward(3, 2) == 0
    assert ranked_mod.placement_reward("bad") == 0


def test_defeat_penalty_matrix():
    assert ranked_mod.defeat_penalty(2000, 500) == -15    # killed by lower
    assert ranked_mod.defeat_penalty(500, 2000) == -5     # killed by higher
    assert ranked_mod.defeat_penalty(500, 500) == -5      # equal
    assert ranked_mod.defeat_penalty(500, None) == -5     # zone/unknown
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
    # static: K = 1 kill (8) + 1st (50) = 58; V = 2nd (30), no death recorded
    assert res["points"] == {"K": 58, "V": 30}
    assert fake.points["K"] == 558
    assert fake.points["V"] == 530
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
    # static: K = 1 kill (8) + 4th (10) = 18 (no death)
    assert res["points"]["K"] == 18
    # V = 1st (50) + died to a lower-points killer (-15) = 35
    assert res["points"]["V"] == 35


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
    # A: 2 kills (16) + 1st (50); B: 2nd (30) + peer death (-5); C: 3rd (20) + zone (-5)
    assert res["points"] == {"A": 66, "B": 25, "C": 15, "D": 10}


def test_seal_match_placements_capped_at_lobby_size(monkeypatch):
    fake = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    monkeypatch.setitem(ranked_mod._points_cache, "K", (0, now))
    monkeypatch.setitem(ranked_mod._points_cache, "V", (0, now))
    # 2-player lobby: a phantom 3rd in the placements map pays 0
    res = ranked_mod.seal_match(rid="R-C", match_version=1, gm="zw_lg",
                                winner_discord_id="K", kills={},
                                placements={"K": 1, "V": 2, "X": 3},
                                players_count=2)
    assert res["ok"] is True
    assert res["points"] == {"K": 50, "V": 30, "X": 0}
    assert "X" not in fake.points  # zero delta => no bump written


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


def test_stale_host_pauses_seal(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(proc, "supabase", fake)
    calls = []
    _patch_ranked(monkeypatch, calls)
    r = _room()
    r.last_packet_at = time.time() - 120
    proc.seal_ranked_match("s", "K", r, "r1", "zw_lg")
    assert calls == []


def test_already_sealed_skips(monkeypatch):
    fake = _FakeDB(results=[{"room_id": "R-X", "match_version": 2}])
    monkeypatch.setattr(proc, "supabase", fake)
    calls = []
    _patch_ranked(monkeypatch, calls)
    proc.seal_ranked_match("s", "K", _room(), "r1", "zw_lg")
    assert calls == []


def test_happy_path_placements_warnings_events(monkeypatch):
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
    assert kw["placements"] == {"K": 1, "V": 2}     # alive=1st, last victim=2nd
    assert kw["kill_events"] == [("K", "V")]
    assert kw["kills"] == {"K": 1}
    assert kw["death_killers"] == {"V": "K"}         # feed attribution
    assert kw["players_count"] == 2                  # roster size caps placements
    assert r.ranked_warnings == ["Outsider"]         # off-roster flagged


def test_zone_death_takes_next_place(monkeypatch):
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
    assert kw["placements"] == {"K": 1, "V": 2}     # zone death takes next place
    assert kw["death_killers"] == {"V": None}       # None killer => zone minus
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
    assert kw["placements"] == {"V": 1, "K": 2}  # winner takes 1st, rest shift down


def test_room_end_flips_at_max(monkeypatch):
    # v2 unsealed, five other versions already sealed -> this seal hits max 5
    fake = _FakeDB(results=[{"room_id": "R-X", "match_version": v} for v in (1, 3, 4, 5, 6)])
    fake.rooms["R-X"] = {"id": "R-X", "status": "live"}
    monkeypatch.setattr(proc, "supabase", fake)
    calls = []
    _patch_ranked(monkeypatch, calls)
    proc.seal_ranked_match("s", "K", _room(), "r1", "zw_lg")
    assert len(calls) == 1
    assert fake.rooms["R-X"]["status"] == "ended"
