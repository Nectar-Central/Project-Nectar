"""Tourney PR: round(2*(kills + 3*wins + max(0, players-avg+1))/players, 1)."""
from server.routes.comp import apply_adjustments, compute_tourney_pr


def _board():
    return [
        {"discord_id": "a", "name": "A", "kills": 10, "wins": 2,
         "points": 0, "matches": 2, "avg_placement": 2.0},
        {"discord_id": "b", "name": "B", "kills": 0, "wins": 0,
         "points": 0, "matches": 1, "avg_placement": 4.0},
        {"discord_id": "c", "name": "C", "kills": 5, "wins": 0,
         "points": 0, "matches": 2, "avg_placement": None},
    ]


def _pm4():
    rows = [{"discord_id": d} for d in ("a", "b", "c", "d")]
    return [rows, rows]


def test_pr_formula_and_order():
    out = compute_tourney_pr(_board(), _pm4())
    by = {r["discord_id"]: r["pr"] for r in out}
    # players=4: a -> 2*(10+6+3)/4=9.5, b -> 2*(0+0+1)/4=0.5, c -> 2*5/4=2.5
    assert by == {"a": 9.5, "c": 2.5, "b": 0.5}
    assert [r["discord_id"] for r in out] == ["a", "c", "b"]


def test_pr_empty_without_matches():
    assert compute_tourney_pr(_board(), []) == []
    assert compute_tourney_pr([], _pm4()) == []


def test_adjustments_fold_into_board():
    board = [{"discord_id": "a", "name": "A", "kills": 5, "wins": 1,
              "points": 20, "matches": 2, "avg_placement": 2.0}]
    out = apply_adjustments(board, [{"discord_id": "a", "delta_points": -7}])
    assert out[0]["points"] == 13
    assert out[0]["adjusted"] == -7
    # unknown player gets a row so the ledger never vanishes on reconcile
    out = apply_adjustments(out, [{"discord_id": "z", "delta_points": 4}])
    z = next(r for r in out if r["discord_id"] == "z")
    assert z["points"] == 4
    # empty ledger is a no-op
    assert apply_adjustments([{"discord_id": "a", "points": 1, "kills": 0}], [])[0]["points"] == 1
