import aiosqlite
import asyncio
import json
import time
import os
from dotenv import load_dotenv
from pathlib import Path
# Bot's OWN .env is authoritative: override=True so a stale OS/parent-process
# SUPABASE_SERVICE_ROLE_KEY (launcher/tray/shell leftovers) can never shadow it
# and 401 every query. The file ships the current key; the process env is the
# usual source of an outdated one.
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
load_dotenv(override=False)  # repo-root .env as a secondary fallback
DB_PATH = str(Path(__file__).resolve().parent / "data.db")

# supabase-py is a BLOCKING client. A call made inline from a coroutine holds
# the event loop for its full timeout, and discord.py declares the gateway dead
# after 10s of silence ("heartbeat blocked for more than 10 seconds"). This
# library's postgrest default is 120 SECONDS, not httpx's 5s -far past that
# window -so the timeout must be set explicitly and verified, not assumed.
POSTGREST_TIMEOUT_SEC = 4


def _create_supabase_client(url: str, key: str):
    """Build the client with a short, VERIFIED postgrest timeout.

    `create_client(url, key, options=SyncClientOptions(...))` is the only
    supported spelling on supabase-py 2.31 -it takes no `http_client=`
    kwarg, and passing one silently costs you the timeout entirely (it raises,
    and the obvious fallback restores the 120s default). So: set it, then read
    it back off the live session and complain loudly if it did not stick.
    """
    from supabase import create_client
    from supabase.lib.client_options import SyncClientOptions
    client = create_client(url, key, options=SyncClientOptions(
        postgrest_client_timeout=POSTGREST_TIMEOUT_SEC,
        function_client_timeout=POSTGREST_TIMEOUT_SEC))
    session = (getattr(getattr(client, "postgrest", None), "session", None)
               or getattr(getattr(client, "postgrest", None), "_client", None))
    timeout = getattr(session, "timeout", None)
    read = getattr(timeout, "read", timeout)
    if read is None or float(read) > 5.0:
        raise RuntimeError(
            f"postgrest timeout not applied (got {timeout!r}); a blocking call "
            f"could stall the event loop for {timeout!r} and kill the heartbeat")
    print(f"[db] postgrest timeout {POSTGREST_TIMEOUT_SEC}s "
          f"(library default is 120s)")
    return client

USE_SUPABASE = bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
supabase = None
if USE_SUPABASE:
    try:
        try:
            import importlib.metadata as _md
            _libv = _md.version("supabase")
        except Exception:
            _libv = "unknown"
        _k = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
        # Fingerprint only: prefix class + length + dot count. Never the key.
        _kf = f"{_k[:3]}... (len {len(_k)}, dots {_k.count('.')})"
        print(f"[db] supabase lib v{_libv}, key {_kf}")
        supabase = _create_supabase_client(os.getenv("SUPABASE_URL"),
                                          os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    except Exception as e:
        print(f"[db] supabase init failed, falling back to sqlite: {e}")
        USE_SUPABASE = False
# Per spec Task 7: force Supabase when env present, warn if fallback will cause stale data (Vercel writes to Supabase only)
if not USE_SUPABASE:
    print("[db] WARNING: Supabase not configured -bot will read sqlite data.db which will be stale vs Vercel. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY per spec.md")


async def _sb_call(call):
    """Run one blocking supabase-py call in a worker thread.

    supabase-py is a synchronous HTTP client, so calling it inline from a
    coroutine stalls the whole event loop for a round trip (~350ms against
    the hosted DB). That stall is what starves Discord interactions past
    their 3s defer window. Pass a callable (lambda / bound method).
    """
    return await asyncio.to_thread(call)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  discord_id TEXT PRIMARY KEY,
  discord_username TEXT NOT NULL,
  token TEXT UNIQUE NOT NULL,
  game_username TEXT,
  game_id TEXT,
  skin TEXT,
  pickaxe TEXT,
  kills INTEGER DEFAULT 0,
  deaths INTEGER DEFAULT 0,
  wins INTEGER DEFAULT 0,
  defeats INTEGER DEFAULT 0,
  playtime_global INTEGER DEFAULT 0,
  playtime_per_gm TEXT DEFAULT '{}',
  first_seen INTEGER DEFAULT 1,
  is_locked INTEGER DEFAULT 0,
  lock_reason TEXT,
  locked_at INTEGER,
  locked_ip TEXT,
  lock_scope TEXT DEFAULT 'default',
  is_admin_locked INTEGER DEFAULT 0,
  registered_at INTEGER,
  last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS login_tokens (
  token TEXT PRIMARY KEY,
  discord_id TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  used INTEGER DEFAULT 0,
  used_at INTEGER,
  used_ip TEXT,
  FOREIGN KEY(discord_id) REFERENCES users(discord_id) ON DELETE CASCADE
);
"""

async def init_db():
    if USE_SUPABASE:
        try:
            await _sb_call(lambda: supabase.table("bngc_users").select("discord_id").limit(1).execute())
        except: pass
        return
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        # migrate existing db: add is_locked columns if missing
        for ddl in [
            "ALTER TABLE users ADD COLUMN is_locked INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN lock_reason TEXT",
            "ALTER TABLE users ADD COLUMN locked_at INTEGER",
            "ALTER TABLE users ADD COLUMN locked_ip TEXT",
            "ALTER TABLE users ADD COLUMN lock_scope TEXT DEFAULT 'default'",
            "ALTER TABLE users ADD COLUMN is_admin_locked INTEGER DEFAULT 0",
        ]:
            try: await db.execute(ddl)
            except: pass
        await db.commit()

async def get_by_token(token: str):
    if USE_SUPABASE:
        # try long-lived token in bngc_users (added column), fallback to login_tokens
        try:
            r = await _sb_call(lambda: supabase.table("bngc_users").select("*").eq("token", token).limit(1).execute())
            if r.data: return r.data[0]
        except: pass
        try:
            r = await _sb_call(lambda: supabase.table("bngc_login_tokens").select("*, bngc_users!inner(*)").eq("token", token).eq("used", False).limit(1).execute())
            if r.data:
                row = r.data[0]
                # merge user fields
                u = row.get("bngc_users") or {}
                return {**u, "token": token, "discord_id": row["discord_id"]}
        except: pass
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE token=?", (token,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_by_discord(discord_id: str):
    if USE_SUPABASE:
        try:
            r = await _sb_call(lambda: supabase.table("bngc_users").select("*").eq("discord_id", discord_id).limit(1).execute())
            if r.data: return r.data[0]
        except: pass
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE discord_id=?", (discord_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def create_login_token(discord_id: str, token: str, expires_in: int = 300):
    exp = int(time.time()) + expires_in
    if USE_SUPABASE:
        try: await _sb_call(lambda: supabase.table("bngc_login_tokens").insert({"token": token, "discord_id": discord_id, "expires_at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(exp)), "used": False}).execute())
        except: pass
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO login_tokens (token, discord_id, expires_at, used) VALUES (?,?,?,0)", (token, discord_id, exp))
        await db.commit()

async def get_login_token(token: str):
    if USE_SUPABASE:
        try:
            r = await _sb_call(lambda: supabase.table("bngc_login_tokens").select("*").eq("token", token).limit(1).execute())
            if r.data: return r.data[0]
        except: pass
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM login_tokens WHERE token=?", (token,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def use_login_token(token: str, ip: str = None):
    if USE_SUPABASE:
        try: await _sb_call(lambda: supabase.table("bngc_login_tokens").update({"used": True, "used_at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), "used_ip": ip}).eq("token", token).execute())
        except: pass
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE login_tokens SET used=1, used_at=?, used_ip=? WHERE token=?", (int(time.time()), ip, token))
        await db.commit()

async def upsert_user(discord_id, discord_username, token):
    if USE_SUPABASE:
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        # upsert into bngc_users with token column (add if missing)
        try:
            await _sb_call(lambda: supabase.table("bngc_users").upsert({
                "discord_id": discord_id,
                "discord_username": discord_username,
                "token": token,
                "first_seen": True,
            }, on_conflict="discord_id").execute())
        except Exception as e:
            # fallback: ensure token column exists then retry
            try: await _sb_call(lambda: supabase.rpc("add_token_column", {}).execute())
            except: pass
            await _sb_call(lambda: supabase.table("bngc_users").upsert({"discord_id": discord_id, "discord_username": discord_username, "token": token}, on_conflict="discord_id").execute())
        return
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
          INSERT INTO users (discord_id, discord_username, token, registered_at, last_seen, first_seen)
          VALUES (?,?,?,?,?,1)
          ON CONFLICT(discord_id) DO UPDATE SET discord_username=excluded.discord_username, token=excluded.token, last_seen=?
        """, (discord_id, discord_username, token, now, now, now))
        await db.commit()

async def update_player_fields(token, fields: dict):
    if USE_SUPABASE:
        try:
            # find user by token
            u = await get_by_token(token)
            if not u: return
            did = u["discord_id"]
            await _sb_call(lambda: supabase.table("bngc_users").update({**fields, "first_seen": False}).eq("discord_id", did).execute())
        except: pass
        return
    sets = ", ".join(f"{k}=?" for k in fields.keys())
    vals = list(fields.values()) + [int(time.time()), token]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE users SET {sets}, last_seen=? WHERE token=?", vals)
        await db.commit()
        await db.execute("UPDATE users SET first_seen=0 WHERE token=? AND first_seen=1", (token,))
        await db.commit()

async def inc_stat(token, field, amount=1):
    if field not in ("kills","deaths","wins","defeats"): raise ValueError(field)
    if USE_SUPABASE:
        try:
            u = await get_by_token(token)
            if not u: return
            did = u["discord_id"]
            cur = u.get(field, 0) or 0
            await _sb_call(lambda: supabase.table("bngc_users").update({field: cur + amount}).eq("discord_id", did).execute())
            # also update bngc_player_stats if exists
            try:
                r = await _sb_call(lambda: supabase.table("bngc_player_stats").select(field).eq("discord_id", did).limit(1).execute())
                if r.data:
                    await _sb_call(lambda: supabase.table("bngc_player_stats").update({field: (r.data[0].get(field,0) or 0)+amount}).eq("discord_id", did).execute())
            except: pass
        except: pass
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE users SET {field}={field}+?, last_seen=? WHERE token=?", (amount, int(time.time()), token))
        await db.commit()

async def add_playtime(token, gm, seconds: int):
    if USE_SUPABASE:
        try:
            u = await get_by_token(token)
            if not u: return
            did = u["discord_id"]
            pg = json.loads(u.get("playtime_per_gm") or "{}")
            pg[gm] = pg.get(gm, 0) + seconds
            await _sb_call(lambda: supabase.table("bngc_users").update({"playtime_global": (u.get("playtime_global",0) or 0)+seconds, "playtime_per_gm": json.dumps(pg)}).eq("discord_id", did).execute())
        except: pass
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT playtime_global, playtime_per_gm FROM users WHERE token=?", (token,)) as cur:
            row = await cur.fetchone()
            if not row: return
            pg = json.loads(row["playtime_per_gm"] or "{}")
            pg[gm] = pg.get(gm, 0) + seconds
            await db.execute("UPDATE users SET playtime_global=playtime_global+?, playtime_per_gm=?, last_seen=? WHERE token=?",
                             (seconds, json.dumps(pg), int(time.time()), token))
            await db.commit()

async def lock_user(discord_id: str, reason: str, ip: str = None, *, scope: str = "default"):
    """
    scope: "default" => user can /unlock; "admin" => only admin can unlock
    """
    scope = "admin" if str(scope).lower() == "admin" else "default"
    is_admin = 1 if scope == "admin" else 0
    if USE_SUPABASE:
        try:
            from datetime import datetime, timezone
            await _sb_call(lambda: supabase.table("bngc_users").update({
                "is_locked": True, "lock_reason": reason, "locked_at": datetime.now(timezone.utc).isoformat(),
                "locked_ip": ip, "lock_scope": scope, "is_admin_locked": bool(is_admin),
            }).eq("discord_id", discord_id).execute())
            try: await _sb_call(lambda: supabase.table("bngc_security_logs").insert({"discord_id": discord_id, "event_type": "account_locked", "ip_address": ip, "details": {"reason": reason, "scope": scope}, "severity": "critical"}).execute())
            except: pass
            try:
                await _sb_call(lambda: supabase.table("bngc_alerts").insert({
                    "discord_id": discord_id, "alert_type": "locked", "ip_address": ip,
                    "details": {"reason": reason, "scope": scope}, "severity": "critical",
                }).execute())
            except: pass
        except: pass
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_locked=1, lock_reason=?, locked_at=?, locked_ip=?, lock_scope=?, is_admin_locked=? WHERE discord_id=?", (reason, int(time.time()), ip, scope, is_admin, discord_id))
        await db.commit()

async def unlock_user(discord_id: str, *, force_admin: bool = False):
    """
    Returns (ok, reason_if_blocked). If locked scope is admin and force_admin is False, blocked.
    """
    # Check scope first (supabase)
    if USE_SUPABASE:
        try:
            r = await _sb_call(lambda: supabase.table("bngc_users").select("lock_scope,is_admin_locked,is_locked").eq("discord_id", discord_id).limit(1).execute())
            if r.data:
                row = r.data[0]
                is_admin = bool(row.get("is_admin_locked")) or str(row.get("lock_scope")).lower() == "admin"
                if is_admin and not force_admin:
                    return False, "admin"
        except Exception:
            pass
        try:
            await _sb_call(lambda: supabase.table("bngc_users").update({"is_locked": False, "lock_reason": None, "locked_at": None, "locked_ip": None, "lock_scope": "default", "is_admin_locked": False}).eq("discord_id", discord_id).execute())
            try: await _sb_call(lambda: supabase.table("bngc_security_logs").insert({"discord_id": discord_id, "event_type": "account_unlocked", "details": {"by": "admin" if force_admin else "self"}, "severity": "info"}).execute())
            except: pass
        except: pass
        return True, None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT is_admin_locked, lock_scope, is_locked FROM users WHERE discord_id=?", (discord_id,)) as cur:
            row = await cur.fetchone()
            if row:
                is_admin = bool(row["is_admin_locked"]) or str(row["lock_scope"] or "default").lower() == "admin"
                if row["is_locked"] and is_admin and not force_admin:
                    return False, "admin"
        await db.execute("UPDATE users SET is_locked=0, lock_reason=NULL, locked_at=NULL, locked_ip=NULL, lock_scope='default', is_admin_locked=0 WHERE discord_id=?", (discord_id,))
        await db.commit()
        return db.total_changes > 0, None

async def is_locked(discord_id: str) -> bool:
    if USE_SUPABASE:
        try:
            r = await _sb_call(lambda: supabase.table("bngc_users").select("is_locked").eq("discord_id", discord_id).limit(1).execute())
            if r.data: return bool(r.data[0].get("is_locked"))
        except: pass
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT is_locked FROM users WHERE discord_id=?", (discord_id,)) as cur:
            row = await cur.fetchone()
            return bool(row["is_locked"]) if row else False

async def delete_user(discord_id):
    if USE_SUPABASE:
        try: await _sb_call(lambda: supabase.table("bngc_users").delete().eq("discord_id", discord_id).execute())
        except: pass
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE discord_id=?", (discord_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# Leaderboard -/leaderboard
# ---------------------------------------------------------------------------

async def get_leaderboard(stat: str = "kills", limit: int = 10):
    """Top players ordered by a stat (kills or wins).

    Returns list of dicts with discord_id/discord_username/game_username/
    kills/deaths/wins/defeats, highest first.
    """
    stat = stat if stat in ("kills", "wins") else "kills"
    try:
        limit = max(1, min(25, int(limit)))
    except Exception:
        limit = 10
    cols = "discord_id,discord_username,game_username,kills,deaths,wins,defeats"
    if USE_SUPABASE and supabase:
        try:
            r = await _sb_call(lambda: supabase.table("bngc_users").select(cols).order(stat, desc=True).limit(limit).execute())
            return r.data or []
        except Exception as e:
            print(f"[db] get_leaderboard failed: {e}")
            return []
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT discord_id,discord_username,game_username,kills,deaths,wins,defeats FROM users ORDER BY {stat} DESC LIMIT ?",
                (limit,),
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]
    except Exception as e:
        print(f"[db] get_leaderboard sqlite failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Match helpers -/matches, /replay, /killfeed
# ---------------------------------------------------------------------------

async def get_recent_matches(discord_id: str, limit: int = 10):
    """Get recent matches (sessions) for a user with event summaries.

    Returns list of dicts: [{session_id, created_at, event_count, kills, deaths,
    wins, defeats, game_modes}]
    """
    if not USE_SUPABASE or not supabase:
        return []

    try:
        # Get sessions for this user -ordered by most recent
        sess_res = await _sb_call(lambda: (
            supabase.table("bngc_sessions")
            .select("session_id, created_at")
            .eq("discord_id", discord_id)
            .order("created_at", desc=True)
            .limit(limit * 2)
            .execute()
        ))
        sessions = sess_res.data or []
        if not sessions:
            return []

        results = []
        for sess in sessions:
            sid = sess.get("session_id")
            if not sid:
                continue
            # Get event counts for this session -ordered so last is most recent
            try:
                ev_res = await _sb_call(lambda: (
                    supabase.table("bngc_game_events")
                    .select("event_type, event_data, created_at")
                    .eq("discord_id", discord_id)
                    .eq("session_id", sid)
                    .order("created_at", desc=True)
                    .execute()
                ))
                events = ev_res.data or []
            except Exception:
                events = []

            if not events:
                continue  # skip sessions with no events

            kills = sum(1 for e in events if e.get("event_type") == "kill")
            deaths = sum(1 for e in events if e.get("event_type") == "death")
            wins = sum(1 for e in events if e.get("event_type") == "win")
            defeats = sum(1 for e in events if e.get("event_type") == "defeat")
            gms = list({e.get("event_data", {}).get("gm") for e in events if e.get("event_data", {}).get("gm")})

            # Last played = most recent event time (not earliest)
            # Compute distinct matches inside this session for badge
            groups = {}
            for ev in events:
                gd = ev.get("event_data") or {}
                rid = gd.get("room_id") or gd.get("code") or gd.get("short_code") or sid
                rid = str(rid)
                mv = gd.get("match_version")
                if mv is not None:
                    try: key = f"{rid}_{int(mv)}"
                    except: key = f"{rid}_{mv}"
                    groups[key] = True
                else:
                    groups[rid] = True
            match_count = len(groups)
            # events are desc ordered, so first is latest
            created_at = events[0].get("created_at") if events else None
            # keep original earliest as fallback if needed, but last played is latest
            last_played = created_at

            results.append({
                "session_id": sid,
                "created_at": last_played,
                "last_played": last_played,
                "event_count": len(events),
                "kills": kills,
                "deaths": deaths,
                "wins": wins,
                "defeats": defeats,
                "game_modes": gms,
                "match_count": match_count,
            })

        # Sort by last played most recent first
        try:
            results.sort(key=lambda x: x.get("last_played") or "", reverse=True)
        except: pass
        return results

    except Exception as e:
        print(f"[db] get_recent_matches failed: {e}")
        return []


async def get_match_events(session_id: str, discord_id: str, event_types: list[str] = None):
    """Get events for a specific match/session.

    Args:
        session_id: The session ID (match ID)
        discord_id: The user's Discord ID
        event_types: Optional filter, e.g. ["kill", "death"]

    Returns list of event dicts sorted by created_at.
    """
    if not USE_SUPABASE or not supabase:
        return []

    try:
        q = (
            supabase.table("bngc_game_events")
            .select("*")
            .eq("discord_id", discord_id)
            .eq("session_id", session_id)
            .order("created_at", desc=False)
        )
        if event_types:
            q = q.in_("event_type", event_types)
        res = await _sb_call(q.execute)
        return res.data or []

    except Exception as e:
        print(f"[db] get_match_events failed: {e}")
        return []


async def get_match_by_short_id(discord_id: str, short_id: str):
    """Find a session by short ID prefix (first 8 chars of session_id).

    Returns the full session_id if unique match found, else None.
    """
    if not USE_SUPABASE or not supabase:
        return None

    try:
        res = await _sb_call(lambda: (
            supabase.table("bngc_sessions")
            .select("session_id")
            .eq("discord_id", discord_id)
            .like("session_id", f"{short_id}%")
            .limit(5)
            .execute()
        ))
        sessions = res.data or []
        if len(sessions) == 1:
            return sessions[0]["session_id"]
        if len(sessions) > 1:
            # Ambiguous -return first but caller should warn
            return sessions[0]["session_id"]
        return None

    except Exception as e:
        print(f"[db] get_match_by_short_id failed: {e}")
        return None


async def get_match_events_grouped(session_id: str, discord_id: str) -> dict[str, list[dict]]:
    """Group events for a session by match/room id + match_version.

    Uses event_data.room_id / code / short_code if present, plus match_version to split
    private zw_lg multi-match on same room_id (stG 3→1). Fallback to session_id for legacy.
    Returns {composite_id: [events...]} with events sorted by created_at.
    """
    events = await get_match_events(session_id, discord_id)
    groups: dict[str, list[dict]] = {}
    for ev in events:
        gd = ev.get("event_data") or {}
        rid = gd.get("room_id") or gd.get("room") or gd.get("code") or gd.get("short_code") or session_id
        rid = str(rid)
        mv = gd.get("match_version")
        if mv is not None:
            try:
                key = f"{rid}_{int(mv)}"
            except Exception:
                key = f"{rid}_{mv}"
        else:
            key = rid
        groups.setdefault(key, []).append(ev)
    return groups


# ---------------------------------------------------------------------------
# Nectar Coins -/balance /daily /pay /coinflip /slots /dice /coins-leaderboard
# ---------------------------------------------------------------------------

COIN_DAILY_MIN = 1  # random payout per daily claim, inclusive
COIN_DAILY_MAX = 5

# Gambling reasons: wins credit total_earned, every gamble counts its
# stake toward total_wagered (mirrors server/routes/coins.py).
_COIN_EARN_REASONS = frozenset({
    "daily", "flip_win", "pay_received", "admin_adjust",
    "slots_win", "dice_win", "duel_win", "earn_play",
    "tourney_first", "tourney_second", "tourney_third", "coin_add",
})
_COIN_WAGER_REASONS = frozenset({
    "flip_win", "flip_loss",
    "slots_win", "slots_loss", "slots_refund",
    "dice_win", "dice_loss", "dice_push",
})

# Slots: 3 uniform reels; trips pay profit multiples, one pair refunds.
SLOT_SYMBOLS = ["🍒", "⭐", "🪙", "💎", "7️⃣"]
SLOT_TRIPS_MULT = {"🍒": 2, "⭐": 3, "🪙": 5, "💎": 10, "7️⃣": 25}

# Dice: roll 1-100 vs 50. Over wins on 51-100, Under on 1-49, 50 pushes.
DICE_PUSH = 50
DICE_SIDES = 100


def spin_slots():
    """Spin 3 uniform reels. Never raises."""
    import secrets as _secrets
    try:
        return [str(_secrets.choice(SLOT_SYMBOLS)) for _ in range(3)]
    except Exception:
        return ["🍒", "🍒", "🍒"]


def slots_kind(symbols) -> str:
    """trips | pair | loss. Never raises."""
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
    """Net delta: trips +mult*bet, pair 0 (refund), else -bet. Never raises."""
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


def roll_dice() -> int:
    """Roll 1-100. Never raises."""
    import secrets as _secrets
    try:
        return int(_secrets.randbelow(DICE_SIDES)) + 1
    except Exception:
        return DICE_PUSH


def dice_delta(bet: int, choice: str, roll: int) -> int:
    """Net delta: +bet win, 0 push (roll == 50), -bet loss. Never raises."""
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


def coin_daily_payout(streak: int) -> int:
    """Random 1-5 coins per daily claim (streak still tracked, no bonus)."""
    import secrets as _secrets
    try:
        return int(_secrets.randbelow(COIN_DAILY_MAX - COIN_DAILY_MIN + 1)) + COIN_DAILY_MIN
    except Exception:
        return COIN_DAILY_MIN


async def get_coin_balance(discord_id: str):
    """Balance row or None. Tolerates pre-migration DBs (no nectar columns)."""
    row = await get_by_discord(str(discord_id))
    if not row:
        return None
    try:
        return {"balance": int(row.get("nectar_coins") or 0),
                "total_earned": int(row.get("nectar_total_earned") or 0),
                "total_wagered": int(row.get("nectar_total_wagered") or 0),
                "daily_streak": int(row.get("daily_streak") or 0),
                "last_daily_claim": row.get("last_daily_claim"),
                "row": row}
    except Exception:
        return None


async def _coin_ledger_insert(discord_id: str, delta: int, balance_after: int,
                              reason: str, meta: dict | None = None):
    """Best-effort ledger append. Never raises (pre-migration -> skip)."""
    if not USE_SUPABASE or not supabase:
        return
    try:
        await _sb_call(lambda: supabase.table("bngc_coin_ledger").insert({
            "discord_id": str(discord_id), "delta": int(delta),
            "balance_after": int(balance_after), "reason": str(reason),
            "meta": dict(meta or {}),
        }).execute())
    except Exception as e:
        if "nectar" not in str(e).lower() and "42703" not in str(e) and "PGRST205" not in str(e):
            print(f"[coins] ledger insert failed: {e}")


async def add_coin_delta(discord_id: str, delta: int, reason: str,
                         meta: dict | None = None):
    """Apply a signed delta; returns new balance. Raises on missing/insufficient."""
    row = await get_by_discord(str(discord_id))
    if not row:
        raise LookupError("user not found")
    try:
        bal = int(row.get("nectar_coins") or 0) + int(delta)
    except Exception:
        raise RuntimeError("Database migration pending -apply add_nectar_coins_v252.sql.")
    if bal < 0:
        raise ValueError("Insufficient balance.")
    if not USE_SUPABASE or not supabase:
        raise RuntimeError("Coin balances need Supabase (sqlite has no nectar columns).")
    try:
        earned = int(row.get("nectar_total_earned") or 0)
        wagered = int(row.get("nectar_total_wagered") or 0)
        if int(delta) > 0 and reason in _COIN_EARN_REASONS:
            earned += int(delta)
        if reason in _COIN_WAGER_REASONS:
            try:
                wagered += int((meta or {}).get("bet") or abs(int(delta)))
            except Exception:
                wagered += abs(int(delta))
        await _sb_call(lambda: supabase.table("bngc_users").update({
            "nectar_coins": bal, "nectar_total_earned": earned,
            "nectar_total_wagered": wagered,
        }).eq("discord_id", str(discord_id)).execute())
    except Exception as e:
        if "nectar" in str(e).lower() or "42703" in str(e):
            raise RuntimeError("Database migration pending -apply add_nectar_coins_v252.sql.")
        raise
    await _coin_ledger_insert(discord_id, int(delta), bal, reason, meta)
    return bal


async def claim_daily_coins(discord_id: str):
    """Advance daily streak + pay coins. Returns dict {claimed,...}."""
    from datetime import date as _date
    row = await get_by_discord(str(discord_id))
    if not row:
        raise LookupError("user not found")
    try:
        today = _date.today()
        lc = row.get("last_daily_claim")
        if isinstance(lc, str):
            lc = _date.fromisoformat(lc[:10])
        if lc is not None and hasattr(lc, "toordinal") and (today - lc).days <= 0:
            return {"claimed": False,
                    "balance": int(row.get("nectar_coins") or 0),
                    "streak": int(row.get("daily_streak") or 0)}
        if lc is None or lc == "":
            new_streak = 1
        elif hasattr(lc, "toordinal") and (today - lc).days == 1:
            try:
                new_streak = int(row.get("daily_streak") or 0) + 1
            except Exception:
                new_streak = 1
        else:
            new_streak = 1
        payout = coin_daily_payout(new_streak)
        if not USE_SUPABASE or not supabase:
            raise RuntimeError("Coin balances need Supabase.")
        await _sb_call(lambda: supabase.table("bngc_users").update({
            "daily_streak": new_streak,
            "last_daily_claim": today.isoformat(),
        }).eq("discord_id", str(discord_id)).execute())
        bal = await add_coin_delta(discord_id, payout, "daily", {"streak": new_streak})
        return {"claimed": True, "balance": bal, "payout": payout, "streak": new_streak}
    except (LookupError, ValueError, RuntimeError):
        raise
    except Exception as e:
        if "nectar" in str(e).lower() or "42703" in str(e):
            raise RuntimeError("Database migration pending -apply add_nectar_coins_v252.sql.")
        raise


async def get_coin_ledger(discord_id: str, limit: int = 10):
    try:
        limit = max(1, min(25, int(limit)))
    except Exception:
        limit = 10
    if not USE_SUPABASE or not supabase:
        return []
    try:
        r = await _sb_call(lambda: supabase.table("bngc_coin_ledger").select(
            "delta,balance_after,reason,meta,created_at").eq(
            "discord_id", str(discord_id)).order(
            "created_at", desc=True).limit(limit).execute())
        return r.data or []
    except Exception:
        return []


async def get_coin_leaderboard(limit: int = 10):
    try:
        limit = max(1, min(25, int(limit)))
    except Exception:
        limit = 10
    if not USE_SUPABASE or not supabase:
        return []
    try:
        r = await _sb_call(lambda: supabase.table("bngc_users").select(
            "discord_id,discord_username,game_username,nectar_coins").order(
            "nectar_coins", desc=True).limit(limit).execute())
        return r.data or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Wager-earnings cap -duel_win + bet_won profit per day (shared with the
# server's WAGER_EARN_CAP; tourney prizes, daily, earn_play and casino are
# NOT capped). Profit comes from ledger meta.earned stamps; legacy rows
# without the stamp fall back to gross delta for duel_win (today only).
# ---------------------------------------------------------------------------

WAGER_PROFIT_REASONS = frozenset({"bet_won", "duel_win"})


def sum_wager_profit(entries) -> int:
    """Daily wager profit over ledger rows. Pure, never raises."""
    total = 0
    try:
        for x in (entries or []):
            try:
                m = (x or {}).get("meta") or {}
                if "earned" in m:
                    total += int(m.get("earned") or 0)
                elif str((x or {}).get("reason")) == "duel_win":
                    total += int((x or {}).get("delta") or 0)
            except Exception:
                continue
    except Exception:
        pass
    return max(0, total)


def cap_wager_profit(profit: int, earned_today: int, cap: int = 50) -> int:
    """Payable profit under the daily wager-earnings cap. Pure, never raises."""
    try:
        room = int(cap) - int(earned_today or 0)
        if room <= 0:
            return 0
        return max(0, min(int(profit or 0), room))
    except Exception:
        return 0


async def wager_profit_today(discord_id: str, limit: int = 500) -> int:
    """Wager profit credited today (UTC). Never raises (0 on any doubt)."""
    if not USE_SUPABASE or not supabase:
        return 0
    try:
        from datetime import datetime, timezone
        midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        r = await _sb_call(lambda: supabase.table("bngc_coin_ledger").select(
            "delta,reason,meta,created_at").eq(
            "discord_id", str(discord_id)).in_(
            "reason", list(WAGER_PROFIT_REASONS)).order(
            "created_at", desc=True).limit(int(limit)).execute())
        rows = (r.data if hasattr(r, "data") else []) or []
        total = 0
        for x in rows:
            try:
                ts = str((x or {}).get("created_at") or "")
                if not ts:
                    continue
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt < midnight:
                    break  # desc order: older rows only age
                m = (x or {}).get("meta") or {}
                if "earned" in m:
                    total += int(m.get("earned") or 0)
                elif str((x or {}).get("reason")) == "duel_win":
                    total += int((x or {}).get("delta") or 0)
            except Exception:
                continue
        return max(0, total)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Presence -/afk, /presence, wager online gate
# ---------------------------------------------------------------------------

async def recent_combat_activity(discord_id: str, window_sec: int = 180,
                                 limit: int = 20):
    """Fresh kill/death/win rows (any room incl. lobby) for live-match fallback.

    Modes without scene/stG signals (bf/DF lobby play) never flip room
    state, so the engine can't mark them In Match -but flowing combat rows
    prove live play anyway. Returns (active, gm|None, created_at|None).
    Never raises.
    """
    if not USE_SUPABASE or not supabase:
        return False, None, None
    try:
        from datetime import datetime as _dt, timezone as _tz
        cutoff = _dt.now(_tz.utc).timestamp() - int(window_sec)
        r = await _sb_call(lambda: supabase.table("bngc_game_events").select(
            "event_type,event_data,created_at").eq(
            "discord_id", str(discord_id)).in_(
            "event_type", ["kill", "death", "win"]).order(
            "created_at", desc=True).limit(int(limit)).execute())
        for x in ((r.data if hasattr(r, "data") else []) or []):
            try:
                ts = str((x or {}).get("created_at") or "")
                if not ts:
                    continue
                dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                if dt.timestamp() < cutoff:
                    break  # desc order: older rows only age
                gm = ((x or {}).get("event_data") or {}).get("gm")
                return True, (str(gm) if gm else None), ts
            except Exception:
                continue
    except Exception:
        pass
    return False, None, None


def online_status(last_online, window_sec: int = 300):
    """(online, ts|None) from a last_online value. Pure, never raises.

    None (pre-migration / never seen) counts as offline here; callers that
    must fail open (wager gate pre-migration) check for None themselves.
    """
    from datetime import datetime as _dt, timezone as _tz
    try:
        ts = None
        if last_online:
            if isinstance(last_online, _dt):
                ts = last_online
            else:
                ts = _dt.fromisoformat(str(last_online).replace("Z", "+00:00"))
        if ts is None:
            return False, None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        return (_dt.now(_tz.utc) - ts).total_seconds() <= int(window_sec), ts
    except Exception:
        return False, None


async def set_afk(discord_id: str, is_afk: bool, message: str | None = None):
    """Set/clear AFK. Returns (is_afk, message). Raises like other writers."""
    if not USE_SUPABASE or not supabase:
        raise RuntimeError("AFK needs Supabase.")
    row = await get_by_discord(str(discord_id))
    if not row:
        raise LookupError("user not found")
    try:
        await _sb_call(lambda: supabase.table("bngc_users").update({
            "is_afk": bool(is_afk),
            "afk_message": (str(message or "").strip()[:140] or None) if is_afk else None,
        }).eq("discord_id", str(discord_id)).execute())
    except Exception as e:
        if "is_afk" in str(e) or "afk_message" in str(e) or "42703" in str(e):
            raise RuntimeError("Database migration pending -apply add_presence_v258.sql.")
        raise
    if is_afk:
        return True, (str(message or "").strip()[:140] or None)
    return False, None
