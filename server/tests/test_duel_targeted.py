"""A targeted challenge must be answerable, and must not pre-invite anyone.

Two bugs, both reported from a live targeted challenge:

1. `/wager create @friend` created the duel thread immediately and force-added
   the challenged player to it. They were not in the duel yet, and the thread
   said "hit Accept" to someone who hadn't accepted anything.

2. Worse: accepting was impossible. `check_eligibility` asks
   `get_user_active(uid)`, and the pending challenge row has
   `opponent_id = <the challenged player>` — so the very challenge being
   accepted counted as "you already have an open wager" and the Accept button
   refused itself. An incoming PENDING challenge is not a conflict: the player
   has to be free to answer it (and to run their own duel meanwhile).

Pinned here: what counts as "busy", that Accept ignores its own wager, and the
DM text a targeted challenge sends instead of a thread.
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
        "duels_targeted_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ME = "222"        # the challenged player
THEM = "111"      # the creator


# --- what counts as busy ---------------------------------------------------

def test_an_incoming_pending_challenge_is_not_busy():
    duels = _load_duels()
    row = {"id": "w1", "status": "pending", "creator_id": THEM, "opponent_id": ME}
    assert duels.is_busy_row(row, ME) is False
    # ...but it IS busy for the creator, who must answer it.
    assert duels.is_busy_row(row, THEM) is True


def test_own_open_active_and_joined_wagers_are_busy():
    duels = _load_duels()
    assert duels.is_busy_row({"id": "w", "status": "open",
                              "creator_id": ME}, ME) is True
    assert duels.is_busy_row({"id": "w", "status": "pending",
                              "creator_id": ME}, ME) is True
    assert duels.is_busy_row({"id": "w", "status": "active",
                              "creator_id": THEM, "opponent_id": ME}, ME) is True


def test_the_wager_being_accepted_can_be_ignored():
    duels = _load_duels()
    row = {"id": "w1", "status": "pending", "creator_id": THEM, "opponent_id": ME}
    assert duels.is_busy_row(row, ME, ignore_id="w1") is False
    assert duels.is_busy_row(row, ME, ignore_id="other") is False


def test_resolved_and_cancelled_rows_are_never_busy():
    duels = _load_duels()
    for st in ("resolved", "cancelled", "expired"):
        assert duels.is_busy_row({"id": "w", "status": st,
                                  "creator_id": ME}, ME) is False
    assert duels.is_busy_row(None, ME) is False
    assert duels.is_busy_row({}, ME) is False


# --- a targeted challenge does not pre-create the thread -------------------

def test_challenge_dm_tells_them_to_accept_and_links_the_board():
    duels = _load_duels()
    text = duels.challenge_dm_text("cis6led", "chris7", 4, 20, 2,
                                  "<#1234567890>", "6c447ecc")
    assert "cis6led" in text
    assert "<#1234567890>" in text
    assert "Accept" in text
    assert "4" in text and "20" in text and "2" in text
    # The thread does not exist yet — it must be promised only AFTER accepting.
    assert "after you accept" in text.lower()


def test_challenge_dm_never_raises_on_junk():
    duels = _load_duels()
    assert duels.challenge_dm_text(None, None, None, None, None, None, None)
    assert duels.challenge_dm_text("a", "b", 0, 0, 0, "", "")


# --- check_eligibility must forward the ignore id --------------------------

@pytest.mark.asyncio
async def test_check_eligibility_ignores_the_wager_being_accepted(monkeypatch):
    duels = _load_duels()
    seen = {}

    async def _busy(uid, *, ignore_id=""):
        seen["ignore_id"] = ignore_id
        return None
    monkeypatch.setattr(duels, "get_user_active", _busy)

    async def _row(uid):
        return {"game_username": "chris7", "nectar_coins": 100}
    monkeypatch.setattr(duels.db, "get_by_discord", _row)
    monkeypatch.setattr(duels, "_sb", lambda: object())
    monkeypatch.setattr(duels, "wager_ban_reason", lambda _u: _none())
    monkeypatch.setattr(duels, "user_sessions_recent", _sessions())

    ok, _reason = await duels.check_eligibility(ME, 0, ignore_wager_id="w1")
    assert seen.get("ignore_id") == "w1"
    assert ok is True


async def _none():
    return ""


def _sessions():
    async def _f(_uid):
        return [{"session_id": "s"}]
    return _f
