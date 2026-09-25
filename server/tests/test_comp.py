"""Unit tests for comp standings math + status derivation (pure, no DB)."""
from server.routes.comp import (
    _next_version,
    _valid_table,
    aggregate_standings,
    apply_adjustments,
    compute_match_points,
    derive_status,
    group_standings_by_team,
    open_tourney_points,
    standings_for_matches,
)


def _ev(etype, did, name=None, alive=None):
    return {"etype": etype, "discord_id": did, "actor_name": name, "alive": alive}


def test_status_derivation():
    assert derive_status({"status": "ended"}, 5) == "ended"
    assert derive_status({"status": "scheduled"}, 0) == "scheduled"
    assert derive_status({"status": "created"}, 0) == "created"
    assert derive_status({"status": "started"}, 0) == "running"
    assert derive_status({"status": "started"}, 3) == "running"
    assert derive_status({}, 0) == "created"
    assert derive_status(None, 9) == "created"


def test_kills_plus_win_plus_placement():
    table = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}
    evs = [_ev("kill", "a", "A"), _ev("kill", "a", "A"), _ev("kill", "b", "B"),
           _ev("alv", "c", "C", False), _ev("alv", "b", "B", False), _ev("alv", "a", "A", False)]
    rows = compute_match_points(evs, table)
    # death order c,b,a -> placement a=1, b=2, c=3; winner a (last alive)
    by = {r["discord_id"]: r for r in rows}
    assert by["a"]["points"] == 2 + 3 + 10
    assert by["a"]["won"] is True and by["a"]["placement"] == 1
    assert by["b"]["points"] == 1 + 7
    assert by["c"]["points"] == 5
    assert rows[0]["discord_id"] == "a"


def test_winner_override():
    table = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}
    evs = [_ev("kill", "a", "A"), _ev("alv", "b", "B", False), _ev("alv", "a", "A", False)]
    rows = compute_match_points(evs, table, winner_override="b")
    by = {r["discord_id"]: r for r in rows}
    assert by["b"]["won"] is True
    # override moves the win bonus only: alv order still says b died 2nd
    assert by["b"]["points"] == 0 + 3 + 7
    assert by["b"]["placement"] == 2


def test_kills_only_without_alv():
    rows = compute_match_points([_ev("kill", "a", "A")])
    assert rows[0]["points"] == 1 and rows[0]["won"] is False
    assert rows[0]["placement"] is None


def test_unmapped_ignored_and_empty():
    assert compute_match_points([_ev("kill", None, "Ghost")]) == []
    assert compute_match_points([]) == []


def test_aggregate_sums_and_sorts():
    m1 = [{"discord_id": "a", "name": "A", "kills": 2, "won": True, "points": 15}]
    m2 = [{"discord_id": "a", "name": "A", "kills": 1, "won": False, "points": 1},
          {"discord_id": "b", "name": "B", "kills": 5, "won": True, "points": 18}]
    out = aggregate_standings([m1, m2])
    assert [(r["discord_id"], r["points"], r["matches"], r["wins"]) for r in out] == [
        ("b", 18, 1, 1), ("a", 16, 2, 1)]
    assert aggregate_standings([]) == []


def test_standings_for_matches_end_to_end():
    t = {"points_table": {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}}
    sealed = [{"id": 11, "winner_discord_id": None}, {"id": 12, "winner_discord_id": None}]
    evs = {11: [{"etype": "kill", "discord_id": "a", "actor_name": "A"},
                {"etype": "alv", "discord_id": "b", "actor_name": "B", "alive": False},
                {"etype": "alv", "discord_id": "a", "actor_name": "A", "alive": False}],
           12: [{"etype": "kill", "discord_id": "b", "actor_name": "B"}]}
    per_match, board = standings_for_matches(t, sealed, evs)
    assert len(per_match) == 2
    assert board[0]["discord_id"] == "a"  # 1 kill + win + P1 = 14 vs b: 1 + P2
    assert board[0]["points"] == 1 + 3 + 10
    assert standings_for_matches(None, [], {}) == ([], [])
    assert standings_for_matches(t, [{"id": 9}], {})[0] == [[]]


def test_open_tourney_points_fail_open():
    assert open_tourney_points([]) == {}
    assert open_tourney_points(None) == {}
    # no supabase configured in tests -> {} (never raises, never lists)
    assert open_tourney_points(["did1"]) == {}


def test_valid_table():
    good = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}
    assert _valid_table(good) == good
    assert _valid_table(None) is None
    assert _valid_table("x") is None
    assert _valid_table({"kill": -1, "win": 3, "placement": [1]}) is None
    assert _valid_table({"kill": 1, "win": 3, "placement": []}) is None
    assert _valid_table({"kill": 1, "win": 3, "placement": list(range(11))}) is None
    assert _valid_table({"kill": "a", "win": 3, "placement": [1]}) is None


def test_next_version_fail_open():
    assert _next_version(999) == 0  # no DB in tests -> safe default


def test_group_duo_teams_share_points():
    board = [
        {"discord_id": "a", "name": "cis6led", "kills": 2, "wins": 1, "points": 15, "matches": 2},
        {"discord_id": "b", "name": "chris7", "kills": 1, "wins": 0, "points": 4, "matches": 2},
        {"discord_id": "c", "name": "Solo", "kills": 5, "wins": 0, "points": 9, "matches": 1},
    ]
    regs = [{"team_label": "", "member_ids": ["a", "b"]}]
    names = {"a": "cis6led", "b": "chris7", "c": "Solo"}
    out = group_standings_by_team(board, regs, names)
    assert len(out) == 2
    team = out[0]
    assert team["team"] is True
    assert team["name"] == "@cis6led & @chris7"
    assert team["member_ids"] == ["a", "b"]
    assert (team["points"], team["kills"], team["wins"]) == (19, 3, 1)
    solo = out[1]
    assert solo["team"] is False and solo["discord_id"] == "c"


def test_group_duo_label_and_unregistered():
    board = [
        {"discord_id": "a", "name": "A", "kills": 1, "wins": 0, "points": 3, "matches": 1},
        {"discord_id": "ghost", "name": "Ghost", "kills": 2, "wins": 0, "points": 2, "matches": 1},
    ]
    regs = [{"team_label": "Champs", "member_ids": ["a", "b"]}]
    out = group_standings_by_team(board, regs, {"a": "A", "b": "B"})
    by_name = {r["name"]: r for r in out}
    assert by_name["Champs"]["points"] == 3
    assert by_name["Champs"]["member_ids"] == ["a", "b"]
    assert by_name["Ghost"]["team"] is False  # played, never registered: stands alone
    assert group_standings_by_team([], []) == []


def test_group_duo_mention_label_is_not_shown_as_a_name():
    # The bot stores the duo label as raw mention syntax ("<@a> / <@b>")
    # because that is what renders in a Discord thread. On the dashboard it
    # put those ids in the Player column instead of names.
    board = [{"discord_id": "1331576167966445703", "name": "Alice", "kills": 1,
              "wins": 0, "points": 3, "matches": 1},
             {"discord_id": "1530006126954024980", "name": "Bob", "kills": 0,
              "wins": 0, "points": 0, "matches": 1}]
    regs = [{"team_label": "<@1331576167966445703> / <@1530006126954024980>",
             "member_ids": ["1331576167966445703", "1530006126954024980"]}]
    names = {"1331576167966445703": "Alice", "1530006126954024980": "Bob"}
    team = group_standings_by_team(board, regs, names)[0]
    assert team["name"] == "@Alice & @Bob"
    assert "<@" not in team["name"]
    assert team["team_label"] is None


def test_group_duo_space_separated_mention_label_is_also_ignored():
    # /tourney-add-player writes "<@a> <@b>" (space, not " / ")
    board = [{"discord_id": "a", "name": "A", "kills": 1, "wins": 0,
              "points": 3, "matches": 1}]
    regs = [{"team_label": "<@a> <@b>", "member_ids": ["a", "b"]}]
    team = group_standings_by_team(board, regs, {"a": "A", "b": "B"})[0]
    assert team["name"] == "@A & @B"


def test_a_human_team_label_still_wins():
    board = [{"discord_id": "a", "name": "A", "kills": 1, "wins": 0,
              "points": 3, "matches": 1}]
    regs = [{"team_label": "Champs", "member_ids": ["a", "b"]}]
    team = group_standings_by_team(board, regs, {"a": "A", "b": "B"})[0]
    assert team["name"] == "Champs"
    assert team["team_label"] == "Champs"


def test_tied_p1_shares_win_bonus():
    evs = [
        {"etype": "kill", "discord_id": "a", "actor_name": "A"},
        {"etype": "kill", "discord_id": "a", "actor_name": "A"},
        {"etype": "alv", "discord_id": "a", "actor_name": "A", "alive": False},
        {"etype": "alv", "discord_id": "b", "actor_name": "B", "alive": False},
    ]
    rows = compute_match_points(evs, None, None, {"a": 1, "b": 1})
    by = {r["discord_id"]: r for r in rows}
    assert by["a"]["won"] and by["b"]["won"]
    assert by["a"]["points"] == 2 + 3 + 10
    assert by["b"]["points"] == 0 + 3 + 10


def test_single_p1_keeps_solo_win():
    evs = [
        {"etype": "alv", "discord_id": "a", "actor_name": "A", "alive": False},
        {"etype": "alv", "discord_id": "b", "actor_name": "B", "alive": False},
    ]
    rows = compute_match_points(evs, None, None, {"a": 1, "b": 2})
    by = {r["discord_id"]: r for r in rows}
    assert by["a"]["won"] and not by["b"]["won"]
    assert by["a"]["points"] == 3 + 10
    assert by["b"]["points"] == 7
