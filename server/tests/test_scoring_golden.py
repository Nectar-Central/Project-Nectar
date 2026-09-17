"""Golden cross-tests for the unified scoring brain (lib/scoring.py).

Pins the invariants that made the unification safe, so future edits can't
silently fork the math again:
  - comp.py and ranked_tourney.py re-export the SAME function objects
    (no more byte-twin copies to drift)
  - lib/ranked.py no longer imports the engine from routes (no layering
    inversion, no circular-import trap)
  - one golden event set scores STRUCTURALLY identically under the static
    strategy (comp) and the dynamic strategy (ranked) when the dynamic
    kill awards are chosen to sum to the same values
  - recalc-replay: rescoring a sealed match from its frozen dynamic table
    reproduces the exact same rows (the promise _recalc_sync relies on)
  - legacy tables without kill_awards replay with zero kill contribution
No network, no fakes: the engine is pure.
"""
import inspect

from server.lib import scoring
from server.routes import comp as comp_mod
from server.routes import ranked_tourney as rt_mod
from server.lib import ranked as ranked_lib


def test_imports_converge_on_one_implementation():
    # identity, not equality: there is exactly ONE engine object
    assert comp_mod.compute_match_points is scoring.compute_match_points
    assert rt_mod.compute_match_points is scoring.compute_match_points
    assert comp_mod.aggregate_standings is scoring.aggregate_standings
    assert rt_mod.aggregate_standings is scoring.aggregate_standings
    assert comp_mod.group_standings_by_team is scoring.group_standings_by_team
    assert rt_mod.group_standings_by_team is scoring.group_standings_by_team
    assert comp_mod.compute_tourney_pr is scoring.compute_tourney_pr
    assert rt_mod.compute_tourney_pr is scoring.compute_tourney_pr
    assert comp_mod.DEFAULT_TABLE is scoring.DEFAULT_TABLE
    assert rt_mod.DEFAULT_TABLE is scoring.DEFAULT_TABLE


def test_forks_are_gone_and_layering_fixed():
    src_comp = inspect.getsource(comp_mod)
    src_rt = inspect.getsource(rt_mod)
    assert "def compute_match_points" not in src_comp
    assert "def compute_match_points" not in src_rt
    assert "def aggregate_standings" not in src_comp
    assert "def aggregate_standings" not in src_rt
    # lib must not reach into routes anymore (was the circular-import trap)
    assert "routes.comp" not in inspect.getsource(ranked_lib)


# The golden event set: 4 players, two kills by A (one on B, one on C),
# a zone death for D (no killfeed), alv snapshot in death order with the
# survivor LAST (bank convention: every alv row is alive=False; the last
# row reads as the winner).
GOLDEN_EVENTS = [
    {"etype": "kill", "discord_id": "A", "actor_name": "alice",
     "target_name": "dave"},
    {"etype": "kill", "discord_id": "A", "actor_name": "alice",
     "target_name": "bobby"},
    {"etype": "knock", "discord_id": "E", "actor_name": "eddie"},  # sub boards at 0
    {"etype": "alv", "discord_id": "D", "actor_name": "dave", "alive": False},
    {"etype": "alv", "discord_id": "C", "actor_name": "carol", "alive": False},
    {"etype": "alv", "discord_id": "B", "actor_name": "bobby", "alive": False},
    {"etype": "alv", "discord_id": "A", "actor_name": "alice", "alive": False},
]

STATIC_TABLE = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}

# Dynamic table with the SAME purse: kill_awards chosen so A's two kills
# pay exactly what the static 1-pt/kill would (upset split across awards).
DYNAMIC_TABLE = {
    "mode": "dynamic-v1", "avg_points": 750.0, "players": 5,
    "kill": None, "win": 3, "placement": [10, 7, 5, 3, 1],
    "kill_awards": [
        {"killer": "A", "victim": "D", "nth": 1, "points": 1},
        {"killer": "A", "victim": "B", "nth": 2, "points": 1},
    ],
    "loss_awards": {"A": -5, "B": -5, "C": -5, "D": -5},
}


def _by_did(rows):
    return {r["discord_id"]: r for r in rows}


def test_golden_static_shape():
    rows = _by_did(scoring.compute_match_points(GOLDEN_EVENTS, STATIC_TABLE))
    assert rows["A"] == {"discord_id": "A", "name": "alice", "kills": 2,
                         "placement": 1, "won": True, "points": 2 + 3 + 10}
    assert rows["B"] == {"discord_id": "B", "name": "bobby", "kills": 0,
                         "placement": 2, "won": False, "points": 7}
    assert rows["C"]["placement"] == 3 and rows["C"]["points"] == 5
    assert rows["D"]["placement"] == 4 and rows["D"]["points"] == 3
    # the knock-only sub boards at 0 with no placement
    assert rows["E"]["points"] == 0 and rows["E"]["placement"] is None
    # board order: points desc
    order = [r["discord_id"] for r in scoring.compute_match_points(GOLDEN_EVENTS, STATIC_TABLE)]
    assert order == ["A", "B", "C", "D", "E"]


def test_golden_cross_strategy_structural_parity():
    static = scoring.compute_match_points(GOLDEN_EVENTS, STATIC_TABLE)
    dynamic = scoring.dynamic_rows(GOLDEN_EVENTS, DYNAMIC_TABLE, None,
                                   kill_awards=DYNAMIC_TABLE["kill_awards"])
    assert len(static) == len(dynamic)
    # same universe, same per-player facts (kills/placement/won/name)
    for s, d in zip(static, dynamic):
        assert s["discord_id"] == d["discord_id"]
        assert s["kills"] == d["kills"]
        assert s["placement"] == d["placement"]
        assert s["won"] == d["won"]
        assert s["name"] == d["name"]
    # and with the awards summing to the static kill value, points agree too
    sby, dby = _by_did(static), _by_did(dynamic)
    for did in sby:
        assert sby[did]["points"] == dby[did]["points"], did


def test_golden_dynamic_pays_upset_kills_not_flat():
    # same events, but the upset curve pays asymmetrically: the two kills
    # are worth 15 and 4 instead of 1 and 1. Placements/wins identical to
    # static; only kill economics move.
    upset = dict(DYNAMIC_TABLE, kill_awards=[
        {"killer": "A", "victim": "D", "nth": 1, "points": 15},
        {"killer": "A", "victim": "B", "nth": 2, "points": 4},
    ])
    rows = _by_did(scoring.dynamic_rows(GOLDEN_EVENTS, upset, None,
                                        kill_awards=upset["kill_awards"]))
    assert rows["A"]["points"] == 10 + 3 + 15 + 4
    assert rows["A"]["placement"] == 1 and rows["A"]["won"] is True
    assert rows["B"]["points"] == 7  # purse untouched


def test_recalc_replay_reproduces_frozen_table():
    # Seal: score live, freeze the table + the exact awards used.
    awards = ranked_lib._kill_awards_for(
        ranked_lib._event_kill_pairs(GOLDEN_EVENTS, {}), {"A": 1200}, 750.0)
    table = ranked_lib.dynamic_table_for(750.0, 5, kill_awards=awards)
    live = scoring.dynamic_rows(GOLDEN_EVENTS, table, None, kill_awards=awards)
    frozen_points = {r["discord_id"]: r["points"] for r in live}
    # Replay (recalc): rescore from the FROZEN table, no fresh economy.
    replay = scoring.dynamic_rows(GOLDEN_EVENTS, table, None,
                                  kill_awards=table.get("kill_awards"))
    assert _by_did(replay) == _by_did(live)
    assert {r["discord_id"]: r["points"] for r in replay} == frozen_points
    # and a second replay diffs to zero (the _recalc_sync invariant)
    replay2 = scoring.dynamic_rows(GOLDEN_EVENTS, table, None,
                                   kill_awards=table.get("kill_awards"))
    assert _by_did(replay2) == _by_did(replay)


def test_legacy_replay_without_kill_awards_scores_zero_kills():
    # Pre-awards tables (mode dynamic-v1 but no kill_awards persisted):
    # replay keeps the purse (win/placement) and pays nothing per kill.
    legacy = dict(DYNAMIC_TABLE)
    legacy.pop("kill_awards")
    rows = _by_did(scoring.dynamic_rows(GOLDEN_EVENTS, legacy, None))
    assert rows["A"]["points"] == 10 + 3  # win + P1, no kill points
    assert rows["A"]["kills"] == 2
    assert rows["B"]["points"] == 7


def test_aggregation_and_pr_parity_across_strategies():
    # two matches: golden + a rematch with manual placements on B
    m2_events = GOLDEN_EVENTS + []
    static_m1 = scoring.compute_match_points(GOLDEN_EVENTS, STATIC_TABLE)
    dynamic_m1 = scoring.dynamic_rows(GOLDEN_EVENTS, DYNAMIC_TABLE, None,
                                      kill_awards=DYNAMIC_TABLE["kill_awards"])
    static_m2 = scoring.compute_match_points(
        m2_events, STATIC_TABLE, placements={"B": 1})
    dynamic_m2 = scoring.dynamic_rows(
        m2_events, DYNAMIC_TABLE, None, placements={"B": 1},
        kill_awards=DYNAMIC_TABLE["kill_awards"])
    agg_static = scoring.aggregate_standings([static_m1, static_m2])
    agg_dynamic = scoring.aggregate_standings([dynamic_m1, dynamic_m2])
    assert len(agg_static) == len(agg_dynamic)
    for s, d in zip(agg_static, agg_dynamic):
        assert s["discord_id"] == d["discord_id"]
        assert s["kills"] == d["kills"]
        assert s["wins"] == d["wins"]
        assert s["matches"] == d["matches"]
        assert s["avg_placement"] == d["avg_placement"]
        assert s["points"] == d["points"]
    # team fold + PR read the same off either strategy
    regs = [{"team_label": "Gold", "member_ids": ["A", "B"], "status": "accepted"}]
    teams_s = scoring.group_standings_by_team(agg_static, regs)
    teams_d = scoring.group_standings_by_team(agg_dynamic, regs)
    assert [t["points"] for t in teams_s] == [t["points"] for t in teams_d]
    assert [t["name"] for t in teams_s] == [t["name"] for t in teams_d]
    pr_s = {r["discord_id"]: r["pr"] for r in
            scoring.compute_tourney_pr(agg_static, [static_m1, static_m2])}
    pr_d = {r["discord_id"]: r["pr"] for r in
            scoring.compute_tourney_pr(agg_dynamic, [dynamic_m1, dynamic_m2])}
    assert pr_s == pr_d
    assert pr_s["A"] > pr_s["C"]  # PR orders the dominant player first
