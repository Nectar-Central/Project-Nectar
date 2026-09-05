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
# seal_match kill_events exact math
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
    # same-skill 1st-place kill: 25*0.5*2.0=25 + win 60 = 85
    assert res["points"] == {"K": 85}
    assert fake.points["K"] == 585
    row = fake.results[0]
    assert (row["room_id"], row["match_version"], row["winner_discord_id"]) == ("R-X", 2, "K")
    assert row["placements"] == {"K": 1, "V": 2}  # stored for the toast + review


def test_seal_match_upset_floor(monkeypatch):
    fake = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    monkeypatch.setitem(ranked_mod._points_cache, "K", (200, now))
    monkeypatch.setitem(ranked_mod._points_cache, "V", (2000, now))
    res = ranked_mod.seal_match(rid="R-X", match_version=1, gm="zw_lg",
                                winner_discord_id="V", kills={"K": 1},
                                placements={"K": 4, "V": 1},
                                kill_events=[("K", "V")])
    assert res["ok"] is True
    assert res["points"]["K"] == 1  # massive gap -> floor
    assert res["points"]["V"] == 60  # winner bonus, zero kills


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
    assert r.ranked_warnings == ["Outsider"]         # off-roster flagged


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
