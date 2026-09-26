"""Tourney PR must be a ratchet: it may go up, never down.

`bngc_users.tourney_pr` is ONE global column, but the score computed for it is
per-TOURNEY: `compute_tourney_pr` has no memory of a player's previous rating,
no expected value and no K-factor. Writing it as an absolute value therefore
meant every tourney that closed overwrote the last one's numbers, so a player
who did less well in tourney #2 went DOWN - and the leaderboard, which is a
pure read of that column, showed it.

Two more ways the same board moved under the players' feet, both fixed here
because a ratchet you cannot reproduce is impossible to reason about:

- the event read was `.limit(5000)` with no ORDER BY, so once a tourney passed
  ~5000 event rows two identical calls returned different 5000-row windows;
  whole players dropped out of the board and reappeared somewhere else;
- the per-tourney history row is the truth for THAT tourney and keeps the real
  value, while the flat column keeps the high-water mark.

The manual override (/tourney-pr-set) stays absolute on purpose: a host
setting a number explicitly is not a recompute.

Fake supabase, no network.
"""
import asyncio

import pytest

from server.routes import comp as comp_mod


class _R:
    def __init__(self, data):
        self.data = data


class _Q:
    """One recorded write, and a table read that returns the seeded rows."""

    def __init__(self, sb, name):
        self._sb, self._name = sb, name
        self._rows = list(sb.t.get(name, []))
        self._op = None
        self._payload = None
        self._filters = {}
        self._order = None
        self._limit = None
        self._in = None

    def select(self, *a):
        self._op = "select"
        return self

    def eq(self, c, v):
        self._filters[c] = v
        return self

    def neq(self, c, v):
        self._filters[c] = ("!=", v)
        return self

    def in_(self, c, vs):
        self._in = (c, list(vs))
        return self

    def order(self, c, desc=False):
        self._order = (c, bool(desc))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def upsert(self, payload, on_conflict=None):
        self._op = "upsert"
        self._payload = dict(payload)
        self.tid = on_conflict
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = dict(payload)
        return self

    def execute(self):
        if self._op == "upsert":
            self._sb.writes.append((self._name, "upsert", dict(self._payload)))
            return _R([dict(self._payload)])
        if self._op == "update":
            # record the row targeted too: an UPDATE carries its predicate in
            # .eq(), not in the payload
            rec = dict(self._payload)
            for c, v in self._filters.items():
                if not isinstance(v, tuple):
                    rec.setdefault(c, v)
            self._sb.writes.append((self._name, "update", rec))
            return _R([dict(rec)])
        if self._in:
            col, vals = self._in
            want = {str(v) for v in vals}
            rows = [r for r in self._rows if str(r.get(col)) in want]
        else:
            rows = [r for r in self._rows
                    if all(str(r.get(c)) == str(v)
                           for c, v in self._filters.items()
                           if not isinstance(v, tuple))]
        if self._order:
            col, desc = self._order
            rows = sorted(rows, key=lambda r: (r.get(col) is None, r.get(col)),
                          reverse=desc)
        if self._limit:
            rows = rows[:self._limit]
        self._sb.reads.append((self._name, self._order, self._limit))
        return _R([dict(r) for r in rows])


class _SB:
    def __init__(self, tables):
        self.t = {k: list(v) for k, v in tables.items()}
        self.writes = []
        self.reads = []

    def table(self, name):
        return _Q(self, name)


def _user(did, pr):
    return {"discord_id": did, "tourney_pr": pr}


def _state(users_pr, extra_events=None):
    """One sealed match: 2 kills + 1 win for A, nothing else unless asked."""
    events = [
        {"match_id": 1, "etype": "kill", "discord_id": "A",
         "actor_name": "a", "alive": None},
        {"match_id": 1, "etype": "kill", "discord_id": "A",
         "actor_name": "b", "alive": None},
        {"match_id": 1, "etype": "win", "discord_id": "A",
         "actor_name": "a", "alive": None},
    ] + list(extra_events or [])
    return _SB({
        "bngc_comp_pr_history": [],
        "bngc_comp_tourneys": [{"id": 7, "points_table": {}}],
        "bngc_comp_matches": [{"id": 1, "tourney_id": 7, "version": 0,
                               "status": "sealed", "winner_discord_id": "A",
                               "placements": {}, "removed_discord_ids": [],
                               "point_awards": {}}],
        "bngc_comp_events": events,
        "bngc_users": [_user(d, p) for d, p in users_pr.items()],
    })


def _run(monkeypatch, sb):
    monkeypatch.setattr(comp_mod, "supabase", sb)
    return comp_mod.compute_and_store_tourney_pr_sync(7)


def _written(sb, table="bngc_users"):
    return [p for (t, _op, p) in sb.writes if t == table]


# --- the ratchet -----------------------------------------------------------

def test_pr_ratchets_up_from_nothing(monkeypatch):
    sb = _state({"A": None})
    board = _run(monkeypatch, sb)
    assert board, "expected a PR board"
    first = float(board[0]["pr"])
    assert first > 0
    assert float(_written(sb)[0]["tourney_pr"]) == pytest.approx(first)


def test_a_worse_tourney_cannot_lower_a_stored_pr(monkeypatch):
    # the whole point: stored 40.0, this tourney scores far lower, and the
    # column must keep 40.0
    sb = _state({"A": 40.0})
    board = _run(monkeypatch, sb)
    fresh = float(board[0]["pr"])
    assert fresh < 40.0, "fixture should score below the stored PR"
    assert float(_written(sb)[0]["tourney_pr"]) == 40.0


def test_a_better_tourney_does_raise_it(monkeypatch):
    sb = _state({"A": 0.5})
    board = _run(monkeypatch, sb)
    fresh = float(board[0]["pr"])
    assert fresh > 0.5
    assert float(_written(sb)[0]["tourney_pr"]) == pytest.approx(fresh)


def test_the_history_row_keeps_the_real_value_not_the_high_water_mark(monkeypatch):
    # the per-tourney archive is the truth for THAT tourney; only the flat
    # column ratchets, so a bad tourney is still visible and auditable
    sb = _state({"A": 40.0})
    board = _run(monkeypatch, sb)
    fresh = float(board[0]["pr"])
    hist = _written(sb, "bngc_comp_pr_history")
    assert float(hist[0]["pr"]) == pytest.approx(fresh)
    assert float(hist[0]["pr"]) < 40.0


def test_each_player_is_compared_against_their_own_stored_pr(monkeypatch):
    # one shared ratchet value would be wrong: the high-water mark is per
    # player, so A's stored 99 must not drag B up to it
    sb = _state({"A": 99.0, "B": 0.1},
                extra_events=[{"match_id": 1, "etype": "kill",
                               "discord_id": "B", "actor_name": "a",
                               "alive": None}])
    _run(monkeypatch, sb)
    got = {p["discord_id"]: float(p["tourney_pr"]) for p in _written(sb)}
    assert got["A"] == 99.0
    assert got["B"] < 99.0


def test_a_null_stored_pr_is_treated_as_no_prior(monkeypatch):
    sb = _state({"A": None})
    board = _run(monkeypatch, sb)
    assert float(_written(sb)[0]["tourney_pr"]) == pytest.approx(float(board[0]["pr"]))


def test_junk_stored_pr_does_not_break_the_write(monkeypatch):
    sb = _state({"A": "not-a-number"})
    board = _run(monkeypatch, sb)
    assert float(_written(sb)[0]["tourney_pr"]) == pytest.approx(float(board[0]["pr"]))


# --- reproducibility -------------------------------------------------------

def test_the_event_read_is_ordered(monkeypatch):
    # an unordered LIMIT means two identical calls can return different rows,
    # which is what made the board churn under a ratchet nobody could explain
    sb = _state({"A": None})
    _run(monkeypatch, sb)
    ev_reads = [r for r in sb.reads if r[0] == "bngc_comp_events"]
    assert ev_reads, "no event read recorded"
    assert ev_reads[0][1] is not None, f"unordered event read: {ev_reads}"


def test_recomputing_the_same_tourney_is_stable(monkeypatch):
    sb = _state({"A": None})
    first = _run(monkeypatch, sb)
    sb.t["bngc_comp_pr_history"] = []          # simulate a pr-reset
    sb.writes.clear()
    second = _run(monkeypatch, sb)
    assert [r["pr"] for r in first] == [r["pr"] for r in second]


def test_existing_history_short_circuits_and_still_ratchets(monkeypatch):
    # the idempotency path returns the stored board without recomputing - but a
    # player whose column was lowered elsewhere must still not be pushed down
    sb = _state({"A": 40.0})
    sb.t["bngc_comp_pr_history"] = [
        {"tourney_id": 7, "discord_id": "A", "pr": 2.0, "kills": 1, "wins": 0,
         "matches": 1, "avg_placement": 1.0, "players": 2.0}]
    board = _run(monkeypatch, sb)
    assert float(board[0]["pr"]) == 2.0
    assert float(_written(sb)[0]["tourney_pr"]) == 40.0
