"""Phase 3: /api/stats ranked block + /api/leaderboard?ranked=true.

Handlers called directly with a stub request (the env's starlette/httpx
mix has a broken TestClient). DB/cache/engine faked. No network.
"""
import asyncio
import json

import pytest

from server.routes import stats as stats_mod
from server.routes import discovery as disc_mod
from server.lib import ranked as ranked_mod


class _FakeReq:
    def __init__(self, params=None):
        self.query_params = dict(params or {})
        self.headers = {}

    async def json(self):
        raise ValueError("no body")


def _call(fn, params=None):
    resp = asyncio.run(fn(_FakeReq(params)))
    assert resp.status_code == 200, resp.body[:200]
    return json.loads(resp.body.decode())


# ---------------------------------------------------------------------------
# Route-level fake DB
# ---------------------------------------------------------------------------

class _Res:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class _Q:
    def __init__(self, fake, table):
        self.fake = fake
        self.table = table
        self.single_mode = False
        self.count_mode = None
        self.filters = []
        self.order_col = None
        self.order_desc = True
        self.lo = None
        self.hi = None

    def select(self, *cols, count=None):
        self.count_mode = count
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def gt(self, col, val):
        self.filters.append(("gt", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, list(vals)))
        return self

    def order(self, col, desc=True):
        self.order_col, self.order_desc = col, bool(desc)
        return self

    def range(self, lo, hi):
        self.lo, self.hi = lo, hi
        return self

    def limit(self, n):
        self.lo, self.hi = 0, n - 1
        return self

    def single(self):
        self.single_mode = True
        return self

    def _rows(self):
        if self.table == "bngc_users":
            rows = list(self.fake.users)
        elif self.table == "ranked_points":
            rows = list(self.fake.points)
        elif self.table == "ranked_results":
            rows = list(self.fake.results)
        elif self.table == "ranked_seasons":
            rows = list(getattr(self.fake, "seasons", []))
        elif self.table == "bngc_game_events":
            rows = list(self.fake.events)
        else:
            rows = []
        for kind, col, val in self.filters:
            if kind == "eq":
                rows = [r for r in rows if str(r.get(col)) == str(val)]
            elif kind == "gt":
                rows = [r for r in rows if (r.get(col) or 0) > val]
            elif kind == "in":
                rows = [r for r in rows if str(r.get(col)) in {str(v) for v in val}]
        if self.order_col:
            rows = sorted(rows, key=lambda r: (r.get(self.order_col) or 0),
                          reverse=self.order_desc)
        if self.lo is not None:
            rows = rows[self.lo:(self.hi + 1) if self.hi is not None else None]
        return rows

    def execute(self):
        rows = self._rows()
        if self.count_mode == "exact":
            return _Res(data=rows, count=len(rows))
        if self.single_mode:
            if not rows:
                raise Exception("PGRST116 no rows")
            return _Res(data=rows[0])
        return _Res(data=rows)


class _FakeDB:
    def __init__(self, users=(), points=(), results=(), events=()):
        self.users = list(users)
        self.points = list(points)
        self.results = list(results)
        self.events = list(events)

    def table(self, name):
        return _Q(self, name)


_USER = {"discord_id": "D1", "discord_username": "p1", "game_username": "Player1",
         "game_id": "g1", "skin": "s", "pickaxe": "p", "kills": 10, "deaths": 5,
         "wins": 2, "defeats": 3, "playtime_global": 100, "playtime_per_gm": {},
         "is_locked": False}


def _live_session(code="ABCDE", mver=3, warnings=None, state="match", players=None):
    return {"current_room_id": "r1",
            "rooms": {"r1": {"room_id": "r1", "short_code": code,
                             "match_version": mver, "state": state,
                             "ranked_warnings": warnings or [],
                             "players": players or {},
                             "game_mode": "zw_lg", "players_count": 4}}}


def _blip_session(fresh=True):
    """Lone-Authenticate shape: current parked on the lobby pseudo-room while
    the ranked room does (fresh) or doesn't (stale ghost) keep capturing."""
    import time as _t
    ts = _t.time() - (10 if fresh else 3600)
    return {"current_room_id": "lobby",
            "rooms": {"lobby": {"room_id": "lobby"},
                      "r1": {"room_id": "r1", "short_code": "ABCDE",
                             "match_version": 3, "state": "lobby",
                             "ranked_warnings": [], "players": {},
                             "game_mode": "zw_lg", "players_count": 2,
                             "last_packet_at": ts}}}


_ROOM = {"id": "R-X", "short_code": "ABCDE", "host_discord_id": "H",
         "status": "live", "min_points": 0, "max_points": None, "fee": 0}


@pytest.fixture
def fakes(monkeypatch):
    fake = _FakeDB(users=[dict(_USER)],
                   points=[{"discord_id": "D1", "points": 750}])
    monkeypatch.setattr(stats_mod, "supabase", fake)
    monkeypatch.setattr(disc_mod, "supabase", fake)
    monkeypatch.setattr(stats_mod.db, "get_session_sync",
                        lambda sid: {"data": {"discord_id": "D1"}})
    monkeypatch.setattr(stats_mod.capture_engine, "get_session", lambda sid: None)
    return fake


def _stats(monkeypatch, session=None, room=None, points=750, did="D1"):
    async def _cached(sid):
        return session
    monkeypatch.setattr(stats_mod.cache, "get_session_state", _cached)
    monkeypatch.setattr(ranked_mod, "get_ranked_room", lambda code: room)
    monkeypatch.setattr(ranked_mod, "get_ranked_points", lambda d: points)
    return _call(stats_mod.get_stats, {"session_id": "s1", "discord_id": did})


# ---------------------------------------------------------------------------
# /api/stats ranked block
# ---------------------------------------------------------------------------

def test_stats_ranked_active(fakes, monkeypatch):
    body = _stats(monkeypatch, session=_live_session(), room=dict(_ROOM))
    rk = body["ranked"]
    assert rk["active"] is True
    assert rk["rid"] == "R-X" and rk["short_code"] == "ABCDE"
    assert rk["is_host"] is False and rk["status"] == "live"
    assert rk["points"] == 750 and rk["milestone"] == "Silver"
    assert rk["milestone_icon_url"].endswith("/silver.png")
    assert rk["milestone_color"] == "#C0C0C0"
    assert rk["match_id"] == "R-X-3"
    assert rk["match_version"] == 3
    for gone in ("warnings", "host_game_username", "host_skin", "host_playing",
                 "points_to_next", "next_milestone", "arena_icon_url",
                 "min_points", "max_points", "fee", "sealed", "needs_start"):
        assert gone not in rk


def test_stats_ranked_tier(fakes, monkeypatch):
    body = _stats(monkeypatch, session=_live_session(), room=dict(_ROOM), points=150)
    assert body["ranked"]["milestone"] == "Unranked"  # 150 < 200


def test_stats_ranked_is_host_true(fakes, monkeypatch):
    fakes.users.append(dict(_USER, discord_id="H", game_username="Host1"))
    async def _cached(sid):
        return _live_session()
    monkeypatch.setattr(stats_mod.cache, "get_session_state", _cached)
    monkeypatch.setattr(ranked_mod, "get_ranked_room", lambda code: dict(_ROOM))
    monkeypatch.setattr(ranked_mod, "get_ranked_points", lambda did: 1600)
    body = _call(stats_mod.get_stats, {"session_id": "s1", "discord_id": "H"})
    rk = body["ranked"]
    assert rk["is_host"] is True and rk["milestone"] == "Platinum"


def test_stats_non_ranked_null(fakes, monkeypatch):
    body = _stats(monkeypatch, session=_live_session(code="ZZZZZ"), room=None)
    assert body["ranked"] is None
    assert body["session"]["short_code"] == "ZZZZZ"  # rest untouched


def test_stats_lobby_blip_rescue(fakes, monkeypatch):
    # lone Authenticate parked current on the lobby pseudo-room, but the
    # ranked room keeps capturing -> BLOCK survives, code intact
    body = _stats(monkeypatch, session=_blip_session(fresh=True), room=dict(_ROOM))
    assert body["ranked"] and body["ranked"]["rid"] == "R-X"
    assert body["session"]["short_code"] == "ABCDE"


def test_stats_stale_ghost_stays_buried(fakes, monkeypatch):
    # same shape but the room's packets are stale (kick/reload ghost) ->
    # no rescue, stays null
    body = _stats(monkeypatch, session=_blip_session(fresh=False), room=dict(_ROOM))
    assert body["ranked"] is None
    assert body["session"]["short_code"] is None


def _blip_session_age(age_s):
    """Blip shape with the old room's last packet age_s seconds ago."""
    import time as _t
    return {"current_room_id": "lobby",
            "rooms": {"lobby": {"room_id": "lobby"},
                      "r1": {"room_id": "r1", "short_code": "ABCDE",
                             "match_version": 3, "state": "lobby",
                             "ranked_warnings": [], "players": {},
                             "game_mode": "zw_lg", "players_count": 2,
                             "last_packet_at": _t.time() - age_s}}}


def test_stats_left_room_ghost_buried_fast(fakes, monkeypatch):
    # real leave 60s ago: old room still "fresh" by the 120s session window
    # but past the 20s blip-rescue window -> no rescue, no ranked BLOCK
    body = _stats(monkeypatch, session=_blip_session_age(60), room=dict(_ROOM))
    assert body["ranked"] is None
    assert body["session"] is None or body["session"].get("short_code") is None


# ---------------------------------------------------------------------------
# direct-DB lookup (no cache: new rooms visible immediately)
# ---------------------------------------------------------------------------

class _RoomDB:
    def __init__(self, rooms):
        self.rooms = list(rooms)
        self.calls = 0

    def table(self, name):
        assert name == "ranked_rooms"
        outer = self

        class _RQ:
            def __init__(self):
                self.filters = []

            def select(self, *a):
                return self

            def eq(self, col, val):
                self.filters.append((col, val))
                return self

            def limit(self, n):
                return self

            def execute(self):
                outer.calls += 1
                rows = list(outer.rooms)
                for col, val in self.filters:
                    rows = [r for r in rows if str(r.get(col)) == str(val)]
                return _Res(data=rows)

        return _RQ()


@pytest.fixture
def fresh_env(monkeypatch):
    monkeypatch.setattr(ranked_mod, "_sb", lambda: None)  # default: no DB
    yield


def test_lookup_hit_reads_db(monkeypatch, fresh_env):
    db = _RoomDB([{"id": "R-1", "short_code": "ABCDE", "status": "live"}])
    monkeypatch.setattr(ranked_mod, "_sb", lambda: db)
    assert ranked_mod.get_ranked_room("abcde") == {"id": "R-1", "short_code": "ABCDE", "status": "live"}
    assert ranked_mod.get_ranked_room("ABCDE")["id"] == "R-1"


def test_lookup_miss_returns_none(monkeypatch, fresh_env):
    db = _RoomDB([])
    monkeypatch.setattr(ranked_mod, "_sb", lambda: db)
    assert ranked_mod.get_ranked_room("ZZZZZ") is None
    assert ranked_mod.get_ranked_room("") is None
    assert ranked_mod.get_ranked_room(None) is None


def test_lookup_prefers_live(monkeypatch, fresh_env):
    db = _RoomDB([
        {"id": "R-old", "short_code": "ABCDE", "status": "lobby"},
        {"id": "R-new", "short_code": "ABCDE", "status": "live"},
    ])
    monkeypatch.setattr(ranked_mod, "_sb", lambda: db)
    assert ranked_mod.get_ranked_room("ABCDE")["id"] == "R-new"


def test_lookup_lobby_when_no_live(monkeypatch, fresh_env):
    db = _RoomDB([{"id": "R-2", "short_code": "FGHIJ", "status": "lobby"}])
    monkeypatch.setattr(ranked_mod, "_sb", lambda: db)
    assert ranked_mod.get_ranked_room("FGHIJ")["id"] == "R-2"


def test_stats_no_session_null(fakes, monkeypatch):
    body = _stats(monkeypatch, session=None, room=dict(_ROOM))
    assert body["session"] is None and body["ranked"] is None


def test_stats_last_result_win(fakes, monkeypatch):
    fakes.results = [{"room_id": "R-X", "match_version": 3, "winner_discord_id": "D1",
                      "kills": {"D1": 2}, "points": {"D1": 85}, "placements": {"D1": 1}}]
    body = _stats(monkeypatch, session=_live_session(), room=dict(_ROOM))
    assert body["ranked"]["last_result"] == {
        "match_version": 3, "gained": 85, "won": True, "kills": 2}


def test_stats_last_result_loss(fakes, monkeypatch):
    fakes.results = [{"room_id": "R-X", "match_version": 3, "winner_discord_id": "H",
                      "kills": {"D1": 2}, "points": {"D1": 11}}]
    body = _stats(monkeypatch, session=_live_session(), room=dict(_ROOM))
    assert body["ranked"]["last_result"] == {
        "match_version": 3, "gained": 11, "won": False, "kills": 2}


def test_stats_last_result_none_when_unsealed(fakes, monkeypatch):
    fakes.results = []
    body = _stats(monkeypatch, session=_live_session(), room=dict(_ROOM))
    assert body["ranked"]["last_result"] is None


def test_stats_is_dev_defaults_false(fakes, monkeypatch):
    body = _stats(monkeypatch, session=None, room=None)
    assert body["is_dev"] is False


def test_stats_is_dev_true(fakes, monkeypatch):
    fakes.users[0]["is_dev"] = True
    body = _stats(monkeypatch, session=None, room=None)
    assert body["is_dev"] is True


def test_stats_arena_block(fakes, monkeypatch):
    monkeypatch.setattr(ranked_mod, "get_current_season", lambda: (3, "2026-10-01T00:00:00+00:00"))
    body = _stats(monkeypatch, session=None, room=None, points=750)
    a = body["arena"]
    assert a["points"] == 750 and a["milestone"] == "Silver"
    assert a["milestone_icon_url"].endswith("/silver.png")
    assert a["points_to_next"] == 250 and a["next_milestone"] == "Gold"
    assert a["next_points_pct"] == 50
    assert a["season"] == 3 and a["reset_at"] == "2026-10-01T00:00:00+00:00"


def test_stats_arena_max_rank(fakes, monkeypatch):
    monkeypatch.setattr(ranked_mod, "get_current_season", lambda: (1, None))
    body = _stats(monkeypatch, session=None, room=None, points=9000)
    a = body["arena"]
    assert a["milestone"] == "Grandmaster" and a["next_milestone"] is None
    assert a["points_to_next"] == 0 and a["next_points_pct"] == 100


def _timed_session(age_s, ranked=True):
    import time as _t
    now = _t.time()
    room = {"room_id": "r1", "short_code": "ABCDE" if ranked else "ZZZZZ",
            "match_version": 2, "state": "match", "ranked_warnings": [],
            "players": {}, "game_mode": "zw_lg", "players_count": 4,
            "last_packet_at": now - age_s}
    return {"current_room_id": "r1", "rooms": {"r1": room}, "_cached_at": now - age_s}


def test_stats_fresh_session_kept(fakes, monkeypatch):
    body = _stats(monkeypatch, session=_timed_session(10), room=dict(_ROOM))
    assert body["session"] is not None
    assert body["ranked"] is not None and body["ranked"]["rid"] == "R-X"


def test_stats_stale_session_dropped(fakes, monkeypatch):
    # kicked/reloaded 5 min ago: ghost room must not serve (session is None)
    # but ranked block is preserved via the stale short_code (user was in a
    # ranked room — the capture just stopped sending packets).
    body = _stats(monkeypatch, session=_timed_session(300), room=dict(_ROOM))
    assert body["session"] is None
    assert body["ranked"] is not None and body["ranked"]["rid"] == "R-X"


def test_stats_stale_cached_at_fallback(fakes, monkeypatch):
    import time as _t
    sess = _timed_session(300)
    del sess["rooms"]["r1"]["last_packet_at"]
    body = _stats(monkeypatch, session=sess, room=dict(_ROOM))
    assert body["session"] is None
    assert body["ranked"] is not None and body["ranked"]["rid"] == "R-X"


def test_stats_no_timestamps_fail_open(fakes, monkeypatch):
    sess = _timed_session(300)
    del sess["rooms"]["r1"]["last_packet_at"]
    del sess["_cached_at"]
    body = _stats(monkeypatch, session=sess, room=dict(_ROOM))
    assert body["session"] is not None  # old caches keep working


def test_stats_stale_casual_room_no_ranked(fakes, monkeypatch):
    # Stale session in a casual room (no short_code) must not show ranked.
    # Override get_ranked_room to return None for the casual code "ZZZZZ".
    monkeypatch.setattr(ranked_mod, "get_ranked_room", lambda code: None)
    body = _stats(monkeypatch, session=_timed_session(300, ranked=False), room=None)
    assert body["session"] is None
    assert body["ranked"] is None


def _event(code, minutes_ago, ed, did="D9"):
    from datetime import datetime, timezone, timedelta
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {"event_type": "kill", "event_data": dict(ed, code=code),
            "discord_id": did, "created_at": ts}


def test_verify_host_in_room(fakes, monkeypatch):
    fakes.events = [
        _event("ABCDE", 2, {"killer": "True Jew", "victim": "Bob"}),
        _event("ABCDE", 5, {"killer": "Bob", "victim": "True Jew"}),
    ]
    body = _call(disc_mod.ranked_verify, {"code": "abcde", "host": "True Jew"})
    assert body["room_active"] is True
    assert body["host_in_room"] is True
    assert "True Jew" in body["players_seen"] and "Bob" in body["players_seen"]


def test_verify_host_missing(fakes, monkeypatch):
    fakes.events = [_event("ABCDE", 2, {"killer": "Bob", "victim": "Zed"})]
    body = _call(disc_mod.ranked_verify, {"code": "ABCDE", "host": "True Jew"})
    assert body["room_active"] is True
    assert body["host_in_room"] is False


def test_verify_stale_room_inactive(fakes, monkeypatch):
    fakes.events = [_event("ABCDE", 60, {"killer": "True Jew", "victim": "Bob"})]
    body = _call(disc_mod.ranked_verify, {"code": "ABCDE", "host": "True Jew"})
    assert body["room_active"] is False
    assert body["host_in_room"] is False


def test_verify_dc_prefix_tolerant(fakes, monkeypatch):
    fakes.events = [_event("ABCDE", 1, {"killer": "dc: True Jew", "victim": "Bob"})]
    body = _call(disc_mod.ranked_verify, {"code": "ABCDE", "host": "True Jew"})
    assert body["host_in_room"] is True


# ---------------------------------------------------------------------------
# /api/leaderboard?ranked=true
# ---------------------------------------------------------------------------

def test_leaderboard_ranked(fakes, monkeypatch):
    fakes.points = [{"discord_id": "D1", "points": 750},
                    {"discord_id": "D2", "points": 100}]
    fakes.users.append({"discord_id": "D2", "discord_username": "p2",
                        "game_username": "Player2", "game_id": "g2", "skin": "s2"})
    body = _call(disc_mod.leaderboard, {"ranked": "true", "limit": "20"})
    assert body["ranked"] is True and body["sort"] == "points"
    assert body["count"] == 2
    first, second = body["results"]
    assert (first["discord_id"], first["points"], first["rank"]) == ("D1", 750, 1)
    assert first["milestone"] == "Silver" and first["game_username"] == "Player1"
    assert (second["discord_id"], second["points"], second["rank"]) == ("D2", 100, 2)
    assert second["milestone"] == "Unranked"


def test_leaderboard_ranked_empty(fakes, monkeypatch):
    fakes.points = []
    body = _call(disc_mod.leaderboard, {"ranked": "1"})
    assert body["count"] == 0


def test_leaderboard_normal_untouched(fakes, monkeypatch):
    body = _call(disc_mod.leaderboard, {"sort": "kills", "limit": "20"})
    assert "ranked" not in body and body["sort"] == "kills"
    assert body["results"][0]["discord_id"] == "D1"


def test_leaderboard_ranked_season_static(fakes, monkeypatch):
    # seasons table deleted: always season 1, no reset
    body = _call(disc_mod.leaderboard, {"ranked": "true"})
    assert body["season"] == 1
    assert body["reset_at"] is None
