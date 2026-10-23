"""
streaks.py -v2.19.0 reward-path math (daily / win / kill streaks).

Pure helpers, never raise. Tunables at top. Used by processor
(batch-end accrual) and stats (read path). Bonus paid in arena
points (same ledger as milestones).
"""

from __future__ import annotations

from datetime import date

# Bonus curves: threshold -> payout (paid once when threshold hit)
DAILY_BONUS = {1: 10, 3: 25, 7: 75, 30: 300}
WIN_BONUS = {2: 20, 3: 50, 5: 150}
KILL_BONUS = {5: 15, 10: 50, 15: 120}


def daily_update(last_claim, today=None) -> tuple[int, int, bool]:
    """Advance daily streak. Returns (new_streak, bonus, claimed).

    Same-day re-claim -> (streak, 0, False). Gap of exactly 1 day
    continues, gap >1 resets to 1. First ever claim -> (1, bonus@1).
    Never raises.
    """
    try:
        t = today or date.today()
        if isinstance(t, str):
            t = date.fromisoformat(t[:10])
        if last_claim in (None, ""):
            s = 1
            return s, DAILY_BONUS.get(s, 0), True
        lc = last_claim
        if isinstance(lc, str):
            lc = date.fromisoformat(lc[:10])
        if not hasattr(lc, "toordinal"):
            return 1, DAILY_BONUS.get(1, 0), True
        gap = (t - lc).days
        if gap <= 0:
            return 0, 0, False  # already claimed (same day or future clock)
        if gap == 1:
            # caller passes current streak separately; bump handled by caller.
            # This helper reports continuity: bonus resolved by _bonus_for.
            return -1, 0, True  # -1 = continue (caller increments)
        return 1, DAILY_BONUS.get(1, 0), True
    except Exception:
        return 1, DAILY_BONUS.get(1, 0), True


def daily_continue(current_streak: int) -> tuple[int, int]:
    """Increment a continuing daily streak. Returns (new_streak, bonus)."""
    try:
        s = int(current_streak or 0) + 1
    except Exception:
        s = 1
    return s, DAILY_BONUS.get(s, 0)


def win_bonus(streak: int) -> int:
    """Bonus for a win streak value (0 when no threshold hit)."""
    try:
        return WIN_BONUS.get(int(streak or 0), 0)
    except Exception:
        return 0


def kill_bonus(kills: int) -> int:
    """Bonus for kills in a single match (0 when no threshold hit)."""
    try:
        return KILL_BONUS.get(int(kills or 0), 0)
    except Exception:
        return 0
