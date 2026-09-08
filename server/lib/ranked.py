"""
ranked.py — Ranked v2 (thin): tiers, static awards, direct-DB room lookup, seal.

Flow (one-way, no chatter):
  client POST /api/capture {records} -> engine.feed + processor writes
  -> seal at stG=3 drain -> response {packets, session, ranked}
  client renders optimistic (+8 kill / +50 win / -5 loss), server corrects.

Awards (kills + win only): kill +8 each, alive at stG=3 +50, dead -5.
No placement table, no scaling, no defeat branches, no caches, no seasons loop.
Supabase is the source of truth; every read hits the DB (2 queries per seal
max — negligible). Nothing here raises.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

from .logger import log_ranked

# v2.19.0 ranked sleep: set RANKED_ENABLED=0 to sleep. Code stays committed
# but no room lookup or seal can activate when disabled. Points reads
# still serve (arena display). Tests run enabled by default (unset = on).
# Never raises.
RANKED_ENABLED = os.getenv("RANKED_ENABLED", "1") == "1"


def ranked_sleeping() -> bool:
    """True when ranked is asleep (default in v2.19.0). Never raises."""
    try:
        return not RANKED_ENABLED
    except Exception:
        return True

# ---------------------------------------------------------------------------
# Tuning constants — flat awards (identical for every tier)
# ---------------------------------------------------------------------------

KILL_POINTS = 8    # per kill
WIN_POINTS = 50    # alive at stG=3
LOSS_POINTS = -5   # dead at stG=3 (flat: zone, unknown, or killed — no branches)

# Back-compat aliases (old names used by callers/tests).
STATIC_KILL = KILL_POINTS
STATIC_PLACEMENT = {1: 50, 2: 30, 3: 20, 4: 10, 5: 5}
STATIC_PLACEMENT_MIN = {1: 5, 2: 3, 3: 2, 4: 1, 5: 1}
STATIC_DEFEAT_LOWER = LOSS_POINTS
STATIC_DEFEAT_PEER = LOSS_POINTS
STATIC_DEFEAT_ZONE = LOSS_POINTS

POINTS_TTL_S = 60  # per-player points read cache (fast path; DB owns truth)

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
# Static rewards (flat — kills + win only)
# ---------------------------------------------------------------------------

def kill_reward() -> int:
    return KILL_POINTS


def win_reward(placement: Any = None) -> int:
    """Winner award: +50. Compat shim (placement ignored)."""
    return WIN_POINTS


def loss_penalty() -> int:
    return LOSS_POINTS


def placement_reward(placement: Any, players_count: Any = None,
                     avg_points: Any = None) -> int:
    """Compat shim: 1st pays WIN_POINTS, everything else 0 (kills+win only)."""
    try:
        return WIN_POINTS if int(placement) == 1 else 0
    except Exception:
        return 0


def defeat_penalty(victim_pts: Any, killer_pts: Any = None) -> int:
    """Compat shim: flat LOSS_POINTS regardless of killer."""
    return LOSS_POINTS


def elo_kill_reward(killer_pts: Any = None, victim_pts: Any = None,
                    placement: Any = None) -> int:
    return KILL_POINTS


# ---------------------------------------------------------------------------
# Room lookup (direct DB — no cache, no refresh loop)
# ---------------------------------------------------------------------------

def _norm_code(short_code: Any) -> str:
    try:
        return str(short_code or "").strip().upper()
    except Exception:
        return ""


def get_ranked_room(short_code: Any) -> dict[str, Any] | None:
    """Open (live preferred, then lobby) room for a 5-char code. None if not ranked."""
    if ranked_sleeping():
        return None
    code = _norm_code(short_code)
    if not code:
        return None
    sb = _sb()
    if not sb:
        return None
    try:
        for status in ("live", "lobby"):
            r = sb.table("ranked_rooms").select("*").eq("short_code", code).eq("status", status).limit(1).execute()
            if r.data:
                return r.data[0]
    except Exception as exc:
        log_ranked(f"[ranked] room lookup failed {code}: {exc}")
    return None


def room_roster_ids(room: dict[str, Any]) -> list[str]:
    """discord_id strings from a room's roster JSON. Never raises."""
    try:
        return [str(x.get("discord_id")) for x in (room.get("roster") or []) if x.get("discord_id")]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------

def get_ranked_points(discord_id: Any) -> int:
    """Points read (60s fast-path cache, DB owns truth). 0 on miss. Never raises."""
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


def _get_points_many(dids) -> dict[str, int]:
    """Bulk points read: cache hits + ONE IN query for misses. Never raises."""
    out: dict[str, int] = {}
    miss: list[str] = []
    now = time.time()
    for did in (dids or []):
        try:
            d = str(did or "").strip()
        except Exception:
            continue
        if not d or d in out:
            continue
        hit = _points_cache.get(d)
        if hit and now - hit[1] < POINTS_TTL_S:
            out[d] = hit[0]
        else:
            miss.append(d)
    if miss:
        try:
            sb = _sb()
            if sb:
                r = sb.table("ranked_points").select("discord_id,points").in_("discord_id", miss).execute()
                for row in (r.data or []):
                    try:
                        d = str(row.get("discord_id"))
                        pts = int(row.get("points") or 0)
                        _points_cache[d] = (pts, now)
                        out[d] = pts
                    except Exception:
                        continue
        except Exception as exc:
            log_ranked(f"[ranked] bulk points read failed: {exc}")
        for d in miss:
            if d not in out:
                out[d] = 0
                _points_cache[d] = (0, now)
    return out


def _bump_points_many(deltas: dict[str, int]) -> dict[str, int]:
    """Bulk award: ONE fresh read + ONE upsert. Never raises."""
    totals: dict[str, int] = {}
    try:
        clean = {str(did): int(sub or 0) for did, sub in (deltas or {}).items()
                 if str(did or "") and int(sub or 0)}
        if not clean:
            return totals
        sb = _sb()
        if not sb:
            raise RuntimeError("supabase not configured")
        cur = _get_points_many(list(clean))
        now = datetime.now(timezone.utc).isoformat()
        rows = [{"discord_id": d, "points": int(cur.get(d, 0)) + delta, "updated_at": now}
                for d, delta in clean.items()]
        sb.table("ranked_points").upsert(rows, on_conflict="discord_id").execute()
        ts = time.time()
        for row in rows:
            _points_cache[row["discord_id"]] = (row["points"], ts)
            totals[row["discord_id"]] = row["points"]
        return totals
    except Exception as exc:
        log_ranked(f"[ranked] bulk bump failed: {exc}")
    return totals


# ---------------------------------------------------------------------------
# Sealing — kills + win only
# ---------------------------------------------------------------------------

def seal_match(*, rid: str, match_version: int, gm: str,
               winner_discord_id: str | None,
               kills: dict[str, int] | None = None,
               placements: dict[str, int] | None = None,
               victim_points: dict[str, int] | None = None,
               kill_events: list | None = None,
               players_count: Any = None,
               death_killers: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Award flat points for one sealed match and persist results.

    kills*w8 + winner +50, everyone else involved -5 (flat loss).
    `winner_discord_id` None (total wipe, no fallback resolved) => all -5.
    Extra kwargs (placements, death_killers, victim_points, players_count)
    are accepted for caller compat and ignored. Idempotent: re-sealing an
    already-sealed (rid, match_version) returns ok without double-awarding.
    Never raises.
    """
    try:
        if ranked_sleeping():
            return {"ok": False, "error": "ranked sleeping"}
        if not rid or int(match_version or 0) <= 0:
            return {"ok": False, "error": "bad rid/match_version"}
        sb = _sb()
        if not sb:
            return {"ok": False, "error": "supabase not configured"}
        try:
            chk = sb.table("ranked_results").select("room_id").eq("room_id", rid).eq(
                "match_version", int(match_version)).limit(1).execute()
            if chk.data:
                return {"ok": True, "match_version": int(match_version), "duplicate": True}
        except Exception as exc:
            log_ranked(f"[ranked] seal check failed {rid} v{match_version}: {exc}")
            return {"ok": False, "error": "seal check failed"}
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
        involved = set(kc_map) | {str(d) for d in (placements or {})} | {str(d) for d in (death_killers or {})}
        winner = str(winner_discord_id) if winner_discord_id else None
        if winner:
            involved.add(winner)
        points_awarded = {did: kc_map.get(did, 0) * KILL_POINTS + (WIN_POINTS if did == winner else LOSS_POINTS)
                          for did in involved}
        totals = _bump_points_many(points_awarded)
        try:
            sb.table("ranked_results").upsert({
                "room_id": rid,
                "match_version": int(match_version),
                "gm": gm,
                "winner_discord_id": winner,
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
# Seasons (compat: static season 1 — table lookup deleted)
# ---------------------------------------------------------------------------

def get_current_season() -> tuple:
    """(season_number, reset_at). Static (1, None) — seasons table deleted."""
    return (1, None)
