"""Account transfer: move a player's profile + lifetime counters to a new id.

Pure planning - no DB, no Discord. `plan_transfer` is the whole decision:
given the source and target `bngc_users` rows it returns the exact patch for
each, so the command is I/O plus `update_wager`-style writes and the rules are
unit-testable.

Scope, deliberately narrow: the live profile and the lifetime counters move;
HISTORICAL rows (game events, comp/ranked events, registrations, per-tourney
PR history, bets, duel wagers) stay behind with the old id, so past boards
keep showing what actually happened. The target therefore inherits totals
without inheriting the matches that produced them, and per-tourney Elo/PR
history starts fresh there.

Merge rules are per-field, not blanket:
  sum      - a total that only makes sense accumulated (kills, coins, playtime)
  max      - a high-water mark or a rating (streaks, best, tourney_pr)
  earliest - account age / a claim stamp that should not be circumvented
  replace  - identity, which is a single value by definition

The source is emptied and stamped `transferred_to`, so the old account cannot
be used for double-dipping afterwards.
"""
from __future__ import annotations

# Accumulate: totals, not high-water marks.
SUM_FIELDS = (
    "kills", "deaths", "wins", "defeats", "playtime_global",
    "play_points", "play_points_hours_paid",
    "nectar_coins", "nectar_total_earned", "nectar_total_wagered",
)

# Keep the better of the two: streaks, records and ratings are maxima.
# best_match_kills is a record (one best game), never a sum of two accounts.
MAX_FIELDS = ("win_streak", "best_win_streak", "best_match_kills",
              "tourney_pr", "daily_streak")

# Account age / claim stamps: the earliest wins, never the later.
EARLIEST_FIELDS = ("first_seen", "registered_at", "last_daily_claim")

# Identity: one value each, the source's wins (that is the account's face).
PROFILE_FIELDS = (
    "game_username", "game_id", "skin", "pickaxe",
    "lobby_music_url", "lobby_music_volume",
)

# Never moved: token (auth), lock state, discord_username (the target's own
# identity), last_online/last_seen (live presence, not data).
KEEP_ON_SOURCE = (
    "token", "discord_id", "discord_username",
    "is_locked", "lock_reason", "locked_at", "locked_ip",
    "lock_scope", "is_admin_locked",
)

# The per-game-mode playtime map merges key by key.
PLAYTIME_MAP_FIELD = "playtime_per_gm"


class TransferRefused(Exception):
    """Why a transfer must not run. The command shows this to the admin."""


def _int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _ts(value) -> str | None:
    """ISO string or None; '' counts as unset."""
    try:
        s = str(value or "").strip()
        return s or None
    except Exception:
        return None


def _earliest(a, b):
    """The older of two timestamps (None = unknown = never wins)."""
    a, b = _ts(a), _ts(b)
    if a and b:
        return a if a <= b else b
    return a or b


def _merge_playtime(src, dst) -> dict:
    """Per-game-mode playtime, summed key by key."""
    out: dict = {}
    for raw in (dst, src):
        try:
            data = raw or {}
            if isinstance(data, str):
                import json
                data = json.loads(data or "{}")
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for k, v in data.items():
            out[str(k)] = _int(out.get(str(k))) + _int(v)
    return out


def plan_transfer(source: dict, target: dict, now_iso: str = "",
                  ranked_points: int = 0) -> dict:
    """Patches that move `source` into `target`.

    Returns {"target": {...}, "source": {...}, "ranked_points": int,
    "moves": [(label, from, to), ...]} - `moves` is the human-readable audit
    line, and is empty-safe.

    Raises TransferRefused for anything that must not be applied. Never
    returns partial work: a refusal happens before any patch is built.
    """
    src = dict(source or {})
    dst = dict(target or {})
    sid = str(src.get("discord_id") or "").strip()
    tid = str(dst.get("discord_id") or "").strip()
    if not sid or not tid:
        raise TransferRefused("Both accounts need a discord id.")
    if sid == tid:
        raise TransferRefused("Source and target are the same account.")
    if str(src.get("transferred_to") or "").strip():
        raise TransferRefused(
            f"Source was already transferred to {src['transferred_to']}.")
    if str(dst.get("transferred_to") or "").strip():
        raise TransferRefused("Target is itself a transferred account.")
    if not str(src.get("token") or "").strip():
        raise TransferRefused("Source has no token - it is not a real account.")

    moves: list = []
    t_patch: dict = {}
    for f in SUM_FIELDS:
        a, b = _int(src.get(f)), _int(dst.get(f))
        if a or b:
            t_patch[f] = a + b
        if a:
            moves.append((f, a, t_patch.get(f, b)))
    for f in MAX_FIELDS:
        a, b = src.get(f), dst.get(f)
        if a is None and b is None:
            continue
        if f in ("tourney_pr", "daily_streak"):
            best = max((_ts(x) for x in (a, b) if _ts(x)), default=None)
            if best is not None:
                t_patch[f] = best
        else:
            t_patch[f] = max(_int(a), _int(b))
        if a is not None:
            moves.append((f, a, t_patch.get(f)))
    for f in EARLIEST_FIELDS:
        a, b = src.get(f), dst.get(f)
        if a is None and b is None:
            continue
        pick = _earliest(a, b)
        if pick is not None:
            t_patch[f] = pick
        if a is not None:
            moves.append((f, a, pick))
    for f in PROFILE_FIELDS:
        v = src.get(f)
        if v is not None and str(v).strip() != "":
            t_patch[f] = v
            moves.append((f, dst.get(f), v))
    src_map = src.get(PLAYTIME_MAP_FIELD)
    if src_map:
        merged = _merge_playtime(src_map, dst.get(PLAYTIME_MAP_FIELD))
        t_patch[PLAYTIME_MAP_FIELD] = merged
        moves.append((PLAYTIME_MAP_FIELD, "source map", f"{len(merged)} modes"))

    # The source is left empty and stamped, so the same stats cannot be moved
    # twice or farmed by re-registering the old account.
    s_patch: dict = {f: 0 for f in SUM_FIELDS}
    s_patch.update({f: 0 for f in MAX_FIELDS})
    s_patch.update({f: None for f in PROFILE_FIELDS})
    s_patch["playtime_per_gm"] = {}
    s_patch["transferred_to"] = tid
    s_patch["transferred_at"] = _ts(now_iso) or None

    return {"target": t_patch, "source": s_patch,
            "ranked_points": _int(ranked_points), "moves": moves}
