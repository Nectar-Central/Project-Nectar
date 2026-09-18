"""Ranked-tourney bridge: stG=1 auto-open, stG=3 auto-seal (fake supabase + fake room).

Contract: the processor bridge mirrors seal_comp_match for the ranked-tourney system.
stG 1 opens a blank match (version = per-tourney max+1) when no open row exists for
this room; stG 2/3 with an open row seals it and auto-calculates. stG 2 alone is
transition noise. Dashboard manual rows are never touched. Host-only writes via
hosts[] + created_by (v233/v247 parity). Spectating host gets no alv row. Seal
retries never duplicate the alv snapshot.
"""
from types import SimpleNamespace

import server.scripts.processor as proc


class _R:
    def __init__(self, data):
        self.data = data


class _Q:
    """Chainable fake supabase query. Filters on eq/neq/in_, sorted order for
    desc=True, update/insert execute against the state dict."""
    def __init__(self, sb, rows, name):
        self._sb, self._rows, self._name = sb, list(rows), name
        self._insert = None
        self._update = None

    def select(self, *a):
        return self

    def eq(self, c, v):
        self._rows = [r for r in self._rows if str(r.get(c)) == str(v)]
        return self

    def neq(self, c, v):
        self._rows = [r for r in self._rows if str(r.get(c)) != str(v)]
        return self

    def in_(self, c, vs):
        s = {str(v) for v in vs}
        self._rows = [r for r in self._rows if str(r.get(c)) in s]
        return self

    def order(self, c, desc=False):
        self._rows = sorted(self._rows, key=lambda r: (r.get(c) is None, r.get(c)),
                            reverse=bool(desc))
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def insert(self, rows):
        self._insert = [rows] if isinstance(rows, dict) else [dict(r) for r in rows]
        return self

    def update(self, data):
        self._update = dict(data)
        return self

    def execute(self):
        base = self._sb.t.setdefault(self._name, [])
        if self._insert is not None:
            wrote = []
            for r in self._insert:
                r = dict(r)
                if "id" not in r:
                    r["id"] = max([x.get("id", 0) for x in base] + [0]) + 1
                base.append(r)
                wrote.append(dict(r))
            return _R(wrote)
        if self._update is not None:
            for r in self._rows:
                r.update(self._update)
            return _R([dict(r) for r in self._rows])
        return _R([dict(r) for r in self._rows])


class _SB:
    def __init__(self, tables):
        self.t = {k: list(v) for k, v in tables.items()}
        self.inserts = []

    def table(self, name):
        return _Q(self, self.t.get(name, []), name)


def _player(username, nr, alive):
    return SimpleNamespace(username=username, actor_nr=nr, is_alive=alive)


def _kill(killer, victim):
    return SimpleNamespace(event_type="killed", instigator=killer, instigated=victim)


def _room(players, kills, me_name="cis6led", last_stg=1, short_code="ABCDE"):
    return SimpleNamespace(
        players=dict(players),
        recent_kills=list(kills),
        me=SimpleNamespace(username=me_name),
        last_stg=last_stg,
        short_code=short_code,
    )


USERS = [
    {"game_username": "cis6led", "discord_id": "A"},
    {"game_username": "chris7", "discord_id": "B"},
    {"game_username": "third", "discord_id": "C"},
]

PLAYERS = {
    1: _player("cis6led", 1, True),
    2: _player("chris7", 2, False),
    3: _player("third", 3, False),
}
KILLS = [_kill("cis6led", "chris7"), _kill("cis6led", "third")]

ROOM = _room(PLAYERS, KILLS)

TOURNEYS = [{
    "id": 0, "code": "ABCDE", "name": "Ranked Cup", "status": "started",
    "mode": "solo", "series_len": 3, "host_playing": True,
    "hosts": ["A"], "created_by": "A",
    "points_table": {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]},
}]

MATCHES = []


def _bridge_sb(monkeypatch, tables=None):
    tables = dict(tables or {})
    tables.setdefault("bngc_ranked_tourneys", list(TOURNEYS))
    tables.setdefault("bngc_ranked_tourney_matches", list(MATCHES))
    tables.setdefault("bngc_ranked_tourney_events", [])
    tables.setdefault("bngc_users", list(USERS))
    sb = _SB(tables)
    monkeypatch.setattr(proc, "supabase", sb)
    monkeypatch.setattr(proc, "_rt_calc", lambda tid, who="bridge": [])
    proc._comp_namemap.clear()
    proc._rt_seen.clear()
    proc._rt_logged_codes.clear()
    proc._rt_host_warned.clear()
    return sb


def test_stg1_auto_opens_match(monkeypatch):
    sb = _bridge_sb(monkeypatch)
    proc.seal_ranked_tourney_match("ses", "A", ROOM, "room-x", "zw_lg")
    open_rows = [r for r in sb.t["bngc_ranked_tourney_matches"] if r.get("status") == "open"]
    assert len(open_rows) == 1
    assert open_rows[0]["tourney_id"] == 0
    assert open_rows[0]["version"] == 0
    assert open_rows[0]["room_id"] == "room-x"


def test_stg3_seals_open_match_and_banks_events(monkeypatch):
    sb = _bridge_sb(monkeypatch)
    # host opens at stG 1, game runs, then stG 3 ends the phase
    proc.seal_ranked_tourney_match("ses", "A", ROOM, "room-x", "zw_lg")
    room3 = _room(PLAYERS, KILLS, last_stg=3)
    proc.seal_ranked_tourney_match("ses", "A", room3, "room-x", "zw_lg")
    sealed = [r for r in sb.t["bngc_ranked_tourney_matches"] if r.get("status") == "sealed"]
    assert len(sealed) == 1 and sealed[0]["version"] == 0
    # one alv row per roster player, dead in kill order, survivor last
    alv = [e for e in sb.t["bngc_ranked_tourney_events"] if e.get("etype") == "alv"]
    assert [(r["actor_name"]) for r in alv] == ["chris7", "third", "cis6led"]
    # kills banked with mapped discord ids
    kills = [e for e in sb.t["bngc_ranked_tourney_events"] if e.get("etype") == "kill"]
    assert len(kills) == 2
    assert all(r["discord_id"] == "A" for r in kills)


def test_stg2_alone_is_noise(monkeypatch):
    sb = _bridge_sb(monkeypatch)
    room2 = _room(PLAYERS, KILLS, last_stg=2)
    proc.seal_ranked_tourney_match("ses", "A", room2, "room-x", "zw_lg")
    assert sb.t["bngc_ranked_tourney_matches"] == []
    assert sb.inserts == []


def test_non_host_session_never_writes(monkeypatch):
    sb = _bridge_sb(monkeypatch)
    proc.seal_ranked_tourney_match("ses", "Z", ROOM, "room-x", "zw_lg")
    assert sb.t["bngc_ranked_tourney_matches"] == []
    assert sb.inserts == []


def test_manual_rows_never_touched(monkeypatch):
    sb = _bridge_sb(monkeypatch, {
        "bngc_ranked_tourney_matches": [
            {"id": 5, "tourney_id": 0, "version": 0, "room_id": None,
             "status": "manual", "winner_discord_id": None, "placements": {},
             "removed_discord_ids": [], "point_awards": {}},
        ],
    })
    # stG 3 with a manual row in the table (but none open for this room)
    room3 = _room(PLAYERS, KILLS, last_stg=3)
    proc.seal_ranked_tourney_match("ses", "A", room3, "room-x", "zw_lg")
    # manual row stays manual, no new open row, nothing sealed
    assert sb.t["bngc_ranked_tourney_matches"][0]["status"] == "manual"
    assert sb.inserts == []


def test_series_complete_blocks_auto_open(monkeypatch):
    sb = _bridge_sb(monkeypatch, {
        "bngc_ranked_tourney_matches": [
            {"id": 1, "tourney_id": 0, "version": 0, "room_id": "r1",
             "status": "sealed", "winner_discord_id": None, "placements": {},
             "removed_discord_ids": [], "point_awards": {}},
            {"id": 2, "tourney_id": 0, "version": 1, "room_id": "r2",
             "status": "sealed", "winner_discord_id": None, "placements": {},
             "removed_discord_ids": [], "point_awards": {}},
            {"id": 3, "tourney_id": 0, "version": 2, "room_id": "r3",
             "status": "sealed", "winner_discord_id": None, "placements": {},
             "removed_discord_ids": [], "point_awards": {}},
        ],
    })
    proc.seal_ranked_tourney_match("ses", "A", ROOM, "room-x", "zw_lg")
    assert not [r for r in sb.t["bngc_ranked_tourney_matches"] if r.get("status") == "open"]


