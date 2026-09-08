import os, secrets, asyncio, discord, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(dotenv_path="../.env")
    load_dotenv(dotenv_path="vercel-api/.env")
    load_dotenv(dotenv_path="app/.env")
except: pass
from discord import app_commands
from discord.ext import commands
import database as db
import formatters as fmt
import views
import alerts as alert_mod
import ranked as ranked_mod
try:
    import aiohttp
except: aiohttp = None
import urllib.request

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN") or os.getenv("BOT_TOKEN")
GUILD_ID = os.getenv("GUILD_ID") or os.getenv("DISCORD_GUILD_ID") or "1543724907651932282"  # guild sync keeps commands immediate
SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:8766")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def gen_token(): return secrets.token_hex(8).upper()  # 16-char hex, e.g. A1B2C3D4E5F6A7B8 (one-time login)

WARNINGS_FILE = Path(__file__).resolve().parent / "warnings.json"
ANNOUNCED_FILE = Path(__file__).resolve().parent / "announced_updates.json"
USERSCRIPT_URL = "https://raw.githubusercontent.com/Nectar-Central/Project-Nectar/main/companion/buildnow-companion.user.js"
REGISTERED_ROLE_ID = 1533401163137810544
PATCH_CHANNEL_ID = 1512793466852212846


async def _grant_registered_role(guild, user_id):
    """Best-effort: give the registered-player role. Never raises (returns bool).

    Requires the bot's top role to sit above the registered role in
    Server Settings → Roles, or Discord rejects with Forbidden (logged).
    """
    try:
        if guild is None:
            return False
        role = guild.get_role(REGISTERED_ROLE_ID)
        if role is None:
            print(f"[role] registered role {REGISTERED_ROLE_ID} not found in guild {guild.id}")
            return False
        member = guild.get_member(int(user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except Exception:
                return False
        if member is None or role in member.roles:
            return True
        await member.add_roles(role, reason="Nectar registration")
        return True
    except Exception as e:
        print(f"[role] grant failed: {e}")
        return False


def load_warnings():
    try:
        with WARNINGS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_warnings():
    WARNINGS_FILE.write_text(json.dumps(warnings, indent=2), encoding="utf-8")


warnings = load_warnings()


def load_announced():
    try:
        with ANNOUNCED_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_announced(data: dict):
    try:
        ANNOUNCED_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[announce] save failed: {e}")


def save_registered_player(discord_id: int, username: str, display_name: str, game_handle: str, avatar_url: str):
    if not db.USE_SUPABASE or not db.supabase:
        raise RuntimeError("Supabase is not configured")
    payload = {
        "id": str(discord_id),
        "username": username,
        "display_name": display_name,
        "game_handle": game_handle,
        "avatar_url": avatar_url,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    db.supabase.table("players").upsert(payload, on_conflict="id").execute()


async def get_active_session(discord_id: str):
    """Get the user's most recent active session_id from bngc_sessions."""
    if not db.USE_SUPABASE or not db.supabase:
        return None
    try:
        r = db.supabase.table("bngc_sessions").select("session_id, created_at").eq("discord_id", discord_id).eq("is_active", True).order("created_at", desc=True).limit(5).execute()
        if r.data:
            # Prefer most recent; caller can try others if engine 404
            return r.data[0].get("session_id")
    except Exception as e:
        print(f"[session] get active failed: {e}")
    return None


async def get_active_sessions(discord_id: str, limit: int = 5):
    """Get multiple recent active sessions ordered by created_at desc.

    Sessions older than 2 days are expired (see SESSION_TTL_DAYS) and skipped.
    """
    if not db.USE_SUPABASE or not db.supabase:
        return []
    try:
        r = db.supabase.table("bngc_sessions").select("session_id, created_at").eq("discord_id", discord_id).eq("is_active", True).order("created_at", desc=True).limit(limit * 2).execute()
        out = []
        for x in (r.data or []):
            if not x.get("session_id"):
                continue
            try:
                from datetime import datetime, timezone
                ts = datetime.fromisoformat(str(x.get("created_at", "")).replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - ts).total_seconds() > 2 * 86400:
                    continue  # expired session — client must re-link
            except Exception:
                pass
            out.append(x["session_id"])
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        print(f"[session] get actives failed: {e}")
        return []

async def get_session_state(session_id: str):
    """Fetch live SessionView from the engine via GET /api/session/{id}/state."""
    if not session_id:
        return None
    url = f"{SERVER_URL}/api/session/{session_id}/state"
    try:
        if aiohttp:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status == 200:
                        return await r.json()
        else:
            import urllib.request
            with urllib.request.urlopen(url, timeout=3) as r:
                return json.loads(r.read().decode())
    except Exception as e:
        print(f"[session] engine fetch failed for {session_id[:8]}: {e}")
    return None

@bot.event
async def on_ready():
    await db.init_db()
    try:
        if GUILD_ID and GUILD_ID.isdigit():
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"Synced commands to guild {GUILD_ID}")
        else:
            await bot.tree.sync()
            print("Synced global commands")
    except Exception as exc:
        print(f"Command sync failed: {exc}")
    print(f"Bot ready as {bot.user} — {len(bot.tree.get_commands())} commands loaded")
    # start alert DM loop (login / locked / cheat / warning / blacklist)
    try:
        bot.loop.create_task(alert_mod.alert_loop(bot))
    except Exception as e:
        print(f"[alerts] failed to start loop: {e}")
    # ranked countdown scheduler
    try:
        bot.loop.create_task(ranked_loop())
    except Exception as e:
        print(f"[ranked] failed to start loop: {e}")


@bot.tree.command(name="register", description="Get a login token and optionally publish your game profile")
@app_commands.describe(game_handle="Optional in-game handle for the Nectar dashboard")
async def register_cmd(interaction: discord.Interaction, game_handle: app_commands.Range[str, 1, 32] | None = None):
    await interaction.response.defer(ephemeral=True)
    try:
        row = await db.get_by_discord(str(interaction.user.id))
    except: row = None
    if row:
        # Already registered — do not issue new token, point to /refresh-token
        try:
            sessions = await db.get_recent_matches(str(interaction.user.id), limit=10)
            total_sessions = len(sessions)
            total_matches = sum(s.get("match_count", 1) for s in sessions) if sessions else 0
            last_played = fmt.fmt_date((sessions[0].get("last_played") or sessions[0].get("created_at")) if sessions else None) if sessions else "—"
            last_sid_short = sessions[0].get("session_id","")[:8] if sessions else "—"
            # Still handle dashboard game_handle update even when already registered
            profile_error = None
            if game_handle:
                clean_handle = str(game_handle).strip()
                try:
                    display_name = getattr(interaction.user, "global_name", None) or interaction.user.display_name
                    await asyncio.to_thread(save_registered_player, interaction.user.id, interaction.user.name, display_name, clean_handle, interaction.user.display_avatar.url)
                except Exception as exc:
                    profile_error = exc
                    print(f"Player profile save failed for {interaction.user.id}: {exc}")
            msg = f"You are already registered. Use **/refresh-token** to get a new login token.\n"
            msg += f"**{total_sessions} sessions · {total_matches} matches · last played {last_played} ({last_sid_short})**"
            if game_handle and profile_error is None:
                msg += f"\nDashboard profile updated for **{game_handle}**."
            elif game_handle:
                msg += "\nI could not save the dashboard profile yet; run the Supabase schema and try again."
            try:
                await _grant_registered_role(interaction.guild, interaction.user.id)
            except: pass
            return await interaction.followup.send(msg, ephemeral=True)
        except Exception as exc:
            print(f"register already-registered path failed: {exc}")
            return await interaction.followup.send("You are already registered. Use **/refresh-token** for a new token.", ephemeral=True)

    # Not registered — issue first token
    token = gen_token()
    try:
        for _ in range(5):
            if not await db.get_login_token(token) and not await db.get_by_token(token):
                break
            token = gen_token()
        await db.upsert_user(str(interaction.user.id), str(interaction.user), token)
        await db.create_login_token(str(interaction.user.id), token, expires_in=300)
    except Exception as exc:
        print(f"Registration token creation failed: {exc}")
        return await interaction.followup.send("Registration is temporarily unavailable. Check the bot's Supabase configuration.", ephemeral=True)

    profile_error = None
    if game_handle:
        clean_handle = str(game_handle).strip()
        try:
            display_name = getattr(interaction.user, "global_name", None) or interaction.user.display_name
            await asyncio.to_thread(save_registered_player, interaction.user.id, interaction.user.name, display_name, clean_handle, interaction.user.display_avatar.url)
        except Exception as exc:
            profile_error = exc
            print(f"Player profile save failed for {interaction.user.id}: {exc}")

    try:
        await interaction.user.send(f"Your BuildNow login token is **{token}**\nEnter it in the userscript prompt (once, 5 min expiry). Keep it secret!\nPlay a match to get your stats saved.")
    except discord.Forbidden:
        pass
    try:
        role_ok = await _grant_registered_role(interaction.guild, interaction.user.id)
    except: role_ok = False
    message = f"Token sent to your DMs: **{token}**\nEnter it in the game iframe prompt on first run. One-time use.\nPlay a match to get your stats saved."
    if role_ok:
        message += "\nRegistered role granted. ✅"
    if game_handle and profile_error is None:
        message += f"\nDashboard profile saved for **{game_handle}**."
    elif game_handle:
        message += "\nI could not save the dashboard profile yet; run the Supabase schema and try again."
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(name="ping", description="Check if the bot is online")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")


@bot.tree.command(name="view_dev_team", description="Shows the main developers for Nectar")
async def view_dev_team_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Nectar Development Team",
        description=(
            "<:people:1543709237987909682> Here are the main developers for Nectar.\n\n"
            "**<@1295478685876617299>** — Senior Developer\n"
            "**<@1267528889383784513>** — Founder\n"
            "**<@1143465561548927037>** — Web Developer\n"
            "**<@1526807976801865870>** — Bot Developer\n"
            "**<@1484202460577796178>** — Manager"
        ),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="coinflip", description="Flip a coin")
async def coinflip_cmd(interaction: discord.Interaction):
    result = secrets.choice(["Heads", "Tails"])
    embed = discord.Embed(
        title="Coin Flip",
        description=f"The coin landed on **{result}!**",
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="serverinfo", description="Shows information about the current server")
async def serverinfo_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
    guild = interaction.guild
    embed = discord.Embed(title=f"Server Info: {guild.name}", color=discord.Color.gold())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Server ID", value=str(guild.id), inline=True)
    embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="Members", value=str(guild.member_count or 0), inline=True)
    embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:F>", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="avatar", description="Show a user's avatar")
@app_commands.describe(user="The user whose avatar you want to see")
async def avatar_cmd(interaction: discord.Interaction, user: discord.Member | None = None):
    member = user or interaction.user
    embed = discord.Embed(title=f"{member.display_name}'s Avatar", color=discord.Color.gold())
    embed.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="warn", description="Warn a member")
@app_commands.describe(user="The member you want to warn", reason="Why you are warning them")
@app_commands.default_permissions(administrator=True)
async def warn_cmd(interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 1, 500]):
    if interaction.guild is None:
        return await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("You need Administrator permissions to use this command.", ephemeral=True)
    if user == interaction.user or user.bot:
        return await interaction.response.send_message("You cannot warn yourself or a bot.", ephemeral=True)
    if user == interaction.guild.owner:
        return await interaction.response.send_message("You cannot warn the server owner.", ephemeral=True)
    if interaction.user != interaction.guild.owner and user.top_role >= interaction.user.top_role:
        return await interaction.response.send_message("You can only warn members below your highest role.", ephemeral=True)
    bot_member = interaction.guild.me
    if bot_member and user.top_role >= bot_member.top_role:
        return await interaction.response.send_message("My role must be above the member I am warning.", ephemeral=True)
    guild_id = str(interaction.guild.id)
    user_id = str(user.id)
    warnings.setdefault(guild_id, {})
    warnings[guild_id][user_id] = warnings[guild_id].get(user_id, 0) + 1
    warning_count = warnings[guild_id][user_id]
    save_warnings()
    if warning_count >= 3:
        try:
            await user.ban(reason="Reached 3 warnings.")
        except discord.Forbidden:
            return await interaction.response.send_message("I could not ban this member. Check my Ban Members permission and role position.", ephemeral=True)
        return await interaction.response.send_message(f"{user.mention} has been banned after 3 warnings. Latest reason: {reason}")
    embed = discord.Embed(title="Member Warned", color=discord.Color.gold())
    embed.add_field(name="Member", value=f"{user.mention} ({user.id})", inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Warnings", value=f"{warning_count}/3", inline=False)
    embed.add_field(name="Warned By", value=interaction.user.mention, inline=False)
    await interaction.response.send_message(embed=embed)
    try:
        await user.send(f"You received a warning in **{interaction.guild.name}**. Reason: {reason} ({warning_count}/3)")
    except discord.Forbidden:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Command error in {getattr(interaction.command, 'name', 'unknown')}: {error}")
    try:
        inner = getattr(error, "original", None)
        if isinstance(inner, discord.errors.NotFound):
            return  # interaction expired (starved loop) — nothing left to answer
        message = "Something went wrong while running that command. Please try again."
        if isinstance(error, app_commands.MissingPermissions):
            message = "You do not have permission to use that command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except (discord.errors.NotFound, discord.errors.HTTPException):
        pass  # interaction died while we answered — nothing to do
    except Exception as e:
        print(f"[error-handler] failed: {e}")


@bot.tree.command(name="token", description="Show your current token")
async def token_cmd(interaction: discord.Interaction):
    row = await db.get_by_discord(str(interaction.user.id))
    if not row: return await interaction.response.send_message("Not registered. Use /register first.", ephemeral=True)
    await interaction.response.send_message(f"Your token: {fmt.censor_token(row.get('token'))}", ephemeral=True)

@bot.tree.command(name="refresh-token", description="Generate a new login token (old one stays valid for 5 min)")
async def refresh_token_cmd(interaction: discord.Interaction):
    did = str(interaction.user.id)
    row = await db.get_by_discord(did)
    if not row:
        return await interaction.response.send_message("Not registered. Use /register first.", ephemeral=True)
    # Generate new token
    new_token = gen_token()
    for _ in range(5):
        if not await db.get_login_token(new_token) and not await db.get_by_token(new_token):
            break
        new_token = gen_token()
    await db.create_login_token(did, new_token, expires_in=300)
    # Optionally update users.token to new one for legacy fallback (not required for new flow)
    # Keep old token valid until expiry for grace period
    try:
        await interaction.user.send(f"Your **new** login token is **{new_token}**\nOld token still works for 5 min. Enter new token in userscript prompt if you re-link.")
    except: pass
    await interaction.response.send_message(f"New token sent to DMs: **{new_token}** (old token still valid 5 min)", ephemeral=True)

@bot.tree.command(name="lock", description="Lock a user's account (default — user can /unlock)")
@app_commands.describe(user="User to lock", reason="Reason for lock")
@app_commands.default_permissions(administrator=True)
async def lock_cmd(interaction: discord.Interaction, user: discord.User, reason: str = "No reason"):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Administrator only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await db.lock_user(str(user.id), reason, ip=None, scope="default")
    try:
        await user.send(f"🔒 Your BuildNow account was locked: **{reason}**\nUse **/unlock** in the server to unlock.")
    except: pass
    await interaction.followup.send(f"🔒 Locked {user.mention} (default — they can /unlock). Reason: {reason}", ephemeral=True)

@bot.tree.command(name="adminlock", description="Admin-lock a user (only admins can unlock)")
@app_commands.describe(user="User to admin-lock", reason="Reason for admin lock")
@app_commands.default_permissions(administrator=True)
async def adminlock_cmd(interaction: discord.Interaction, user: discord.User, reason: str = "No reason"):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Administrator only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await db.lock_user(str(user.id), reason, ip=None, scope="admin")
    try:
        await user.send(f"⛔ Your BuildNow account was **admin-locked**: **{reason}**\nOnly an admin can unlock it with **/adminunlock**. Disable the tracker to play normally.")
    except: pass
    await interaction.followup.send(f"⛔ Admin-locked {user.mention}. Only admins can unlock. Reason: {reason}", ephemeral=True)

@bot.tree.command(name="adminunlock", description="Unlock any user (admin only, bypasses admin-lock)")
@app_commands.describe(user="User to unlock")
@app_commands.default_permissions(administrator=True)
async def adminunlock_cmd(interaction: discord.Interaction, user: discord.User):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Administrator only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ok, _ = await db.unlock_user(str(user.id), force_admin=True)
    try:
        await user.send("🔓 An admin unlocked your BuildNow account. You can continue.")
    except: pass
    await interaction.followup.send(f"🔓 Admin-unlocked {user.mention}.", ephemeral=True)

@bot.tree.command(name="unlock", description="Unlock your account if it was default-locked")
async def unlock_cmd(interaction: discord.Interaction):
    did = str(interaction.user.id)
    row = await db.get_by_discord(did)
    if not row:
        return await interaction.response.send_message("No account found. Use /register.", ephemeral=True)
    locked = row.get("is_locked") if isinstance(row.get("is_locked"), bool) else bool(row.get("is_locked"))
    # check admin vs default scope
    scope = str(row.get("lock_scope") or "default").lower()
    is_admin = bool(row.get("is_admin_locked")) or scope == "admin"
    if not locked:
        try:
            if await db.is_locked(did) is False:
                return await interaction.response.send_message("Your account is not locked.", ephemeral=True)
        except: pass
        if not locked:
            return await interaction.response.send_message("Your account is not locked.", ephemeral=True)
    if is_admin:
        return await interaction.response.send_message("⛔ This is an **admin lock** — only an admin can run **/adminunlock** on you. Disable the userscript to play without tracking.", ephemeral=True)
    ok, blocked = await db.unlock_user(did, force_admin=False)
    if not ok and blocked == "admin":
        return await interaction.response.send_message("⛔ Admin lock — ask an admin to run **/adminunlock**.", ephemeral=True)
    # DM confirmation
    try:
        await interaction.user.send("🔓 Your BuildNow account has been unlocked. You can continue playing or use /register for a new token.")
    except: pass
    await interaction.response.send_message("🔓 Account unlocked. You can continue now.", ephemeral=True)

@bot.tree.command(name="unregister", description="Delete your linked data")
async def unregister_cmd(interaction: discord.Interaction):
    await db.delete_user(str(interaction.user.id))
    await interaction.response.send_message("Deleted your data.")

@bot.tree.command(name="info", description="Show your linked BuildNow info")
async def info_cmd(interaction: discord.Interaction):
    row = await db.get_by_discord(str(interaction.user.id))
    if not row: return await interaction.response.send_message("Not registered.")
    live_me = None
    try:
        sid = await get_active_session(str(interaction.user.id))
        st = await get_session_state(sid) if sid else None
        if st and st.get("me"):
            live_me = st.get("me")
    except: pass
    game_username = (live_me.get("username") if live_me else None) or row.get("game_username") or "-"
    skin_val = (live_me.get("skin") if live_me else None) or row.get("skin")
    pickaxe_val = (live_me.get("pickaxe") if live_me else None) or row.get("pickaxe") or "-"
    gm_raw = (st.get("game_mode") if 'st' in locals() and st else None) or row.get("game_mode") or None
    gm_val = fmt.format_game_mode(gm_raw)
    thumb = fmt.skin_url(skin_val)
    e = discord.Embed(title="Your BuildNow Link", color=0x7c3aed)
    if thumb: e.set_thumbnail(url=thumb)
    e.add_field(name="Discord", value=row["discord_username"], inline=True)
    e.add_field(name="Token", value=fmt.censor_token(row.get("token")), inline=True)
    e.add_field(name="Game username", value=game_username, inline=True)
    if skin_val:
        e.add_field(name="Skin", value=fmt.skin_display(skin_val), inline=True)
    if pickaxe_val and pickaxe_val != "-":
        e.add_field(name="Pickaxe", value=fmt.format_skin_name(pickaxe_val), inline=True)
    e.add_field(name="Game mode", value=gm_val, inline=True)
    await interaction.response.send_message(embed=e)

@bot.tree.command(name="stats", description="Show your BuildNow stats")
async def stats_cmd(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    row = await db.get_by_discord(str(target.id))
    if not row: return await interaction.response.send_message(f"No data for {target.mention}. They need /register.")
    import json as _json
    pp = _json.loads(row["playtime_per_gm"] or "{}")
    try:
        sid = await get_active_session(str(target.id))
        st = await get_session_state(sid) if sid else None
        if st and st.get("me"):
            live_me = st.get("me")
            if live_me.get("username"):
                row["game_username"] = live_me.get("username")
            if live_me.get("skin"):
                row["skin"] = live_me.get("skin")
    except: pass
    thumb = fmt.skin_url(row.get("skin"))
    e = discord.Embed(title=f"Stats — {row['discord_username']}", color=0x06b6d4)
    if thumb: e.set_thumbnail(url=thumb)
    e.add_field(name="Kills", value=str(row["kills"]), inline=True)
    e.add_field(name="Deaths", value=str(row["deaths"]), inline=True)
    e.add_field(name="Wins", value=str(row["wins"]), inline=True)
    e.add_field(name="Defeats", value=str(row["defeats"]), inline=True)
    e.add_field(name="Playtime", value=fmt.format_playtime(row.get("playtime_global") or 0), inline=True)
    per = fmt.format_playtime_per_mode(pp)
    e.add_field(name="Per mode", value=per[:1024] or "—", inline=False)
    if row.get("game_username"):
        e.add_field(name="Game username", value=row["game_username"], inline=True)
    if row.get("game_mode"):
        e.add_field(name="Game mode", value=fmt.format_game_mode(row["game_mode"]), inline=True)
    await interaction.response.send_message(embed=e)

async def _match_logic(interaction: discord.Interaction):
    did = str(interaction.user.id)
    sids = await get_active_sessions(did, limit=5)
    if not sids:
        return await interaction.followup.send("No active match found. Start a game first.")
    # Fetch states for all candidates, prefer the one actually in a match room
    # (first-with-state could be a stale lobby while another session is live).
    states: list[tuple[str, dict]] = []
    for cand in sids:
        st = await get_session_state(cand)
        if st:
            states.append((cand, st))
    state = None
    sid = None
    for cand, st in states:
        rooms = st.get("rooms") or {}
        if any(rid != "lobby" and (rv.get("state") == "match") for rid, rv in rooms.items()):
            state, sid = st, cand
            break
    if state is None and states:
        sid, state = states[0]
    if not state:
        sid = sids[0]
        row = await db.get_by_discord(did)
        if row:
            try:
                events = await db.get_match_events(sid, did)
                hint = f"{len(events)} events" if events else "No events yet"
            except:
                hint = "No events yet"
            e = discord.Embed(title="Match in Progress", color=0x2b2d31, description="Connection lost — showing saved data.")
            thumb = fmt.skin_url(row.get("skin"))
            if thumb: e.set_thumbnail(url=thumb)
            e.add_field(name="Status", value=hint, inline=True)
            if row.get("skin"):
                e.add_field(name="Skin", value=fmt.skin_display(row.get("skin")), inline=True)
            e.set_footer(text="Try again in a moment")
            return await interaction.followup.send(embed=e)
        return await interaction.followup.send("No active match — start a game first.")

    me = state.get("me") or {}
    rooms = state.get("rooms") or {}
    # Determine active match room: prefer rooms where state == "match" and not lobby
    # Filter out lobby room, then pick match room with highest players_count or most recent
    non_lobby_rooms = {rid: rv for rid, rv in rooms.items() if rid != "lobby" and (rv.get("state") != "lobby" or rv.get("game_mode"))}
    match_rooms = {rid: rv for rid, rv in non_lobby_rooms.items() if rv.get("state") == "match"}
    # Choose display room: current room if it's a match, else best match_rooms, else first non-lobby
    display_room = None
    display_rid = None
    cur_id = state.get("current_room_id")
    if cur_id and cur_id in rooms and rooms[cur_id].get("state") == "match":
        display_room = rooms[cur_id]
        display_rid = cur_id
    elif match_rooms:
        # pick room with most players or newest (last in dict)
        display_rid, display_room = max(match_rooms.items(), key=lambda kv: (kv[1].get("players_count", 0), kv[1].get("match_version", 0)))
    elif non_lobby_rooms:
        display_rid, display_room = next(iter(non_lobby_rooms.items()))
    # Use display_room if exists, else fallback to top-level state (legacy)
    if display_room:
        players = display_room.get("players") or {}
        state_val = display_room.get("state", "lobby")
        gm = fmt.format_game_mode(display_room.get("game_mode") or state.get("game_mode"))
        cur_scn = (display_room.get("cur_scn") or state.get("cur_scn") or "?").replace("_", " ")
        zone = display_room.get("zone") or {}
        players_count = display_room.get("players_count", 0)
        max_players = display_room.get("max_players") or "?"
        party = display_room.get("party") or {}
        # keep session me skin
        skin = (me.get("skin") or display_room.get("me", {}).get("skin"))
    else:
        players = state.get("players") or {}
        state_val = state.get("state", "lobby")
        gm = fmt.format_game_mode(state.get("game_mode"))
        cur_scn = (state.get("cur_scn") or "?").replace("_", " ")
        zone = state.get("zone") or {}
        players_count = state.get("players_count", 0)
        max_players = state.get("max_players") or "?"
        party = state.get("party") or {}
        skin = me.get("skin")
    thumb = fmt.skin_url(skin)

    # Only show lobby if truly no match room exists and top-level is lobby
    if state_val == "lobby" and not match_rooms:
        e = discord.Embed(title="In Lobby", color=0x2b2d31)
        if thumb: e.set_thumbnail(url=thumb)
        e.add_field(name="Status", value="In Lobby", inline=True)
        e.add_field(name="Game mode", value=gm, inline=True)
        e.add_field(name="Scene", value=cur_scn, inline=True)
        if non_lobby_rooms: e.add_field(name="Matches in session", value=str(len(non_lobby_rooms)), inline=True)
        elif rooms: e.add_field(name="Matches in session", value=str(len(rooms)), inline=True)
        if skin: e.add_field(name="Skin", value=fmt.skin_display(skin), inline=True)
        if party.get("is_party"): e.add_field(name="Party", value="In Party", inline=True)
        return await interaction.followup.send(embed=e)

    alive_count = sum(1 for p in players.values() if p.get("is_alive", True))
    dead_count = len(players) - alive_count
    player_lines = []
    for p in sorted(players.values(), key=lambda x: (not x.get("is_alive", True), x.get("username", ""))):
        name = p.get("username", "?")
        alive = "alive" if p.get("is_alive", True) else "dead"
        is_self = " (you)" if p.get("is_self") else ""
        player_lines.append(f"{name} — {alive}{is_self}")
    kills_lines = "\n".join(player_lines[:12]) or "No players"

    e = discord.Embed(title="Current Match", color=0x7c3aed)
    if thumb: e.set_thumbnail(url=thumb)
    e.add_field(name="Game mode", value=gm, inline=True)
    e.add_field(name="Players", value=f"{players_count}/{max_players}", inline=True)
    e.add_field(name="Zone", value=fmt.format_zone(zone), inline=True)
    e.add_field(name="Alive / Dead", value=f"{alive_count} alive, {dead_count} dead", inline=True)
    e.add_field(name="Scene", value=cur_scn, inline=True)
    # Only count non-lobby matches in footer
    match_count = len(non_lobby_rooms) if non_lobby_rooms else (len(rooms) if rooms else 0)
    if match_count: e.add_field(name="Matches", value=f"{match_count} in session", inline=True)
    if display_rid and display_rid != "lobby":
        e.add_field(name="Match ID", value=f"`{display_rid[:8]}`", inline=True)
    if party.get("is_party"): e.add_field(name="Party", value="In Party", inline=True)
    if skin: e.add_field(name="Skin", value=fmt.skin_display(skin), inline=True)
    e.add_field(name="Player List", value=kills_lines[:1024], inline=False)
    await interaction.followup.send(embed=e)


@bot.tree.command(name="status", description="Show your status: in lobby or in match + game mode")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    did = str(interaction.user.id)
    sids = await get_active_sessions(did, limit=5)
    if not sids:
        return await interaction.followup.send("No linked session — use **/register**, then play a match to get your stats saved.")
    # Prefer the session with a live match room, else first with state
    best = None
    for cand in sids:
        st = await get_session_state(cand)
        if not st:
            continue
        rooms = st.get("rooms") or {}
        if any(rid != "lobby" and rv.get("state") == "match" for rid, rv in rooms.items()):
            best = (cand, st)
            break
        if best is None:
            best = (cand, st)
    if best is None:
        return await interaction.followup.send("Linked, but offline — start the game.")
    sid, state = best
    rooms = state.get("rooms") or {}
    match_rid, match_room = None, None
    for rid, rv in rooms.items():
        if rid != "lobby" and rv.get("state") == "match":
            match_rid, match_room = rid, rv
            break
    cur_id = state.get("current_room_id")
    if match_room is None and cur_id and cur_id in rooms:
        match_rid, match_room = cur_id, rooms[cur_id]
    in_match = bool(match_room and match_room.get("state") == "match")
    gm_raw = (match_room.get("game_mode") if match_room else None) or state.get("game_mode")
    gm = fmt.format_game_mode(gm_raw) if gm_raw else "—"
    e = discord.Embed(title="Status", color=0x10b981 if in_match else 0x2b2d31)
    e.add_field(name="Status", value="In Match" if in_match else "In Lobby", inline=True)
    e.add_field(name="Game mode", value=gm, inline=True)
    if in_match and match_room:
        e.add_field(name="Players", value=f"{match_room.get('players_count', '?')}/{match_room.get('max_players') or '?'}", inline=True)
        if match_rid and match_rid != "lobby":
            e.add_field(name="Match ID", value=f"`{match_rid[:8]}`", inline=True)
    await interaction.followup.send(embed=e)


@bot.tree.command(name="match", description="Show current match info")
async def match_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    await _match_logic(interaction)


@bot.tree.command(name="currentmatch", description="Show current match info (alias for /match)")
async def currentmatch_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    await _match_logic(interaction)

@bot.tree.command(name="skin", description="Show your or another user's current skin")
async def skin_cmd(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    tid = str(target.id)
    row = await db.get_by_discord(tid)
    skin = (row.get("skin") if row else None)

    # Fallback: try live engine state
    if not skin:
        sid = await get_active_session(tid)
        state = await get_session_state(sid) if sid else None
        if state:
            me = state.get("me") or {}
            skin = me.get("skin")
            if not skin:
                # Check other players in room
                for p in (state.get("players") or {}).values():
                    if p.get("username") == (row.get("game_username") if row else None):
                        skin = p.get("skin")
                        break

    if not skin:
        return await interaction.response.send_message(f"No skin found for {target.mention}. Play a match so it gets captured.", ephemeral=True)
    url = fmt.skin_url(skin)
    name = fmt.format_skin_name(skin)
    embed = discord.Embed(title=f"Skin — {target.display_name}", color=0x7c3aed, description=f"**{name}**")
    embed.set_image(url=url)
    embed.add_field(name="Preview", value=f"[Open]({url})", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="sessions", description="Browse your sessions and matches")
async def sessions_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    did = str(interaction.user.id)
    sessions = await db.get_recent_matches(did, limit=10)
    if not sessions:
        return await interaction.followup.send("No sessions yet. Play a match first.")
    embed = views.sessions_overview_embed(sessions)
    view = views.SessionSelectView(sessions, did)
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(name="recentmatches", description="Show your recent individual matches across sessions")
async def recentmatches_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    did = str(interaction.user.id)
    sessions = await db.get_recent_matches(did, limit=5)
    if not sessions:
        return await interaction.followup.send("No matches yet. Play a match first.")
    items: list[dict] = []
    for s in sessions[:3]:
        sid = s.get("session_id")
        if not sid:
            continue
        try:
            grouped = await db.get_match_events_grouped(sid, did)
        except Exception:
            continue
        for key, evs in grouped.items():
            if not evs:
                continue
            try:
                ts = max((ev.get("created_at") or "" for ev in evs))
            except Exception:
                ts = s.get("last_played") or s.get("created_at")
            gm = next(((ev.get("event_data") or {}).get("gm") for ev in evs if (ev.get("event_data") or {}).get("gm")), None)
            # room short: composite room_version keys keep room part + version
            room_short = key
            try:
                if "_" in key:
                    rp, mv = key.rsplit("_", 1)
                    room_short = rp[:8] + (f" v{mv}" if mv.isdigit() else "")
                else:
                    room_short = key[:8]
            except Exception:
                room_short = str(key)[:8]
            items.append({
                "session_id": sid,
                "session_short": str(sid)[:8],
                "room_short": room_short,
                "ts": ts,
                "gm": gm,
                "kills": sum(1 for ev in evs if ev.get("event_type") == "kill"),
                "deaths": sum(1 for ev in evs if ev.get("event_type") == "death"),
                "win": any(ev.get("event_type") == "win" for ev in evs),
                "defeat": any(ev.get("event_type") == "defeat" for ev in evs),
            })
    if not items:
        return await interaction.followup.send("No matches yet. Play a match first.")
    try:
        items.sort(key=lambda m: m.get("ts") or "", reverse=True)
    except Exception:
        pass
    await interaction.followup.send(embed=views.recent_matches_embed(items[:10]))


@bot.tree.command(name="replay", description="Show replay for a match")
async def replay_cmd(interaction: discord.Interaction, matchid: str):
    await interaction.response.defer(ephemeral=False)
    did = str(interaction.user.id)
    full_sid = await db.get_match_by_short_id(did, matchid)
    if not full_sid:
        return await interaction.followup.send(f"No match found for `{matchid}`. Try **/sessions**.", ephemeral=True)
    events = await db.get_match_events(full_sid, did)
    if not events:
        return await interaction.followup.send("No events for this match.")
    # Per-match grouping — don't merge all kills flat
    grouped = await db.get_match_events_grouped(full_sid, did)
    if len(grouped) > 1:
        lines: list[str] = []
        for idx, (rid, evs) in enumerate(grouped.items(), 1):
            gm = next((fmt.format_game_mode((ev.get("event_data") or {}).get("gm")) for ev in evs if (ev.get("event_data") or {}).get("gm")), f"Match {idx}")
            lines.append(f"**Match {idx} — {gm}**")
            lines.extend(views.replay_lines_for_events(evs))
        footer = f"{len(events)} events · {len(grouped)} matches"
    else:
        lines = views.replay_lines_for_events(events)
        footer = f"{len(events)} events"
    if not lines:
        return await interaction.followup.send("No replay data.")
    # Use paginated view if many lines
    if len(lines) <= 12:
        e = views.paged_embed("Replay", lines, 0x7C3AED, footer, page=0)
        await interaction.followup.send(embed=e)
    else:
        view = views.SimplePagedView("Replay", lines, 0x7C3AED, footer)
        await interaction.followup.send(embed=view.current_embed(), view=view)


@bot.tree.command(name="killfeed", description="Show eliminations for a match")
async def killfeed_cmd(interaction: discord.Interaction, matchid: str):
    await interaction.response.defer(ephemeral=False)
    did = str(interaction.user.id)
    full_sid = await db.get_match_by_short_id(did, matchid)
    if not full_sid:
        return await interaction.followup.send(f"No match found for `{matchid}`. Try **/sessions**.", ephemeral=True)
    events = await db.get_match_events(full_sid, did, event_types=["killfeed"])
    if not events:
        return await interaction.followup.send("No eliminations in this match.")
    grouped = await db.get_match_events_grouped(full_sid, did)
    # Only keep killfeed events grouped
    kill_groups = {rid: [ev for ev in evs if ev.get("event_type") == "killfeed"] for rid, evs in grouped.items()}
    # Filter empty
    kill_groups = {k: v for k, v in kill_groups.items() if v}
    if len(kill_groups) > 1:
        lines: list[str] = []
        for idx, (rid, evs) in enumerate(kill_groups.items(), 1):
            gm = next((fmt.format_game_mode((ev.get("event_data") or {}).get("gm")) for ev in evs if (ev.get("event_data") or {}).get("gm")), f"Match {idx}")
            lines.append(f"**Match {idx} — {gm}**")
            lines.extend(views.killfeed_lines_for_events(evs))
        footer = f"{len(events)} eliminations · {len(kill_groups)} matches"
    else:
        lines = views.killfeed_lines_for_events(events)
        footer = f"{len(lines)} eliminations"
    if not lines:
        return await interaction.followup.send("No eliminations.")
    if len(lines) <= 12:
        e = views.paged_embed("Killfeed", lines, 0xDC2626, footer, page=0)
        await interaction.followup.send(embed=e)
    else:
        view = views.SimplePagedView("Killfeed", lines, 0xDC2626, footer)
        await interaction.followup.send(embed=view.current_embed(), view=view)

@bot.tree.command(name="alerts", description="List recent security alerts (admin)")
@app_commands.describe(limit="How many alerts (1-20)")
@app_commands.default_permissions(administrator=True)
async def alerts_cmd(interaction: discord.Interaction, limit: int = 5):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Admin only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    rows = await alert_mod.fetch_unnotified(limit=50)  # reuse fetch, but also include notified
    # fetch recent regardless of notified
    try:
        if db.USE_SUPABASE and db.supabase:
            r = db.supabase.table("bngc_alerts").select("*").order("created_at", desc=True).limit(max(1, min(20, limit))).execute()
            rows = r.data or []
    except Exception:
        pass
    if not rows:
        return await interaction.followup.send("No alerts.", ephemeral=True)
    e = discord.Embed(title="Recent alerts", color=0x7C3AED)
    for a in rows[:limit]:
        t = a.get("alert_type", "?")
        who = f"<@{a.get('discord_id')}>"
        where = f"{a.get('country') or '?'}"  # country only — never show IPs
        e.add_field(name=f"#{a['id']} {t} — {fmt.fmt_date(a.get('created_at'))}", value=f"{who} {where}\n{(a.get('details') or {}).get('reason','')[:200]}", inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="set-lobby-music", description="Upload a looped MP3 for lobby (max 10MB)")
@app_commands.describe(file="MP3 file, max 10MB, will loop in lobby and stop in game")
async def set_lobby_music_cmd(interaction: discord.Interaction, file: discord.Attachment):
    did = str(interaction.user.id)
    row = await db.get_by_discord(did)
    if not row:
        return await interaction.response.send_message("Not registered. Use /register first.", ephemeral=True)
    fname = (file.filename or "").lower()
    if not fname.endswith(".mp3"):
        return await interaction.response.send_message("Only .mp3 files allowed.", ephemeral=True)
    if file.size and file.size > 10 * 1024 * 1024:
        return await interaction.response.send_message("File too large — max 10MB.", ephemeral=True)
    ctype = (file.content_type or "").lower()
    if ctype and "mpeg" not in ctype and "mp3" not in ctype and "audio" not in ctype:
        pass
    if not db.USE_SUPABASE or not db.supabase:
        return await interaction.response.send_message("Supabase not configured.", ephemeral=True)
    # loading state — non-ephemeral embed visible to channel
    loading = discord.Embed(
        title="🎵 Uploading lobby music…",
        description=f"Uploading `{file.filename}` ({(file.size or 0)/1024/1024:.2f}MB) — please wait…",
        color=0x7c3aed,
    )
    loading.add_field(name="Status", value="⏳ Uploading to storage…", inline=False)
    await interaction.response.send_message(embed=loading)
    try:
        data = await file.read()
    except Exception as exc:
        err = discord.Embed(title="❌ Read failed", description=str(exc)[:1000], color=0xef4444)
        return await interaction.edit_original_response(embed=err)
    if len(data) > 10 * 1024 * 1024:
        err = discord.Embed(title="❌ Too large", description="Max 10MB.", color=0xef4444)
        return await interaction.edit_original_response(embed=err)
    try:
        path = f"{did}.mp3"
        # show uploading -> processing state
        prog = discord.Embed(title="🎵 Processing…", description=f"`{file.filename}` received, saving…", color=0x7c3aed)
        prog.add_field(name="Status", value="⏳ Saving…", inline=False)
        await interaction.edit_original_response(embed=prog)
        # ensure bucket exists
        try:
            buckets = db.supabase.storage.list_buckets()
            bucket_ids = [b.id for b in buckets] if hasattr(buckets, '__iter__') else []
            if "lobby-music" not in bucket_ids:
                db.supabase.storage.create_bucket("lobby-music", options={"public": True})
        except Exception as bucket_exc:
            print(f"bucket check/create failed: {bucket_exc}")

        try:
            db.supabase.storage.from_("lobby-music").remove([path])
        except Exception:
            pass
        upload_ok = False
        try:
            db.supabase.storage.from_("lobby-music").upload(path, data, {"content-type": "audio/mpeg", "upsert": "true"})
            upload_ok = True
        except TypeError:
            db.supabase.storage.from_("lobby-music").upload(path, data, file_options={"content-type": "audio/mpeg", "upsert": True})
            upload_ok = True
        except Exception as upload_exc:
            print(f"storage upload failed for {did}: {upload_exc}")

        if not upload_ok:
            raise Exception("File upload to Supabase storage failed. Check bucket 'lobby-music' exists and is public.")

        pub = db.supabase.storage.from_("lobby-music").get_public_url(path)
        if isinstance(pub, str):
            url = pub
        elif isinstance(pub, dict):
            url = pub.get("publicUrl") or pub.get("public_url") or pub.get("data", {}).get("publicUrl") or str(pub)
        else:
            url = str(pub)
        if url and not url.startswith("http"):
            base = os.getenv("SUPABASE_URL", "").rstrip("/")
            if base:
                url = f"{base}/storage/v1/object/public/lobby-music/{path}"
        db.supabase.table("bngc_users").update({"lobby_music_url": url}).eq("discord_id", did).execute()
        done = discord.Embed(
            title="✅ Lobby music set!",
            description="Will **loop in lobby** and **stop in game**. Adjust volume with the slider in the drawer.",
            color=0x10b981,
        )
        done.add_field(name="File", value=f"`{file.filename}` • {len(data)/1024/1024:.2f}MB", inline=False)
        await interaction.edit_original_response(embed=done)
    except Exception as exc:
        print(f"lobby music upload failed for {did}: {exc}")
        err = discord.Embed(title="❌ Upload failed", description=str(exc)[:1000], color=0xef4444)
        await interaction.edit_original_response(embed=err)


@bot.tree.command(name="remove-lobby-music", description="Remove your lobby music")
async def remove_lobby_music_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    did = str(interaction.user.id)
    if not db.USE_SUPABASE or not db.supabase:
        return await interaction.followup.send("Supabase not configured.", ephemeral=True)
    try:
        path = f"{did}.mp3"
        try:
            db.supabase.storage.from_("lobby-music").remove([path])
        except Exception:
            pass
        db.supabase.table("bngc_users").update({"lobby_music_url": None}).eq("discord_id", did).execute()
        await interaction.followup.send("🗑️ Lobby music removed.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"Failed: {exc}", ephemeral=True)

async def _fetch_health():
    """Fetch GET /api/health from the API (same base as session state)."""
    url = f"{SERVER_URL}/api/health"
    try:
        if aiohttp:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        return await r.json()
        else:
            import urllib.request
            with urllib.request.urlopen(url, timeout=8) as r:
                return json.loads(r.read().decode())
    except Exception as e:
        print(f"[health] fetch failed: {e}")
    return None

@bot.tree.command(name="precision", description="Show server-side processing precision (kills/deaths/wins/defeats)")
async def precision_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    data = await _fetch_health()
    if not data or not isinstance(data.get("precision"), dict):
        return await interaction.followup.send("Precision data unavailable — API unreachable.")
    p = data["precision"]
    pct = p.get("precision_pct", 0)
    verified = p.get("verified", 0)
    e = discord.Embed(title="Server Precision", color=0x10b981,
                      description=f"**{pct}%** · {verified} verified")
    for action in ("kill", "death", "win", "defeat"):
        pa = (p.get("per_action") or {}).get(action) or {}
        e.add_field(name=action.capitalize(),
                    value=f"{pa.get('pct', 0)}% ({pa.get('ok', 0)} ok / {pa.get('fail', 0)} failed)",
                    inline=True)
    e.add_field(name="Pending", value=str(p.get("pending", 0)), inline=True)
    e.add_field(name="Skipped", value=str(p.get("skipped", 0)), inline=True)
    await interaction.followup.send(embed=e)


@bot.tree.command(name="leaderboard", description="Top players by kills or wins")
@app_commands.describe(stat="Stat to rank by", limit="How many players (1-25)")
@app_commands.choices(stat=[
    app_commands.Choice(name="Kills", value="kills"),
    app_commands.Choice(name="Wins", value="wins"),
])
async def leaderboard_cmd(interaction: discord.Interaction, stat: str = "kills", limit: int = 10):
    await interaction.response.defer(ephemeral=False)
    rows = await db.get_leaderboard(stat if stat in ("kills", "wins") else "kills", limit)
    if not rows:
        return await interaction.followup.send("No players yet. Play a match first.")
    title = f"Leaderboard — {'Kills' if stat == 'kills' else 'Wins'}"
    e = discord.Embed(title=title, color=0x7c3aed)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows, 1):
        name = r.get("discord_username") or r.get("game_username") or "?"
        kills = r.get("kills") or 0
        deaths = r.get("deaths") or 0
        wins = r.get("wins") or 0
        defeats = r.get("defeats") or 0
        if stat == "wins":
            total = wins + defeats
            wr = f"{(wins / total * 100):.1f}% WR" if total else "—"
            val = f"{wins} wins ({wr})"
        else:
            kd = f"{(kills / deaths):.2f} K/D" if deaths else (f"{kills} K/D" if kills else "—")
            val = f"{kills} kills ({kd})"
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{prefix} **{name}** — {val}")
    e.description = "\n".join(lines)[:4000]
    await interaction.followup.send(embed=e)


async def _post_patch_channel(embed, view=None):
    """Best-effort: mirror patch notes to the updates channel. Never raises."""
    try:
        channel = bot.get_channel(PATCH_CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(PATCH_CHANNEL_ID)
            except Exception:
                return False
        if view is not None:
            await channel.send(embed=embed, view=view)
        else:
            await channel.send(embed=embed)
        return True
    except Exception as e:
        print(f"[announce] channel post failed: {e}")
        return False


async def _all_registered_discord_ids():
    """Every registered discord_id (DM targets for update broadcasts)."""
    if db.USE_SUPABASE and db.supabase:
        try:
            r = db.supabase.table("bngc_users").select("discord_id").limit(10000).execute()
            return [str(x.get("discord_id")) for x in (r.data or []) if x.get("discord_id")]
        except Exception as e:
            print(f"[announce] supabase ids failed: {e}")
            return []
    try:
        import aiosqlite
        async with aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute("SELECT discord_id FROM users") as cur:
                return [str(row[0]) for row in await cur.fetchall() if row[0]]
    except Exception as e:
        print(f"[announce] sqlite ids failed: {e}")
        return []


@bot.tree.command(name="announce-update", description="DM all registered players about a new update (admin)")
@app_commands.describe(version="Update version, e.g. 2.14.3",
                       notes="Patch notes (shown in the DM)",
                       link="Userscript install link (default: current raw file)",
                       force="Resend even if this version was already announced")
@app_commands.default_permissions(administrator=True)
async def announce_update_cmd(interaction: discord.Interaction,
                              version: app_commands.Range[str, 1, 32],
                              notes: app_commands.Range[str, 1, 1000],
                              link: str | None = None,
                              force: bool = False):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Administrator only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    version = version.strip()
    announced = load_announced()
    if version in announced and not force:
        prev = announced[version]
        return await interaction.followup.send(
            f"Version **{version}** was already announced ({prev.get('sent', 0)} sent). "
            f"Use `force:True` to resend.", ephemeral=True)
    url = (link or "").strip() or USERSCRIPT_URL
    targets = await _all_registered_discord_ids()
    if not targets:
        return await interaction.followup.send("No registered players found.", ephemeral=True)
    embed = discord.Embed(title=f"🚀 Nectar update {version} is out",
                          description=notes[:1000], color=0x7c3aed)
    embed.add_field(name="Reinstall", value=f"[Click here to reinstall]({url})", inline=False)
    embed.set_footer(text="Open the link in Tampermonkey → Reinstall, then reload the game page.")
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Reinstall userscript", url=url))
    sent, failed = 0, 0
    for did in targets:
        try:
            user = await bot.fetch_user(int(did))
            await user.send(embed=embed, view=view)
            sent += 1
        except discord.Forbidden:
            failed += 1  # DMs closed — skip silently
        except Exception as e:
            print(f"[announce] DM failed for {did}: {e}")
            failed += 1
        try:
            await asyncio.sleep(0.6)  # stay under DM rate limits
        except: pass
    announced[version] = {"at": datetime.now(timezone.utc).isoformat(),
                          "by": str(interaction.user.id), "sent": sent, "failed": failed}
    save_announced(announced)
    posted = await _post_patch_channel(embed, view)
    await interaction.followup.send(
        f"Update **{version}** announced: **{sent}** DMs sent, **{failed}** failed (DMs closed)."
        + (" Posted to patch channel." if posted else " Patch-channel post failed — check channel access."),
        ephemeral=True)


# ---------------------------------------------------------------------------
# Ranked v1 — host-authored rooms (/ranked create/start/end/...)
# v2.19.0 ranked sleep: commands stay registered but reply asleep unless
# RANKED_ENABLED=1. Canonical API: project-nectar.onrender.com primary,
# api-compbng.vercel.app fallback (see docs/api.md).
# ---------------------------------------------------------------------------

RANKED_ENABLED_BOT = os.getenv("RANKED_ENABLED", "1") == "1"

ranked_group = app_commands.Group(name="ranked", description="Ranked rooms")


def _ranked_lobby_embed(room: dict) -> discord.Embed:
    spec = ranked_mod.lobby_embed(room)
    e = discord.Embed(title=spec["title"], color=0x7c3aed)
    for name, value, inline in spec["fields"]:
        e.add_field(name=name, value=value, inline=inline)
    e.set_footer(text=spec["footer"])
    return e


class RankedLobbyView(discord.ui.View):
    """Join/Leave buttons for a ranked lobby (timeout=None while alive)."""

    def __init__(self, rid: str):
        super().__init__(timeout=None)
        self.rid = rid

    async def _refresh_embed(self, interaction: discord.Interaction):
        try:
            room = ranked_mod.get_room(self.rid)
            if room and interaction.message:
                await interaction.message.edit(embed=_ranked_lobby_embed(room), view=self)
        except Exception:
            pass

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="ranked-join")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not RANKED_ENABLED_BOT:
            return await interaction.followup.send("Ranked is sleeping in v2.19.0 — streak rewards are live.", ephemeral=True)
        did = str(interaction.user.id)
        try:
            row = await db.get_by_discord(did)
        except Exception:
            row = None
        if not row:
            return await interaction.followup.send("Registered players only — use **/register** first.", ephemeral=True)
        room = ranked_mod.get_room(self.rid)
        if not room or room.get("status") != "lobby":
            return await interaction.followup.send("This lobby is no longer open.", ephemeral=True)
        if ranked_mod.is_banned(did):
            return await interaction.followup.send("You are banned from ranked.", ephemeral=True)
        pts = ranked_mod.get_points(did)
        if pts < (room.get("min_points") or 0):
            return await interaction.followup.send(f"Needs **{room['min_points']}+** points (you have {pts}).", ephemeral=True)
        if room.get("max_points") is not None and pts > room["max_points"]:
            return await interaction.followup.send(f"Capped at **{room['max_points']}** points (you have {pts}).", ephemeral=True)
        room = ranked_mod.get_room(self.rid)  # refresh: capacity check on live roster
        if len(ranked_mod.roster_ids(room)) >= (room.get("max_players") or 16):
            return await interaction.followup.send("Lobby is full.", ephemeral=True)
        ranked_mod.roster_add(room, did)
        await self._refresh_embed(interaction)
        await interaction.followup.send(f"You're in **{self.rid}**. Wait for start — the code is DM'd at start.", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.danger, custom_id="ranked-leave")
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        did = str(interaction.user.id)
        room = ranked_mod.get_room(self.rid)
        if not room or room.get("status") != "lobby":
            return await interaction.followup.send("This lobby is no longer open.", ephemeral=True)
        if did == str(room.get("host_discord_id")):
            return await interaction.followup.send("Host can't leave — use **/ranked change-host** or **/ranked end**.", ephemeral=True)
        ranked_mod.roster_remove(room, did)
        await self._refresh_embed(interaction)
        await interaction.followup.send(f"You left **{self.rid}**.", ephemeral=True)


@ranked_group.command(name="create", description="Host a ranked room")
@app_commands.describe(short_code="5-char room code from game",
                       max_players="Roster cap",
                       max_matches="Matches per session",
                       min_points="Minimum points to join, 0 = open",
                       max_points="Maximum points to join, 0 = no cap",
                       fee="Join fee in points (burned)",
                       starts_in_min="Auto-start countdown in minutes, 0 = manual start",
                       host_status="Whether the host plays or just hosts")
@app_commands.choices(gm=[app_commands.Choice(name="Late Game Zonewars", value="zw_lg"),
                           app_commands.Choice(name="Free Mode", value="df"),
                           app_commands.Choice(name="Wild West", value="wwzw")],
                      host_status=[app_commands.Choice(name="Playing", value="is_host_playing"),
                                   app_commands.Choice(name="Hosting Only", value="hosting_only")])
async def ranked_create_cmd(interaction: discord.Interaction, short_code: str, max_players: app_commands.Range[int, 2, 16],
                            gm: str, max_matches: app_commands.Range[int, 1, 50],
                            min_points: int = 0, max_points: int = 0,
                            fee: int = 0, starts_in_min: int = 0,
                            host_status: str = "is_host_playing"):
    if not await _defer(interaction):
        return
    if not RANKED_ENABLED_BOT:
        return await interaction.followup.send("Ranked is sleeping in v2.19.0 — streak rewards are live. Back soon.", ephemeral=True)
    did = str(interaction.user.id)
    try:
        row = await db.get_by_discord(did)
    except Exception:
        row = None
    if not row:
        return await interaction.followup.send("Registered players only — use **/register** first.", ephemeral=True)
    if ranked_mod.is_banned(did):
        return await interaction.followup.send("You are banned from ranked.", ephemeral=True)
    code = (short_code or "").strip().upper()
    if len(code) != 5 or not code.isalnum():
        return await interaction.followup.send("Short code must be 5 letters/digits (the code players type in game).", ephemeral=True)
    if gm not in ranked_mod.ALLOWED_GMS:
        return await interaction.followup.send("Mode must be zw_lg, df or wwzw.", ephemeral=True)
    if ranked_mod.get_open_room_by_code(code):
        return await interaction.followup.send(f"Code **{code}** already has an open ranked room.", ephemeral=True)
    starts_at = None
    if starts_in_min and starts_in_min > 0:
        starts_at = (datetime.now(timezone.utc) + timedelta(minutes=min(starts_in_min, 180))).isoformat()
    capped_max = max_points if max_points and max_points > 0 else None
    try:
        room = ranked_mod.create_room(code=code, host_discord_id=did, gm=gm,
                                      max_players=max_players, max_matches=max_matches,
                                      min_points=min_points or 0, max_points=capped_max,
                                      fee=fee or 0, starts_at=starts_at)
        hs = host_status if host_status in ("is_host_playing", "hosting_only") else "is_host_playing"
        if hs != "is_host_playing":
            ranked_mod.update_room(room["id"], {"host_status": hs})
            room = ranked_mod.get_room(room["id"])
    except Exception as exc:
        print(f"[ranked] create failed: {exc}")
        return await interaction.followup.send("Couldn't create the room (DB error).", ephemeral=True)
    print(f"[ranked] room created {room['id']} code={code} gm={gm} host={did} max={max_players}x{max_matches}")
    # post lobby embed (configured channel, else current channel)
    target = None
    try:
        cid = ranked_mod.CREATE_CHANNEL_ID or (interaction.channel_id)
        if cid:
            try:
                target = interaction.guild.get_channel(cid) if interaction.guild else None
            except Exception:
                target = None
            if target is None:
                try:
                    target = await bot.fetch_channel(cid)
                except Exception:
                    target = None
        if target is None:
            target = interaction.channel
    except Exception:
        target = interaction.channel
    ping = f"<@&{ranked_mod.RANKED_PING_ROLE_ID}>" if ranked_mod.RANKED_PING_ROLE_ID else ""
    try:
        msg = await target.send(content=ping or None, embed=_ranked_lobby_embed(room), view=RankedLobbyView(room["id"]))
        try:
            ranked_mod.update_room(room["id"], {"lobby_channel_id": str(getattr(target, 'id', '')), "lobby_message_id": str(msg.id)})
        except Exception:
            pass
    except Exception as exc:
        print(f"[ranked] lobby post failed: {exc}")
        return await interaction.followup.send(f"Room **{room['id']}** created, but I couldn't post the lobby — check my channel access.", ephemeral=True)
    await interaction.followup.send(f"Ranked room **{room['id']}** open — lobby posted. You (host) are on the roster.", ephemeral=True)


bot.tree.add_command(ranked_group)


async def _verify_host_present(code: str, game_username: str | None):
    """Ask the API if the room code is live and the host is in it.

    Returns (True/False/None, detail). None = API unreachable or no name to
    check (fail-open: start proceeds with a logged warning, since a fake room
    seals nothing without real host packets).
    """
    if not game_username:
        return None, "no game username to check"
    if aiohttp is None:
        return None, "http client unavailable"
    try:
        from urllib.parse import quote
        url = f"{SERVER_URL}/api/ranked/verify?code={quote(code)}&host={quote(game_username)}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=10) as r:
                if r.status != 200:
                    return None, f"api {r.status}"
                j = await r.json()
        if not j.get("room_active"):
            return False, "room not active in game"
        if not j.get("host_in_room"):
            return False, "host not in room"
        return True, f"{j.get('events_seen', 0)} recent events"
    except Exception as e:
        print(f"[ranked] verify failed: {e}")
        return None, "api unreachable"


async def _post_thread_leaderboard(thread, rid: str):
    """Top-10 arena standings in the session thread. Best-effort."""
    try:
        top = ranked_mod.top_points(10)
        if not top:
            return
        lines = []
        for i, (did, pts) in enumerate(top, start=1):
            tier, _f = ranked_mod.milestone_for(pts)
            try:
                row = await db.get_by_discord(did)
                nm = (row or {}).get("game_username") or (row or {}).get("discord_username") or did
            except Exception:
                nm = did
            lines.append(f"**{i}.** {nm} — {pts} pts ({tier})")
        e = discord.Embed(title="Arena leaderboard", color=0x7c3aed,
                          description="\n".join(lines))
        e.set_footer(text=f"{rid} · live standings")
        await thread.send(embed=e)
    except Exception as e:
        print(f"[ranked] thread leaderboard failed: {e}")


async def _set_lobby_embed(room: dict, spec: dict, color: int, buttons: bool = False):
    """Edit the lobby message to a spec embed. Best-effort."""
    try:
        e = discord.Embed(title=spec["title"], color=color)
        for name, value, inline in spec["fields"]:
            e.add_field(name=name, value=value, inline=inline)
        e.set_footer(text=spec["footer"])
        if room.get("lobby_channel_id") and room.get("lobby_message_id"):
            ch = bot.get_channel(int(room["lobby_channel_id"])) or await bot.fetch_channel(int(room["lobby_channel_id"]))
            msg = await ch.fetch_message(int(room["lobby_message_id"]))
            await msg.edit(embed=e, view=RankedLobbyView(room["id"]) if buttons else None)
    except Exception as e:
        print(f"[ranked] lobby flip failed: {e}")


async def _post_thread(room: dict, content=None, embed=None):
    """Post into the session thread. Returns True on success."""
    try:
        tid = room.get("thread_id")
        if not tid:
            return False
        th = bot.get_channel(int(tid))
        if th is None:
            th = await bot.fetch_channel(int(tid))
        await th.send(content=content, embed=embed)
        return True
    except Exception as e:
        print(f"[ranked] thread post failed: {e}")
        return False


def _ord(n) -> str:
    """1 -> 1st, 2 -> 2nd, 3 -> 3rd, 11-13 -> th. Never raises."""
    try:
        n = int(n)
        if 11 <= (n % 100) <= 13:
            return f"{n}th"
        return f"{n}{({1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th'))}"
    except Exception:
        return str(n)


async def _display_name(did: str) -> str:
    """Game username > discord username > raw did. Never raises."""
    try:
        row = await db.get_by_discord(str(did))
        return (row or {}).get("game_username") or (row or {}).get("discord_username") or str(did)
    except Exception:
        return str(did)


async def _post_match_board(room: dict, seal: dict):
    """Match-only leaderboard in the session thread: winner + per-player
    placement/kills/points for THIS sealed match (not global standings).
    Best-effort, never raises."""
    try:
        v = seal.get("match_version")
        pts = seal.get("points") or {}
        kills = seal.get("kills") or {}
        plc = seal.get("placements") or {}
        if not isinstance(pts, dict):
            pts = {}
        winner = str(seal.get("winner_discord_id") or "")
        wnm = await _display_name(winner) if winner else "—"
        # order: placement asc, then points desc, then did
        def _key(did):
            try:
                p = plc.get(did)
                p = int(p) if p is not None else 99
            except Exception:
                p = 99
            try:
                g = -int(pts.get(did) or 0)
            except Exception:
                g = 0
            return (p, g, str(did))
        lines = []
        for did in sorted(pts.keys(), key=_key):
            try:
                g = int(pts.get(did) or 0)
            except Exception:
                g = 0
            try:
                p = plc.get(did)
                ptxt = _ord(p) if p is not None else "—"
            except Exception:
                ptxt = "—"
            try:
                k = int((kills or {}).get(did) or 0)
            except Exception:
                k = 0
            nm = await _display_name(did)
            crown = " 👑" if winner and str(did) == winner else ""
            lines.append(f"**{ptxt}** {nm} — {'+' if g >= 0 else ''}{g} pts ({k} kill{'s' if k != 1 else ''}){crown}")
        e = discord.Embed(title=f"Match v{v} sealed — {ranked_mod.gm_name(seal.get('gm') or room.get('gm'))}",
                          color=0x10b981,
                          description=f"Winner **{wnm}**\n" + ("\n".join(lines) or "No points awarded."))
        e.set_footer(text=f"{room['id']} · match-only results")
        await _post_thread(room, embed=e)
        print(f"[ranked] match board posted {room['id']} v{v}")
    except Exception as e:
        print(f"[ranked] match board failed: {e}")


async def _close_thread(room: dict) -> bool:
    """Close (archive+lock) then delete the session thread. The session is
    over — nothing should linger. True when the thread is gone (or was
    already gone); False only when Discord refused. Never raises."""
    try:
        tid = room.get("thread_id")
        if not tid:
            return True
        try:
            th = bot.get_channel(int(tid))
            if th is None:
                th = await bot.fetch_channel(int(tid))
        except discord.errors.NotFound:
            return True  # already gone
        except Exception as e:
            print(f"[ranked] thread close fetch failed: {e}")
            return False
        try:
            await th.edit(archived=True, locked=True)
        except discord.errors.NotFound:
            return True
        except Exception as e:
            print(f"[ranked] thread archive failed (continuing to delete): {e}")
        try:
            await th.delete()
        except discord.errors.NotFound:
            return True
        except Exception as e:
            print(f"[ranked] thread delete failed: {e}")
            return False
        print(f"[ranked] thread closed+deleted {room['id']} tid={tid}")
        return True
    except Exception as e:
        print(f"[ranked] thread close failed: {e}")
        return False


async def _defer(interaction: discord.Interaction) -> bool:
    """Defer an interaction. False = interaction already dead (starved loop),
    caller must abort silently (there is nobody left to answer)."""
    try:
        await interaction.response.defer(ephemeral=True)
        return True
    except discord.errors.NotFound:
        print(f"[ranked] interaction expired before defer (loop starved?)")
        return False
    except Exception as e:
        print(f"[ranked] defer failed: {e}")
        return False


def _host_or_admin(interaction: discord.Interaction, room: dict) -> bool:
    did = str(interaction.user.id)
    if did == str(room.get("host_discord_id")):
        return True
    try:
        return bool(interaction.guild and interaction.user.guild_permissions.administrator)
    except Exception:
        return False


async def _resolve_room(interaction: discord.Interaction, rid: str = None):
    """Resolve a ranked room from multiple sources. Returns (room, error_msg).
    Resolution order:
      1. If rid starts with 'R-' → R-ID lookup
      2. If rid is 5 chars → short_code lookup (open rooms)
      3. If rid is None → thread context → user's active room
    Returns (room_dict, None) on success, (None, error_string) on failure.
    """
    did = str(interaction.user.id)
    # 1. Explicit R-ID
    if rid and rid.strip().upper().startswith("R-"):
        room = ranked_mod.get_room(rid.strip().upper())
        if not room:
            return None, f"No such ranked room: `{rid.strip().upper()}`"
        return room, None
    # 2. Explicit short code (5 chars)
    if rid and len(rid.strip()) == 5 and rid.strip().isalnum():
        room = ranked_mod.get_open_room_by_code(rid.strip())
        if not room:
            return None, f"No open ranked room with code **{rid.strip().upper()}**"
        # Check if user is on roster
        ids = ranked_mod.roster_ids(room)
        if did not in ids and not (interaction.guild and interaction.user.guild_permissions.administrator):
            return None, f"You're not on the roster for **{room.get('short_code', '?')}**"
        return room, None
    # 3. Auto-detect: thread context
    try:
        ch = interaction.channel
        if ch and hasattr(ch, 'id'):
            room = ranked_mod.get_room_by_thread_id(str(ch.id))
            if room:
                return room, None
    except Exception:
        pass
    # 4. Auto-detect: user's active room
    try:
        room = ranked_mod.get_user_active_room(did)
        if room:
            return room, None
    except Exception:
        pass
    # 5. Nothing found
    if rid:
        return None, f"Couldn't find room for `{rid}`. Try the 5-char game code or a room R-ID."
    return None, "No active ranked room found. Run this in a ranked thread, or pass the game code (e.g. ABC12) or R-ID."


async def _start_ranked_room(room: dict, via: str = "manual"):
    """Shared manual/countdown start. Returns (ok, message). Steps: eligibility
    re-check (drop banned/under-lock), host-in-room verify, fee pass (drop +
    DM the broke), thread + code/rules post, code DMs (incl. host), go live."""
    room = ranked_mod.get_room(room["id"])
    if not room or room.get("status") != "lobby":
        return False, "Room is no longer in lobby."
    print(f"[ranked] start {room['id']} via={via} code={room.get('short_code')}")
    roster = ranked_mod.roster_ids(room)
    # eligibility re-check: bans / points locks can change after joining.
    # Batched reads (one roundtrip) — per-player reads starve the event loop
    # past the 3s interaction window (see _defer).
    banned = ranked_mod.banned_many(roster)
    pts_map = ranked_mod.get_points_many(roster)
    for did in list(roster):
        reason = None
        if did in banned:
            reason = "you are banned from ranked"
        else:
            pts = pts_map.get(did, 0)
            if pts < (room.get("min_points") or 0):
                reason = f"needs **{room['min_points']}+** points (you have {pts})"
            elif room.get("max_points") is not None and pts > room["max_points"]:
                reason = f"capped at **{room['max_points']}** points (you have {pts})"
        if reason:
            ranked_mod.roster_remove(ranked_mod.get_room(room["id"]), did)
            try:
                user = await bot.fetch_user(int(did))
                await user.send(f"Removed from **{room['id']}** before start: {reason}.")
            except Exception:
                pass
    # host-in-room verify (fail-open with warning — a fake room seals nothing)
    host_status = "unverified"
    try:
        hrow = await db.get_by_discord(str(room.get("host_discord_id")))
        hname = (hrow or {}).get("game_username")
    except Exception:
        hname = None
    verified, detail = await _verify_host_present(str(room.get("short_code") or ""), hname)
    if verified is True:
        host_status = "in room ✓"
    elif verified is False:
        print(f"[ranked] start {room['id']} host NOT verified: {detail}")
        return False, f"Can't start — host isn't in the room ({detail}). Join the room first, then start."
    else:
        print(f"[ranked] start {room['id']} host verify skipped: {detail}")
        host_status = "unverified"
    room = ranked_mod.get_room(room["id"])
    roster = ranked_mod.roster_ids(room)  # refresh: eligibility may have dropped players
    # fee pass (no thread yet — aborts cleanly if roster collapses)
    dropped = []
    pts_map = ranked_mod.get_points_many(roster)
    for did in list(roster):
        pts = pts_map.get(did, 0)
        fee = ranked_mod.entry_fee(room, pts)
        if fee > 0 and pts < fee:
            ranked_mod.roster_remove(ranked_mod.get_room(room["id"]), did)
            dropped.append(did)
            try:
                user = await bot.fetch_user(int(did))
                await user.send(f"Dropped from **{room['id']}**: need **{fee}** points to enter (you have {pts}).")
            except Exception:
                pass
        elif fee > 0:
            try:
                ranked_mod.add_points(did, -fee)
            except Exception as e:
                print(f"[ranked] fee charge failed for {did}: {e}")
    room = ranked_mod.get_room(room["id"])
    roster = ranked_mod.roster_ids(room)
    if len(roster) < 2:
        return False, "Need at least 2 players to start."
    # thread (private, public fallback when the server isn't boosted)
    parent = None
    try:
        cid = ranked_mod.MATCHES_CHANNEL_ID or ranked_mod.CREATE_CHANNEL_ID
        if cid:
            try:
                parent = bot.get_channel(cid)
            except Exception:
                parent = None
            if parent is None:
                parent = await bot.fetch_channel(cid)
    except Exception as e:
        print(f"[ranked] parent channel failed: {e}")
    if parent is None:
        return False, "Matches channel not reachable — check my access."
    try:
        thread = await parent.create_thread(name=f"Ranked {room['id']} {ranked_mod.gm_name(room.get('gm'))}",
                                            type=discord.ChannelType.private_thread)
        private = True
    except Exception:
        try:
            thread = await parent.create_thread(name=f"Ranked {room['id']} {ranked_mod.gm_name(room.get('gm'))}",
                                                type=discord.ChannelType.public_thread)
            private = False
        except Exception as e:
            print(f"[ranked] thread create failed: {e}")
            return False, "Couldn't create the session thread — check my thread perms."
    code = room.get("short_code")
    if private:
        for did in roster:
            try:
                member = parent.guild.get_member(int(did)) if hasattr(parent, "guild") and parent.guild else None
                if member is None and hasattr(parent, "guild") and parent.guild:
                    member = await parent.guild.fetch_member(int(did))
                if member is not None:
                    await thread.add_user(member)
            except Exception:
                pass
    code_embed = discord.Embed(title=f"Room code: {code}", color=0x10b981,
                               description=f"Mode **{ranked_mod.gm_name(room.get('gm'))}** · first to finish {room.get('max_matches')} matches")
    code_embed.add_field(name="Rules", value=ranked_mod.RULES_TEXT, inline=False)
    code_embed.set_footer(text=f"{room['id']} · started {via}")
    try:
        await thread.send(embed=code_embed)
        await _post_thread_leaderboard(thread, room["id"])
    except Exception as e:
        print(f"[ranked] thread post failed: {e}")
    for did in roster:
        try:
            user = await bot.fetch_user(int(did))
            tag = " (host)" if did == str(room.get("host_discord_id")) else ""
            await user.send(f"**{room['id']}** is live{tag} — code: **{code}** ({ranked_mod.gm_name(room.get('gm'))}). Join in game now.")
        except Exception:
            pass
    print(f"[ranked] room live {room['id']} players={len(roster)} via={via} host={host_status}")
    ranked_mod.update_room(room["id"], {"status": "live", "thread_id": str(thread.id), "host_status": host_status})
    # flip lobby embed
    await _set_lobby_embed(room, ranked_mod.live_embed(room, thread.id, host_status), 0x10b981)
    return True, f"**{room['id']}** live with {len(roster)} players ({'countdown' if via == 'countdown' else 'manual'} start)."


@ranked_group.command(name="start", description="Start your ranked room (host)")
@app_commands.describe(rid="Room code (ABC12), R-ID, or leave blank to auto-detect")
async def ranked_start_cmd(interaction: discord.Interaction, rid: str = None):
    if not await _defer(interaction):
        return
    room, err = await _resolve_room(interaction, rid)
    if not room:
        return await interaction.followup.send(err, ephemeral=True)
    did = str(interaction.user.id)
    is_admin = bool(interaction.user.guild_permissions.administrator) if interaction.guild else False
    if did != str(room.get("host_discord_id")) and not is_admin:
        return await interaction.followup.send("Only the host (or an admin) can start.", ephemeral=True)
    ok, msg = await _start_ranked_room(room, via="manual")
    await interaction.followup.send(msg, ephemeral=True)


@ranked_group.command(name="end", description="End the ranked session (host)")
@app_commands.describe(rid="Room code (ABC12), R-ID, or leave blank to auto-detect")
async def ranked_end_cmd(interaction: discord.Interaction, rid: str = None):
    if not await _defer(interaction):
        return
    room, err = await _resolve_room(interaction, rid)
    if not room:
        return await interaction.followup.send(err, ephemeral=True)
    if not _host_or_admin(interaction, room):
        return await interaction.followup.send("Only the host (or an admin) can end.", ephemeral=True)
    if room.get("status") == "ended":
        return await interaction.followup.send("Session already ended.", ephemeral=True)
    print(f"[ranked] end {room['id']} by={interaction.user.id}")
    ranked_mod.update_room(room["id"], {"status": "ended"})
    room = ranked_mod.get_room(room["id"])
    sealed = ranked_mod.sealed_count(room["id"])
    await _post_thread(room, embed=discord.Embed(
        title="Session ended", color=0xef4444,
        description=f"**{room['id']}** closed with {sealed}/{room.get('max_matches')} matches sealed."))
    await _set_lobby_embed(room, ranked_mod.ended_embed(room), 0xef4444)
    # session over: close (archive+lock) then delete the private thread
    closed = await _close_thread(room)
    if closed:
        _thread_cleaned.add(room["id"])
    tail = " Thread closed and deleted." if closed else " Couldn't delete the thread — check my thread perms."
    await interaction.followup.send(f"**{room['id']}** ended ({sealed} matches sealed).{tail}", ephemeral=True)


@ranked_group.command(name="backout", description="Back out to lobby, session continues (host)")
@app_commands.describe(rid="Room code (ABC12), R-ID, or leave blank to auto-detect", reason="Why (shown to players)")
async def ranked_backout_cmd(interaction: discord.Interaction, rid: str = None, reason: str = ""):
    if not await _defer(interaction):
        return
    room, err = await _resolve_room(interaction, rid)
    if not room:
        return await interaction.followup.send(err, ephemeral=True)
    if not _host_or_admin(interaction, room):
        return await interaction.followup.send("Only the host (or an admin) can back out.", ephemeral=True)
    if room.get("status") != "live":
        return await interaction.followup.send("Only a live room can back out (this one isn't live).", ephemeral=True)
    print(f"[ranked] backout {room['id']} by={interaction.user.id} reason={reason!r}")
    ranked_mod.update_room(room["id"], {"status": "lobby"})
    room = ranked_mod.get_room(room["id"])
    sealed = ranked_mod.sealed_count(room["id"])
    note = f" — {reason}" if reason else ""
    await _post_thread(room, embed=discord.Embed(
        title="Backed out to lobby", color=0xf59e0b,
        description=f"Host called a backout{note}. Sealed matches stand ({sealed}). Rejoin the lobby — the count stays where it was."))
    for did in ranked_mod.roster_ids(room):
        try:
            user = await bot.fetch_user(int(did))
            await user.send(f"**{room['id']}** backed out to lobby{note}. Sealed matches stand — rejoin when the host restarts.")
        except Exception:
            pass
    await _set_lobby_embed(room, ranked_mod.lobby_embed(room), 0x7c3aed, buttons=True)
    await interaction.followup.send(f"**{room['id']}** back to lobby — session continues ({sealed} sealed). Sealing is paused until you run `/ranked start` again.", ephemeral=True)


@ranked_group.command(name="restart", description="Void the current match and start a fresh one (host)")
@app_commands.describe(rid="Room code (ABC12), R-ID, or leave blank to auto-detect", reason="Why (shown to players)")
async def ranked_restart_cmd(interaction: discord.Interaction, rid: str = None, reason: str = ""):
    if not await _defer(interaction):
        return
    room, err = await _resolve_room(interaction, rid)
    if not room:
        return await interaction.followup.send(err, ephemeral=True)
    if not _host_or_admin(interaction, room):
        return await interaction.followup.send("Only the host (or an admin) can restart.", ephemeral=True)
    if room.get("status") != "live":
        return await interaction.followup.send("Only a live room can restart (this one isn't live — use `/ranked start`).", ephemeral=True)
    if room.get("status") == "ended":
        return await interaction.followup.send("Session ended — can't restart.", ephemeral=True)
    print(f"[ranked] restart {room['id']} by={interaction.user.id} reason={reason!r}")
    sealed = ranked_mod.sealed_count(room["id"])
    note = f" — {reason}" if reason else ""
    # back to lobby first (sealed matches stand; the in-progress match is void)
    ranked_mod.update_room(room["id"], {"status": "lobby"})
    room = ranked_mod.get_room(room["id"])
    await _post_thread(room, embed=discord.Embed(
        title="Match restarted", color=0xf59e0b,
        description=f"Host restarted the session{note}. The in-progress match is void — sealed matches stand ({sealed}). Starting fresh…"))
    # fresh start re-runs eligibility + fees (entry is charged again)
    ok, msg = await _start_ranked_room(room, via="restart")
    if not ok:
        await _set_lobby_embed(room, ranked_mod.lobby_embed(ranked_mod.get_room(room["id"])), 0x7c3aed, buttons=True)
        return await interaction.followup.send(f"Backed out to lobby, but the fresh start failed: {msg} Run `/ranked start` when ready.", ephemeral=True)
    await interaction.followup.send(f"**{room['id']}** restarted — {msg} (Sealed matches stand; entry fees were charged again.)", ephemeral=True)


@ranked_group.command(name="change-code", description="Change the room code (host)")
@app_commands.describe(rid="Room code (ABC12), R-ID, or leave blank to auto-detect", short_code="New 5-char room code from game")
async def ranked_chcode_cmd(interaction: discord.Interaction, short_code: str, rid: str = None):
    if not await _defer(interaction):
        return
    room, err = await _resolve_room(interaction, rid)
    if not room:
        return await interaction.followup.send(err, ephemeral=True)
    if not _host_or_admin(interaction, room):
        return await interaction.followup.send("Only the host (or an admin) can change the code.", ephemeral=True)
    if room.get("status") == "ended":
        return await interaction.followup.send("Session ended — code can't change.", ephemeral=True)
    code = (short_code or "").strip().upper()
    if len(code) != 5 or not code.isalnum():
        return await interaction.followup.send("Code must be 5 letters/digits.", ephemeral=True)
    if code != room.get("short_code") and ranked_mod.get_open_room_by_code(code):
        return await interaction.followup.send(f"Code **{code}** already has an open ranked room.", ephemeral=True)
    print(f"[ranked] change-code {room['id']} {room.get('short_code')}->{code} by={interaction.user.id}")
    ranked_mod.update_room(room["id"], {"short_code": code})
    room = ranked_mod.get_room(room["id"])
    await _post_thread(room, embed=discord.Embed(
        title="Room code changed", color=0xf59e0b,
        description=f"New code: **{code}**. Host — join the new room, sealing follows your packets."))
    if room.get("status") == "lobby":
        await _set_lobby_embed(room, ranked_mod.lobby_embed(room), 0x7c3aed, buttons=True)
    await interaction.followup.send(f"**{room['id']}** code is now **{code}**.", ephemeral=True)


@ranked_group.command(name="change-host", description="Transfer host to another player (host)")
@app_commands.describe(rid="Room code (ABC12), R-ID, or leave blank to auto-detect", member="New host")
async def ranked_chhost_cmd(interaction: discord.Interaction, member: discord.Member, rid: str = None):
    if not await _defer(interaction):
        return
    room, err = await _resolve_room(interaction, rid)
    if not room:
        return await interaction.followup.send(err, ephemeral=True)
    if not _host_or_admin(interaction, room):
        return await interaction.followup.send("Only the host (or an admin) can transfer.", ephemeral=True)
    if room.get("status") == "ended":
        return await interaction.followup.send("Session ended — host can't change.", ephemeral=True)
    ndid = str(member.id)
    try:
        row = await db.get_by_discord(ndid)
    except Exception:
        row = None
    if not row:
        return await interaction.followup.send(f"{member.mention} isn't registered.", ephemeral=True)
    if ranked_mod.is_banned(ndid):
        return await interaction.followup.send(f"{member.mention} is banned from ranked.", ephemeral=True)
    print(f"[ranked] change-host {room['id']} {room.get('host_discord_id')}->{ndid} by={interaction.user.id}")
    ranked_mod.update_room(room["id"], {"host_discord_id": ndid})
    room = ranked_mod.get_room(room["id"])
    ranked_mod.roster_add(room, ndid)
    room = ranked_mod.get_room(room["id"])
    await _post_thread(room, content=f"Host transferred to {member.mention} — sealing now follows their packets.")
    if room.get("status") == "lobby":
        await _set_lobby_embed(room, ranked_mod.lobby_embed(room), 0x7c3aed, buttons=True)
    await interaction.followup.send(f"**{room['id']}** host is now {member.mention}.", ephemeral=True)


@ranked_group.command(name="roster", description="Show the room roster with points")
@app_commands.describe(rid="Room code (ABC12), R-ID, or leave blank to auto-detect")
async def ranked_roster_cmd(interaction: discord.Interaction, rid: str = None):
    if not await _defer(interaction):
        return
    room, err = await _resolve_room(interaction, rid)
    if not room:
        return await interaction.followup.send(err, ephemeral=True)
    lines = []
    pts_map = ranked_mod.get_points_many(ranked_mod.roster_ids(room))
    for did in ranked_mod.roster_ids(room):
        pts = pts_map.get(did, 0)
        tier, _f = ranked_mod.milestone_for(pts)
        tag = " 👑" if did == str(room.get("host_discord_id")) else ""
        try:
            row = await db.get_by_discord(did)
            nm = (row or {}).get("game_username") or (row or {}).get("discord_username") or did
        except Exception:
            nm = did
        lines.append(f"<@{did}> {nm} — {pts} pts ({tier}){tag}")
    e = discord.Embed(title=f"Roster {room['id']} ({len(lines)}/{room.get('max_players')})",
                      color=0x7c3aed, description="\n".join(lines) or "Empty.")
    e.set_footer(text=f"{room['id']} · {room.get('status')}")
    await interaction.followup.send(embed=e, ephemeral=True)


@ranked_group.command(name="leaderboard", description="Session standings: points earned per player")
@app_commands.describe(rid="Room code (ABC12), R-ID, or leave blank to auto-detect")
async def ranked_leaderboard_cmd(interaction: discord.Interaction, rid: str = None):
    if not await _defer(interaction):
        return
    room, err = await _resolve_room(interaction, rid)
    if not room:
        return await interaction.followup.send(err, ephemeral=True)
    board, sealed = ranked_mod.session_standings(room["id"])
    if not board:
        return await interaction.followup.send(
            f"**{room['id']}** — no sealed matches yet ({sealed}/{room.get('max_matches')} sealed).", ephemeral=True)
    lines = []
    for i, (did, (gained, wins, played)) in enumerate(board, start=1):
        nm = await _display_name(did)
        tag = " 👑" if did == str(room.get("host_discord_id")) else ""
        lines.append(f"**{i}.** {nm} — {'+' if gained >= 0 else ''}{gained} pts ({wins}W · {played} match{'es' if played != 1 else ''}){tag}")
    e = discord.Embed(title=f"Session standings {room['id']}", color=0x7c3aed, description="\n".join(lines))
    e.set_footer(text=f"{room['id']} · {sealed}/{room.get('max_matches')} sealed · {room.get('status')}")
    await interaction.followup.send(embed=e, ephemeral=True)


@ranked_group.command(name="ban", description="Ban a player from ranked (admin)")
@app_commands.describe(member="Player to ban", reason="Why")
async def ranked_ban_cmd(interaction: discord.Interaction, member: discord.Member, reason: str = ""):
    if not await _defer(interaction):
        return
    if not (interaction.guild and interaction.user.guild_permissions.administrator):
        return await interaction.followup.send("Admins only.", ephemeral=True)
    ok = ranked_mod.ban_player(str(member.id), reason, str(interaction.user.id))
    print(f"[ranked] ban {member.id} reason={reason!r} by={interaction.user.id} ok={ok}")
    await interaction.followup.send(f"{member.mention} banned from ranked." if ok else "Ban failed (DB error).", ephemeral=True)


@ranked_group.command(name="unban", description="Unban a player from ranked (admin)")
@app_commands.describe(member="Player to unban")
async def ranked_unban_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _defer(interaction):
        return
    if not (interaction.guild and interaction.user.guild_permissions.administrator):
        return await interaction.followup.send("Admins only.", ephemeral=True)
    ok = ranked_mod.unban_player(str(member.id))
    print(f"[ranked] unban {member.id} by={interaction.user.id} ok={ok}")
    await interaction.followup.send(f"{member.mention} unbanned." if ok else "Unban failed (DB error).", ephemeral=True)


@ranked_group.command(name="host-status", description="Toggle host playing or hosting-only (host)")
@app_commands.describe(rid="Room code (ABC12), R-ID, or leave blank to auto-detect")
async def ranked_host_status_cmd(interaction: discord.Interaction, rid: str = None):
    if not await _defer(interaction):
        return
    room, err = await _resolve_room(interaction, rid)
    if not room:
        return await interaction.followup.send(err, ephemeral=True)
    if not _host_or_admin(interaction, room):
        return await interaction.followup.send("Only the host (or an admin) can change host status.", ephemeral=True)
    if room.get("status") == "ended":
        return await interaction.followup.send("Session ended — can't change host status.", ephemeral=True)
    current = (room.get("host_status") or "").strip()
    if current == "hosting_only":
        new_status = "is_host_playing"
    else:
        new_status = "hosting_only"
    ranked_mod.update_room(room["id"], {"host_status": new_status})
    room = ranked_mod.get_room(room["id"])
    print(f"[ranked] host-status {room['id']} -> {new_status} by={interaction.user.id}")
    if room.get("status") == "lobby":
        await _set_lobby_embed(room, ranked_mod.lobby_embed(room), 0x7c3aed, buttons=True)
    elif room.get("status") == "live":
        await _set_lobby_embed(room, ranked_mod.live_embed(room, room.get("thread_id")), 0x10b981)
    label = "Hosting only — your stats won't count" if new_status == "hosting_only" else "Playing — your stats count normally"
    await interaction.followup.send(f"Host status: **{label}**.", ephemeral=True)


@ranked_group.command(name="review", description="Review a ranked room: status, seals, results")
@app_commands.describe(rid="Room code (ABC12), R-ID, or leave blank to auto-detect")
async def ranked_review_cmd(interaction: discord.Interaction, rid: str = None):
    if not await _defer(interaction):
        return
    room, err = await _resolve_room(interaction, rid)
    if not room:
        return await interaction.followup.send(err, ephemeral=True)
    sealed = ranked_mod.sealed_count(room["id"])
    lines = [f"Status **{room.get('status')}** · code **{room.get('short_code')}** · {ranked_mod.gm_name(room.get('gm'))}",
             f"Host <@{room.get('host_discord_id')}> · {len(ranked_mod.roster_ids(room))}/{room.get('max_players')} players",
             f"Sealed **{sealed}/{room.get('max_matches')}** · fee {room.get('fee') or 0} · lock {room.get('min_points') or 0}"]
    try:
        r = ranked_mod._sb().table("ranked_results").select(
            "match_version,winner_discord_id,kills,points").eq("room_id", room["id"]).order("match_version").limit(50).execute()
        for row in (r.data or []):
            try:
                wrow = await db.get_by_discord(str(row.get("winner_discord_id") or ""))
                wnm = (wrow or {}).get("game_username") or row.get("winner_discord_id") or "—"
            except Exception:
                wnm = row.get("winner_discord_id") or "—"
            pts = row.get("points") or {}
            tot = sum(int(v or 0) for v in pts.values()) if isinstance(pts, dict) else 0
            lines.append(f"v{row.get('match_version')}: winner **{wnm}** · {tot} pts awarded")
    except Exception as e:
        lines.append(f"(results lookup failed: {e})")
    e = discord.Embed(title=f"Review {room['id']}", color=0x7c3aed, description="\n".join(lines))
    await interaction.followup.send(embed=e, ephemeral=True)


# Seal-watch + auto-end state (module level; the loop owns them).
_seal_seen: dict = {}      # rid -> max match_version already posted to the thread
_live_known: set = set()   # rids seen live (detects server-side ended flips)
_pending_end: set = set()  # ex-live rids awaiting an ended verdict
_thread_cleaned: set = set()  # rids whose thread is gone


async def _finalize_ended_room(room: dict, via: str):
    """A room the server ended (max matches sealed): final embed, lobby flip,
    thread close+delete. Idempotent via _thread_cleaned. Never raises."""
    try:
        rid = room["id"]
        if rid in _thread_cleaned:
            return
        sealed = ranked_mod.sealed_count(rid)
        await _post_thread(room, embed=discord.Embed(
            title="Session ended", color=0xef4444,
            description=f"**{rid}** closed with {sealed}/{room.get('max_matches')} matches sealed ({via})."))
        await _set_lobby_embed(room, ranked_mod.ended_embed(room), 0xef4444)
        if await _close_thread(room):
            _thread_cleaned.add(rid)
    except Exception as e:
        print(f"[ranked] auto-end finalize failed: {e}")


async def ranked_loop():
    """Every 30s: auto-start countdown lobbies; post per-match boards for new
    seals in live rooms; finalize rooms the server ended (max matches)."""
    await bot.wait_until_ready()
    # seed: pre-existing seals were (presumably) posted before this boot —
    # only future seals trigger posts. Logged so a gap is visible.
    try:
        for room in await asyncio.to_thread(ranked_mod.list_live_rooms):
            try:
                seals = await asyncio.to_thread(ranked_mod.room_seals, room["id"])
                if seals:
                    _seal_seen[room["id"]] = max(int(s.get("match_version") or 0) for s in seals)
                _live_known.add(room["id"])
            except Exception:
                continue
        if _seal_seen:
            print(f"[ranked] seal-watch seeded (skipping already-sealed): {_seal_seen}")
    except Exception as e:
        print(f"[ranked] seal-watch seed failed: {e}")
    while not bot.is_closed():
        try:
            for room in ranked_mod.list_due_starts():
                try:
                    ok, msg = await _start_ranked_room(room, via="countdown")
                    print(f"[ranked] countdown start {room.get('id')}: {msg}")
                except Exception as e:
                    print(f"[ranked] countdown failed for {room.get('id')}: {e}")
                await asyncio.sleep(2)  # let interactions interleave between rooms
            # seal-watch: new sealed versions -> match-only board in the thread
            try:
                live = await asyncio.to_thread(ranked_mod.list_live_rooms)
            except Exception as e:
                print(f"[ranked] live list failed: {e}")
                live = []
            live_ids = set()
            for room in live:
                try:
                    rid = room["id"]
                    live_ids.add(rid)
                    _live_known.add(rid)
                    seals = await asyncio.to_thread(ranked_mod.room_seals, rid)
                    known = _seal_seen.get(rid, 0)
                    fresh = sorted(
                        [s for s in seals if int(s.get("match_version") or 0) > known],
                        key=lambda s: int(s.get("match_version") or 0))
                    for seal in fresh:
                        try:
                            await _post_match_board(room, seal)
                        finally:
                            # mark seen even when the post failed: no 30s retry
                            # spam, the next seal carries the session forward
                            # (and /ranked review|leaderboard show it all)
                            _seal_seen[rid] = max(_seal_seen.get(rid, 0),
                                                  int(seal.get("match_version") or 0))
                except Exception as e:
                    print(f"[ranked] seal-watch failed for {room.get('id')}: {e}")
                await asyncio.sleep(1)  # let interactions interleave between rooms
            # ex-live rooms: server-side ended flip (max matches) -> finalize;
            # backout-to-lobby just drops out of the pending set (thread stays).
            for rid in _live_known - live_ids:
                _pending_end.add(rid)
            _live_known.intersection_update(live_ids)
            for rid in list(_pending_end):
                try:
                    room = await asyncio.to_thread(ranked_mod.get_room, rid)
                    if not room or room.get("status") != "ended":
                        _pending_end.discard(rid)
                        continue
                    if rid in _thread_cleaned:
                        _pending_end.discard(rid)
                        continue
                    print(f"[ranked] auto-end finalize {rid}")
                    await _finalize_ended_room(room, "max matches reached")
                    if rid in _thread_cleaned:
                        _pending_end.discard(rid)
                except Exception as e:
                    print(f"[ranked] auto-end check failed for {rid}: {e}")
        except Exception as e:
            print(f"[ranked] loop failed: {e}")
        await asyncio.sleep(30)


if __name__ == "__main__":
    if not TOKEN:
        print("Set DISCORD_TOKEN env")
        raise SystemExit(1)
    import asyncio as _aio
    _aio.run(db.init_db())
    bot.run(TOKEN)
