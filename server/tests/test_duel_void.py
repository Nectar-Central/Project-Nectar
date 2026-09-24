"""Creator-only "void round" (the opponent crashed / false start).

A crashed opponent produces NO events at all — no leave, no death, no
killfeed — so the round can never be scored and the duel stalls until the AFK
guard eventually cancels it an hour later. The creator needs a manual way to
throw that match away: the round is REMOVED from the score (it does not award
anyone anything) and is remembered so it can never seal later either.

Guards, because this is the one button that edits history: creator only, once
per match, and never while the creator is behind. Every use DMs the opponent
and is logged.
Pure helpers, no Discord, no DB.
"""
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_void_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RID = "6bfa213a-0000"
ME = "111"
THEM = "222"


def _wager(**extra):
    w = {"id": "22222222-2222-2222-2222-222222222222", "creator_id": ME,
         "opponent_id": THEM, "mode": "zw_lg", "race": "wins", "first_to": 5,
         "stake": 2, "pot": 4, "room_id": RID, "score_creator": 2,
         "score_opponent": 1, "kills_creator": 9, "kills_opponent": 7,
         "sealed": [], "voided": [], "status": "active"}
    w.update(extra)
    return w


def _seal(version, winner, kc, ko):
    return {"room_id": RID, "match_version": version, "winner": winner,
            "winner_name": "x", "kills_c": kc, "kills_o": ko}


# --- totals derived from the seal history ----------------------------------

def test_totals_come_from_the_seals():
    duels = _load_duels()
    sealed = [_seal(0, ME, 4, 2), _seal(1, THEM, 1, 5), _seal(2, ME, 3, 1)]
    t = duels.totals_from_seals(sealed, "wins", ME, THEM)
    assert t == {"score_c": 2, "score_o": 1, "kills_c": 8, "kills_o": 8}


def test_totals_for_a_kills_race():
    duels = _load_duels()
    t = duels.totals_from_seals([_seal(0, None, 4, 2)], "kills", ME, THEM)
    assert t["kills_c"] == 4 and t["kills_o"] == 2
    assert t["score_c"] == 0 and t["score_o"] == 0


def test_totals_never_raise_on_junk():
    duels = _load_duels()
    assert duels.totals_from_seals(None, "wins", ME, THEM)["kills_c"] == 0
    assert duels.totals_from_seals([None, {}], "wins", ME, THEM)["score_c"] == 0


# --- voiding ---------------------------------------------------------------

def test_void_removes_a_sealed_round_and_its_kills():
    duels = _load_duels()
    w = _wager(sealed=[_seal(0, ME, 4, 2), _seal(1, THEM, 1, 5)],
               score_creator=1, score_opponent=1,
               kills_creator=5, kills_opponent=7)
    ok, note, updates = duels.void_round(w, RID, 1, by=ME, reason="crash")
    assert ok is True
    assert note
    # The opponent's sealed round is gone and the score recalculated.
    assert [s["match_version"] for s in updates["sealed"]] == [0]
    assert updates["score_creator"] == 1 and updates["score_opponent"] == 0
    assert updates["kills_creator"] == 4 and updates["kills_opponent"] == 2
    assert updates["voided"][0]["match_version"] == 1
    assert updates["voided"][0]["by"] == ME


def test_void_awards_nobody_the_round():
    duels = _load_duels()
    w = _wager(sealed=[_seal(0, ME, 4, 2)])
    _ok, _note, updates = duels.void_round(w, RID, 0, by=ME)
    assert updates["sealed"] == []
    assert updates["score_creator"] == 0 and updates["score_opponent"] == 0


def test_void_blocks_an_unsealed_round_from_sealing_later():
    # The stuck round has no seal yet: recording the void is the whole point.
    duels = _load_duels()
    ok, _note, updates = duels.void_round(_wager(), RID, 2, by=ME,
                                          reason="false start")
    assert ok is True
    assert updates["voided"][0]["match_version"] == 2
    assert updates["sealed"] == []


def test_void_is_once_per_match():
    duels = _load_duels()
    w = _wager(voided=[{"room_id": RID, "match_version": 1, "by": ME}])
    ok, note, _updates = duels.void_round(w, RID, 1, by=ME)
    assert ok is False
    assert "already" in note.lower()


def test_only_the_creator_can_void():
    duels = _load_duels()
    ok, note, _u = duels.void_round(_wager(), RID, 1, by=THEM)
    assert ok is False
    assert "creator" in note.lower()


def test_void_is_blocked_while_the_creator_is_behind():
    duels = _load_duels()
    w = _wager(sealed=[_seal(0, THEM, 1, 9)], score_creator=0, score_opponent=1)
    ok, note, _u = duels.void_round(w, RID, 1, by=ME)
    assert ok is False
    assert "behind" in note.lower()


def test_void_is_allowed_while_level_or_ahead():
    duels = _load_duels()
    w = _wager(sealed=[_seal(0, ME, 4, 2), _seal(1, THEM, 1, 5)],
               score_creator=1, score_opponent=1)
    assert duels.void_round(w, RID, 1, by=ME)[0] is True
    w2 = _wager(sealed=[_seal(0, ME, 4, 2), _seal(1, ME, 3, 1)],
                score_creator=2, score_opponent=0)
    assert duels.void_round(w2, RID, 1, by=ME)[0] is True


def test_void_never_raises_on_junk():
    duels = _load_duels()
    ok, note, _u = duels.void_round({}, None, None, by=None)
    assert ok is False and note
    ok2, _n, _u2 = duels.void_round(None, RID, 0, by=ME)
    assert ok2 is False


# --- the resolver must respect a void --------------------------------------

def test_voided_versions_are_not_rescored():
    duels = _load_duels()
    m = {"room_id": RID, "match_version": 1, "kills_c": 2, "kills_o": 0,
         "win_id": ME}
    assert duels.is_voided(RID, 1, [{"room_id": RID, "match_version": 1}]) is True
    assert duels.is_voided(RID, 2, [{"room_id": RID, "match_version": 1}]) is False
    assert duels.is_voided(RID, 1, None) is False
    assert duels.is_voided(RID, 1, []) is False
    assert duels.is_voided("other", 1, [{"room_id": RID, "match_version": 1}]) is False
