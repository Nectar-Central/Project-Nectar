"""
bets.py -match betting pool math + fixed-odds engine (pure, never raise).

Bet types:
  win    parimutuel pool on the match winner (existing behavior).
  out    fixed odds: the pick gets eliminated. Live rooms only, where
         death/leave rows are banked (elsewhere "not winning" is not an
         elimination, so the bet is meaningless there).
  kills  fixed odds: the pick reaches `line` kills (line 2..20).

Fixed-odds multipliers lock at placement (odds = payout per 1 staked).
They price off the player's own ledger history: scoring rate λ (avg
kills/match) via Poisson for kills lines, win rate + lobby size for
outs -8% margin, clamped to [1.05x, 15x]. No-history players get
conservative priors (flagged provisional).

Pure module: no I/O, no Supabase imports.
"""

from __future__ import annotations

import math

BET_MIN = 1
BET_MAX = 10_000
RAKE_BPS = 500  # 5% of a decisive pool is burned

KINDS = ("comp", "rt", "room", "live")
BET_TYPES = ("win", "out", "kills")

# kills-line menu bounds (bettor picks any integer X in range).
LINE_MIN = 2
LINE_MAX = 20

# fixed-odds rails: margin under fair, payout clamp per 1 staked.
ODDS_MARGIN = 0.92
ODDS_MIN = 1.05
ODDS_MAX = 15.0

# priors for players with no ledger history (slightly below average,
# flagged provisional so the UI can say so).
PRIOR_LAM = 1.2
PRIOR_WIN_RATE = 0.12

# Live rooms (any public/private match, evidence from bngc_game_events):
# first to this many kills takes the pool even before the match ends.
LIVE_KILL_TARGET = 10
# listing shows rooms with evidence this fresh; older rooms settle as
# abandoned (refund) once nothing new arrives within STALE_S.
LIVE_WINDOW_S = 600
STALE_S = 900
# discovery looks back this far when grouping recent evidence rows.
DISCOVERY_SCAN_S = 1800
EVIDENCE_ROW_LIMIT = 2000


def parse_key(match_key) -> dict | None:
    """Parse a bet key into {kind, tourney_id|room_id, version}. None if bad."""
    try:
        parts = str(match_key or "").strip().split(":")
        if len(parts) != 3:
            return None
        kind, a, b = parts[0].strip().lower(), parts[1].strip(), parts[2].strip()
        if kind not in KINDS or not a or not b:
            return None
        version = int(b)
        if version < 0:
            return None
        if kind == "room" or kind == "live":
            return {"kind": kind, "room_id": a, "version": version}
        return {"kind": kind, "tourney_id": int(a), "version": version}
    except Exception:
        return None


def make_key(kind: str, ref, version: int) -> str | None:
    """Build a canonical bet key. None when invalid. Never raises."""
    try:
        k = str(kind or "").strip().lower()
        if k not in KINDS:
            return None
        return f"{k}:{str(ref).strip()}:{int(version)}"
    except Exception:
        return None


def normalize_bet_type(bet_type) -> str:
    """win | out | kills (default win). Never raises."""
    try:
        t = str(bet_type or "").strip().lower()
    except Exception:
        return "win"
    return t if t in BET_TYPES else "win"


def validate_line(line) -> tuple[bool, str | None]:
    """Kills-line menu bounds. Never raises."""
    try:
        x = int(line)
    except Exception:
        return False, f"Pick a kills line {LINE_MIN}-{LINE_MAX}."
    if x < LINE_MIN or x > LINE_MAX:
        return False, f"Line must be {LINE_MIN}-{LINE_MAX} kills."
    return True, None


def _clamp_mult(x: float) -> float:
    try:
        v = float(x or 0)
    except Exception:
        return ODDS_MIN
    if v != v:  # NaN guard
        return ODDS_MIN
    return max(ODDS_MIN, min(ODDS_MAX, round(v, 2)))


def poisson_reach(lam: float, line: int) -> float:
    """P(kills >= line) for scoring rate lam. Never raises."""
    try:
        l = max(0.0, float(lam or 0))
        x = int(line)
    except Exception:
        return 0.0
    if x <= 0:
        return 1.0
    if l <= 0:
        return 0.0
    try:
        term = math.exp(-l)
        cdf = term  # P(X == 0)
        for i in range(1, x):
            term *= l / i
            cdf += term
        return max(0.0, min(1.0, 1.0 - cdf))
    except Exception:
        return 0.0


def kills_odds(lam: float, line: int) -> float:
    """Fixed payout per 1 staked that the pick reaches `line` kills."""
    try:
        return _clamp_mult(ODDS_MARGIN / max(1e-9, poisson_reach(lam, line)))
    except Exception:
        return ODDS_MIN


def out_odds(win_rate: float, field_size: int) -> float:
    """Fixed payout per 1 staked that the pick gets eliminated.

    p(win) scales the player's base win rate to lobby size around an
    8-player reference; elimination is the complement. Never raises.
    """
    try:
        wr = max(0.0, min(1.0, float(win_rate or 0)))
        n = max(2, int(field_size or 8))
        p_win = max(0.02, min(0.90, wr * 8.0 / n))
        return _clamp_mult(ODDS_MARGIN / max(1e-9, 1.0 - p_win))
    except Exception:
        return ODDS_MIN


def payout_for(stake: int, odds) -> int:
    """Coin payout for a fixed-odds win (includes returned stake)."""
    try:
        return max(0, int(math.floor(int(stake) * float(odds) + 0.5)))
    except Exception:
        return 0


def form_from_ledger(kill_rows: int, win_rows: int, matches: int) -> dict:
    """Scoring form from ledger counts: {lam, win_rate, provisional}."""
    try:
        m = int(matches or 0)
    except Exception:
        m = 0
    if m <= 0:
        return {"lam": PRIOR_LAM, "win_rate": PRIOR_WIN_RATE,
                "provisional": True}
    try:
        lam = max(0.0, float(kill_rows or 0) / m)
    except Exception:
        lam = PRIOR_LAM
    try:
        wr = max(0.0, min(1.0, float(win_rows or 0) / m))
    except Exception:
        wr = PRIOR_WIN_RATE
    return {"lam": lam, "win_rate": wr, "provisional": False}


def validate_stake(balance: int, amount) -> tuple[bool, str | None]:
    """Validate a bet stake against the shared 10 / 10,000 limits."""
    try:
        amt = int(amount)
    except Exception:
        return False, "Amount must be a whole number."
    try:
        bal = int(balance or 0)
    except Exception:
        bal = 0
    if amt < BET_MIN:
        return False, f"Minimum bet is {BET_MIN} coins."
    if amt > BET_MAX:
        return False, f"Maximum bet is {BET_MAX:,} coins."
    if amt > bal:
        return False, f"Insufficient balance ({bal:,} coins)."
    return True, None


def winners_with_teammates(winner_discord_id, regs) -> set[str]:
    """Winner + duo team-mates (accepted reg containing the winner).

    Solo winner with no reg entry -> {winner}. Empty winner -> empty set
    (caller refunds). Never raises.
    """
    try:
        w = str(winner_discord_id or "").strip()
    except Exception:
        return set()
    if not w:
        return set()
    out = {w}
    try:
        for r in (regs or []):
            try:
                members = [str(x) for x in ((r or {}).get("member_ids") or []) if x]
            except Exception:
                continue
            if w in members:
                out.update(members)
                break
    except Exception:
        pass
    return out


def settle_pool(bets: list[dict], winners: set[str]) -> dict:
    """Split a sealed pool. Pure, unit-tested.

    bets: [{id, bettor, pick, amount}]. winners: winning discord_ids
    (empty -> full refund, e.g. sealed with no winner).
    Returns {credits: {bet_id: payout}, rake, pool, refunded: bool}.
    Payout includes the returned stake; floor dust stays burned with rake.
    Nobody picked the winner -> full refund (no rake).
    """
    try:
        rows = list(bets or [])
    except Exception:
        rows = []
    pool = 0
    clean: list[dict] = []
    for b in rows:
        try:
            amt = int((b or {}).get("amount") or 0)
            if amt <= 0:
                continue
            pool += amt
            clean.append({"id": (b or {}).get("id"),
                          "pick": str((b or {}).get("pick") or ""),
                          "amount": amt})
        except Exception:
            continue
    if not clean:
        return {"credits": {}, "rake": 0, "pool": 0, "refunded": True}
    try:
        win_set = {str(x) for x in (winners or set()) if str(x or "").strip()}
    except Exception:
        win_set = set()
    won = [b for b in clean if b["pick"] in win_set]
    if not won:
        # no winner, or nobody picked them: everybody refunded whole
        return {"credits": {b["id"]: b["amount"] for b in clean},
                "rake": 0, "pool": pool, "refunded": True}
    try:
        rake = pool * int(RAKE_BPS) // 10_000
    except Exception:
        rake = 0
    pot = pool - rake
    w_stakes = sum(b["amount"] for b in won)
    credits: dict = {}
    try:
        for b in clean:
            if b["pick"] in win_set and w_stakes > 0:
                credits[b["id"]] = b["amount"] * pot // w_stakes
            else:
                credits[b["id"]] = 0
    except Exception:
        credits = {b["id"]: 0 for b in clean}
    return {"credits": credits, "rake": rake, "pool": pool, "refunded": False}


def live_result(kills, dead, left, win_did, participants,
                kill_target: int = LIVE_KILL_TARGET) -> dict:
    """Winner for a live public/private room from capture evidence. Pure.

    kills: {discord_id: kills}. dead/left/participants: sets of discord_ids.
    win_did: discord_id on the banked win row (None when the match is still
    running). Priority: banked win > first to kill_target > last player
    standing (every other participant dead or gone). Anything else ->
    pending (caller refunds once the room goes stale or a newer version
    starts). Ties at/above the target stay pending. Never raises.
    """
    try:
        parts = {str(x) for x in (participants or set()) if str(x or "").strip()}
    except Exception:
        parts = set()
    if len(parts) < 2:
        return {"status": "pending", "winner": None}
    try:
        w = str(win_did or "").strip()
    except Exception:
        w = ""
    if w and w in parts:
        return {"status": "won", "winner": w}
    try:
        target = int(kill_target or 0)
    except Exception:
        target = 0
    counts: dict[str, int] = {}
    try:
        for did, n in (kills or {}).items():
            try:
                if str(did) in parts:
                    counts[str(did)] = int(n or 0)
            except Exception:
                continue
    except Exception:
        pass
    if target > 0 and counts:
        try:
            best = max(counts.values())
            leaders = [d for d, n in counts.items() if n == best]
            if best >= target and len(leaders) == 1:
                return {"status": "won", "winner": leaders[0]}
        except Exception:
            pass
    try:
        out = {str(x) for x in (dead or set())} | {str(x) for x in (left or set())}
        standing = [d for d in parts if d not in out]
        if len(standing) == 1:
            return {"status": "won", "winner": standing[0]}
    except Exception:
        pass
    return {"status": "pending", "winner": None}
