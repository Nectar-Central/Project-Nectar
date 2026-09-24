"""Free Mode duels (first to N kills) score cumulatively.

In df the zone timer never ends the match: `stG` stays at 1, so
`match_version` never moves and EVERY kill of the session lands in the same
(room, version) group. The old path sealed that group once and then skipped it
forever (`seen`), so the second kill and every kill after it were ignored, and
each tick logged a DEFEAT for whoever merely held a kill lead at that moment.

A kills race therefore has to be scored as one growing match: totals update
in place, and the duel only resolves when a duelist actually REACHES the
target kills (first to reach it wins, decided by kill order, not by who
happens to be ahead when the bot polls).
Pure helpers + one routing test, no Discord, no DB.
"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_kills_race_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RID = "6bfa213a-0000"
CREATOR = "111"
OPPONENT = "222"
OPP_NAME = "chris7"
CREATOR_NAME = "cis6led"
SINCE = "2026-09-23T16:00:00+00:00"


def _ev(et, ver=0, ts="2026-09-23T16:00:00+00:00", extra=None, did=CREATOR,
        rid=RID, code="5H1A2", gm="df"):
    gd = {"room_id": rid, "match_version": ver, "gm": gm,
          "short_code": code, "code": code}
    gd.update(extra or {})
    return {"discord_id": did, "event_type": et, "event_data": gd, "created_at": ts}


def _creator_kill(ts, victim=OPP_NAME):
    return _ev("kill", ts=ts, extra={"victim": victim})


def _foe_killfeed(ts, victim="cis6led"):
    return _ev("killfeed", ts=ts, extra={"kill_type": "killed",
                                         "instigator": OPP_NAME, "instigated": victim})


# --- timeline --------------------------------------------------------------

def test_timeline_counts_roster_kills_only():
    duels = _load_duels()
    rows = [_creator_kill("2026-09-23T16:01:00+00:00"),
            _creator_kill("2026-09-23T16:01:20+00:00", victim="SomeRandomGuy"),
            _foe_killfeed("2026-09-23T16:01:30+00:00")]
    t = duels.kills_race_timeline(rows, CREATOR, OPPONENT, OPP_NAME, 3, CREATOR_NAME)
    assert t["kills_c"] == 1
    assert t["kills_o"] == 1
    assert t["winner"] is None


def test_timeline_creator_reaching_the_target_wins():
    duels = _load_duels()
    rows = [_creator_kill(f"2026-09-23T16:0{i}:00+00:00") for i in range(3)]
    t = duels.kills_race_timeline(rows, CREATOR, OPPONENT, OPP_NAME, 3, CREATOR_NAME)
    assert t["kills_c"] == 3
    assert t["winner"] == CREATOR


def test_timeline_opponent_reaching_the_target_wins():
    duels = _load_duels()
    rows = [_foe_killfeed(f"2026-09-23T16:0{i}:00+00:00") for i in range(3)]
    t = duels.kills_race_timeline(rows, CREATOR, OPPONENT, OPP_NAME, 3, CREATOR_NAME)
    assert t["kills_o"] == 3
    assert t["winner"] == OPPONENT


def test_timeline_first_to_reach_wins_not_who_is_ahead_now():
    duels = _load_duels()
    # The creator hit 3 first, then the opponent ran up 5. Polling late must
    # not hand the duel to the bigger total.
    rows = [_creator_kill("2026-09-23T16:01:00+00:00"),
            _creator_kill("2026-09-23T16:01:10+00:00"),
            _creator_kill("2026-09-23T16:01:20+00:00"),
            _foe_killfeed("2026-09-23T16:02:00+00:00"),
            _foe_killfeed("2026-09-23T16:02:10+00:00"),
            _foe_killfeed("2026-09-23T16:02:20+00:00"),
            _foe_killfeed("2026-09-23T16:02:30+00:00"),
            _foe_killfeed("2026-09-23T16:02:40+00:00")]
    t = duels.kills_race_timeline(rows, CREATOR, OPPONENT, OPP_NAME, 3, CREATOR_NAME)
    assert t["kills_c"] == 3 and t["kills_o"] == 5
    assert t["winner"] == CREATOR


def test_timeline_ignores_knocks_and_leaves():
    duels = _load_duels()
    rows = [_ev("knock", extra={"victim": OPP_NAME}),
            _ev("leave", extra={"player": OPP_NAME}),
            _ev("killfeed", extra={"kill_type": "left", "instigator": OPP_NAME})]
    t = duels.kills_race_timeline(rows, CREATOR, OPPONENT, OPP_NAME, 1, CREATOR_NAME)
    assert t["kills_c"] == 0 and t["kills_o"] == 0
    assert t["winner"] is None


def test_timeline_never_raises_on_junk():
    duels = _load_duels()
    t = duels.kills_race_timeline([None, {}, {"event_data": None}],
                                  CREATOR, OPPONENT, OPP_NAME, 3, CREATOR_NAME)
    assert t["kills_c"] == 0 and t["winner"] is None
    assert duels.kills_race_timeline(None, None, None, None, None)["winner"] is None


# --- cumulative seal -------------------------------------------------------

def test_kill_race_seal_is_one_growing_entry():
    duels = _load_duels()
    first = duels.kill_race_seal(RID, 0, 1, 0, None)
    second = duels.kill_race_seal(RID, 0, 2, 1, None)
    assert first["room_id"] == RID and first["match_version"] == 0
    assert (first["kills_c"], first["kills_o"]) == (1, 0)
    assert (second["kills_c"], second["kills_o"]) == (2, 1)
    assert second["winner"] is None
    # Same identity, so the caller can replace instead of append.
    assert (first["room_id"], first["match_version"]) == (
        second["room_id"], second["match_version"])


def test_kill_race_seal_records_the_winner():
    duels = _load_duels()
    s = duels.kill_race_seal(RID, 0, 3, 2, CREATOR)
    assert s["winner"] == CREATOR
    assert s["kills_c"] == 3


def test_kill_race_seal_never_raises():
    duels = _load_duels()
    assert duels.kill_race_seal(None, None, None, None, None)["kills_c"] == 0


# --- routing ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_score_one_routes_a_kills_race_to_the_cumulative_path(monkeypatch):
    duels = _load_duels()

    async def _no_afk(_bot, _w):
        return False
    monkeypatch.setattr(duels, "_afk_guard", _no_afk)

    now_iso = (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat()

    async def _events(_c, _o, _since):
        # both duelists must appear (co-presence) for a group to form, and the
        # rows must be inside active_since
        return [_ev("kill", ts=now_iso, extra={"victim": OPP_NAME}),
                _ev("death", ts=now_iso, did=OPPONENT)]
    monkeypatch.setattr(duels, "duel_events", _events)

    async def _row(uid):
        # the roster gate needs the two duelists under DIFFERENT game names
        return {"game_username": "cis6led" if str(uid) == CREATOR else OPP_NAME,
                "nectar_coins": 10}
    monkeypatch.setattr(duels.db, "get_by_discord", _row)

    called = {}

    async def _kills_path(bot, w, *a, **k):
        called["yes"] = True
        return "kills"
    monkeypatch.setattr(duels, "_score_kills_race", _kills_path, raising=False)

    w = {"id": "11111111-1111-1111-1111-111111111111", "creator_id": CREATOR,
         "opponent_id": OPPONENT, "mode": "df", "race": "kills",
         "first_to": 3, "stake": 0,
         "active_since": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}
    out = await duels._score_one(None, w)
    assert called.get("yes") is True
    assert out == "kills"
