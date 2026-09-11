"""open_tourney_points must score on FULL event scope (regression).

Bug: events were pre-filtered per user (.in_(discord_id)), so every
player became the "winner" of their own filtered view (+win bonus, P1)
— drawer showed 118 pts / 9W vs the board's 106 / 7W. Full scope first,
user-row selection after.
"""
from server.routes import comp as comp_mod


class _R:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]

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
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def execute(self):
        return _R(self._rows)


class _SB:
    def __init__(self, tables):
        self.t = tables

    def table(self, name):
        return _Q(self.t.get(name, []))


TABLE = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}

TOURNEY = {"id": 3, "name": "Testing Cup", "status": "started",
           "points_table": dict(TABLE)}
MATCH = {"id": 11, "tourney_id": 3, "version": 0, "status": "sealed",
         "winner_discord_id": None, "placements": {}}
# A kills B; B dies first, A last -> A P1+win (14), B P2 (7)
EVENTS = [
    {"match_id": 11, "etype": "kill", "discord_id": "A", "actor_name": "a", "alive": None},
    {"match_id": 11, "etype": "alv", "discord_id": "B", "actor_name": "b", "alive": False},
    {"match_id": 11, "etype": "alv", "discord_id": "A", "actor_name": "a", "alive": False},
]


def _sb():
    return _SB({"bngc_comp_tourneys": [TOURNEY],
                "bngc_comp_matches": [MATCH],
                "bngc_comp_events": list(EVENTS)})


def test_loser_scores_p2_without_win_bonus(monkeypatch):
    """B lost: 7 pts, 0 wins. Filtered-scope code gave B 13 pts / 1 win."""
    monkeypatch.setattr(comp_mod, "supabase", _sb())
    out = comp_mod.open_tourney_points(["B"])
    assert out["B"]["points"] == 7
    assert out["B"]["kills"] == 0
    assert out["B"]["wins"] == 0
    assert out["B"]["tourney_name"] == "Testing Cup"


def test_winner_scores_full(monkeypatch):
    monkeypatch.setattr(comp_mod, "supabase", _sb())
    out = comp_mod.open_tourney_points(["A", "B"])
    assert out["A"]["points"] == 1 + 3 + 10
    assert out["A"]["wins"] == 1
    assert out["A"]["avg_placement"] == 1.0
    assert out["B"]["avg_placement"] == 2.0


def test_aggregate_avg_placement():
    m1 = [{"discord_id": "a", "name": "A", "kills": 2, "won": True, "points": 15, "placement": 1},
          {"discord_id": "b", "name": "B", "kills": 0, "won": False, "points": 7, "placement": 2}]
    m2 = [{"discord_id": "a", "name": "A", "kills": 0, "won": False, "points": 5, "placement": 3}]
    out = comp_mod.aggregate_standings([m1, m2])
    by = {r["discord_id"]: r for r in out}
    assert by["a"]["avg_placement"] == 2.0  # (1+3)/2
    assert by["b"]["avg_placement"] == 2.0
    # kills-only rows (no placement) -> None, never crash
    out2 = comp_mod.aggregate_standings(
        [[{"discord_id": "c", "name": "C", "kills": 1, "won": False, "points": 1}]])
    assert out2[0]["avg_placement"] is None


def test_team_avg_weighted(monkeypatch):
    board = [
        {"discord_id": "a", "name": "A", "kills": 2, "wins": 1, "points": 15,
         "matches": 2, "avg_placement": 1.0},
        {"discord_id": "b", "name": "B", "kills": 1, "wins": 0, "points": 4,
         "matches": 2, "avg_placement": 3.0},
    ]
    regs = [{"team_label": "", "member_ids": ["a", "b"]}]
    out = comp_mod.group_standings_by_team(board, regs, {"a": "A", "b": "B"})
    assert len(out) == 1
    assert out[0]["avg_placement"] == 2.0  # (1*2 + 3*2)/4
    assert out[0]["name"] == "@A & @B"
