"""Duel scoring must pair wins with the kills of the match they ended.

Engine versions bump on stG=3 (match END), and the processor drains the win
after the bump — so every match's kills sit one version BELOW its win row
(v0 kills + v1 win for a room's first match). Grouping used to reject v0
rows outright and file wins under the post-bump version, so kills and wins
could never share a group and wins-races never sealed a thing.

Rules pinned here: wins file under the played version (ver-1), v0 counts in
real coded rooms (lobby rows stay rejected), room_code/gm filters intact.
Pure functions, no Discord, no DB.
"""
import importlib.util
import sys
from pathlib import Path


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_scoring_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RID = "77dc5908-b709-4f59-bdc2-4e4149e61e72"
CREATOR = "111"
OPPONENT = "222"
SINCE = "2026-01-01T00:00:00+00:00"
TS = "2026-09-23T14:32:14+00:00"


def _ev(did, et, ver=0, rid=RID, gm="zw_lg", code="LCKGT", extra=None, ts=TS):
    gd = {"room_id": rid, "match_version": ver, "gm": gm,
          "short_code": code, "code": code}
    gd.update(extra or {})
    return {"discord_id": did, "event_type": et, "event_data": gd,
            "created_at": ts}


def _opp_kill_rows(ver, ts=TS):
    """The real 14:12 shape: a mobile opponent (no capture, so no win rows)
    eliminated the tracked creator. killfeed + death, exactly what the
    tracked session persisted."""
    return [
        _ev(CREATOR, "killfeed", ver=ver,
            extra={"kill_type": "killed", "instigator": "chris7",
                   "instigated": "cis6led"}, ts=ts),
        _ev(CREATOR, "death", ver=ver, ts=ts),
    ]


def _match1_rows():
    """The real 14:32 shape: v0 kill + killfeed, v1 win."""
    return [
        _ev(CREATOR, "kill", ver=0, extra={"victim": "chris7"}),
        _ev(CREATOR, "killfeed", ver=0,
            extra={"kill_type": "killed", "instigator": "cis6led",
                   "instigated": "chris7"}),
        _ev(OPPONENT, "death", ver=0),
        _ev(CREATOR, "win", ver=1, extra={"winner": "cis6led"}),
    ]


def test_win_files_under_played_version_with_kills():
    duels = _load_duels()
    groups = duels.group_duel_matches(
        _match1_rows(), CREATOR, OPPONENT, "zw_lg", None, None, SINCE)
    assert len(groups) == 1
    g = groups[0]
    assert (g["room_id"], g["match_version"]) == (RID, 0)
    assert g["kills_c"] == 1 and g["win_id"] == CREATOR
    assert duels.score_match(g["kills_c"], g["kills_o"], g["win_id"],
                             CREATOR, OPPONENT) == CREATOR


def test_mobile_seal_via_name_matched_killfeed():
    duels = _load_duels()
    groups = duels.group_mobile_matches(
        _match1_rows(), CREATOR, "chris7", "zw_lg", None, None, SINCE)
    assert len(groups) == 1
    g = groups[0]
    assert g["match_version"] == 0 and g["win_id"] == CREATOR
    assert duels.score_match(g["kills_c"], g["kills_o"], g["win_id"],
                             CREATOR, OPPONENT) == CREATOR


def test_lobby_rows_never_group():
    duels = _load_duels()
    rows = [_ev(CREATOR, "kill", ver=1, rid="lobby", code=""),
            _ev(CREATOR, "win", ver=2, rid="lobby", code="")]
    assert duels.group_duel_matches(
        rows, CREATOR, OPPONENT, "zw_lg", None, None, SINCE) == []
    assert duels.group_mobile_matches(
        rows, CREATOR, "chris7", "zw_lg", None, None, SINCE) == []


def test_room_code_mismatch_filters_all():
    duels = _load_duels()
    assert duels.group_duel_matches(
        _match1_rows(), CREATOR, OPPONENT, "zw_lg", None, "ZZBBC", SINCE) == []
    assert duels.group_mobile_matches(
        _match1_rows(), CREATOR, "chris7", "zw_lg", None, "ZZBBC", SINCE) == []
    # ...while the played code groups fine.
    assert len(duels.group_duel_matches(
        _match1_rows(), CREATOR, OPPONENT, "zw_lg", None, "LCKGT", SINCE)) == 1


def test_lonely_win_without_killfeed_names_does_not_seal_mobile():
    duels = _load_duels()
    rows = [_ev(CREATOR, "win", ver=1, extra={"winner": "cis6led"})]
    assert duels.group_mobile_matches(
        rows, CREATOR, "chris7", "zw_lg", None, None, SINCE) == []


def test_gm_mismatch_drops():
    duels = _load_duels()
    rows = [_ev(CREATOR, "kill", ver=1, gm="df"),
            _ev(OPPONENT, "death", ver=1, gm="df")]
    assert duels.group_duel_matches(
        rows, CREATOR, OPPONENT, "zw_lg", None, None, SINCE) == []


def _leave_rows():
    """Post-match leave traffic (the v2 shape that sealed a phantom Draw)."""
    return [
        _ev(CREATOR, "leave", ver=2, extra={"player": "chris7"}),
        _ev(CREATOR, "killfeed", ver=2,
            extra={"kill_type": "left", "instigator": "chris7",
                   "instigated": None}),
    ]


def test_leave_rows_never_group():
    duels = _load_duels()
    assert duels.group_duel_matches(
        _leave_rows(), CREATOR, OPPONENT, "zw_lg", None, None, SINCE) == []
    assert duels.group_mobile_matches(
        _leave_rows(), CREATOR, "chris7", "zw_lg", None, None, SINCE) == []


def test_empty_groups_not_sealable():
    duels = _load_duels()
    # wins race (default): ONLY a real win seals — kills alone can never
    # hand out a match win or pollute history.
    assert duels._match_sealable({"kills_c": 1, "kills_o": 0}, "wins") is False
    assert duels._match_sealable(
        {"kills_c": 0, "kills_o": 0, "win_id": CREATOR}, "wins") is True
    # kills race: real combat seals.
    assert duels._match_sealable({"kills_c": 1, "kills_o": 0}, "kills") is True
    assert duels._match_sealable({"kills_c": 0, "kills_o": 0}, "kills") is False
    assert duels._match_sealable({}) is False


def test_wins_race_never_decides_on_kills():
    # A kills-only group in a wins race is a DRAW (no score), never a
    # match win — kills are a display tiebreak, not the objective.
    duels = _load_duels()
    assert duels.score_match(5, 0, None, CREATOR, OPPONENT, race="wins") is None
    assert duels.score_match(0, 5, None, CREATOR, OPPONENT, race="wins") is None
    # A real win still decides.
    assert duels.score_match(0, 9, CREATOR, CREATOR, OPPONENT,
                             race="wins") == CREATOR
    # kills race still uses kills.
    assert duels.score_match(3, 1, None, CREATOR, OPPONENT,
                             race="kills") == CREATOR
    assert duels.score_match(1, 1, None, CREATOR, OPPONENT,
                             race="kills") is None


def test_leave_noise_does_not_shadow_real_match():
    # Leave rows in a LATER version must not disturb the real match group.
    duels = _load_duels()
    groups = duels.group_mobile_matches(
        _match1_rows() + _leave_rows(), CREATOR, "chris7",
        "zw_lg", None, None, SINCE)
    assert [(g["match_version"], g["kills_c"], g["win_id"])
            for g in groups] == [(0, 1, CREATOR)]


def test_roster_gate_ignores_kills_on_bystanders():
    # A private zw_lg room is full of strangers: only kills whose VICTIM is
    # the other duelist may count.
    duels = _load_duels()
    rows = [
        _ev(CREATOR, "kill", ver=1, extra={"victim": "SomeRandomGuy"}),
        _ev(OPPONENT, "kill", ver=1, extra={"victim": "AnotherStranger"}),
    ]
    g = duels.group_duel_matches(rows, CREATOR, OPPONENT, "zw_lg", None, None,
                                 SINCE, opp_name="chris7", creator_name="cis6led")
    assert len(g) == 1
    assert g[0]["kills_c"] == 0 and g[0]["kills_o"] == 0


def test_roster_gate_counts_kills_on_the_opponent():
    duels = _load_duels()
    rows = [
        _ev(CREATOR, "kill", ver=1, extra={"victim": "chris7"}),
        _ev(OPPONENT, "kill", ver=1, extra={"victim": "cis6led"}),
    ]
    g = duels.group_duel_matches(rows, CREATOR, OPPONENT, "zw_lg", None, None,
                                 SINCE, opp_name="chris7", creator_name="cis6led")
    assert g[0]["kills_c"] == 1 and g[0]["kills_o"] == 1


def test_mobile_roster_gate_ignores_bystander_kills():
    duels = _load_duels()
    rows = [
        _ev(CREATOR, "kill", ver=1, extra={"victim": "RandomPlayer"}),
    ]
    g = duels.group_mobile_matches(rows, CREATOR, "chris7", "zw_lg", None, None,
                                   SINCE)
    # No opponent-name row at all -> group dropped entirely.
    assert g == []
    rows2 = [
        _ev(CREATOR, "kill", ver=1, extra={"victim": "chris7"}),
        _ev(CREATOR, "killfeed", ver=1,
            extra={"kill_type": "killed", "instigator": "chris7",
                   "instigated": "cis6led"}),
    ]
    g2 = duels.group_mobile_matches(rows2, CREATOR, "chris7", "zw_lg", None, None,
                                    SINCE)
    assert g2 and g2[0]["kills_c"] == 1 and g2[0]["kills_o"] == 1


def test_event_room_accepts_v0_real_room():
    duels = _load_duels()
    assert duels._event_room(_ev(CREATOR, "kill", ver=0)) == (
        RID, 0, "zw_lg", "LCKGT")
    assert duels._event_room(_ev(CREATOR, "kill", ver=0, rid="lobby",
                                 code="")) is None
    assert duels._event_room({"event_data": {"room_id": RID}}) is None


# --- Mobile opponent wins (no capture on their side => no win rows) ---------
# The processor only writes a win row for the locally captured player, so an
# uncaptured opponent's round wins are never persisted. A wins-race then
# counted only the tracked creator's win rows and scored 1-0 after chris7
# took v0+v1. When the tracked player has NO win row and the last roster
# combat event in that version is "opponent eliminated tracked player", the
# round belongs to the opponent.


def test_mobile_opponent_elimination_scores_the_round():
    duels = _load_duels()
    g = duels.group_mobile_matches(_opp_kill_rows(0), CREATOR, "chris7",
                                   "zw_lg", None, None, SINCE)
    assert len(g) == 1
    m = g[0]
    assert (m["kills_c"], m["kills_o"], m["win_id"]) == (0, 1, None)
    assert m["opponent_win"] is True
    assert duels._match_sealable(m, "wins") is True
    assert duels.score_match(m["kills_c"], m["kills_o"], m["win_id"],
                             CREATOR, OPPONENT, race="wins",
                             opponent_win=m["opponent_win"]) == OPPONENT


def test_creator_striking_last_is_not_an_opponent_win():
    # Creator killed the opponent AFTER being killed: respawn, traded it back.
    # No win row for the creator means the server denied his win, so this must
    # stay a Draw (WIN-DENIED path), never hand the round to the opponent.
    duels = _load_duels()
    rows = _opp_kill_rows(0, ts="2026-09-23T14:12:21+00:00") + [
        _ev(CREATOR, "kill", ver=0, extra={"victim": "chris7"},
            ts="2026-09-23T14:12:30+00:00"),
    ]
    m = duels.group_mobile_matches(rows, CREATOR, "chris7", "zw_lg",
                                   None, None, SINCE)[0]
    assert (m["kills_c"], m["kills_o"]) == (1, 1)
    assert m["opponent_win"] is False
    assert duels._match_sealable(m, "wins") is False


def test_creator_win_row_outranks_an_earlier_elimination():
    # Elimination first, then the creator survived to the phase end and the
    # server filed his win: the win row is authoritative.
    duels = _load_duels()
    rows = _opp_kill_rows(0, ts="2026-09-23T14:12:21+00:00") + [
        _ev(CREATOR, "win", ver=1, extra={"winner": "cis6led"},
            ts="2026-09-23T14:12:24+00:00"),
    ]
    m = duels.group_mobile_matches(rows, CREATOR, "chris7", "zw_lg",
                                   None, None, SINCE)[0]
    assert m["win_id"] == CREATOR
    assert duels.score_match(m["kills_c"], m["kills_o"], m["win_id"],
                             CREATOR, OPPONENT, race="wins",
                             opponent_win=m["opponent_win"]) == CREATOR


def test_opponent_win_inference_ignores_bystander_kills():
    # A stranger killing the creator proves nothing about the wager roster.
    duels = _load_duels()
    rows = [_ev(CREATOR, "killfeed", ver=0,
                extra={"kill_type": "killed", "instigator": "SomeRandomGuy",
                       "instigated": "cis6led"})]
    assert duels.group_mobile_matches(
        rows, CREATOR, "chris7", "zw_lg", None, None, SINCE) == []


def test_kills_race_ignores_opponent_win_inference():
    duels = _load_duels()
    # df duels decide on kills only; the flag must not leak into that race.
    assert duels.score_match(3, 1, None, CREATOR, OPPONENT, race="kills",
                             opponent_win=True) == CREATOR
    assert duels.score_match(0, 2, None, CREATOR, OPPONENT, race="kills",
                             opponent_win=True) == OPPONENT


# --- zone / fall damage: a death with NO killfeed --------------------------
# Zone damage and fall damage never appear in a killfeed — the only evidence
# is an `alv` elimination, which the processor files as a `death` row with
# killer=None. The mobile grouper used to require the opponent's name in a
# killfeed row for co-presence, so the whole version was dropped ("NO MATCH")
# and a zone death could never decide a round.


def _zone_death_rows(ver=0, ts="2026-09-23T16:35:40+00:00"):
    return [_ev(CREATOR, "death", ver=ver,
                extra={"killer": None, "via": "alv", "name": "cis6led"}, ts=ts)]


def test_mobile_zone_death_without_killfeed_scores_the_round():
    duels = _load_duels()
    g = duels.group_mobile_matches(_zone_death_rows(0), CREATOR, "chris7",
                                   "zw_lg", None, None, SINCE)
    assert len(g) == 1
    m = g[0]
    assert (m["kills_c"], m["kills_o"], m["win_id"]) == (0, 0, None)
    assert m["death_c"] == 1
    assert m["opponent_win"] is True
    assert duels._match_sealable(m, "wins") is True
    assert duels.score_match(m["kills_c"], m["kills_o"], m["win_id"],
                             CREATOR, OPPONENT, race="wins",
                             opponent_win=m["opponent_win"]) == OPPONENT


def test_zone_death_is_combat_evidence_for_the_win_rejection_guard():
    # A zone death IS the combat: the "win without combat" guard must not
    # throw the round away just because nobody landed a kill.
    duels = _load_duels()
    m = duels.group_mobile_matches(_zone_death_rows(0), CREATOR, "chris7",
                                   "zw_lg", None, None, SINCE)[0]
    evidence = int(m["kills_c"]) + int(m["kills_o"]) + int(m["death_c"])
    assert evidence > 0


def test_creator_striking_back_after_a_zone_death_is_not_an_opponent_win():
    # Respawned and traded it back: the creator's own kill is the last
    # evidence, so the round must NOT be handed to the opponent.
    duels = _load_duels()
    rows = _zone_death_rows(0, ts="2026-09-23T16:35:40+00:00") + [
        _ev(CREATOR, "kill", ver=0, extra={"victim": "chris7"},
            ts="2026-09-23T16:36:10+00:00"),
    ]
    m = duels.group_mobile_matches(rows, CREATOR, "chris7", "zw_lg",
                                   None, None, SINCE)[0]
    assert m["opponent_win"] is False
    assert m["kills_c"] == 1


def test_a_lone_win_row_still_needs_co_presence():
    # Regression guard: a win row with no death and no killfeed names is not
    # enough to invent a round.
    duels = _load_duels()
    assert duels.group_mobile_matches(
        [_ev(CREATOR, "win", ver=1, extra={"winner": "cis6led"})],
        CREATOR, "chris7", "zw_lg", None, None, SINCE) == []


def test_both_captured_group_also_counts_creator_deaths():
    duels = _load_duels()
    rows = [_ev(CREATOR, "death", ver=0, extra={"via": "alv"}),
            _ev(OPPONENT, "kill", ver=0, extra={"victim": "cis6led"})]
    g = duels.group_duel_matches(rows, CREATOR, OPPONENT, "zw_lg", None, None,
                                 SINCE, opp_name="chris7", creator_name="cis6led")
    assert len(g) == 1 and g[0]["death_c"] == 1
