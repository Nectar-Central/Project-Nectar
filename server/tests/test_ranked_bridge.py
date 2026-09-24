"""Ranked seal rails: database-enforced idempotency contracts (v250).

Ports the comp bridge test discipline (test_comp_bridge_alv.py,
test_ranked_tourney_bridge.py) to the ranked-rooms seal path:

  - one ranked_results row per (room_id, match_version), ever
  - a re-sealed (rid, version) is a no-op (duplicate=True, zero bumps)
  - banked bngc_ranked_events rows drive winner/placement/points exactly
    like the comp alv snapshot does (reverse death order, last alive = P1)
  - the server seal READS the event bank but never WRITES it (the host
    tracker is the single events writer; v250 seal_id keys dedupe there)
  - the v250 migration file carries the three rail indexes (cheap pin so
    the constraints cannot silently disappear from the repo)

DB is faked (chainable stub, same pattern as test_ranked_seal.py). No network.
"""
import time
from pathlib import Path

import pytest

import server.scripts.processor as proc
from server.lib import ranked as ranked_mod
from server.lib.photon_engine.models import KillInfo, Player, RoomView


# ---------------------------------------------------------------------------
# Fake Supabase (chainable stub — same shape as test_ranked_seal.py)
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

    def neq(self, col, val):
        self.filters.append(("neq", col, val))
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
        self.ranked_events = []       # bngc_ranked_events rows
        self.calls = []

    def table(self, name):
        return _FakeQ(self, name)

    def _run(self, q):
        self.calls.append((q.table, q.op, q.payload, list(q.filters)))
        if q.table == "bngc_users" and q.op == "select":
            rows = list(self.users)
            for f in q.filters:
                if len(f) == 2:
                    col, val = f
                    if isinstance(val, list):
                        rows = [r for r in rows if str(r.get(col)) in {str(v) for v in val}]
                    else:
                        rows = [r for r in rows if str(r.get(col)) == str(val)]
            return _FakeRes(rows)
        if q.table == "ranked_results" and q.op == "select":
            rows = self.results
            for f in q.filters:
                if len(f) == 3:
                    _, col, val = f
                    rows = [r for r in rows if str(r.get(col)) != str(val)]
                else:
                    col, val = f
                    if isinstance(val, list):
                        rows = [r for r in rows if str(r.get(col)) in {str(v) for v in val}]
                    else:
                        rows = [r for r in rows if str(r.get(col)) == str(val)]
            return _FakeRes(rows)
        if q.table == "ranked_results" and q.op in ("upsert", "insert"):
            row = dict(q.payload)
            key = (str(row.get("room_id")), int(row.get("match_version", 0)))
            # the real table is UNIQUE (room_id, match_version) — the v250
            # rail. The fake enforces it so double-award bugs show here.
            for i, existing in enumerate(self.results):
                if (str(existing.get("room_id")), int(existing.get("match_version", 0))) == key:
                    existing.update(row)
                    return _FakeRes([existing])
            self.results.append(row)
            return _FakeRes([row])
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
        if q.table == "bngc_ranked_events" and q.op == "select":
            rows = list(self.ranked_events)
            for f in q.filters:
                if len(f) == 3:
                    _, col, val = f
                    rows = [r for r in rows if str(r.get(col)) != str(val)]
                else:
                    col, val = f
                    rows = [r for r in rows if str(r.get(col)) == str(val)]
            return _FakeRes(rows)
        if q.table == "bngc_ranked_events" and q.op == "insert":
            payloads = q.payload if isinstance(q.payload, list) else [q.payload]
            for p in payloads:
                self.ranked_events.append(dict(p))
            return _FakeRes(payloads)
        return _FakeRes([])


def _use_fake_for_ranked(monkeypatch, fake):
    """Point ranked._sb() and processor.supabase at the fake."""
    class _Adapter:
        def table(self, name):
            return _FakeQ(fake, name)

    monkeypatch.setattr(ranked_mod, "_sb", lambda: _Adapter())
    monkeypatch.setattr(proc, "supabase", fake)


@pytest.fixture(autouse=True)
def _static_economy(monkeypatch):
    """Same pin as test_ranked_seal.py: static table math (8/50/-5)."""
    monkeypatch.setattr(ranked_mod, "RANKED_DYNAMIC", False)


# ---------------------------------------------------------------------------
# Seal fixtures
# ---------------------------------------------------------------------------

_ROOM_DATA = {"id": "R-X", "short_code": "ABCDE", "host_discord_id": "K",
              "status": "live", "max_matches": 5, "gm": "zw_lg",
              "roster": [{"discord_id": "K"}, {"discord_id": "V"}, {"discord_id": "C"}]}
_NAME2DID = {"Killer": "K", "Victim": "V", "Third": "C"}


def _patch_host(monkeypatch, did="K"):
    monkeypatch.setattr(ranked_mod, "get_ranked_room", lambda code: dict(_ROOM_DATA))
    monkeypatch.setattr(proc, "_resolve_ranked_dids", lambda names: dict(_NAME2DID))
    monkeypatch.setattr(ranked_mod, "_points_cache", {})


def _room():
    r = RoomView(room_id="r1")
    r.short_code = "ABCDE"
    r.match_version = 2
    r.last_packet_at = time.time()
    r.players[1] = Player(actor_nr=1, username="Killer", is_alive=True)
    r.players[2] = Player(actor_nr=2, username="Victim", is_alive=False)
    r.players[3] = Player(actor_nr=3, username="Third", is_alive=False)
    r.recent_kills.append(KillInfo(instigator="Killer", instigated="Victim", event_type="killed"))
    r.recent_kills.append(KillInfo(instigator="Killer", instigated="Third", event_type="killed"))
    return r


# ---------------------------------------------------------------------------
# 1) happy-path seal banks exactly one result row
# ---------------------------------------------------------------------------

def test_host_seal_banks_single_result_row(monkeypatch):
    fake = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake)
    _patch_host(monkeypatch)
    proc.seal_ranked_match("s", "K", _room(), "r1", "zw_lg")
    rows = [r for r in fake.results if r.get("room_id") == "R-X"]
    assert len(rows) == 1
    assert int(rows[0]["match_version"]) == 2
    assert rows[0]["winner_discord_id"] == "K"


# ---------------------------------------------------------------------------
# 2) re-sealing the same (rid, version) never double-awards
# ---------------------------------------------------------------------------

def test_engine_reseal_is_noop(monkeypatch):
    fake = _FakeDB()
    _use_fake_for_ranked(monkeypatch, fake)
    now = time.time()
    monkeypatch.setitem(ranked_mod._points_cache, "K", (500, now))
    monkeypatch.setitem(ranked_mod._points_cache, "V", (500, now))
    monkeypatch.setitem(ranked_mod._points_cache, "C", (500, now))
    kw = dict(rid="R-X", match_version=2, gm="zw_lg", winner_discord_id="K",
              kills={"K": 2}, placements={"K": 1, "V": 3, "C": 2},
              kill_events=[("K", "V"), ("K", "C")])
    first = ranked_mod.seal_match(**kw)
    assert first["ok"] is True
    after_first = dict(fake.points)
    again = ranked_mod.seal_match(**kw)
    assert again["ok"] is True and again.get("duplicate") is True
    assert fake.points == after_first  # zero point bumps on the retry
    assert len(fake.results) == 1


# ---------------------------------------------------------------------------
# 3) banked events drive winner + placements + points (comp alv parity)
# ---------------------------------------------------------------------------

def _bank_events(fake):
    """Kill rows + death-order alv rows: Victim out first, then Third, Killer
    alive last. Last alv row reads as the winner, same contract as the comp
    snapshot test."""
    fake.ranked_events = [
        {"room_id": "R-X", "match_version": 2, "etype": "kill",
         "actor_name": "Killer", "target_name": "Victim", "discord_id": "K"},
        {"room_id": "R-X", "match_version": 2, "etype": "kill",
         "actor_name": "Killer", "target_name": "Third", "discord_id": "K"},
        {"room_id": "R-X", "match_version": 2, "etype": "alv",
         "actor_name": "Victim", "discord_id": "V", "alive": False},
        {"room_id": "R-X", "match_version": 2, "etype": "alv",
         "actor_name": "Third", "discord_id": "C", "alive": False},
        {"room_id": "R-X", "match_version": 2, "etype": "alv",
         "actor_name": "Killer", "discord_id": "K", "alive": False},
    ]


def test_scores_from_banked_events_kill_and_alv_order(monkeypatch):
    fake = _FakeDB(users=[
        {"game_username": "Killer", "discord_id": "K"},
        {"game_username": "Victim", "discord_id": "V"},
        {"game_username": "Third", "discord_id": "C"},
    ])
    _use_fake_for_ranked(monkeypatch, fake)
    _bank_events(fake)
    now = time.time()
    for did in ("K", "V", "C"):
        monkeypatch.setitem(ranked_mod._points_cache, did, (500, now))
    # The engine bridge resolves the winner from the roster (as in production);
    # everything else — kill counts AND placements — comes from the banked
    # events alone. No kill_events / kills / placements params passed.
    res = ranked_mod.seal_match(rid="R-X", match_version=2, gm="zw_lg",
                                winner_discord_id="K")
    assert res["ok"] is True
    row = fake.results[0]
    assert row["winner_discord_id"] == "K"
    # static table, all from evidence: K = 2 banked kills (16) + win (50)
    # + P1 (10) = 76 -> clamp 30; C died after V -> reverse alv death order
    # gives C P2 (7) - 5 = 2 and V P3 (5) - 5 = 0. The placement split proves
    # the banked alv rows drove it (no placements were passed in).
    assert res["points"] == {"K": 30, "C": 2, "V": 0}


# ---------------------------------------------------------------------------
# 4) empty event bank: legacy synthesis path still seals
# ---------------------------------------------------------------------------

def test_no_banked_events_synthesizes_legacy(monkeypatch):
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
    # K = 1 kill (8) + win (50) + P1 (10) = 68 -> clamp 30; V = P2 (7) - 5 = 2
    assert res["points"] == {"K": 30, "V": 2}
    assert len(fake.results) == 1


# ---------------------------------------------------------------------------
# 5) the server seal reads the bank but never writes it
# ---------------------------------------------------------------------------

def test_seal_never_inserts_events(monkeypatch):
    fake = _FakeDB(users=[
        {"game_username": "Killer", "discord_id": "K"},
        {"game_username": "Victim", "discord_id": "V"},
        {"game_username": "Third", "discord_id": "C"},
    ])
    _use_fake_for_ranked(monkeypatch, fake)
    _bank_events(fake)
    _patch_host(monkeypatch)
    now = time.time()
    for did in ("K", "V", "C"):
        monkeypatch.setitem(ranked_mod._points_cache, did, (500, now))
    proc.seal_ranked_match("s", "K", _room(), "r1", "zw_lg")
    event_writes = [c for c in fake.calls
                    if c[0] == "bngc_ranked_events" and c[1] in ("insert", "upsert", "update")]
    assert event_writes == []
    # the read happened (banked events drove the seal)
    event_reads = [c for c in fake.calls if c[0] == "bngc_ranked_events" and c[1] == "select"]
    assert event_reads


# ---------------------------------------------------------------------------
# 6) the v250 migration file carries the three rail indexes
# ---------------------------------------------------------------------------

def test_migration_pins_rail_indexes():
    sql = (Path(__file__).resolve().parents[2]
           / "supabase" / "add_ranked_v250_ranked_seal_rails.sql").read_text(encoding="utf-8")
    assert "ranked_results_room_ver_uidx" in sql
    assert "bngc_ranked_matches_one_open_per_room_uidx" in sql
    assert "bngc_ranked_events_seal_seq_uidx" in sql
    # the events rail is keyed per seal (room, version, seal_id, event_seq)
    # and partial — legacy NULL rows must stay untouched
    assert "(room_id, match_version, seal_id, event_seq)" in sql
    assert "WHERE seal_id IS NOT NULL" in sql
