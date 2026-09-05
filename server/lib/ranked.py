"""
ranked.py — Ranked v1 server-side: room cache, milestones, static rewards, sealing.

Contract (Phase 1):
  - `get_ranked_room(short_code)` -> room dict | None (in-memory, refreshed every 30s)
  - `get_ranked_points(discord_id)` -> int (60s per-player cache, 0 on miss)
  - `milestone_for(points)` / `next_milestone(points)` -> tier info
  - `kill_reward()` / `placement_reward(placement)` / `defeat_penalty(...)` -> int
  - `seal_match(...)` -> {"ok": True, ...} | {"ok": False, "error": ...} (Phase 2 calls this)

Notes:
  - Supabase is the source of truth; memory is only a read cache. A cold
    start just means an empty cache until the first refresh (never fatal).
  - Nothing here raises: cache misses and DB blips degrade to empty/0/false.
  - Static awards (same for every tier): kill +8, 1st +50, 2nd +30, 3rd +20,
    4th +10, 5th +5, 6th+ 0; defeat minus -15 (killed by lower-points player),
    -5 (equal-or-higher killer or zone/unknown death). Placements are capped
    at the lobby size (2 players => only 1st/2nd exist).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from .logger import log_ranked

# ---------------------------------------------------------------------------
# Tuning constants — static awards (identical for every milestone tier)
# ---------------------------------------------------------------------------

STATIC_KILL = 8              # per roster kill
STATIC_PLACEMENT = {         # placement award (6th+ = 0)
    1: 50, 2: 30, 3: 20, 4: 10, 5: 5,
}
STATIC_DEFEAT_LOWER = -15    # died to a lower-points player
STATIC_DEFEAT_PEER = -5      # died to an equal-or-higher-points player
STATIC_DEFEAT_ZONE = -5      # died with no killer (zone / unknown)

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
_MISS_TTL_S = 10               # negative read-through cache (bounds DB hits)
_miss_cache: dict[str, float] = {}  # code -> last miss ts


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
# Static rewards
# ---------------------------------------------------------------------------

def kill_reward() -> int:
    """Flat per-kill award. Never raises."""
    return STATIC_KILL


def placement_reward(placement: Any, players_count: Any = None) -> int:
    """Placement award, capped at the lobby size.

    2 players => only 1st/2nd can pay out (phantom 3rds pay 0).
    players_count None/0 disables the cap. Never raises.
    """
    try:
        pl = int(placement)
    except Exception:
        return 0
    try:
        cap = int(players_count or 0)
    except Exception:
        cap = 0
    if cap > 0 and pl > cap:
        return 0
    return int(STATIC_PLACEMENT.get(pl, 0))


def defeat_penalty(victim_pts: Any, killer_pts: Any = None) -> int:
    """Defeat minus for a dead player.

    killer None (zone/unknown) => STATIC_DEFEAT_ZONE; killer with fewer
    points than the victim => STATIC_DEFEAT_LOWER; otherwise PEER.
    Never raises.
    """
    try:
        if killer_pts is None:
            return STATIC_DEFEAT_ZONE
        return STATIC_DEFEAT_LOWER if int(killer_pts) < int(victim_pts or 0) else STATIC_DEFEAT_PEER
    except Exception:
        return STATIC_DEFEAT_ZONE


def elo_kill_reward(killer_pts: Any = None, victim_pts: Any = None,
                    placement: Any = None) -> int:
    """Back-compat shim: static kill value regardless of ratings."""
    return STATIC_KILL


def win_reward(placement: Any = None) -> int:
    """Back-compat shim: winner (1st-place) award."""
    try:
        return STATIC_PLACEMENT[1] if placement is None or int(placement) == 1 else 0
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
        log_ranked(f"[ranked] refresh failed (keeping {len(_rooms)} cached): {exc}")
        return len(_rooms)


def get_ranked_room(short_code: Any) -> dict[str, Any] | None:
    """Fast in-memory lookup by 5-char code. None when not a ranked room."""
    code = _norm_code(short_code)
    if not code:
        return None
    return _rooms.get(code)


def get_ranked_room_fresh(short_code: Any) -> dict[str, Any] | None:
    """Cache lookup with a DB read-through on miss.

    The 30s room-cache refresh leaves newly created rooms invisible to
    /api/stats (while /api/ranked/by-code reads the DB directly and sees
    them) — a 30s+ BLOCK gap on every room creation. On miss this queries
    Supabase once, caches positives in _rooms (the refresh loop owns them
    afterwards) and negatives for _MISS_TTL_S. Never raises.
    """
    room = get_ranked_room(short_code)
    if room is not None:
        return room
    code = _norm_code(short_code)
    if not code:
        return None
    now = time.time()
    try:
        if now - _miss_cache.get(code, 0) < _MISS_TTL_S:
            return None
        _miss_cache[code] = now
    except Exception:
        pass
    sb = _sb()
    if not sb:
        return None
    try:
        for status in ("live", "lobby"):  # matches first (seal path is hot)
            r = sb.table("ranked_rooms").select("*").eq("short_code", code).eq("status", status).limit(1).execute()
            rows = r.data or []
            if rows:
                _rooms[code] = rows[0]
                return rows[0]
    except Exception as exc:
        log_ranked(f"[ranked] fresh lookup failed {code}: {exc}")
    return None


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
               kill_events: list | None = None,
               players_count: Any = None,
               death_killers: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Award static points for one sealed match and persist results.

    kills: discord_id -> kill count (roster only, host-harvested).
    placements: discord_id -> placement (1-based, from host alv).
    victim_points: optional discord_id -> points snapshot override.
    kill_events: optional [(killer_did, victim_did)] for exact per-kill
        counts. When given, it replaces the kills loop; `kills` is still
        stored in the results row for review.
    players_count: roster/lobby size — placements above it pay 0.
    death_killers: victim did -> killer did | None (None = zone/unknown).
        A listed victim takes the defeat minus even with zero kills.
    Per player: kills * STATIC_KILL + capped placement award + defeat
    minus (when dead). Winner missing from placements still gets 1st.
    Returns {"ok": True, "points": {...}, "match_version": N} or {"ok": False, "error": ...}.
    Never raises.

    NOTE: points bumps are additive — callers must pre-check that
    (rid, match_version) isn't already sealed, or points double-award
    (the results-row upsert alone does not prevent that).
    """
    try:
        if not rid or int(match_version or 0) <= 0:
            return {"ok": False, "error": "bad rid/match_version"}
        # per-kill counts: exact events win over the kills dict
        kc_map: dict[str, int] = {}
        if kill_events:
            for pair in kill_events:
                try:
                    kdid = str(pair[0])
                except Exception:
                    continue
                kc_map[kdid] = kc_map.get(kdid, 0) + 1
        else:
            for did, kc in (kills or {}).items():
                try:
                    kc_map[str(did)] = max(0, int(kc or 0))
                except Exception:
                    continue
        deaths = dict(death_killers or {})
        involved = set(kc_map) | {str(d) for d in (placements or {})} | {str(d) for d in deaths}
        if winner_discord_id:
            involved.add(str(winner_discord_id))
        # pre-award snapshot (defeat minus compares killer vs victim)
        snap: dict[str, int] = {}
        for did in involved:
            try:
                snap[did] = int((victim_points or {}).get(did, get_ranked_points(did)))
            except Exception:
                snap[did] = 0
        points_awarded: dict[str, int] = {}
        totals: dict[str, int] = {}
        for did in involved:
            try:
                sub = kc_map.get(did, 0) * STATIC_KILL
                pl = (placements or {}).get(did)
                if pl is not None:
                    sub += placement_reward(pl, players_count)
                if did in deaths:
                    kdid = deaths.get(did)
                    try:
                        kpts = snap.get(str(kdid)) if kdid else None
                    except Exception:
                        kpts = None
                    sub += defeat_penalty(snap.get(did, 0), kpts)
                if winner_discord_id and did == str(winner_discord_id):
                    try:
                        wpl = int((placements or {}).get(did, 99))
                    except Exception:
                        wpl = 99
                    if wpl != 1:
                        sub += placement_reward(1, players_count)
                if sub:
                    totals[did] = _bump_points(did, sub)
                points_awarded[did] = sub
            except Exception as exc:
                log_ranked(f"[ranked] award failed for {did} in {rid}: {exc}")
                points_awarded[str(did)] = 0
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
            log_ranked(f"[ranked] results write failed for {rid} v{match_version}: {exc}")
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
        log_ranked(f"[ranked] season lookup failed: {exc}")
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
        log_ranked(f"[ranked] cache primed with {n} rooms")
    except Exception as exc:
        log_ranked(f"[ranked] prime failed: {exc}")
    while True:
        try:
            await asyncio.sleep(REFRESH_S)
            n = await asyncio.to_thread(refresh_rooms)
            log_ranked(f"[ranked] cache refresh: {n} rooms")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log_ranked(f"[ranked] loop tick failed: {exc}")
