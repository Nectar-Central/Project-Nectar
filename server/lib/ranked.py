"""
ranked.py — Ranked v1 server-side: room cache, milestones, ELO rewards, sealing.

Contract (Phase 1):
  - `get_ranked_room(short_code)` -> room dict | None (in-memory, refreshed every 30s)
  - `get_ranked_points(discord_id)` -> int (60s per-player cache, 0 on miss)
  - `milestone_for(points)` / `next_milestone(points)` -> tier info
  - `elo_kill_reward(killer_pts, victim_pts, placement)` / `win_reward(placement)` -> int
  - `seal_match(...)` -> {"ok": True, ...} | {"ok": False, "error": ...} (Phase 2 calls this)

Notes:
  - Supabase is the source of truth; memory is only a read cache. A cold
    start just means an empty cache until the first refresh (never fatal).
  - Nothing here raises: cache misses and DB blips degrade to empty/0/false.
  - ELO params: K=25, S=150, WIN_BASE=30, floor 1/kill, placement mults 1st=2.0.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any

from .logger import log_sys, log_err

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

K_FACTOR = 25            # base volatility per kill
RATING_SCALE = 150       # rating scale (fits the 0-2500 points range)
WIN_BASE = 30            # flat win bonus (x placement mult)
MIN_KILL_REWARD = 1      # floor per kill so upsets always show something
PLACEMENT_MULTS = {1: 2.0, 2: 1.5, 3: 1.2}  # 4th+ = 1.0

REFRESH_S = 30           # room cache refresh interval
POINTS_TTL_S = 60        # per-player points cache TTL

# Milestone tiers, ascending. icon_url points at the public assets bucket.
_ASSETS = "https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/assets"
MILESTONE_TIERS = [
    {"name": "Unranked",    "min": 0,    "fee": 0,   "color": "#6b7280", "icon_url": f"{_ASSETS}/unranked.png"},
    {"name": "Bronze",      "min": 200,  "fee": 10,  "color": "#CD7F32", "icon_url": f"{_ASSETS}/bronze.png"},
    {"name": "Silver",      "min": 500,  "fee": 25,  "color": "#C0C0C0", "icon_url": f"{_ASSETS}/silver.png"},
    {"name": "Gold",        "min": 1000, "fee": 50,  "color": "#FFD700", "icon_url": f"{_ASSETS}/gold.png"},
    {"name": "Platinum",    "min": 1500, "fee": 75,  "color": "#E5E7EB", "icon_url": f"{_ASSETS}/platinium.png"},
    {"name": "Diamond",     "min": 2000, "fee": 100, "color": "#B9F2FF", "icon_url": f"{_ASSETS}/diamond.png"},
    {"name": "Emerald",     "min": 3000, "fee": 150, "color": "#10b981", "icon_url": f"{_ASSETS}/emerald.png"},
    {"name": "Champion",    "min": 4000, "fee": 200, "color": "#f59e0b", "icon_url": f"{_ASSETS}/champion.png"},
    {"name": "Master",      "min": 5000, "fee": 250, "color": "#9333EA", "icon_url": f"{_ASSETS}/master.png"},
    {"name": "Grandmaster", "min": 7500, "fee": 300, "color": "#ef4444", "icon_url": f"{_ASSETS}/grandmaster.png"},
]

# Overlay arena icon (bottom-left box next to points).
ARENA_ICON_URL = f"{_ASSETS}/arenaicon.png"

# ---------------------------------------------------------------------------
# In-memory caches (read-only; Supabase owns the truth)
# ---------------------------------------------------------------------------

_rooms: dict[str, dict[str, Any]] = {}          # short_code.upper() -> room row
_points_cache: dict[str, tuple[int, float]] = {}  # discord_id -> (points, ts)


def _sb():
    """Supabase client or None (tests / unconfigured env). Never raises."""
    try:
        from .supabase_client import supabase
        return supabase
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

def milestone_for(points: Any) -> dict[str, Any]:
    """Tier dict for a points value. Never raises."""
    try:
        p = int(points or 0)
    except Exception:
        p = 0
    tier = MILESTONE_TIERS[0]
    for t in MILESTONE_TIERS:
        if p >= t["min"]:
            tier = t
    return tier


def next_milestone(points: Any) -> tuple[dict[str, Any] | None, int, int]:
    """(next_tier | None, points_needed, pct_progress 0-100). Never raises."""
    try:
        p = int(points or 0)
    except Exception:
        p = 0
    cur = milestone_for(p)
    nxt = None
    for t in MILESTONE_TIERS:
        if t["min"] > cur["min"] and (nxt is None or t["min"] < nxt["min"]):
            nxt = t
    if nxt is None:
        return None, 0, 100
    span = nxt["min"] - cur["min"]
    pct = round(100 * (p - cur["min"]) / span) if span > 0 else 100
    return nxt, nxt["min"] - p, max(0, min(100, pct))


# ---------------------------------------------------------------------------
# ELO rewards
# ---------------------------------------------------------------------------

def elo_kill_reward(killer_pts: Any, victim_pts: Any, placement: Any) -> int:
    """ELO-scaled kill reward. Same-skill ~= 13, floor 1. Never raises."""
    try:
        k = int(killer_pts or 0)
        v = int(victim_pts or 0)
        pl = int(placement or 99)
    except Exception:
        return MIN_KILL_REWARD
    try:
        expected = 1.0 / (1.0 + 10.0 ** ((v - k) / RATING_SCALE))
        raw = K_FACTOR * expected * PLACEMENT_MULTS.get(pl, 1.0)
        return max(MIN_KILL_REWARD, round(raw))
    except Exception:
        return MIN_KILL_REWARD


def win_reward(placement: Any) -> int:
    """Winner-only bonus. Never raises."""
    try:
        return round(WIN_BASE * PLACEMENT_MULTS[1]) if int(placement) == 1 else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Room cache
# ---------------------------------------------------------------------------

def _norm_code(short_code: Any) -> str:
    try:
        return str(short_code or "").strip().upper()
    except Exception:
        return ""


def refresh_rooms() -> int:
    """Pull lobby+live rooms from Supabase into memory. Returns room count.

    Never raises — a DB blip keeps the previous cache.
    """
    sb = _sb()
    if not sb:
        return len(_rooms)
    try:
        r = sb.table("ranked_rooms").select("*").in_("status", ["lobby", "live"]).limit(200).execute()
        rows = r.data or []
        fresh: dict[str, dict[str, Any]] = {}
        for row in rows:
            code = _norm_code(row.get("short_code"))
            if code:
                fresh[code] = row
        _rooms.clear()
        _rooms.update(fresh)
        return len(_rooms)
    except Exception as exc:
        log_err(f"[ranked] refresh failed (keeping {len(_rooms)} cached): {exc}")
        return len(_rooms)


def get_ranked_room(short_code: Any) -> dict[str, Any] | None:
    """Fast in-memory lookup by 5-char code. None when not a ranked room."""
    code = _norm_code(short_code)
    if not code:
        return None
    return _rooms.get(code)


def room_roster_ids(room: dict[str, Any]) -> list[str]:
    """discord_id strings from a room's roster JSON. Never raises."""
    try:
        return [str(x.get("discord_id")) for x in (room.get("roster") or []) if x.get("discord_id")]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Points (cached reads, direct writes live in seal_match)
# ---------------------------------------------------------------------------

def get_ranked_points(discord_id: Any) -> int:
    """Cached points read. 0 on miss/error. Never raises."""
    try:
        did = str(discord_id or "").strip()
    except Exception:
        return 0
    if not did:
        return 0
    now = time.time()
    hit = _points_cache.get(did)
    if hit and now - hit[1] < POINTS_TTL_S:
        return hit[0]
    sb = _sb()
    if not sb:
        return hit[0] if hit else 0
    try:
        r = sb.table("ranked_points").select("points").eq("discord_id", did).limit(1).execute()
        pts = int((r.data[0].get("points") if r.data else 0) or 0)
        _points_cache[did] = (pts, now)
        return pts
    except Exception:
        return hit[0] if hit else 0


def _bump_points(discord_id: str, delta: int) -> int:
    """Read-modify-write a player's points. Returns new total. May raise (caller handles)."""
    sb = _sb()
    if not sb:
        raise RuntimeError("supabase not configured")
    cur = get_ranked_points(discord_id)
    total = cur + int(delta)
    sb.table("ranked_points").upsert({
        "discord_id": str(discord_id),
        "points": total,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="discord_id").execute()
    _points_cache[str(discord_id)] = (total, time.time())
    return total


# ---------------------------------------------------------------------------
# Sealing (Phase 2 calls this; contract defined now)
# ---------------------------------------------------------------------------

def seal_match(*, rid: str, match_version: int, gm: str,
               winner_discord_id: str | None,
               kills: dict[str, int], placements: dict[str, int],
               victim_points: dict[str, int] | None = None,
               kill_events: list | None = None) -> dict[str, Any]:
    """Award ELO points for one sealed match and persist results.

    kills: discord_id -> kill count (roster only, host-harvested).
    placements: discord_id -> placement (1-based, from host alv).
    victim_points: optional discord_id -> points snapshot override.
    kill_events: optional [(killer_did, victim_did)] for exact per-kill ELO
        (victim's own points per kill). When given, it replaces the kills
        loop; `kills` is still stored in the results row for review.
    Returns {"ok": True, "points": {...}, "match_version": N} or {"ok": False, "error": ...}.
    Never raises.

    NOTE: points bumps are additive — callers must pre-check that
    (rid, match_version) isn't already sealed, or points double-award
    (the results-row upsert alone does not prevent that).
    """
    try:
        if not rid or int(match_version or 0) <= 0:
            return {"ok": False, "error": "bad rid/match_version"}
        points_awarded: dict[str, int] = {}
        totals: dict[str, int] = {}
        if kill_events:
            per_killer: dict[str, int] = {}
            for pair in kill_events:
                try:
                    kdid, vdid = str(pair[0]), str(pair[1])
                except Exception:
                    continue
                try:
                    kpts = get_ranked_points(kdid)
                    vpts = int((victim_points or {}).get(vdid, get_ranked_points(vdid)))
                    pl = int((placements or {}).get(kdid, 99))
                    per_killer[kdid] = per_killer.get(kdid, 0) + elo_kill_reward(kpts, vpts, pl)
                except Exception as exc:
                    log_err(f"[ranked] kill reward failed for {kdid} in {rid}: {exc}")
            for did, sub in per_killer.items():
                try:
                    if winner_discord_id and did == str(winner_discord_id):
                        sub += win_reward(1)
                    if sub:
                        totals[did] = _bump_points(did, sub)
                    points_awarded[did] = sub
                except Exception as exc:
                    log_err(f"[ranked] award failed for {did} in {rid}: {exc}")
                    points_awarded[str(did)] = 0
        else:
            for did, kc in (kills or {}).items():
                try:
                    did = str(did)
                    pl = int((placements or {}).get(did, 99))
                    killer_pts = get_ranked_points(did)
                    sub = 0
                    for _ in range(max(0, int(kc or 0))):
                        vpts = int((victim_points or {}).get(did, killer_pts))
                        sub += elo_kill_reward(killer_pts, vpts, pl)
                    if winner_discord_id and did == str(winner_discord_id):
                        sub += win_reward(1)
                    if sub:
                        totals[did] = _bump_points(did, sub)
                    points_awarded[did] = sub
                except Exception as exc:
                    log_err(f"[ranked] award failed for {did} in {rid}: {exc}")
                    points_awarded[str(did)] = 0
        # winner with zero kills still gets the win bonus + a result row
        if winner_discord_id and str(winner_discord_id) not in points_awarded:
            try:
                w = str(winner_discord_id)
                bonus = win_reward(1)
                totals[w] = _bump_points(w, bonus)
                points_awarded[w] = bonus
            except Exception as exc:
                log_err(f"[ranked] winner award failed in {rid}: {exc}")
        sb = _sb()
        if not sb:
            return {"ok": False, "error": "supabase not configured"}
        try:
            sb.table("ranked_results").upsert({
                "room_id": rid,
                "match_version": int(match_version),
                "gm": gm,
                "winner_discord_id": str(winner_discord_id) if winner_discord_id else None,
                "kills": kills or {},
                "points": points_awarded,
                "placements": placements or {},
            }, on_conflict="room_id,match_version").execute()
        except Exception as exc:
            log_err(f"[ranked] results write failed for {rid} v{match_version}: {exc}")
            return {"ok": False, "error": "results write failed"}
        return {"ok": True, "points": points_awarded, "totals": totals,
                "match_version": int(match_version)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Seasons (ranked_seasons table: season int PK, ends_at timestamptz)
# ---------------------------------------------------------------------------

_SEASON_TTL_S = 300
_season_cache: dict = {"at": 0.0, "value": (1, None)}


def get_current_season() -> tuple:
    """(season_number, reset_at_iso | None). Defaults (1, None). Never raises.

    Cached 5 min. A missing table (pre-migration) degrades to the default
    instead of erroring every call.
    """
    now = time.time()
    try:
        if now - float(_season_cache.get("at") or 0) < _SEASON_TTL_S:
            return _season_cache["value"]
    except Exception:
        pass
    try:
        sb = _sb()
        if sb:
            r = sb.table("ranked_seasons").select("season,ends_at").order("season", desc=True).limit(1).execute()
            rows = r.data or []
            if rows:
                val = (int(rows[0].get("season") or 1), rows[0].get("ends_at"))
                _season_cache["at"] = now
                _season_cache["value"] = val
                return val
    except Exception as exc:
        log_err(f"[ranked] season lookup failed: {exc}")
    _season_cache["at"] = now
    _season_cache["value"] = (1, None)
    return (1, None)

# ---------------------------------------------------------------------------
# Background loop (wired in main.py startup)
# ---------------------------------------------------------------------------

async def load_loop() -> None:
    """Refresh the room cache every REFRESH_S. Never exits/raises out."""
    try:
        n = await asyncio.to_thread(refresh_rooms)
        log_sys(f"[ranked] cache primed with {n} rooms")
    except Exception as exc:
        log_err(f"[ranked] prime failed: {exc}")
    while True:
        try:
            await asyncio.sleep(REFRESH_S)
            n = await asyncio.to_thread(refresh_rooms)
            log_sys(f"[ranked] cache refresh: {n} rooms")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log_err(f"[ranked] loop tick failed: {exc}")
