"""An opponent eliminated by the zone/fall must score the round for the creator.

A duel needs only ONE side to have the companion, and the tracker sees the
whole room's `alive` packets - not just its own. The processor used to persist
a death row only when the alv subject was ME, so every elimination of the
COMPANION-LESS opponent was thrown away. That is unrecoverable for the exact
case it mattered: zone and fall damage never reach a killfeed, so with no
persisted row there was no evidence the round happened at all, the group was
dropped for missing co-presence, and the duel stalled until the AFK guard
cancelled it an hour later.

The observation is now persisted as a killfeed row with kill_type="out" (a new
value in an existing jsonb field - a new event_type would have leaked into
stats, insights and play points, which read bngc_game_events by type). The
processor cannot know who the opponent is, so it records "this player went
out" and the WAGER decides whether that name is the opponent - the same split
the mobile path already uses.

The round goes to the creator, but NO kill is credited to anyone: the creator
did not land it. Rules pinned here:

- opponent out (no killfeed)  -> seen, creator wins the round, 0 kills credited
- a bystander out              -> proves nothing, group still dropped
- the creator being eliminated afterwards -> not a creator win (last event wins)
- a df kills race ignores it entirely - that race is decided by kill order
- opp_out counts as combat, or the walkover guard would reject the win
"""
import importlib.util
import sys
from pathlib import Path


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_oppout_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RID = "77dc5908-b709-4f59-bdc2-4e4149e61e72"
CREATOR = "111"
OPPONENT = "222"
OPP_NAME = "chris7"
SINCE = "2026-01-01T00:00:00+00:00"
TS = "2026-09-23T14:32:14+00:00"
LATER = "2026-09-23T14:33:20+00:00"


def _ev(did, et, ver=0, rid=RID, gm="zw_lg", code="LCKGT", extra=None, ts=TS):
    gd = {"room_id": rid, "match_version": ver, "gm": gm,
          "short_code": code, "code": code}
    gd.update(extra or {})
    return {"discord_id": did, "event_type": et, "event_data": gd,
            "created_at": ts}


def _out_row(who=OPP_NAME, ver=0, ts=TS, killer=None):
    """What the processor persists when it watches someone else go out."""
    return _ev(CREATOR, "killfeed", ver=ver, ts=ts,
               extra={"kill_type": "out", "via": "alv",
                      "instigator": killer, "instigated": who,
                      "player": who})


def _mobile(events):
    duels = _load_duels()
    return duels, duels.group_mobile_matches(events, CREATOR, OPP_NAME, "zw_lg",
                                             None, None, SINCE)


# --- the requirement -------------------------------------------------------

def test_opponent_out_with_no_killfeed_scores_the_round_for_the_creator():
    duels, groups = _mobile([_out_row()])
    assert len(groups) == 1, "an eliminated opponent is the co-presence proof"
    g = groups[0]
    assert g["creator_win"] is True
    assert g["opponent_win"] is False
    assert duels.score_match(g["kills_c"], g["kills_o"], g["win_id"],
                             CREATOR, OPPONENT, opponent_win=g["opponent_win"],
                             creator_win=g["creator_win"]) == CREATOR


def test_opponent_out_credits_no_kill_to_anyone():
    # the creator did not land it, and the opponent cannot have killed itself
    _duels, groups = _mobile([_out_row()])
    g = groups[0]
    assert g["kills_c"] == 0
    assert g["kills_o"] == 0
    assert g.get("opp_out_c") == 1


def test_an_opponent_out_alongside_real_kills_keeps_them():
    duels, groups = _mobile([
        _ev(CREATOR, "kill", extra={"victim": OPP_NAME}),
        _out_row(ts=LATER),
    ])
    g = groups[0]
    assert g["kills_c"] == 1
    assert g["creator_win"] is True


def test_a_named_killer_on_the_out_row_does_not_become_a_kill():
    # fall damage names nobody; if a killer is present it is a real killfeed
    # line and would already have been counted as one
    _duels, groups = _mobile([_out_row(killer="cis6led")])
    assert groups[0]["kills_o"] == 0
    assert groups[0]["kills_c"] == 0


# --- a bystander proves nothing -------------------------------------------

def test_a_bystander_out_is_not_co_presence_and_awards_nothing():
    _duels, groups = _mobile([_out_row(who="SomeGuy")])
    assert groups == [], "a stranger going out must not conjure a round"


def test_bystander_out_beside_a_real_opponent_kill_awards_nothing():
    # a knock proves the opponent was in the room; the bystander's elimination
    # must not become the round
    duels, groups = _mobile([
        _ev(CREATOR, "killfeed", extra={"kill_type": "knocked",
                                        "instigator": "cis6led",
                                        "instigated": OPP_NAME}),
        _out_row(who="SomeGuy", ts=LATER),
    ])
    g = groups[0]
    assert g["seen_o"] is True
    assert g["creator_win"] is False
    assert duels.score_match(g["kills_c"], g["kills_o"], g["win_id"],
                             CREATOR, OPPONENT, opponent_win=g["opponent_win"],
                             creator_win=g["creator_win"]) is None


# --- ordering --------------------------------------------------------------

def test_a_creator_elimination_after_the_opponent_out_is_not_a_creator_win():
    duels, groups = _mobile([
        _out_row(ts=TS),
        _ev(CREATOR, "death", ts=LATER),      # creator then died
    ])
    g = groups[0]
    assert g["creator_win"] is False
    assert g["opponent_win"] is True
    assert duels.score_match(g["kills_c"], g["kills_o"], g["win_id"],
                             CREATOR, OPPONENT, opponent_win=g["opponent_win"],
                             creator_win=g["creator_win"]) == OPPONENT


def test_a_creator_elimination_before_the_opponent_out_still_wins():
    duels, groups = _mobile([
        _ev(CREATOR, "death", ts=TS),
        _out_row(ts=LATER),
    ])
    g = groups[0]
    assert g["creator_win"] is True
    assert g["opponent_win"] is False


# --- the seals -------------------------------------------------------------

def test_an_opponent_out_round_is_sealable():
    duels = _load_duels()
    assert duels._match_sealable({"kills_c": 0, "kills_o": 0, "win_id": None,
                                  "creator_win": True}, "wins") is True


def test_a_bare_opponent_out_is_still_combat_evidence():
    # the walkover guard rejects a winner with zero kills AND zero deaths; an
    # elimination IS the round happening, so it has to count
    duels = _load_duels()
    m = {"kills_c": 0, "kills_o": 0, "win_id": None, "death_c": 0,
         "opp_out_c": 1, "creator_win": True}
    combat = m["kills_c"] + m["kills_o"] + m["death_c"] + m["opp_out_c"]
    assert combat > 0


def test_kills_race_ignores_an_out_row():
    # df is decided by kill order; an elimination ends nothing
    duels = _load_duels()
    tl = duels.kills_race_timeline([_out_row()], CREATOR, OPPONENT, OPP_NAME, 3)
    assert (tl.get("kills_c", 0), tl.get("kills_o", 0)) == (0, 0)


def test_creator_win_is_ignored_in_a_kills_race():
    duels = _load_duels()
    assert duels.score_match(0, 0, None, CREATOR, OPPONENT,
                             race="kills", creator_win=True) is None


def test_a_win_row_still_outranks_a_creator_win():
    duels = _load_duels()
    assert duels.score_match(0, 0, CREATOR, CREATOR, OPPONENT,
                             creator_win=True) == CREATOR
    assert duels.score_match(0, 0, OPPONENT, CREATOR, OPPONENT,
                             creator_win=True) == OPPONENT


def test_existing_call_shapes_still_work():
    # creator_win/opponent_win default off, so no other caller changes
    duels = _load_duels()
    assert duels.score_match(1, 0, None, CREATOR, OPPONENT) is None
    assert duels.score_match(1, 0, None, CREATOR, OPPONENT,
                             opponent_win=True) == OPPONENT


# --- the processor <-> wager contract --------------------------------------
#
# These are different processes writing and reading one table. The wager can
# only recognise a watched elimination if the processor writes the subject's
# name under a key the wager actually inspects, so that agreement is pinned
# here rather than left to two docstrings agreeing.

def test_processor_payload_is_recognised_by_the_wager():
    import server.scripts.processor as proc
    payload = proc.observed_out_event("zw_lg", "chris7", "room-1", "LCKGT", 3)
    assert payload["kill_type"] == "out"
    assert payload["via"] == "alv"
    # no killer: we did not land it
    assert payload["instigator"] is None


def test_the_subject_lands_on_a_name_key_the_wager_reads():
    import server.scripts.processor as proc
    duels = _load_duels()
    payload = proc.observed_out_event("zw_lg", "chris7", "room-1", "LCKGT", 3)
    keys = {_norm for _norm in
            (duels._norm_duel_name(payload.get(k)) for k in duels._MOBILE_NAME_KEYS)
            if _norm}
    assert "chris7" in keys, (
        f"processor writes keys {sorted(payload)} but the wager only reads "
        f"{duels._MOBILE_NAME_KEYS}")


def test_the_written_payload_actually_scores_the_round_end_to_end():
    # the real processor output, fed to the real grouper
    import server.scripts.processor as proc
    duels = _load_duels()
    payload = proc.observed_out_event("zw_lg", "chris7", RID, "LCKGT", 0)
    ev = {"discord_id": CREATOR, "event_type": "killfeed",
          "event_data": dict({"room_id": RID, "match_version": 0, "gm": "zw_lg",
                              "code": "LCKGT", "short_code": "LCKGT"}, **payload),
          "created_at": TS}
    groups = duels.group_mobile_matches([ev], CREATOR, OPP_NAME, "zw_lg",
                                        None, None, SINCE)
    assert len(groups) == 1
    assert groups[0]["creator_win"] is True
    assert duels._match_sealable(groups[0], "wins") is True


def test_a_new_event_type_was_not_introduced():
    # guards the reason this rides on killfeed: a new event_type would be read
    # by stats/insights/play-points, which all filter bngc_game_events by type
    import server.scripts.processor as proc
    payload = proc.observed_out_event("zw_lg", "chris7", "room-1", "LCKGT", 3)
    assert "kill_type" in payload
    assert set(payload) <= {"gm", "instigator", "instigated", "player",
                            "kill_type", "via", "room_id", "code", "match_version"}
