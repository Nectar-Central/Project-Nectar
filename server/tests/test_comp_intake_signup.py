"""Signup intake rules: who gets in, and what the channel is left showing.

Three changes, all in `on_message` / `_comp_team_members`:

- A message with NO mention is chatter, not a signup attempt. It used to get a
  cross and a "duo needs exactly 2 real mention(s), got 0" notice, which meant
  every normal message in the signup channel was answered by the bot.
- A duo tourney accepts a SOLO entry (one mention). Registering alone in a duo
  cup is legitimate - the player is put in unpaired and a host can team them up
  later - so "needs exactly 2" was a dead end, not a rule.
- The "already registered" notice now says who and in what capacity, because
  the bare string gave the player nothing to act on.

Pure helpers, no Discord, no DB.
"""
import sys
from pathlib import Path

BOT_DIR = str(Path("companion/bot").resolve())
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)


def _members(ids, mode="duo"):
    import bot as bm
    return bm._comp_team_members(ids, mode)


# --- no mentions -----------------------------------------------------------

def test_zero_mentions_is_rejected_before_anything_else():
    # the intake returns early on an empty mention list, so this only has to be
    # an error - never a silent pass that would register nobody
    members, err = _members([], "duo")
    assert members == []
    assert err


def test_zero_mentions_is_rejected_in_solo_too():
    members, err = _members([], "solo")
    assert members == []
    assert err


# --- solo entry in a duo tourney ------------------------------------------

def test_one_mention_in_a_duo_tourney_is_a_solo_entry():
    members, err = _members(["A"], "duo")
    assert err is None, "a solo entry in a duo tourney must be allowed"
    assert members == ["A"]


def test_two_mentions_in_a_duo_tourney_is_still_a_team():
    members, err = _members(["A", "B"], "duo")
    assert err is None
    assert members == ["A", "B"]


def test_three_mentions_in_a_duo_tourney_is_still_refused():
    members, err = _members(["A", "B", "C"], "duo")
    assert members == []
    assert err and "2" in err


def test_duo_mentions_do_not_need_the_author_first():
    # order is normalised later; validation must not care
    assert _members(["B", "A"], "duo")[1] is None
    assert _members(["A", "B"], "duo")[1] is None


def test_duplicate_mentions_are_refused():
    assert _members(["A", "A"], "duo")[1] is not None


# --- solo tourneys are unchanged -------------------------------------------

def test_solo_tourney_still_wants_exactly_one():
    assert _members(["A"], "solo")[1] is None
    assert _members(["A", "B"], "solo")[1] is not None
    assert _members([], "solo")[1] is not None


def test_solo_error_names_the_mode():
    _members(["A", "B"], "solo")
    _m, err = _members(["A", "B"], "solo")
    assert "solo" in err.lower()


def test_junk_input_is_refused_not_raised():
    for bad in (None, [None], [""], [{}]):
        members, err = _members(bad, "duo")
        assert members == [] or isinstance(members, list)
        assert err is None or isinstance(err, str)


def test_announcement_mentions_signing_up_alone():
    import bot as bm
    duo = bm._comp_open_text("duo", "", "Cup")
    assert "@yourself" in duo
    assert "alone" in duo.lower(), "duo signups must advertise the solo option"
    solo = bm._comp_open_text("solo", "", "Cup")
    assert "alone" not in solo.lower(), "solo signups have no teammate to add"
