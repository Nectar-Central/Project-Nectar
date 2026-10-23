"""
guilds.py — multi-server helpers (one bot, many Discord servers).

Every tourney, board and PR row is owned by exactly one guild. This module is
the single place that answers "who is this user, and what may they touch?".

Rules of thumb:
  * `bngc_users` is the global identity (login token, in-game name, lock).
  * `nectar_guilds` is the server registry.
  * `nectar_guild_admins` is the permission list (owner/admin/moderator).
  * `nectar_guild_players` is the per-server scoreboard row. Stats never
    cross servers.

All functions are best-effort and never raise on a DB blip: they return None,
[] or False. Callers decide whether a missing row is a 404 or a 503.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

from .supabase_client import supabase, db_io
from .logger import log_err


# The original Nectar server. Used as the fallback owner for rows that
# predate multi-guild (see add_multi_guild_v263.sql) and as the default when
# no guild is supplied by an older caller.
DEFAULT_GUILD_ID = int(os.getenv("NECTAR_DEFAULT_GUILD_ID", "1512788791805349888"))

SESSION_TTL_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


# ---------------------------------------------------------------------------
# Guilds
# ---------------------------------------------------------------------------

async def get_guild(guild_id: int) -> dict | None:
    """One guild row, or None when unknown/unconfigured."""
    if not supabase or not guild_id:
        return None
    try:
        res = await db_io(
            lambda: supabase.table("nectar_guilds")
            .select("*")
            .eq("id", int(guild_id))
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as e:
        log_err(f"GUILDS get_guild {guild_id}: {e}")
        return None


async def list_guilds(include_inactive: bool = False) -> list[dict]:
    """Every registered guild (platform-owner view)."""
    if not supabase:
        return []
    try:
        q = supabase.table("nectar_guilds").select("*").order("created_at")
        if not include_inactive:
            q = q.eq("is_active", True)
        res = await db_io(lambda: q.execute())
        return res.data or []
    except Exception as e:
        log_err(f"GUILDS list_guilds: {e}")
        return []


async def ensure_guild(guild_id: int, name: str, owner_id: str,
                       icon_url: str | None = None) -> dict | None:
    """Register a guild if it is not registered yet; return the row.

    Idempotent on purpose: `/setup` may be run twice (or by two admins at
    once) and the second call must not fail — it just returns the existing
    row so the caller can show the dashboard link.
    """
    existing = await get_guild(guild_id)
    if existing:
        return existing
    if not supabase:
        return None
    row = {
        "id": int(guild_id),
        "name": str(name or "Server")[:100],
        "owner_id": str(owner_id),
        "icon_url": icon_url,
    }
    try:
        res = await db_io(
            lambda: supabase.table("nectar_guilds").insert(row).execute()
        )
        created = (res.data or [None])[0]
        if created:
            await add_admin(guild_id, owner_id, role="owner", added_by=owner_id)
        return created
    except Exception as e:
        # A race (two inserts) surfaces as a duplicate-key error; re-read.
        log_err(f"GUILDS ensure_guild {guild_id}: {e}")
        return await get_guild(guild_id)


async def update_guild(guild_id: int, patch: dict) -> bool:
    """Write arbitrary guild columns/settings. Returns True on success."""
    if not supabase or not patch:
        return False
    try:
        payload = dict(patch)
        payload["updated_at"] = _iso()
        await db_io(
            lambda: supabase.table("nectar_guilds")
            .update(payload)
            .eq("id", int(guild_id))
            .execute()
        )
        return True
    except Exception as e:
        log_err(f"GUILDS update {guild_id}: {e}")
        return False


async def patch_guild_settings(guild_id: int, settings_patch: dict) -> dict | None:
    """Merge into settings jsonb (never replaces the whole object).

    Read-modify-write: settings is small and there is exactly one writer
    per guild in practice (the dashboard), so a lost-update race is not
    worth a database function here.
    """
    guild = await get_guild(guild_id)
    if not guild:
        return None
    current = guild.get("settings") or {}
    if isinstance(current, str):
        try:
            import json
            current = json.loads(current)
        except Exception:
            current = {}
    merged = {**current, **(settings_patch or {})}
    ok = await update_guild(guild_id, {"settings": merged})
    return merged if ok else None


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------

async def add_admin(guild_id: int, user_id: str, role: str = "admin",
                    added_by: str | None = None) -> bool:
    if not supabase:
        return False
    role = role if role in ("owner", "admin", "moderator") else "admin"
    try:
        await db_io(
            lambda: supabase.table("nectar_guild_admins").upsert({
                "guild_id": int(guild_id),
                "user_id": str(user_id),
                "role": role,
                "added_by": str(added_by) if added_by else None,
            }, on_conflict="guild_id,user_id").execute()
        )
        return True
    except Exception as e:
        log_err(f"GUILDS add_admin {guild_id}/{user_id}: {e}")
        return False


async def remove_admin(guild_id: int, user_id: str) -> bool:
    """Remove staff. The owner row is never removable (avoids a locked-out guild)."""
    if not supabase:
        return False
    try:
        row = await get_admin(guild_id, user_id)
        if row and row.get("role") == "owner":
            return False
        await db_io(
            lambda: supabase.table("nectar_guild_admins")
            .delete()
            .eq("guild_id", int(guild_id))
            .eq("user_id", str(user_id))
            .execute()
        )
        return True
    except Exception as e:
        log_err(f"GUILDS remove_admin {guild_id}/{user_id}: {e}")
        return False


async def get_admin(guild_id: int, user_id: str) -> dict | None:
    if not supabase:
        return None
    try:
        res = await db_io(
            lambda: supabase.table("nectar_guild_admins")
            .select("guild_id,user_id,role,added_at")
            .eq("guild_id", int(guild_id))
            .eq("user_id", str(user_id))
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        return None


async def list_admins(guild_id: int) -> list[dict]:
    if not supabase:
        return []
    try:
        res = await db_io(
            lambda: supabase.table("nectar_guild_admins")
            .select("guild_id,user_id,role,added_at,added_by")
            .eq("guild_id", int(guild_id))
            .order("added_at")
            .execute()
        )
        return res.data or []
    except Exception as e:
        log_err(f"GUILDS list_admins {guild_id}: {e}")
        return []


async def list_user_guilds(user_id: str, provider_token: str | None = None) -> list[dict]:
    """All Discord servers where the user has Manage Server, merged with Nectar.

    Returns both registered (has a Nectar row, user is staff) and unregistered
    (user has MANAGE_GUILD on Discord but the server hasn't run /setup yet).
    Registered servers are clickable; unregistered ones are dimmed with an
    invite-bot link.
    """
    if not supabase or not user_id:
        return []

    # Fetch Nectar-registered guilds where this user is staff
    nectar_guilds = {}
    try:
        res = await db_io(
            lambda: supabase.table("nectar_guild_admins")
            .select("guild_id,role,nectar_guilds(id,name,icon_url,is_active,settings)")
            .eq("user_id", str(user_id))
            .execute()
        )
        for row in (res.data or []):
            guild = row.get("nectar_guilds") or {}
            if not guild or guild.get("is_active") is False:
                continue
            gid = int(row.get("guild_id"))  # outer table's FK, not the join's id
            nectar_guilds[gid] = {
                "id": gid,
                "name": guild.get("name"),
                "icon_url": guild.get("icon_url"),
                "role": row.get("role"),
                "settings": guild.get("settings") or {},
                "registered": True,
            }
    except Exception as e:
        log_err(f"GUILDS list_user_guilds nectar query {user_id}: {e}")

    # Fetch Discord guilds where the user has MANAGE_GUILD (0x20 = 32)
    discord_guilds = []
    if provider_token:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    "https://discord.com/api/v10/users/@me/guilds",
                    headers={"Authorization": f"Bearer {provider_token}"},
                )
            if r.status_code == 200:
                for g in (r.json() or []):
                    perms = int(g.get("permissions") or 0)
                    # MANAGE_GUILD = 0x20, ADMINISTRATOR = 0x8
                    if perms & 0x20 or perms & 0x8:
                        discord_guilds.append({
                            "id": int(g["id"]),
                            "name": g.get("name"),
                            "icon": g.get("icon"),
                        })
        except Exception as e:
            log_err(f"GUILDS fetch Discord guilds {user_id}: {e}")

    # Merge: Discord guilds that aren't in Nectar yet appear as unregistered
    merged = {}
    for dg in discord_guilds:
        gid = dg["id"]
        if gid in nectar_guilds:
            merged[gid] = nectar_guilds[gid]
        else:
            icon_url = None
            if dg.get("icon"):
                icon_url = f"https://cdn.discordapp.com/icons/{gid}/{dg['icon']}.png?size=128"
            merged[gid] = {
                "id": gid,
                "name": dg["name"],
                "icon_url": icon_url,
                "role": None,
                "settings": {},
                "registered": False,
            }

    # Include Nectar-only guilds too (in case the token expired or scope missing)
    for gid, ng in nectar_guilds.items():
        if gid not in merged:
            merged[gid] = ng

    out = list(merged.values())
    out.sort(key=lambda g: (not g.get("registered"), str(g.get("name") or "").lower()))
    return out


async def is_staff(guild_id: int, user_id: str) -> bool:
    return (await get_admin(guild_id, user_id)) is not None


async def can_manage(guild_id: int, user_id: str) -> bool:
    """owner + admin may change tourneys; moderators may not.

    Kept separate from `is_staff` because a moderator should be able to look
    at boards and ban rule-breakers without being able to rewrite results.
    """
    row = await get_admin(guild_id, user_id)
    return bool(row and row.get("role") in ("owner", "admin"))


# ---------------------------------------------------------------------------
# Dashboard sessions
# ---------------------------------------------------------------------------

async def create_session(discord_id: str, username: str | None = None,
                         avatar_url: str | None = None,
                         provider_token: str | None = None) -> str | None:
    if not supabase:
        return None
    token = secrets.token_urlsafe(32)
    try:
        await db_io(
            lambda: supabase.table("nectar_sessions").insert({
                "token": token,
                "discord_id": str(discord_id),
                "discord_username": username,
                "avatar_url": avatar_url,
                "provider_token": provider_token,
                "expires_at": _iso(_now() + timedelta(days=SESSION_TTL_DAYS)),
            }).execute()
        )
        return token
    except Exception as e:
        log_err(f"GUILDS create_session: {e}")
        return None


async def get_session(token: str) -> dict | None:
    """Live session row, or None when missing/expired."""
    if not supabase or not token:
        return None
    try:
        res = await db_io(
            lambda: supabase.table("nectar_sessions")
            .select("*")
            .eq("token", token)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        row = rows[0]
        try:
            exp = datetime.fromisoformat(str(row.get("expires_at")).replace("Z", "+00:00"))
            if exp < _now():
                await delete_session(token)
                return None
        except Exception:
            pass
        return row
    except Exception:
        return None


async def touch_session(token: str) -> None:
    """Best-effort last_seen stamp (used to prune idle sessions)."""
    if not supabase or not token:
        return
    try:
        await db_io(
            lambda: supabase.table("nectar_sessions")
            .update({"last_seen_at": _iso()})
            .eq("token", token)
            .execute()
        )
    except Exception:
        pass


async def delete_session(token: str) -> None:
    if not supabase or not token:
        return
    try:
        await db_io(
            lambda: supabase.table("nectar_sessions").delete().eq("token", token).execute()
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-guild players
# ---------------------------------------------------------------------------

async def get_guild_player(guild_id: int, discord_id: str) -> dict | None:
    if not supabase:
        return None
    try:
        res = await db_io(
            lambda: supabase.table("nectar_guild_players")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("discord_id", str(discord_id))
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        return None


async def ensure_guild_player(guild_id: int, discord_id: str,
                              game_username: str | None = None) -> dict | None:
    """Create the per-server profile on first touch. Idempotent."""
    existing = await get_guild_player(guild_id, discord_id)
    if existing:
        if game_username and not existing.get("game_username"):
            await update_guild_player(guild_id, discord_id, {"game_username": game_username})
            existing["game_username"] = game_username
        return existing
    if not supabase:
        return None
    try:
        row = {
            "guild_id": int(guild_id),
            "discord_id": str(discord_id),
            "game_username": game_username,
        }
        res = await db_io(
            lambda: supabase.table("nectar_guild_players").insert(row).execute()
        )
        return (res.data or [None])[0]
    except Exception as e:
        log_err(f"GUILDS ensure_player {guild_id}/{discord_id}: {e}")
        return await get_guild_player(guild_id, discord_id)


async def update_guild_player(guild_id: int, discord_id: str, patch: dict) -> bool:
    if not supabase or not patch:
        return False
    try:
        await db_io(
            lambda: supabase.table("nectar_guild_players")
            .update(patch)
            .eq("guild_id", int(guild_id))
            .eq("discord_id", str(discord_id))
            .execute()
        )
        return True
    except Exception as e:
        log_err(f"GUILDS update_player {guild_id}/{discord_id}: {e}")
        return False


async def list_guild_players(guild_id: int, limit: int = 500,
                             search: str | None = None) -> list[dict]:
    if not supabase:
        return []
    try:
        q = (supabase.table("nectar_guild_players")
             .select("*")
             .eq("guild_id", int(guild_id))
             .order("points", desc=True)
             .limit(max(1, min(limit, 2000))))
        if search:
            like = f"%{str(search).strip()[:32]}%"
            q = q.or_(f"game_username.ilike.{like},discord_id.ilike.{like}")
        res = await db_io(lambda: q.execute())
        return res.data or []
    except Exception as e:
        log_err(f"GUILDS list_players {guild_id}: {e}")
        return []
