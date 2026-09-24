"""
coins.py — Nectar Coins economy (pure helpers, never raise).

Earning (per user request): daily claim + streaks only.
Spending: gambling / bets / wagers (coinflip, slots, dice)
+ player-to-player transfers.

Balances live on bngc_users.nectar_coins; every mutation appends a row
to bngc_coin_ledger (see supabase/add_nectar_coins_v252.sql).

Pure module: no I/O, no Supabase imports.
"""

from __future__ import annotations

import os
import secrets

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

DAILY_MIN = 1  # random payout per daily claim, inclusive
DAILY_MAX = 5

COINFLIP_MIN = 1
COINFLIP_MAX = 10_000

# Slots + dice share the coinflip limits (per user request: same limits).
SLOTS_MIN = COINFLIP_MIN
SLOTS_MAX = COINFLIP_MAX
DICE_MIN = COINFLIP_MIN
DICE_MAX = COINFLIP_MAX

# Slots: 3 uniform reels. Trips pay profit multiples below (delta = +mult*bet);
# exactly one pair refunds the bet (delta 0); anything else loses the bet.
# RTP ≈ 0.88 (trips 4% @ avg 10x return + pairs 48% @ 1x return).
SLOT_SYMBOLS: list[str] = ["🍒", "⭐", "🪙", "💎", "7️⃣"]
SLOT_TRIPS_MULT: dict[str, int] = {"🍒": 2, "⭐": 3, "🪙": 5, "💎": 10, "7️⃣": 25}

# Dice: roll 1-100 vs 50. Over wins on 51-100, Under wins on 1-49,
# exactly 50 pushes (refunded). Winner doubles (delta +bet). RTP = 0.98.
DICE_PUSH = 50
DICE_SIDES = 100

PAY_MIN = 1
PAY_MAX = 100_000

STARTING_BALANCE = 0

# Daily wager-earnings cap (profit, not gross): duel wins + match-bet wins.
# Tourney prizes, daily claims, play-to-earn and casino wins are NOT capped.
WAGER_EARN_CAP = int(os.getenv("WAGER_EARN_CAP", "50") or 50)
WAGER_PROFIT_REASONS: frozenset = frozenset({"bet_won", "duel_win"})


def cap_wager_profit(profit: int, earned_today: int,
                     cap: int = WAGER_EARN_CAP) -> int:
    """Payable profit under the daily wager-earnings cap. Never raises."""
    try:
        room = int(cap) - int(earned_today or 0)
        if room <= 0:
            return 0
        return max(0, min(int(profit or 0), room))
    except Exception:
        return 0


def daily_coins_for(streak: int = 0, rng=None) -> int:
    """Random DAILY_MIN-DAILY_MAX coins per daily claim.

    The streak is still tracked (shown in /balance) but carries no bonus.
    Injectable rng (needs .randint) for tests. Never raises.
    """
    try:
        if rng is not None:
            return int(rng.randint(DAILY_MIN, DAILY_MAX))
        return int(secrets.randbelow(DAILY_MAX - DAILY_MIN + 1)) + DAILY_MIN
    except Exception:
        return DAILY_MIN


def validate_bet(balance: int, amount) -> tuple[bool, str | None]:
    """Validate a coinflip wager. Returns (ok, error). Never raises."""
    try:
        amt = int(amount)
    except Exception:
        return False, "Bet must be a whole number."
    try:
        bal = int(balance or 0)
    except Exception:
        bal = 0
    if amt < COINFLIP_MIN:
        return False, f"Minimum bet is {COINFLIP_MIN} coins."
    if amt > COINFLIP_MAX:
        return False, f"Maximum bet is {COINFLIP_MAX:,} coins."
    if amt > bal:
        return False, f"Insufficient balance ({bal:,} coins)."
    return True, None


def flip_coin(rng=None) -> str:
    """Heads/Tails flip. Injectable rng for tests. Never raises."""
    try:
        if rng is not None:
            return str(rng.choice(["Heads", "Tails"]))
        return str(secrets.choice(["Heads", "Tails"]))
    except Exception:
        return "Heads"


def normalize_choice(choice) -> str | None:
    """Normalize heads/tails input. None when invalid. Never raises."""
    try:
        c = str(choice or "").strip().lower()
    except Exception:
        return None
    if c in ("heads", "head", "h"):
        return "Heads"
    if c in ("tails", "tail", "t"):
        return "Tails"
    return None


def resolve_flip(bet: int, choice: str, result: str) -> int:
    """Net delta for a resolved flip: +bet on win, -bet on loss. Never raises."""
    try:
        b = int(bet or 0)
    except Exception:
        return 0
    try:
        return b if str(choice) == str(result) else -b
    except Exception:
        return -b


def spin_slots(rng=None) -> list[str]:
    """Spin 3 uniform reels. Injectable rng (needs .choice) for tests."""
    try:
        pick = rng.choice if rng is not None else secrets.choice
        return [str(pick(SLOT_SYMBOLS)) for _ in range(3)]
    except Exception:
        return ["🍒", "🍒", "🍒"]


def slots_kind(symbols) -> str:
    """trips | pair | loss for 3 symbols. Never raises."""
    try:
        s = [str(x) for x in (symbols or [])][:3]
        if len(s) < 3:
            return "loss"
        if s[0] == s[1] == s[2]:
            return "trips"
        if s[0] == s[1] or s[0] == s[2] or s[1] == s[2]:
            return "pair"
        return "loss"
    except Exception:
        return "loss"


def slots_delta(bet: int, symbols) -> int:
    """Net delta for a spin: trips +mult*bet, pair 0 (refund), else -bet."""
    try:
        b = int(bet or 0)
    except Exception:
        return 0
    try:
        kind = slots_kind(symbols)
        if kind == "trips":
            s = [str(x) for x in (symbols or [])][:3]
            return b * int(SLOT_TRIPS_MULT.get(s[0], 0))
        if kind == "pair":
            return 0
        return -b
    except Exception:
        try:
            return -int(bet or 0)
        except Exception:
            return 0


def normalize_dice_choice(choice) -> str | None:
    """Normalize over/under input. None when invalid. Never raises."""
    try:
        c = str(choice or "").strip().lower()
    except Exception:
        return None
    if c in ("over", "o", "high", "hi", "h"):
        return "Over"
    if c in ("under", "u", "low", "lo", "l"):
        return "Under"
    return None


def roll_dice(rng=None) -> int:
    """Roll 1-100. Injectable rng (needs .randrange or .randint) for tests."""
    try:
        if rng is not None:
            if hasattr(rng, "randrange"):
                return int(rng.randrange(1, DICE_SIDES + 1))
            if hasattr(rng, "randint"):
                return int(rng.randint(1, DICE_SIDES))
            return int(rng.choice(list(range(1, DICE_SIDES + 1))))
        return int(secrets.randbelow(DICE_SIDES)) + 1
    except Exception:
        return DICE_PUSH


def dice_delta(bet: int, choice: str, roll: int) -> int:
    """Net delta: +bet on win, 0 on push (roll == 50), -bet on loss."""
    try:
        b = int(bet or 0)
        r = int(roll)
    except Exception:
        return 0
    try:
        if r == DICE_PUSH:
            return 0
        won = (str(choice) == "Over" and r > DICE_PUSH) or \
              (str(choice) == "Under" and r < DICE_PUSH)
        return b if won else -b
    except Exception:
        try:
            return -int(bet or 0)
        except Exception:
            return 0


def validate_transfer(balance: int, amount, from_id, to_id) -> tuple[bool, str | None]:
    """Validate a player-to-player transfer. Returns (ok, error). Never raises."""
    try:
        amt = int(amount)
    except Exception:
        return False, "Amount must be a whole number."
    try:
        if str(from_id or "") == str(to_id or ""):
            return False, "You can't pay yourself."
    except Exception:
        pass
    if amt < PAY_MIN:
        return False, f"Minimum transfer is {PAY_MIN} coin."
    if amt > PAY_MAX:
        return False, f"Maximum transfer is {PAY_MAX:,} coins."
    try:
        if amt > int(balance or 0):
            return False, f"Insufficient balance ({int(balance or 0):,} coins)."
    except Exception:
        return False, "Insufficient balance."
    if not str(to_id or "").strip():
        return False, "Recipient is required."
    return True, None
