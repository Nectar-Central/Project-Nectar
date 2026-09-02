import aiosqlite
import json
import time
import os
from dotenv import load_dotenv
from pathlib import Path
load_dotenv()
DB_PATH = str(Path(__file__).resolve().parent / "data.db")

USE_SUPABASE = bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
supabase = None
if USE_SUPABASE:
    try:
        from supabase import create_client
        supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    except Exception as e:
        print(f"[db] supabase init failed, falling back to sqlite: {e}")
        USE_SUPABASE = False
# Per spec Task 7: force Supabase when env present, warn if fallback will cause stale data (Vercel writes to Supabase only)
if not USE_SUPABASE:
    print("[db] WARNING: Supabase not configured — bot will read sqlite data.db which will be stale vs Vercel. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY per spec.md")

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
            supabase.table("bngc_users").select("discord_id").limit(1).execute()
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
            r = supabase.table("bngc_users").select("*").eq("token", token).limit(1).execute()
            if r.data: return r.data[0]
        except: pass
        try:
            r = supabase.table("bngc_login_tokens").select("*, bngc_users!inner(*)").eq("token", token).eq("used", False).limit(1).execute()
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
            r = supabase.table("bngc_users").select("*").eq("discord_id", discord_id).limit(1).execute()
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
        try: supabase.table("bngc_login_tokens").insert({"token": token, "discord_id": discord_id, "expires_at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(exp)), "used": False}).execute()
        except: pass
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO login_tokens (token, discord_id, expires_at, used) VALUES (?,?,?,0)", (token, discord_id, exp))
        await db.commit()

async def get_login_token(token: str):
    if USE_SUPABASE:
        try:
            r = supabase.table("bngc_login_tokens").select("*").eq("token", token).limit(1).execute()
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
        try: supabase.table("bngc_login_tokens").update({"used": True, "used_at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), "used_ip": ip}).eq("token", token).execute()
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
            supabase.table("bngc_users").upsert({
                "discord_id": discord_id,
                "discord_username": discord_username,
                "token": token,
                "first_seen": True,
            }, on_conflict="discord_id").execute()
        except Exception as e:
            # fallback: ensure token column exists then retry
            try: supabase.rpc("add_token_column", {}).execute()
            except: pass
            supabase.table("bngc_users").upsert({"discord_id": discord_id, "discord_username": discord_username, "token": token}, on_conflict="discord_id").execute()
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
            supabase.table("bngc_users").update({**fields, "first_seen": False}).eq("discord_id", did).execute()
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
            supabase.table("bngc_users").update({field: cur + amount}).eq("discord_id", did).execute()
            # also update bngc_player_stats if exists
            try:
                r = supabase.table("bngc_player_stats").select(field).eq("discord_id", did).limit(1).execute()
                if r.data:
                    supabase.table("bngc_player_stats").update({field: (r.data[0].get(field,0) or 0)+amount}).eq("discord_id", did).execute()
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
            supabase.table("bngc_users").update({"playtime_global": (u.get("playtime_global",0) or 0)+seconds, "playtime_per_gm": json.dumps(pg)}).eq("discord_id", did).execute()
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
            supabase.table("bngc_users").update({
                "is_locked": True, "lock_reason": reason, "locked_at": datetime.now(timezone.utc).isoformat(),
                "locked_ip": ip, "lock_scope": scope, "is_admin_locked": bool(is_admin),
            }).eq("discord_id", discord_id).execute()
            try: supabase.table("bngc_security_logs").insert({"discord_id": discord_id, "event_type": "account_locked", "ip_address": ip, "details": {"reason": reason, "scope": scope}, "severity": "critical"}).execute()
            except: pass
            try:
                supabase.table("bngc_alerts").insert({
                    "discord_id": discord_id, "alert_type": "locked", "ip_address": ip,
                    "details": {"reason": reason, "scope": scope}, "severity": "critical",
                }).execute()
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
            r = supabase.table("bngc_users").select("lock_scope,is_admin_locked,is_locked").eq("discord_id", discord_id).limit(1).execute()
            if r.data:
                row = r.data[0]
                is_admin = bool(row.get("is_admin_locked")) or str(row.get("lock_scope")).lower() == "admin"
                if is_admin and not force_admin:
                    return False, "admin"
        except Exception:
            pass
        try:
            supabase.table("bngc_users").update({"is_locked": False, "lock_reason": None, "locked_at": None, "locked_ip": None, "lock_scope": "default", "is_admin_locked": False}).eq("discord_id", discord_id).execute()
            try: supabase.table("bngc_security_logs").insert({"discord_id": discord_id, "event_type": "account_unlocked", "details": {"by": "admin" if force_admin else "self"}, "severity": "info"}).execute()
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
            r = supabase.table("bngc_users").select("is_locked").eq("discord_id", discord_id).limit(1).execute()
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
        try: supabase.table("bngc_users").delete().eq("discord_id", discord_id).execute()
        except: pass
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE discord_id=?", (discord_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# Match helpers — /matches, /replay, /killfeed
# ---------------------------------------------------------------------------

async def get_recent_matches(discord_id: str, limit: int = 10):
    """Get recent matches (sessions) for a user with event summaries.

    Returns list of dicts: [{session_id, created_at, event_count, kills, deaths,
    wins, defeats, game_modes}]
    """
    if not USE_SUPABASE or not supabase:
        return []

    try:
        # Get sessions for this user — no last_used_at column, just get session_ids
        sess_res = (
            supabase.table("bngc_sessions")
            .select("session_id")
            .eq("discord_id", discord_id)
            .limit(limit * 2)
            .execute()
        )
        sessions = sess_res.data or []
        if not sessions:
            return []

        results = []
        for sess in sessions:
            sid = sess.get("session_id")
            if not sid:
                continue
            # Get event counts for this session
            try:
                ev_res = (
                    supabase.table("bngc_game_events")
                    .select("event_type, event_data, created_at")
                    .eq("discord_id", discord_id)
                    .eq("session_id", sid)
                    .execute()
                )
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

            # Use earliest event created_at as match time
            created_at = events[0].get("created_at") if events else None

            results.append({
                "session_id": sid,
                "created_at": created_at,
                "event_count": len(events),
                "kills": kills,
                "deaths": deaths,
                "wins": wins,
                "defeats": defeats,
                "game_modes": gms,
            })

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
        res = q.execute()
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
        res = (
            supabase.table("bngc_sessions")
            .select("session_id")
            .eq("discord_id", discord_id)
            .like("session_id", f"{short_id}%")
            .limit(5)
            .execute()
        )
        sessions = res.data or []
        if len(sessions) == 1:
            return sessions[0]["session_id"]
        if len(sessions) > 1:
            # Ambiguous — return first but caller should warn
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
