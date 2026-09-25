"""/wager reset - recover a duel after backing out to the lobby.

Backing out leaves the wager bound to a room nobody is in any more, and the
`active_since` it was stamped with keeps feeding two clocks: the AFK grace
(already spent, so the next tick can cancel a duelist for being "offline")
and the 24h stall refund. So a reset has to move the binding AND the clock.

The hard part is what a reset must NOT touch. `sealed` is the score,
`score_*`/`kills_*` are the victory check, and `voided` is the anti-farm
list: clearing any of them lets a duelist erase a loss. And reviving a
CANCELLED wager is a re-buy - its stakes were already refunded, so without
re-escrow the pot is unfunded and settling it mints coins.

Pure helpers, no Discord, no DB.
"""
import importlib.util
import sys
from pathlib import Path


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_reset_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NOW = "2026-01-02T03:04:05+00:00"
OLD_ROOM = "aaaa1111-2222"
NEW_ROOM = "bbbb3333-4444"


def _wager(**over):
    w = {"id": "aaaa1111-2222-3333-4444-555566667777", "status": "active",
         "creator_id": "C", "opponent_id": "O", "stake": 100, "pot": 200,
         "first_to": 3, "mode": "zw_lg", "race": "wins",
         "room_id": OLD_ROOM, "room_code": "OLDCD",
         "active_since": "2026-01-01T00:00:00+00:00",
         "resolved_at": None, "winner_id": None,
         "sealed": [{"room_id": OLD_ROOM, "match_version": 0, "winner": "C"}],
         "voided": [{"room_id": OLD_ROOM, "match_version": 9}],
         "score_creator": 1, "score_opponent": 0,
         "kills_creator": 12, "kills_opponent": 9}
    w.update(over)
    return w


# --- who may reset --------------------------------------------------------

def test_a_duelist_may_reset_a_live_wager():
    duels = _load_duels()
    w = _wager()
    assert duels.reset_refusal(w, is_duelist=True, is_admin=False) == ""
    assert duels.reset_refusal(w, is_duelist=False, is_admin=False) != ""


def test_a_stranger_can_never_reset():
    duels = _load_duels()
    assert duels.reset_refusal(_wager(), is_duelist=False, is_admin=False)
    assert duels.reset_refusal(_wager(status="cancelled"),
                               is_duelist=False, is_admin=False)


def test_reviving_a_cancelled_wager_is_admin_only():
    # both stakes were refunded on cancel, so this re-buys the duel
    duels = _load_duels()
    w = _wager(status="cancelled", resolved_at="2026-01-01T02:00:00+00:00")
    assert duels.reset_refusal(w, is_duelist=True, is_admin=False) != ""
    assert duels.reset_refusal(w, is_duelist=False, is_admin=True) == ""


def test_a_settled_wager_is_never_resettable():
    duels = _load_duels()
    for w in (_wager(status="resolved", winner_id="C"),
              _wager(status="active", winner_id="C")):
        assert "final" in duels.reset_refusal(w, is_duelist=True, is_admin=True)


def test_a_wager_that_never_started_has_nothing_to_reset():
    duels = _load_duels()
    for st in ("open", "pending"):
        assert duels.reset_refusal(_wager(status=st), is_duelist=True,
                                   is_admin=True) != ""


def test_reset_needs_an_opponent():
    # the resolver no-ops without one, so a reset would look like it worked
    duels = _load_duels()
    assert duels.reset_refusal(_wager(opponent_id=None), is_duelist=True,
                               is_admin=True) != ""


def test_a_0_stake_wager_is_still_resettable():
    # for-fun duels escrow nothing, so the money check must not gate them
    duels = _load_duels()
    assert duels.reset_refusal(_wager(stake=0), is_duelist=True, is_admin=True) == ""


# --- what the reset writes -------------------------------------------------

def test_reset_moves_the_binding_and_the_clock():
    duels = _load_duels()
    p = duels.reset_patch(_wager(), now_iso=NOW)
    assert p["active_since"] == NOW      # restarts the AFK grace + 24h stall
    assert p["room_id"] is None          # forces an immediate bind
    assert p["room_code"] is None        # drops the abandoned pID filter
    assert p["resolved_at"] is None


def test_reset_keeps_the_score_and_the_anti_farm_state():
    duels = _load_duels()
    p = duels.reset_patch(_wager(), now_iso=NOW)
    for f in ("sealed", "voided", "score_creator", "score_opponent",
              "kills_creator", "kills_opponent", "stake", "pot", "first_to",
              "mode", "race", "creator_id", "opponent_id", "status", "winner_id"):
        assert f not in p, f"{f} must survive a reset"


def test_a_reset_can_carry_the_code_in_one_step():
    duels = _load_duels()
    rooms = [{"room_id": OLD_ROOM, "code": "OLDCD", "gm": "zw_lg",
              "last_ts": "2026-01-02T03:00:00+00:00"},
             {"room_id": NEW_ROOM, "code": "REAL1", "gm": "zw_lg",
              "last_ts": "2026-01-02T03:04:00+00:00"}]
    p = duels.reset_patch(_wager(), now_iso=NOW, code="real1", rooms=rooms)
    assert p["room_code"] == "REAL1"
    assert p["room_id"] == NEW_ROOM      # binding pulled across, not just the code


def test_an_unseen_code_releases_the_binding():
    duels = _load_duels()
    p = duels.reset_patch(_wager(), now_iso=NOW, code="LATER", rooms=[])
    assert p == {"active_since": NOW, "room_id": None,
                 "room_code": "LATER", "resolved_at": None}


def test_reset_without_a_code_rebinds_to_the_freshest_room():
    duels = _load_duels()
    rooms = [{"room_id": OLD_ROOM, "code": "OLDCD", "gm": "zw_lg",
              "last_ts": "2026-01-02T02:00:00+00:00"},
             {"room_id": NEW_ROOM, "code": "REAL1", "gm": "zw_lg",
              "last_ts": "2026-01-02T03:04:00+00:00"}]
    p = duels.reset_patch(_wager(), now_iso=NOW, rooms=rooms)
    assert p["room_id"] == NEW_ROOM
    assert p["room_code"] == "REAL1"


def test_reset_survives_junk():
    duels = _load_duels()
    p = duels.reset_patch({"status": "active"}, now_iso="", code=None, rooms=None)
    assert p["room_id"] is None
    assert p["room_code"] is None
    assert duels.reset_refusal({}, is_duelist=True, is_admin=True) != ""


# --- the revive has to re-escrow -----------------------------------------

def test_a_revive_needs_escrow_because_cancel_already_refunded():
    duels = _load_duels()
    assert duels.reset_needs_escrow(_wager(status="active")) is False
    assert duels.reset_needs_escrow(_wager(status="cancelled")) is True
    assert duels.reset_needs_escrow(_wager(status="expired")) is True


def test_a_zero_stake_revive_needs_no_escrow():
    duels = _load_duels()
    assert duels.reset_needs_escrow(_wager(status="cancelled", stake=0)) is False
