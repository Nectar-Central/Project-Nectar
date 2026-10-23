"""Play-to-earn: credit Nectar Coins for tracked kills/wins.

1 coin per kill, 2 per win, capped at 50/day (all env-tunable).
Runs as a background loop from bot.py on_ready -deliberately bot-side
(not in the capture path) so a bug here can never slow down ingest.

Idempotency: bngc_earn_watermarks stores the last credited
bngc_game_events.id per user (monotonic identity PK). Each tick only
events with id > watermark are credited, then the watermark advances —
restarts included. First run per user initializes the watermark to the
current max id (no back-pay for history).

Daily cap: sums today's reason='earn_play' ledger deltas; excess above
the cap is forfeited (standard faucet behavior). Locked accounts earn
nothing but their watermark still advances (no unlock ambush).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

print("[earn] module loaded", flush=True)

EARN_KILL_COINS = int(os.getenv("EARN_KILL_COINS", "1") or 1)
EARN_WIN_COINS = int(os.getenv("EARN_WIN_COINS", "2") or 2)
EARN_DAILY_CAP = int(os.getenv("EARN_DAILY_CAP", "50") or 50)
EARN_LOOP_SEC = int(os.getenv("EARN_LOOP_SEC", "120") or 120)
EARN_SCAN_MIN = int(os.getenv("EARN_SCAN_MIN", "10") or 10)

import database as db


# ---------------------------------------------------------------------------
# Pure helpers (no I/O -unit-testable, never raise)
# ---------------------------------------------------------------------------

def credit_for_events(events, kill_rate: int = EARN_KILL_COINS,
                      win_rate: int = EARN_WIN_COINS) -> tuple[int, int, int]:
    """(kills, wins, coins) for a batch of game events. Never raises."""
    try:
        kr = int(kill_rate)
        wr = int(win_rate)
    except Exception:
        kr, wr = 1, 2
    kills = wins = 0
    try:
        for ev in (events or []):
            try:
                et = str((ev or {}).get("event_type") or "")
                if et == "kill":
                    kills += 1
                elif et == "win":
                    wins += 1
            except Exception:
                continue
    except Exception:
        pass
    try:
        return kills, wins, kills * kr + wins * wr
    except Exception:
        return 0, 0, 0


def cap_credit(total: int, earned_today: int, cap: int = EARN_DAILY_CAP) -> int:
    """Payable coins after the daily cap. Never raises."""
    try:
        room = int(cap) - int(earned_today or 0)
        if room <= 0:
            return 0
        return max(0, min(int(total or 0), room))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Supabase access (via database._sb_call so the event loop never blocks)
# ---------------------------------------------------------------------------

def _sb():
    if not db.USE_SUPABASE or not db.supabase:
        return None
    return db.supabase


async def recent_earners(minutes: int = EARN_SCAN_MIN, limit: int = 200):
    """Distinct discord_ids with kill/win rows in the lookback window. Never raises."""
    from datetime import timedelta
    sb = _sb()
    if sb is None:
        return []
    try:
        since = (datetime.now(timezone.utc) - timedelta(minutes=int(minutes))).isoformat()
        r = await db._sb_call(lambda: sb.table("bngc_game_events")
                              .select("discord_id")
                              .in_("event_type", ["kill", "win"])
                              .eq("is_valid", True)
                              .gte("created_at", since)
                              .limit(2000).execute())
        seen: list[str] = []
        for row in ((r.data if hasattr(r, "data") else []) or []):
            try:
                did = str((row or {}).get("discord_id") or "")
                if did and did not in seen:
                    seen.append(did)
                    if len(seen) >= int(limit):
                        break
            except Exception:
                continue
        return seen
    except Exception as e:
        print(f"[earn] recent_earners failed: {e}")
        return []


async def get_watermark(discord_id: str):
    """Last credited event id, or None when the user has no row yet. Never raises."""
    sb = _sb()
    if sb is None:
        return None
    try:
        r = await db._sb_call(lambda: sb.table("bngc_earn_watermarks")
                              .select("last_event_id")
                              .eq("discord_id", str(discord_id)).limit(1).execute())
        rows = (r.data if hasattr(r, "data") else []) or []
        return int(rows[0].get("last_event_id") or 0) if rows else None
    except Exception as e:
        print(f"[earn] get_watermark failed: {e}")
        return None


async def set_watermark(discord_id: str, event_id: int) -> bool:
    """Upsert the watermark. Never raises (bool ok)."""
    sb = _sb()
    if sb is None:
        return False
    try:
        await db._sb_call(lambda: sb.table("bngc_earn_watermarks").upsert(
            {"discord_id": str(discord_id), "last_event_id": int(event_id),
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="discord_id").execute())
        return True
    except Exception as e:
        print(f"[earn] set_watermark failed: {e}")
        return False


async def max_event_id_for(discord_id: str) -> int:
    """Current max kill/win event id for a user (first-run init). Never raises."""
    sb = _sb()
    if sb is None:
        return 0
    try:
        r = await db._sb_call(lambda: sb.table("bngc_game_events")
                              .select("id")
                              .eq("discord_id", str(discord_id))
                              .in_("event_type", ["kill", "win"])
                              .order("id", desc=True).limit(1).execute())
        rows = (r.data if hasattr(r, "data") else []) or []
        return int((rows[0] or {}).get("id") or 0) if rows else 0
    except Exception as e:
        print(f"[earn] max_event_id_for failed: {e}")
        return 0


async def new_events(discord_id: str, after_id: int, limit: int = 500):
    """Kill/win rows with id > watermark, oldest first. Never raises."""
    sb = _sb()
    if sb is None:
        return []
    try:
        r = await db._sb_call(lambda: sb.table("bngc_game_events")
                              .select("id,event_type")
                              .eq("discord_id", str(discord_id))
                              .in_("event_type", ["kill", "win"])
                              .eq("is_valid", True)
                              .gt("id", int(after_id))
                              .order("id", desc=False).limit(int(limit)).execute())
        return (r.data if hasattr(r, "data") else []) or []
    except Exception as e:
        print(f"[earn] new_events failed: {e}")
        return []


async def earned_today(discord_id: str) -> int:
    """Sum of today's earn_play ledger deltas (for the daily cap). Never raises."""
    sb = _sb()
    if sb is None:
        return 0
    try:
        midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).isoformat()
        r = await db._sb_call(lambda: sb.table("bngc_coin_ledger")
                              .select("delta")
                              .eq("discord_id", str(discord_id))
                              .eq("reason", "earn_play")
                              .gte("created_at", midnight).limit(1000).execute())
        total = 0
        for row in ((r.data if hasattr(r, "data") else []) or []):
            try:
                total += int((row or {}).get("delta") or 0)
            except Exception:
                continue
        return max(0, total)
    except Exception as e:
        print(f"[earn] earned_today failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# Per-user credit + background loop
# ---------------------------------------------------------------------------

async def earn_user(discord_id: str) -> int:
    """Credit one user for new kills/wins. Returns coins paid. Never raises."""
    uid = str(discord_id)
    try:
        wm = await get_watermark(uid)
        if wm is None:
            # First run: initialize to current max (no back-pay for history).
            top = await max_event_id_for(uid)
            await set_watermark(uid, top)
            return 0
        evs = await new_events(uid, wm)
        if not evs:
            return 0
        try:
            top = max(int((e or {}).get("id") or 0) for e in evs)
        except Exception:
            top = wm
        if top <= wm:
            return 0
        # Locked / missing accounts earn nothing, but the watermark still
        # advances so unlocking later doesn't dump a backlog.
        try:
            row = await db.get_by_discord(uid)
        except Exception:
            row = None
        if row is not None:
            try:
                locked = row.get("is_locked")
                locked = bool(locked) if isinstance(locked, bool) else bool(locked)
                scope = str(row.get("lock_scope") or "default").lower()
                if locked or bool(row.get("is_admin_locked")) or scope == "admin":
                    row = None
            except Exception:
                row = None
        if row is None:
            await set_watermark(uid, top)
            return 0
        kills, wins, total = credit_for_events(evs)
        if total <= 0:
            await set_watermark(uid, top)
            return 0
        today = await earned_today(uid)
        payable = cap_credit(total, today)
        await set_watermark(uid, top)
        if payable <= 0:
            return 0  # capped -excess forfeited
        try:
            await db.add_coin_delta(uid, payable, "earn_play",
                                    {"kills": kills, "wins": wins,
                                     "events": len(evs)})
        except Exception as e:
            print(f"[earn] credit failed for {uid[:6]}: {e}")
            return 0
        print(f"[earn] +{payable} to {uid[:6]} ({kills}K/{wins}W)")
        return payable
    except Exception as e:
        print(f"[earn] earn_user failed: {e}")
        return 0


async def earn_loop(bot=None):
    """Credit recent killers/winners every EARN_LOOP_SEC. Never raises out."""
    await asyncio.sleep(20)
    print(f"[earn] loop started ({EARN_KILL_COINS}/kill {EARN_WIN_COINS}/win, "
          f"cap {EARN_DAILY_CAP}/day)", flush=True)
    while True:
        try:
            users = await recent_earners()
            paid_total, paid_users = 0, 0
            for uid in users:
                try:
                    paid = await earn_user(uid)
                    if paid > 0:
                        paid_total += paid
                        paid_users += 1
                except Exception as e:
                    print(f"[earn] user tick failed: {e}")
            if paid_users:
                print(f"[earn] tick paid {paid_total:,} to {paid_users} users")
        except Exception as e:
            print(f"[earn] loop tick failed: {e}")
        await asyncio.sleep(EARN_LOOP_SEC)
