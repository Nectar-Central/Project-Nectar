import asyncio
import discord
import database as db

TYPE_CFG = {
    "login":     {"color": 0x06B6D4, "title": "🔐 New login", "emoji": "🔐"},
    "locked":    {"color": 0xEF4444, "title": "🔒 Account locked", "emoji": "🔒"},
    "blacklist": {"color": 0x991B1B, "title": "⛔ Blacklisted", "emoji": "⛔"},
    "warning":   {"color": 0xF59E0B, "title": "⚠️ Warning", "emoji": "⚠️"},
    "cheat":     {"color": 0x7C3AED, "title": "🚨 Cheat detected", "emoji": "🚨"},
}

async def fetch_unnotified(limit: int = 20):
    if not db.USE_SUPABASE or not db.supabase:
        return []
    try:
        # Explicit columns only: select("*") ships every new column on every
        # poll (Service-Initiated egress 24/7). These are all build_embed needs.
        # MUST go through _sb_call: supabase-py is blocking, and this poll runs
        # on the event loop every few seconds — inline .execute() here is what
        # produced "heartbeat blocked for more than 10 seconds".
        r = await db._sb_call(
            lambda: db.supabase.table("bngc_alerts").select(
                "id,discord_id,alert_type,discord_username,game_username,game_id,"
                "country,details,created_at,notified"
            ).eq("notified", False).order("created_at", desc=False)
            .limit(limit).execute())
        return r.data or []
    except Exception as e:
        print(f"[alerts] fetch failed: {e}")
        return []

async def mark_notified(alert_id: int):
    if not db.USE_SUPABASE or not db.supabase:
        return
    try:
        await db._sb_call(
            lambda: db.supabase.table("bngc_alerts").update(
                {"notified": True}).eq("id", alert_id).execute())
    except Exception:
        pass

# Detail keys that may carry an IP — never rendered to users (DB keeps them).
_IP_DETAIL_KEYS = {"ip", "ip_address", "ipaddress", "used_ip", "locked_ip", "client_ip", "remote_ip"}


def build_embed(a: dict) -> discord.Embed:
    t = (a.get("alert_type") or "login").lower()
    cfg = TYPE_CFG.get(t, TYPE_CFG["login"])
    e = discord.Embed(color=cfg["color"], title=cfg["title"])
    discord_id = a.get("discord_id")
    if discord_id:
        e.add_field(name="Account", value=f"<@{discord_id}>", inline=True)
    if a.get("discord_username"):
        e.add_field(name="Discord", value=str(a["discord_username"])[:100], inline=True)
    if a.get("game_username"):
        e.add_field(name="Game username", value=str(a["game_username"])[:100], inline=True)
    if a.get("game_id"):
        e.add_field(name="Game ID", value=str(a["game_id"])[:32], inline=True)
    if a.get("country"):
        e.add_field(name="Country", value=str(a["country"]), inline=True)
    # NEVER show IPs to users — country only. IP stays in DB for enforcement.
    details = a.get("details") or {}
    if isinstance(details, dict) and details:
        # show reason if present
        if details.get("reason"):
            e.add_field(name="Reason", value=str(details["reason"])[:500], inline=False)
        # keep other details truncated, scrubbing any IP-ish keys
        rest = {k:v for k,v in details.items() if k not in ("reason",) and k.lower() not in _IP_DETAIL_KEYS}
        if rest:
            e.add_field(name="Details", value=str(rest)[:900], inline=False)
    e.timestamp = discord.utils.parse_time(a.get("created_at")) if a.get("created_at") else None
    e.set_footer(text=f"Alert #{a.get('id')} · {t}")
    return e

async def send_alert(bot: discord.Client, a: dict):
    discord_id = a.get("discord_id")
    if not discord_id:
        await mark_notified(a["id"])
        return
    try:
        user = await bot.fetch_user(int(discord_id))
    except Exception:
        try:
            user = bot.get_user(int(discord_id))
        except Exception:
            user = None
    if not user:
        print(f"[alerts] cannot fetch user {discord_id}")
        await mark_notified(a["id"])
        return
    embed = build_embed(a)
    try:
        await user.send(embed=embed)
        # extra hint for locked
        t = (a.get("alert_type") or "").lower()
        if t == "locked":
            try:
                await user.send("Use **/unlock** in the server to unlock, or ask an admin. Disable the userscript to play without tracking.")
            except Exception:
                pass
    except discord.Forbidden:
        print(f"[alerts] DM forbidden for {discord_id}")
    except Exception as e:
        print(f"[alerts] send failed {discord_id}: {e}")
    await mark_notified(a["id"])

async def alert_loop(bot: discord.Client):
    await bot.wait_until_ready()
    print("[alerts] loop started — polling bngc_alerts every 60s (idle) / 15s (active)")
    _idle = 0
    while not bot.is_closed():
        try:
            rows = await fetch_unnotified(limit=20)
            for a in rows:
                await send_alert(bot, a)
                await asyncio.sleep(0.5)
            # Back off when empty: alerts are rare, polling every 15s 24/7
            # is ~5760 Supabase reads/day for mostly-empty responses.
            _idle = _idle + 1 if not rows else 0
        except Exception as e:
            print(f"[alerts] loop error: {e}")
        await asyncio.sleep(60 if _idle >= 2 else 15)
