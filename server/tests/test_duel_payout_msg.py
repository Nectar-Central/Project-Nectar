"""The payout must not ping the winner in public.

`_settle` announced the money with `<@winner_id> takes **N** Nectar Coins` in
the duel thread — a public mention of somebody's balance, in a channel the
duelists (and mods) can see, on every single resolution. The money detail
belongs in a DM; the thread only needs the fact that the duel resolved.

Pinned here: the public line carries no mention and no balance, and the DM
carries the amount, the new balance and the daily-cap note.
Pure text builders, no Discord, no DB.
"""
import importlib.util
import sys
from pathlib import Path


def _load_duels():
    bot_dir = str(Path("companion/bot").resolve())
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    spec = importlib.util.spec_from_file_location(
        "duels_payout_under_test", Path(bot_dir) / "duels.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WINNER = "1499874358229991708"


def test_public_resolve_line_has_no_mention_and_no_balance():
    duels = _load_duels()
    line = duels.payout_public_text(pay_amount=4, pot=4, capped=False,
                                    new_bal=1234)
    assert "<@" not in line
    assert "1234" not in line          # balance stays private
    assert "4" in line
    assert "Nectar Coins" in line


def test_public_resolve_line_for_a_for_fun_duel():
    duels = _load_duels()
    line = duels.payout_public_text(pay_amount=0, pot=0, capped=False,
                                    new_bal=None)
    assert "<@" not in line
    assert "Nectar Coins" not in line
    assert "win" in line.lower()


def test_public_resolve_line_mentions_the_cap():
    duels = _load_duels()
    line = duels.payout_public_text(pay_amount=12, pot=20, capped=True,
                                    new_bal=100)
    assert "cap" in line.lower()
    assert "<@" not in line


def test_payout_dm_carries_the_money_details():
    duels = _load_duels()
    text = duels.payout_dm_text("6c447ecc", 4, 1234, capped=False,
                               how="Late Game Zonewars first to 5")
    assert "4" in text and "1,234" in text
    assert "6c447ecc" in text
    assert "Nectar Coins" in text
    # The cap rule is only spelled out when it actually applied.
    assert "50" not in text
    assert "50" in duels.payout_dm_text("6c447ecc", 4, 1234, capped=True, how="x")


def test_payout_dm_handles_a_for_fun_duel_and_junk():
    duels = _load_duels()
    text = duels.payout_dm_text("6c447ecc", 0, None, capped=False, how="forfeit")
    assert "6c447ecc" in text
    assert "for fun" in text
    assert duels.payout_dm_text(None, None, None, None, None)
    assert duels.payout_public_text(None, None, None, None)


def test_payout_dm_survives_a_missing_balance():
    duels = _load_duels()
    text = duels.payout_dm_text("6c447ecc", 4, None, capped=True, how="x")
    assert "4" in text
    assert "cap" in text.lower()
