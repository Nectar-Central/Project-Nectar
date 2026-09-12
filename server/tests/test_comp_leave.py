"""Leave removal: excluded players ignored in placement and calculations.

Pure-function tests on compute_match_points + standings_for_matches.
No network.
"""
from server.routes import comp as comp_mod

TABLE = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}


def _evs():
    # A kills B x2, C kills A; death order: B out first, A out, C last alive.
    return [
        {"etype": "kill", "discord_id": "A", "actor_name": "An", "alive": None},
        {"etype": "kill", "discord_id": "A", "actor_name": "An", "alive": None},
        {"etype": "kill", "discord_id": "C", "actor_name": "Cn", "alive": None},
        {"etype": "alv", "discord_id": "B", "actor_name": "Bn", "alive": False},
        {"etype": "alv", "discord_id": "A", "actor_name": "An", "alive": False},
    ]


def test_removed_ignored_in_board():
    board = comp_mod.compute_match_points(_evs(), TABLE, None, None, excluded={"A"})
    dids = [r["discord_id"] for r in board]
    assert "A" not in dids
    # B is last in the remaining death order so B takes P1; C keeps its kill.
    assert board[0]["discord_id"] == "B"
    assert board[0]["won"] is True
    assert [r["kills"] for r in board if r["discord_id"] == "C"] == [1]


def test_removed_winner_falls_back():
    board = comp_mod.compute_match_points(
        _evs(), TABLE, winner_override="A", excluded=["A"])
    assert all(r["discord_id"] != "A" for r in board)
    assert board[0]["discord_id"] == "B"


def test_removed_manual_placement_ignored():
    board = comp_mod.compute_match_points(
        _evs(), TABLE, None, {"A": 1, "C": 2}, excluded={"A"})
    assert all(r["discord_id"] != "A" for r in board)
    assert all(r["discord_id"] != "A" for r in board)


def test_standings_excludes_removed():
    tourney = {"points_table": TABLE}
    sealed = [{"id": 1, "winner_discord_id": None, "placements": {},
               "removed_discord_ids": ["A"]}]
    by_mid = {1: _evs()}
    per, board = comp_mod.standings_for_matches(tourney, sealed, by_mid, [])
    assert all(r["discord_id"] != "A" for r in board)
    assert all(r["discord_id"] != "A" for r in per[0])


def test_no_excluded_behaves_as_before():
    b1 = comp_mod.compute_match_points(_evs(), TABLE)
    b2 = comp_mod.compute_match_points(_evs(), TABLE, None, None, None)
    assert [r["discord_id"] for r in b1] == [r["discord_id"] for r in b2]
