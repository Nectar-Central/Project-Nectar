"""A duel must follow its players into the next room.

The wager binds `room_id` on the first evidence and then every grouper drops
anything from another room ("prevents hopping"). After a crash + void the
creator returns to the lobby and builds a new room — same party code, brand new
room_id — and from that moment the score silently stops: every row is filtered
as `room != bound`. Creating a new room with a NEW code was equally dead,
because the stale room_id still filtered it.

Binding rules pinned here:
  bind    — nothing bound yet: take the room with the FRESHEST evidence
            (matches[0] used to be the oldest room seen).
  keep    — bound room still has recent evidence: it wins, even if another
            room is also being played (the anti-hop guard).
  rebind  — bound room has been quiet for WAGER_REBIND_IDLE_SEC: the duel moved,
            so follow it (and follow the code with it).
  never   — public rooms (no code) are never bound.
Pure helpers, no Discord, no DB.
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
        "duels_rebind_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OLD_ROOM = "e26945f4-0000"
NEW_ROOM = "b6864d75-0000"


def _iso(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _ev(room, code, ts, et="kill", extra=None):
    gd = {"room_id": room, "match_version": 0, "gm": "zw_lg",
          "short_code": code, "code": code}
    gd.update(extra or {})
    return {"discord_id": "111", "event_type": et, "event_data": gd, "created_at": ts}


# --- room evidence ---------------------------------------------------------

def test_room_evidence_lists_private_rooms_newest_first():
    duels = _load_duels()
    rows = [_ev(OLD_ROOM, "TEPUU", _iso(minutes=30)),
            _ev(NEW_ROOM, "TEPUU", _iso(seconds=5)),
            _ev("pub-1234", "", _iso(seconds=1))]
    rooms = duels.room_evidence(rows, "")
    assert [r["room_id"] for r in rooms] == [NEW_ROOM, OLD_ROOM]
    assert rooms[0]["code"] == "TEPUU"
    assert rooms[0]["count"] == 1


def test_room_evidence_counts_rows_and_ignores_rows_before_active_since():
    duels = _load_duels()
    since = _iso(minutes=10)
    rows = [_ev(OLD_ROOM, "TEPUU", _iso(minutes=30)),
            _ev(NEW_ROOM, "TEPUU", _iso(minutes=5)),
            _ev(NEW_ROOM, "TEPUU", _iso(minutes=4), et="killfeed")]
    rooms = duels.room_evidence(rows, since)
    assert len(rooms) == 1
    assert rooms[0]["room_id"] == NEW_ROOM
    assert rooms[0]["count"] == 2


def test_room_evidence_never_raises_on_junk():
    duels = _load_duels()
    assert duels.room_evidence(None, "") == []
    assert duels.room_evidence([None, {}, {"event_data": None}], "") == []


# --- choosing the bound room ----------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def test_room_evidence_carries_the_room_game_mode():
    duels = _load_duels()
    rows = [_ev(OLD_ROOM, "TEPUU", _iso(seconds=30), extra={"gm": "zw_lg"}),
            _ev(NEW_ROOM, "DFNEW", _iso(seconds=5), extra={"gm": "df"})]
    rooms = duels.room_evidence(rows, "")
    assert [r["gm"] for r in rooms] == ["df", "zw_lg"]


def test_room_evidence_keeps_a_mode_seen_on_any_row_of_the_room():
    # newest row has no gm, an older one does: still mode-aware
    duels = _load_duels()
    rows = [_ev(NEW_ROOM, "DFNEW", _iso(seconds=30), extra={"gm": "df"}),
            _ev(NEW_ROOM, "DFNEW", _iso(seconds=5), extra={"gm": ""})]
    assert duels.room_evidence(rows, "")[0]["gm"] == "df"


# --- the mode-blind bind (kills + embeds dead in a second wager) ----------

def test_a_wager_never_binds_a_room_of_another_game_mode():
    # Second wager, different game mode. The PREVIOUS room is still the
    # freshest evidence (the opponent is still playing it), so a mode-blind
    # bind locks the wager onto it - and grouping then drops every row of the
    # real room as "gm != mode": no kills, no seal, no thread embed.
    duels = _load_duels()
    rooms = [{"room_id": OLD_ROOM, "code": "TEPUU", "gm": "zw_lg",
              "last_ts": _iso(seconds=2)},
             {"room_id": NEW_ROOM, "code": "DFNEW", "gm": "df",
              "last_ts": _iso(seconds=30)}]
    room, code, action = duels.choose_bound_room("", rooms, _now_iso(), "", "df")
    assert (room, code, action) == (NEW_ROOM, "DFNEW", "bind")


def test_a_bound_room_of_another_mode_is_released_not_kept():
    # the anti-hop guard must not pin a wager to a room of the wrong mode,
    # even while that room is still being played
    duels = _load_duels()
    rooms = [{"room_id": OLD_ROOM, "code": "TEPUU", "gm": "zw_lg",
              "last_ts": _iso(seconds=2)},
             {"room_id": NEW_ROOM, "code": "DFNEW", "gm": "df",
              "last_ts": _iso(seconds=30)}]
    room, code, action = duels.choose_bound_room(OLD_ROOM, rooms, _now_iso(),
                                                "TEPUU", "df")
    assert (room, code, action) == (NEW_ROOM, "DFNEW", "rebind")


def test_a_room_without_gm_evidence_is_still_bindable():
    # no gm on the rows must never strand a working wager: unknown mode
    # keeps the room a candidate
    duels = _load_duels()
    rooms = [{"room_id": OLD_ROOM, "code": "TEPUU", "last_ts": _iso(seconds=2)},
             {"room_id": NEW_ROOM, "code": "TEPUU", "last_ts": _iso(seconds=1)}]
    room, _code, action = duels.choose_bound_room("", rooms, _now_iso(), "", "df")
    assert (room, action) == (NEW_ROOM, "bind")


def test_the_anti_hop_guard_still_holds_within_one_mode():
    # same mode, old room still live: the guard is unchanged
    duels = _load_duels()
    rooms = [{"room_id": OLD_ROOM, "code": "TEPUU", "gm": "zw_lg",
              "last_ts": _iso(seconds=10)},
             {"room_id": NEW_ROOM, "code": "XXXXX", "gm": "zw_lg",
              "last_ts": _iso(seconds=2)}]
    assert duels.choose_bound_room(OLD_ROOM, rooms, _now_iso(), "TEPUU",
                                   "zw_lg")[2] == "keep"


# --- the reported symptom, end to end -------------------------------------

def _second_wager_events():
    """Previous zw_lg room still producing rows + the new df room."""
    def row(did, room, code, gm, ts, victim=None):
        gd = {"room_id": room, "match_version": 0, "gm": gm,
              "short_code": code, "code": code}
        if victim:
            gd["victim"] = victim
        return {"discord_id": did, "event_type": "kill", "event_data": gd,
                "created_at": ts}
    return [
        # opponent still playing the OLD room (the freshest evidence)
        row("222", OLD_ROOM, "TEPUU", "zw_lg", _iso(seconds=2), "Nemo"),
        # the new room, where the second wager is actually played
        row("111", NEW_ROOM, "DFNEW", "df", _iso(seconds=40), "Nemo"),
        row("222", NEW_ROOM, "DFNEW", "df", _iso(seconds=39)),
    ]


def test_second_wager_in_a_new_mode_tracks_kills_through_the_grouper():
    # A finished wager, then a new one with the same player in a different
    # game mode and a different room. Before the fix the wager bound the old
    # room (freshest evidence) and every real row was dropped as "gm != mode":
    # no kills, no seal, no thread embed.
    duels = _load_duels()
    events = _second_wager_events()
    since = _iso(minutes=5)
    room, code, _act = duels.choose_bound_room(
        "", duels.room_evidence(events, since), _now_iso(), "", "df")
    assert (room, code) == (NEW_ROOM, "DFNEW")
    matches = duels.group_duel_matches(
        events, "111", "222", "df", room, code, since,
        opp_name="nemo", creator_name="aqua")
    assert len(matches) == 1
    assert matches[0]["kills_c"] == 1          # the creator's kill counts
    assert duels._match_sealable(matches[0], duels.race_for_mode("df"))


def test_the_same_rows_produce_nothing_when_the_old_room_was_bound():
    # the mechanism, pinned: the old room's mode makes every real row
    # ungroupable, which is what left kills at zero and no embed posted
    duels = _load_duels()
    events = _second_wager_events()
    since = _iso(minutes=5)
    assert duels.group_duel_matches(
        events, "111", "222", "df", OLD_ROOM, "TEPUU", since,
        opp_name="nemo", creator_name="aqua") == []


def test_a_stale_stored_code_never_pins_a_live_bound_room():
    # The host typed a code, then abandoned that room; the bound room is
    # running under a different code. Preferring the STORED one filters
    # every real row as "code != bound" and the wager goes silently dead.
    duels = _load_duels()
    rooms = [{"room_id": OLD_ROOM, "code": "REAL1", "gm": "zw_lg",
              "last_ts": _iso(seconds=5)}]
    room, code, action = duels.choose_bound_room(OLD_ROOM, rooms, _now_iso(),
                                                "OLDCD", "zw_lg")
    assert (room, code, action) == (OLD_ROOM, "REAL1", "keep")


def test_the_stored_code_still_stands_when_the_room_has_none():
    duels = _load_duels()
    rooms = [{"room_id": OLD_ROOM, "code": "", "last_ts": _iso(seconds=5)}]
    assert duels.choose_bound_room(OLD_ROOM, rooms, _now_iso(), "OLDCD",
                                   "zw_lg")[1] == "OLDCD"


def test_first_evidence_binds_the_newest_room_not_the_oldest():
    duels = _load_duels()
    rooms = [{"room_id": OLD_ROOM, "code": "TEPUU", "last_ts": _iso(minutes=30)},
             {"room_id": NEW_ROOM, "code": "TEPUU", "last_ts": _iso(seconds=5)}]
    room, code, action = duels.choose_bound_room("", rooms, _now_iso(), "TEPUU")
    assert (room, code, action) == (NEW_ROOM, "TEPUU", "bind")


def test_a_live_bound_room_is_kept_even_if_another_room_is_also_played():
    duels = _load_duels()
    rooms = [{"room_id": OLD_ROOM, "code": "TEPUU", "last_ts": _iso(seconds=10)},
             {"room_id": NEW_ROOM, "code": "XXXXX", "last_ts": _iso(seconds=2)}]
    room, code, action = duels.choose_bound_room(OLD_ROOM, rooms, _now_iso(), "TEPUU")
    assert (room, code, action) == (OLD_ROOM, "TEPUU", "keep")


def test_a_quiet_bound_room_rebinds_to_the_live_one_same_code():
    duels = _load_duels()
    rooms = [{"room_id": NEW_ROOM, "code": "TEPUU", "last_ts": _iso(seconds=5)}]
    room, code, action = duels.choose_bound_room(OLD_ROOM, rooms, _now_iso(), "TEPUU")
    assert (room, code, action) == (NEW_ROOM, "TEPUU", "rebind")


def test_rebind_follows_a_new_code_too():
    # The creator rebuilt the room with a fresh code after the crash.
    duels = _load_duels()
    rooms = [{"room_id": NEW_ROOM, "code": "ZZZZZ", "last_ts": _iso(seconds=5)}]
    room, code, action = duels.choose_bound_room(OLD_ROOM, rooms, _now_iso(), "TEPUU")
    assert (room, code, action) == (NEW_ROOM, "ZZZZZ", "rebind")


def test_rebind_only_after_the_bound_room_went_quiet():
    duels = _load_duels()
    rooms = [{"room_id": NEW_ROOM, "code": "ZZZZZ", "last_ts": _iso(seconds=5)}]
    # Bound room evidence 2s ago: still live, so no hop.
    live = [{"room_id": OLD_ROOM, "code": "TEPUU", "last_ts": _iso(seconds=2)}] + rooms
    assert duels.choose_bound_room(OLD_ROOM, live, _now_iso(), "TEPUU")[2] == "keep"


def test_a_stale_bound_room_with_no_live_room_reports_none():
    duels = _load_duels()
    rooms = [{"room_id": OLD_ROOM, "code": "TEPUU", "last_ts": _iso(minutes=30)}]
    room, code, action = duels.choose_bound_room(OLD_ROOM, rooms, _now_iso(), "TEPUU")
    assert action == "keep"      # nothing better to move to
    assert room == OLD_ROOM
    assert duels.choose_bound_room("", [], _now_iso(), "") == ("", "", "none")


def test_choose_bound_room_never_raises():
    duels = _load_duels()
    assert duels.choose_bound_room(None, None, None, None) == ("", "", "none")
    # A bound room with no usable candidates is kept, never invented away.
    room, code, action = duels.choose_bound_room("r", [{"room_id": None}], "bad", "")
    assert (room, code, action) == ("r", "", "keep")


# --- the wiring: rebind must happen BEFORE grouping ------------------------

@pytest.mark.asyncio
async def test_score_one_rebinds_before_grouping(monkeypatch):
    duels = _load_duels()

    async def _no_afk(_bot, _w):
        return False
    monkeypatch.setattr(duels, "_afk_guard", _no_afk)

    now = datetime.now(timezone.utc)
    rows = [_ev(OLD_ROOM, "TEPUU", (now - timedelta(minutes=30)).isoformat()),
            _ev(NEW_ROOM, "TEPUU", (now - timedelta(seconds=5)).isoformat())]

    async def _events(_c, _o, _since):
        return rows
    monkeypatch.setattr(duels, "duel_events", _events)

    saved = {}

    async def _update(wid, fields):
        saved.update(fields)
        return True
    monkeypatch.setattr(duels, "update_wager", _update)

    async def _row(uid):
        return {"game_username": "cis6led" if str(uid) == "111" else "chris7",
                "nectar_coins": 10}
    monkeypatch.setattr(duels.db, "get_by_discord", _row)

    seen_room = {}

    def _group(_events, _creator, _opp, _mode, room_id, room_code, _since, **kw):
        seen_room["room_id"] = room_id
        seen_room["room_code"] = room_code
        return []
    monkeypatch.setattr(duels, "group_mobile_matches", _group)
    monkeypatch.setattr(duels, "group_duel_matches", _group)

    w = {"id": "33333333-3333-3333-3333-333333333333", "creator_id": "111",
         "opponent_id": "222", "mode": "zw_lg", "race": "wins", "first_to": 5,
         "stake": 0, "is_mobile_duel": True, "room_id": OLD_ROOM,
         "room_code": "TEPUU",
         "active_since": (now - timedelta(hours=1)).isoformat()}
    await duels._score_one(None, w)
    # The grouper must be called with the NEW room, not the stale one.
    assert seen_room.get("room_id") == NEW_ROOM
    assert saved.get("room_id") == NEW_ROOM
