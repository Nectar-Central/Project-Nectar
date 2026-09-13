"""Manual awards, winner fix, zero clamp on compute_match_points. Pure, no network."""
from server.routes import comp as comp_mod

TABLE = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}


def _evs():
    return [
        {"etype": "kill", "discord_id": "A", "actor_name": "An", "alive": None},
        {"etype": "kill", "discord_id": "A", "actor_name": "An", "alive": None},
        {"etype": "alv", "discord_id": "B", "actor_name": "Bn", "alive": False},
        {"etype": "alv", "discord_id": "A", "actor_name": "An", "alive": False},
    ]


def test_winner_override_without_rows():
    # manual blank match: winner sticks even with zero events
    board = comp_mod.compute_match_points([], TABLE, winner_override="C")
    assert [r["discord_id"] for r in board] == ["C"]
    assert board[0]["won"] is True
    assert board[0]["points"] == 3  # win bonus only, no placement


def test_winner_override_beats_alv_order():
    board = comp_mod.compute_match_points(_evs(), TABLE, winner_override="B")
    b = next(r for r in board if r["discord_id"] == "B")
    assert b["won"] is True  # crown + win bonus move, sort stays points-based


def test_manual_awards_folded():
    board = comp_mod.compute_match_points(_evs(), TABLE, awards={"B": 5})
    b = next(r for r in board if r["discord_id"] == "B")
    assert b["points"] == 5 + 7  # award + P2 (B second in death order)


def test_award_only_player_appears():
    board = comp_mod.compute_match_points(_evs(), TABLE, awards={"Z": 7})
    z = next(r for r in board if r["discord_id"] == "Z")
    assert z["points"] == 7 and z["kills"] == 0


def test_totals_floored_at_zero():
    board = comp_mod.compute_match_points(_evs(), TABLE, awards={"B": -1000})
    b = next(r for r in board if r["discord_id"] == "B")
    assert b["points"] == 0


def test_adjustments_floored_at_zero():
    board = [{"discord_id": "A", "name": "An", "kills": 1, "wins": 0,
              "points": 4, "matches": 1, "avg_placement": 2.0}]
    out = comp_mod.apply_adjustments(board, [{"discord_id": "A", "delta_points": -50}])
    assert out[0]["points"] == 0
    assert out[0]["adjusted"] == -50
