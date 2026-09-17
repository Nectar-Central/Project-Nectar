"""
ranked.py — Ranked v2 (thin): tiers, static awards, direct-DB room lookup, seal.

Flow (one-way, no chatter):
  client POST /api/capture {records} -> engine.feed + processor writes
  -> seal at stG=3 drain -> response {packets, session, ranked}
  client renders optimistic (+8 kill / +50 win / -5 loss), server corrects.

Scoring (comp math, ranked economy): each seal banks comp-style
kill/alv/leave rows (bngc_ranked_events) and scores them with
compute_match_points (kill +8, win +50, no placement purse); every
involved non-winner still takes the flat -5 loss. Placements derive
from reverse alv order with the snapshot winner cohered to P1.
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
# Dynamic awards are deliberately switchable while the rollout is observed.
# Dynamic awards are the live economy: per-kill upset values with taper,
# lobby-scaled placement/win purses, tier-scaled losses — all frozen per
# seal. Set RANKED_DYNAMIC=0 to fall back to the legacy lobby-average
# purse (kill/win scaled by lobby avg, flat -5 loss).
RANKED_DYNAMIC = os.getenv("RANKED_DYNAMIC", "1") == "1"


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

# ---------------------------------------------------------------------------
# Play points — activity ledger on bngc_users (all modes, not just ranked).
# Unlike the ranked ledger above, defeats cost nothing here: play points
# only ever go up. Hourly pay is derived from the cumulative playtime
# counter (floor(total/3600) - hours_already_paid), so batch splits and
# replays can never double-pay. Never raises.
# ---------------------------------------------------------------------------

PLAY_KILL = 8     # per kill (mirrors ranked kill value)
PLAY_KNOCK = 2    # per knock (finishing blow pays the kill on top)
PLAY_WIN = 50     # per win (mirrors ranked win value)
PLAY_HOUR = 20    # per hour of tracked playtime


def hourly_play_points(playtime_global: int, hours_paid: int) -> tuple[int, int]:
    """Owed hourly pay + new paid checkpoint. Pure, unit-tested.

    Returns (owed_points, new_hours_paid). owed is 0 when no full unpaid
    hour exists yet. new_hours_paid only advances (never rewound).
    """
    try:
        total_h = int(playtime_global or 0) // 3600
        paid = int(hours_paid or 0)
    except Exception:
        return 0, int(hours_paid or 0)
    if total_h <= paid:
        return 0, paid
    return (total_h - paid) * PLAY_HOUR, total_h


def play_points_for(kills: int, wins: int, knocks: int, playtime_s: int) -> int:
    """Full backfill value for one user. Pure, unit-tested."""
    try:
        k = int(kills or 0)
        w = int(wins or 0)
        n = int(knocks or 0)
        h = int(playtime_s or 0) // 3600
    except Exception:
        return 0
    return max(0, k * PLAY_KILL + w * PLAY_WIN + n * PLAY_KNOCK + h * PLAY_HOUR)

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
# Banked-evidence detection (comp-style rows -> kills/placements)
# ---------------------------------------------------------------------------

def detect_ranked_kills(events: list | None, roster: Any = None,
                        skip: Any = None, name2did: dict | None = None
                        ) -> tuple[dict[str, int], list]:
    """Kills from banked rows. Pure, unit-tested.

    Counts `kill` rows per mapped killer did (roster only, skip excluded).
    Returns (kills_count {did: n}, kill_events [(kdid, vdid)]) where vdid
    resolves via name2did and falls back to kdid (same convention as the
    snapshot path). Unmapped/non-roster killers ignored. Never raises.
    """
    kills: dict[str, int] = {}
    pairs: list = []
    try:
        rset = {str(d) for d in (roster or []) if str(d or "").strip()}
    except Exception:
        rset = set()
    try:
        xset = {str(d) for d in (skip or []) if str(d or "").strip()}
    except Exception:
        xset = set()
    try:
        n2d = dict(name2did or {})
    except Exception:
        n2d = {}
    try:
        for e in (events or []):
            try:
                if (e or {}).get("etype") != "kill":
                    continue
                kdid = str((e or {}).get("discord_id") or "").strip()
                if not kdid or kdid in xset or (rset and kdid not in rset):
                    continue
                kills[kdid] = kills.get(kdid, 0) + 1
                try:
                    vdid = str(n2d.get(str((e or {}).get("target_name") or "")) or "")
                except Exception:
                    vdid = ""
                pairs.append((kdid, vdid or kdid))
            except Exception:
                continue
    except Exception:
        pass
    return kills, pairs


def detect_ranked_placements(events: list | None, winner_discord_id: Any = None,
                             roster: Any = None, skip: Any = None) -> dict[str, int]:
    """Placements from banked alv rows. Pure, unit-tested.

    Reverse alv order over mapped roster dids (skip excluded), with the
    snapshot-decided winner forced to P1 so the board always agrees with
    the crown (double-KO: most-recent death wins, not the last banked row).
    Unmapped/non-roster rows ignored. Never raises.
    """
    try:
        rset = {str(d) for d in (roster or []) if str(d or "").strip()}
    except Exception:
        rset = set()
    try:
        xset = {str(d) for d in (skip or []) if str(d or "").strip()}
    except Exception:
        xset = set()
    order: list[str] = []
    try:
        for e in reversed(list(events or [])):
            try:
                if (e or {}).get("etype") != "alv":
                    continue
                did = str((e or {}).get("discord_id") or "").strip()
                if not did or did in xset or (rset and did not in rset):
                    continue
                if did not in order:
                    order.append(did)
            except Exception:
                continue
    except Exception:
        pass
    try:
        w = str(winner_discord_id or "").strip()
    except Exception:
        w = ""
    if w and w in order:
        order.remove(w)
        order.insert(0, w)
    elif w and (not rset or w in rset) and w not in xset:
        order.insert(0, w)
    return {did: i + 1 for i, did in enumerate(order)}


# ---------------------------------------------------------------------------
# Room lookup (direct DB — no cache, no refresh loop)
# ---------------------------------------------------------------------------

def _norm_code(short_code: Any) -> str:
    try:
        return str(short_code or "").strip().upper()
    except Exception:
        return ""


_ROOM_CACHE: dict[str, tuple[float, Any]] = {}
_ROOM_TTL_S = 30.0
_ROOM_COLS = "id,short_code,status,host_discord_id,match_version,max_matches,roster,updated_at"


def _room_cache_bypassed() -> bool:
    try:
        import os as _os
        import sys as _sys
        if _os.getenv("PYTEST_CURRENT_TEST"):
            return True
        return "pytest" in _sys.modules
    except Exception:
        return False


def get_ranked_room(short_code: Any) -> dict[str, Any] | None:
    """Open (live preferred, then lobby) room for a 5-char code. None if not ranked.

    Cached 30s per code: capture + stats poll this every few seconds per
    player; each miss was 1-2 Supabase RTs of Service-Initiated egress.
    """
    if ranked_sleeping():
        return None
    code = _norm_code(short_code)
    if not code:
        return None
    try:
        if not _room_cache_bypassed():
            hit = _ROOM_CACHE.get(code)
            if hit and time.time() < hit[0]:
                return hit[1]
    except Exception:
        pass
    sb = _sb()
    if not sb:
        return None
    try:
        for status in ("live", "lobby"):
            r = sb.table("ranked_rooms").select(_ROOM_COLS).eq("short_code", code).eq("status", status).limit(1).execute()
            if r.data:
                try:
                    if len(_ROOM_CACHE) > 500:
                        _ROOM_CACHE.clear()
                    _ROOM_CACHE[code] = (time.time() + _ROOM_TTL_S, r.data[0])
                except Exception:
                    pass
                return r.data[0]
        try:
            if len(_ROOM_CACHE) > 500:
                _ROOM_CACHE.clear()
            _ROOM_CACHE[code] = (time.time() + _ROOM_TTL_S, None)
        except Exception:
            pass
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


def _get_points_many(dids, *, fresh: bool = False) -> dict[str, int]:
    """Bulk points read, optionally bypassing the short lived display cache.

    Seal-time economy uses ``fresh=True`` so the rating snapshot that chooses
    a kill award is not silently based on a 60 second old overlay read.
    """
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
        if not fresh and hit and now - hit[1] < POINTS_TTL_S:
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
# Sealing — comp math (kills + win + placements) with the ranked economy
# ---------------------------------------------------------------------------

# Base static table (avg-0 lobby): kill +8 / win +50 mirror the legacy flat
# awards. Seals score a lobby-strength-scaled copy (scaled_table_for); the
# -5 non-winner loss stays flat and is applied as a post-step.
RANKED_POINTS_TABLE = {"kill": KILL_POINTS, "win": WIN_POINTS,
                       "placement": [10, 7, 5, 3, 1]}
_BASE_PURSE = [10, 7, 5, 3, 1]


def _bounded(value: Any, low: int, high: int) -> int:
    """Round and clamp a score. Kept tiny so the economy functions are pure."""
    try:
        return max(int(low), min(int(high), int(round(float(value)))))
    except Exception:
        return int(low)


def kill_delta(killer_pts: Any, victim_pts: Any, avg_pts: Any = 0) -> int:
    """Bounded Elo-ish award for one elimination (4..18).

    The logistic term is the *upset* chance: beating a stronger victim pays
    toward 12, while farming a much lower-rated victim trends to the 4 point
    floor.  Lobby strength adds at most 15% before the hard cap.
    """
    try:
        import math
        killer = float(killer_pts or 0)
        victim = float(victim_pts or 0)
        avg = max(0.0, float(avg_pts or 0))
        upset = 1.0 / (1.0 + math.pow(10.0, (killer - victim) / 600.0))
        return _bounded(KILL_POINTS * (0.5 + upset + 0.15 * avg / 4000.0), 4, 18)
    except Exception:
        return KILL_POINTS


def placement_delta(place: Any, avg_pts: Any = 0, players: Any = 0) -> int:
    """Bounded placement purse for places 1..5 (6..22; 0 outside it)."""
    try:
        p = int(place)
        if p < 1 or p > len(_BASE_PURSE):
            return 0
        avg = max(0.0, float(avg_pts or 0))
        n = max(0, int(players or 0))
        return _bounded(_BASE_PURSE[p - 1] * (1.0 + avg / 5000.0 + 0.04 * n), 6, 22)
    except Exception:
        return 0


def win_delta(avg_pts: Any = 0, players: Any = 0) -> int:
    """Bounded victory award (35..95), based on lobby quality and size."""
    try:
        avg = max(0.0, float(avg_pts or 0))
        n = max(0, int(players or 0))
        return _bounded(WIN_POINTS * (1.0 + avg / 4000.0 + 0.03 * n), 35, 95)
    except Exception:
        return WIN_POINTS


# Kill taper: the Nth kill by one player in one match pays a fraction of
# its upset award. Placement decides matches; farming one lobby does not.
KILL_TAPER = (1.0, 1.0, 0.8, 0.65, 0.5)  # 6th+ kills pay half


def taper_for(nth: Any) -> float:
    """Taper factor for the Nth kill (1-indexed). Pure, unit-tested."""
    try:
        n = int(nth)
    except Exception:
        return 1.0
    if n < 1:
        return 1.0
    return KILL_TAPER[n - 1] if n <= len(KILL_TAPER) else KILL_TAPER[-1]


def tier_index_for(points: Any) -> int:
    """0..9 position in MILESTONE_TIERS for a pre-seal total. Pure."""
    try:
        p = float(points or 0)
    except Exception:
        return 0
    idx = 0
    try:
        for i, t in enumerate(MILESTONE_TIERS):
            try:
                if p >= float((t or {}).get("min", 0)):
                    idx = i
            except Exception:
                continue
    except Exception:
        pass
    return idx


def loss_for(points: Any) -> int:
    """Tier-scaled loss for a non-winner, from the PRE-seal total.

    -(5 + 3*tier): Unranked -5, Bronze -8, Silver -11, Gold -14,
    Platinum -17, Diamond -20, Emerald -23, Champion -26, Master -29,
    Grandmaster -32. Climbing raises the stakes; the floor never moves.
    Pure, unit-tested.
    """
    try:
        return -(5 + 3 * tier_index_for(points))
    except Exception:
        return LOSS_POINTS


def dynamic_table_for(avg_points: Any, players_count: Any,
                      kill_awards: list[dict] | None = None,
                      loss_awards: dict | None = None) -> dict:
    """Frozen dynamic scoring metadata for one ranked match.

    ``kill_awards`` records every exact per-kill amount; ``loss_awards``
    maps each involved did to its tier-scaled seal-time loss. Both are
    intentionally stored in ``points_table``: a later rating change must
    never alter a historic seal or a host-triggered recalculation.
    """
    try:
        avg = max(0.0, float(avg_points or 0))
    except Exception:
        avg = 0.0
    try:
        players = max(0, int(players_count or 0))
    except Exception:
        players = 0
    try:
        frozen_loss = {str(k): int(v) for k, v in (loss_awards or {}).items()
                       if str(k or "").strip()}
    except Exception:
        frozen_loss = {}
    return {
        "mode": "dynamic-v1",
        "avg_points": round(avg, 1),
        "players": players,
        "kill": None,
        "win": win_delta(avg, players),
        "placement": [placement_delta(i, avg, players) for i in range(1, 6)],
        "kill_awards": list(kill_awards or []),
        "loss_awards": frozen_loss,
    }


def scaled_table_for(avg_points: Any) -> dict:
    """Static table scaled by lobby strength. Pure, unit-tested.

    factor = 1 + max(0, avg)/4000: an avg-0 lobby pays exactly the legacy
    8/50; a 4000-avg (Champion) lobby pays double; Grandmaster ~2.9x.
    Loss is NOT scaled (flat -5 consolation, applied downstream).
    """
    try:
        avg = max(0.0, float(avg_points or 0))
    except Exception:
        avg = 0.0
    f = 1.0 + avg / 4000.0
    return {"kill": max(1, round(KILL_POINTS * f)),
            "win": max(1, round(WIN_POINTS * f)),
            "placement": [max(0, round(p * f)) for p in _BASE_PURSE]}


def table_of_result(row: dict | None) -> dict:
    """Scoring table for a sealed row: the stored seal-time table, else the
    legacy flat table (8/50, no purse) so pre-purse seals recompute exactly
    what was awarded. Pure."""
    try:
        t = (row or {}).get("points_table")
        if isinstance(t, dict) and t.get("mode") == "dynamic-v1":
            try:
                frozen_loss = {str(k): int(v) for k, v in
                               ((t or {}).get("loss_awards") or {}).items()
                               if str(k or "").strip()}
            except Exception:
                frozen_loss = {}
            return {"mode": "dynamic-v1",
                    "avg_points": float(t.get("avg_points") or 0),
                    "players": int(t.get("players") or 0),
                    "kill": None,
                    "win": int(t.get("win", WIN_POINTS)),
                    "placement": [int(x) for x in (t.get("placement") or [])],
                    "kill_awards": list(t.get("kill_awards") or []),
                    "loss_awards": frozen_loss}
        if isinstance(t, dict) and t.get("kill") is not None:
            return {"kill": int(t.get("kill", KILL_POINTS)),
                    "win": int(t.get("win", WIN_POINTS)),
                    "placement": [int(x) for x in (t.get("placement") or [])]}
    except Exception:
        pass
    return {"kill": KILL_POINTS, "win": WIN_POINTS, "placement": []}


def lobby_avg_points_sync(dids, *, fresh: bool = False) -> float:
    """Mean ranked points over a lobby.

    The ordinary overlay path can use the short display cache. Dynamic
    previews and seals request ``fresh=True`` so the advertised/awarded
    economy is based on the current rating snapshot.
    """
    try:
        pts = _get_points_many([str(d) for d in (dids or []) if str(d or "").strip()],
                                fresh=fresh)
        vals = [int(v or 0) for v in (pts or {}).values()]
        return round(sum(vals) / len(vals), 1) if vals else 0.0
    except Exception:
        return 0.0


def _kill_awards_for(kill_events: Any, points: dict[str, int], avg_points: Any) -> list[dict]:
    """Normalize pairs into immutable per-kill award rows. Never raises.

    Each row carries its taper position (nth kill by that killer this
    match): later kills pay a tapered fraction of the upset award, floored
    at 1 so every kill still pays something.
    """
    out: list[dict] = []
    counts: dict[str, int] = {}
    try:
        for pair in (kill_events or []):
            try:
                killer = str(pair[0] or "").strip()
                victim = str(pair[1] or killer).strip() or killer
                if not killer:
                    continue
                n = int(counts.get(killer, 0)) + 1
                counts[killer] = n
                base = kill_delta(points.get(killer, 0),
                                  points.get(victim, 0), avg_points)
                out.append({"killer": killer, "victim": victim, "nth": n,
                            "points": max(1, int(round(base * taper_for(n))))})
            except Exception:
                continue
    except Exception:
        pass
    return out


def _game_names_by_discord_sync(dids: Any) -> dict[str, str]:
    """Best-effort game-name -> discord-id map for live killfeed previews.

    Banked kill rows identify the killer but retain the victim as a game
    name.  This resolver lets a live dynamic board use the victim's rating
    when that name belongs to someone in the current roster.  Ambiguous or
    unavailable mappings simply fall back to the conservative killer rating.
    """
    out: dict[str, str] = {}
    try:
        ids = sorted({str(d).strip() for d in (dids or []) if str(d or "").strip()})
        if not ids:
            return out
        sb = _sb()
        if not sb:
            return out
        rows = sb.table("bngc_users").select("discord_id,game_username").in_(
            "discord_id", ids[:200]).execute().data or []
        for row in rows:
            try:
                name = str((row or {}).get("game_username") or "").strip()
                did = str((row or {}).get("discord_id") or "").strip()
                if name and did:
                    out.setdefault(name, did)
                    out.setdefault(name.casefold(), did)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _event_kill_pairs(events: Any, name2did: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Extract exact-or-best-known ``(killer_did, victim_did)`` pairs."""
    out: list[tuple[str, str]] = []
    lookup = name2did or {}
    try:
        for event in (events or []):
            try:
                if (event or {}).get("etype") != "kill":
                    continue
                killer = str((event or {}).get("discord_id") or "").strip()
                if not killer:
                    continue
                target = str((event or {}).get("target_name") or "").strip()
                victim = lookup.get(target) or lookup.get(target.casefold()) or killer
                out.append((killer, str(victim)))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _ranked_rows(events: list, table: dict, winner: str | None,
                 placements: dict | None = None,
                 kill_awards: list[dict] | None = None,
                 excluded: set[str] | list[str] | None = None,
                 point_awards: dict | None = None) -> list[dict]:
    """Dynamic economy rows (thin alias: the math lives in
    lib/scoring.dynamic_rows, shared with the static strategy)."""
    try:
        from server.lib.scoring import dynamic_rows as _dynamic_rows
        return _dynamic_rows(events, table, winner, placements,
                             kill_awards, excluded, point_awards,
                             default_win=WIN_POINTS)
    except Exception:
        return []


def _read_ranked_events(sb, rid: str, match_version: int, limit: int = 500) -> list:
    """Banked comp-style rows for one seal. [] on any failure. Never raises."""
    try:
        r = sb.table("bngc_ranked_events").select(
            "etype,discord_id,actor_name,target_name,alive").eq(
            "room_id", str(rid)).eq(
            "match_version", int(match_version)).limit(int(limit)).execute()
        return [dict(e) for e in (r.data or [])]
    except Exception:
        return []


def live_match_board_sync(rid: str, match_version: int, top_n: int = 5,
                          roster: Any = None) -> dict:
    """Live board for stats-poll freshness (comp_tourney-style). Sync for
    db_io. Returns {sealed, winner_discord_id, board:[top rows]}.

    Sealed matches score with the stored seal-time table (legacy flat
    fallback) and keep the -5 non-winner loss; open matches scale by the
    current lobby avg with no loss (same semantics as the coming seal).
    Empty board when nothing is banked yet. Never raises.
    """
    out: dict = {"sealed": False, "winner_discord_id": None, "board": []}
    try:
        mver = int(match_version or 0)
        if not rid or mver <= 0:
            return out
        sb = _sb()
        if not sb:
            return out
        sealed = None
        try:
            try:
                r = sb.table("ranked_results").select(
                    "winner_discord_id,placements,points_table").eq("room_id", str(rid)).eq(
                    "match_version", mver).limit(1).execute()
            except Exception as exc:
                # pre-migration (no points_table column): read classic shape
                if "points_table" not in str(exc) and "42703" not in str(exc):
                    raise
                r = sb.table("ranked_results").select(
                    "winner_discord_id,placements").eq("room_id", str(rid)).eq(
                    "match_version", mver).limit(1).execute()
            rows = r.data or []
            sealed = dict(rows[0]) if rows else None
        except Exception:
            sealed = None
        events = _read_ranked_events(sb, str(rid), mver, 1000)
        if not events:
            return out
        winner = (sealed or {}).get("winner_discord_id") if sealed else None
        pmap = dict((sealed or {}).get("placements") or {}) if sealed else None
        try:
            if sealed:
                table = table_of_result(sealed)
            elif RANKED_DYNAMIC:
                rids = [str(d) for d in (roster or []) if str(d or "").strip()]
                avg = lobby_avg_points_sync(rids, fresh=True)
                table = dynamic_table_for(avg, len(rids))
            else:
                table = scaled_table_for(lobby_avg_points_sync(roster))
        except Exception:
            table = {"kill": KILL_POINTS, "win": WIN_POINTS, "placement": []}
        try:
            if table.get("mode") == "dynamic-v1":
                awards = table.get("kill_awards") or []
                if not sealed:
                    pairs = _event_kill_pairs(events, _game_names_by_discord_sync(rids))
                    awards = _kill_awards_for(pairs, _get_points_many(rids, fresh=True),
                                               table.get("avg_points"))
                rows = _ranked_rows(events, table, winner, pmap, awards)
            else:
                from server.lib.scoring import compute_match_points as _cmp
                rows = _cmp(events, table, winner, pmap)
        except Exception:
            return out
        board = []
        try:
            _frozen_loss = {}
            try:
                if sealed:
                    _frozen_loss = {str(k): int(v) for k, v in
                                    ((table or {}).get("loss_awards") or {}).items()}
            except Exception:
                _frozen_loss = {}
            for r in rows[:max(1, int(top_n or 5))]:
                try:
                    did = str(r.get("discord_id"))
                    pts = int(r.get("points") or 0)
                    if sealed and not r.get("won"):
                        pts += int(_frozen_loss.get(did, LOSS_POINTS))
                    board.append({"discord_id": did, "kills": int(r.get("kills") or 0),
                                  "placement": r.get("placement"),
                                  "won": bool(r.get("won")), "points": pts,
                                  "name": r.get("name") or did[:6]})
                except Exception:
                    continue
        except Exception:
            board = []
        out["sealed"] = sealed is not None
        out["winner_discord_id"] = winner
        out["board"] = board
        return out
    except Exception:
        return out

def seal_match(*, rid: str, match_version: int, gm: str,
               winner_discord_id: str | None,
               kills: dict[str, int] | None = None,
               placements: dict[str, int] | None = None,
               victim_points: dict[str, int] | None = None,
               kill_events: list | None = None,
               players_count: Any = None,
               player_ids: Any = None,
               death_killers: dict[str, str | None] | None = None,
               removed_discord_ids: set[str] | list[str] | None = None,
               point_awards: dict | None = None,
               avg_points: Any = None) -> dict[str, Any]:
    """Score one sealed match with comp math and persist results.

    Events come from the banked bngc_ranked_events rows when present (kill /
    placement / win detection off evidence); otherwise they are synthesized
    from the legacy inputs so direct callers seal exactly as before.
    Scoring: compute_match_points with the lobby-strength static table
    (scaled_table_for(avg_points): kill/win/placement purse scaled by lobby
    avg, 8/50/[10,7,5,3,1] at avg 0), then the loss for every involved
    non-winner — flat -5 static, tier-scaled and frozen per did under
    dynamic (loss_awards in the stored table). The table used is stored on
    the results row so boards/recalc reproduce the seal-time economics
    (pre-migration DBs skip the column and keep sealing).
    `winner_discord_id` None (total wipe, no fallback resolved) => all lose.
    Idempotent: re-sealing an already-sealed (rid, match_version) returns ok
    without double-awarding. Never raises.
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
        try:
            removed = {str(d).strip() for d in (removed_discord_ids or []) if str(d or "").strip()}
        except Exception:
            removed = set()
        involved = set(kc_map) | {str(d) for d in (placements or {})} | {str(d) for d in (death_killers or {})}
        involved -= removed
        winner = str(winner_discord_id) if winner_discord_id else None
        if winner in removed:
            winner = None
        if winner:
            involved.add(winner)
        try:
            banked = _read_ranked_events(sb, rid, int(match_version))
        except Exception:
            banked = []
        try:
            if banked:
                events = banked
                pmap = {str(k): int(v) for k, v in (placements or {}).items()}
            else:
                # legacy synthesis: kill rows from counts + alv rows for the
                # attributed dead so the comp universe matches `involved`.
                events = ([{"etype": "kill", "discord_id": did,
                             "actor_name": None, "alive": None}
                           for did, n in kc_map.items() for _ in range(max(0, int(n or 0)))]
                          + [{"etype": "alv", "discord_id": str(d),
                              "actor_name": None, "alive": False}
                             for d in (death_killers or {})])
                pmap = {str(k): int(v) for k, v in (placements or {}).items()}
            from server.lib.scoring import compute_match_points as _cmp
            # Keep Comp's winner/placement semantics as the engine. Dynamic
            # ranked kills are injected below as exact per-event awards.
        except Exception:
            events, pmap = [], {}
        # Seal-time loss map (dynamic branch fills it; static/legacy keeps
        # {} so every lookup below falls back to flat LOSS_POINTS).
        _loss_map: dict = {}
        try:
            pair_rows = [(str(p[0]), str(p[1])) for p in (kill_events or [])
                         if len(p) >= 2 and str(p[0]) not in removed]
            if not pair_rows:
                pair_rows = [(str(e.get("discord_id")), str(e.get("discord_id")))
                             for e in events if e.get("etype") == "kill" and e.get("discord_id")]
            involved.update(str((e or {}).get("discord_id")) for e in events
                            if (e or {}).get("discord_id"))
            try:
                lobby_ids = {str(d).strip() for d in (player_ids or []) if str(d or "").strip()}
            except Exception:
                lobby_ids = set()
            players = int(players_count or 0)
            if players <= 0:
                players = len(lobby_ids or involved)
            if RANKED_DYNAMIC:
                # Read all participants, including victims who may not be in
                # a placement row, in one uncached query at the seal boundary.
                rating_ids = set(involved) | lobby_ids
                for pair in pair_rows:
                    try:
                        rating_ids.update((str(pair[0]), str(pair[1])))
                    except Exception:
                        continue
                rating_points = _get_points_many(rating_ids, fresh=True)
                avg_ids = lobby_ids or involved
                fresh_avg = (round(sum(rating_points.get(d, 0) for d in avg_ids) / len(avg_ids), 1)
                             if avg_ids else float(avg_points or 0))
                awards = _kill_awards_for(pair_rows, rating_points, fresh_avg)
                # Tier-scaled seal-time losses, frozen with the table so
                # recalc and boards replay them exactly. Stored for every
                # involved did (winner included: a later crown swap must
                # find the dethroned winner's loss here).
                try:
                    _loss_map = {str(d): loss_for(rating_points.get(str(d), 0))
                                 for d in involved}
                except Exception:
                    _loss_map = {}
                table = dynamic_table_for(fresh_avg, players, awards, _loss_map)
                comp_rows = _ranked_rows(events, table, winner, pmap or None, awards,
                                         removed, point_awards)
                comp_by = {str(r.get("discord_id")): int(r.get("points") or 0)
                           for r in comp_rows}
            else:
                table = scaled_table_for(avg_points)
                comp_rows = _cmp(events, dict(table), winner, pmap or None,
                                 removed, point_awards)
                comp_by = {str(r.get("discord_id")): int(r.get("points") or 0)
                           for r in comp_rows}
        except Exception:
            table = dict(RANKED_POINTS_TABLE)
            comp_by = {}
            _loss_map = {}
        # Frozen seal-time losses (dynamic) or flat -5 (static/legacy).
        _loss_get = lambda did: int(_loss_map.get(str(did), LOSS_POINTS))
        points_awarded = {}
        for did in involved:
            try:
                if did in comp_by:
                    pts = comp_by[did] + (0 if did == winner else _loss_get(did))
                else:
                    pts = kc_map.get(did, 0) * KILL_POINTS + (
                        WIN_POINTS if did == winner else _loss_get(did))
                points_awarded[did] = pts
            except Exception:
                continue
        totals = _bump_points_many(points_awarded)
        _row = {
            "room_id": rid,
            "match_version": int(match_version),
            "gm": gm,
            "winner_discord_id": winner,
            "kills": kc_map,
            "points": points_awarded,
            "placements": {str(k): int(v) for k, v in (placements or {}).items()
                           if str(k) not in removed},
            "points_table": table,
        }
        try:
            sb.table("ranked_results").upsert(_row, on_conflict="room_id,match_version").execute()
        except Exception as exc:
            # pre-migration DBs (no points_table column): keep sealing with
            # the classic row shape instead of failing the seal.
            if "points_table" in str(exc) or "42703" in str(exc):
                try:
                    _row.pop("points_table", None)
                    sb.table("ranked_results").upsert(
                        _row, on_conflict="room_id,match_version").execute()
                except Exception as exc2:
                    log_ranked(f"[ranked] results write failed for {rid} v{match_version}: {exc2}")
                    return {"ok": False, "error": "results write failed"}
            else:
                log_ranked(f"[ranked] results write failed for {rid} v{match_version}: {exc}")
                return {"ok": False, "error": "results write failed"}
        # v245 mirror.  Failure is non-fatal while deployments apply the
        # migration; ranked_results remains the compatibility ledger.
        try:
            sb.table("bngc_ranked_matches").upsert({
                "room_id": rid, "match_version": int(match_version),
                "status": "sealed", "winner_discord_id": winner,
                # ``point_awards`` is deliberately the host's *manual*
                # per-player adjustment map, not the final result map.
                # The latter stays in ranked_results.points.  Keeping the
                # two separate makes a later Recalc deterministic instead of
                # adding the whole result a second time.
                "placements": _row["placements"], "point_awards": {
                    str(k): int(v) for k, v in (point_awards or {}).items()
                    if str(k or "").strip() and int(v or 0)
                },
                "removed_discord_ids": sorted(removed),
                "points_table": table,
                "sealed_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="room_id,match_version").execute()
        except Exception:
            pass
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
