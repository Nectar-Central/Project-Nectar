import os, secrets, asyncio, discord, json
from datetime import datetime, timezone
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


def load_warnings():
    try:
        with WARNINGS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_warnings():
    WARNINGS_FILE.write_text(json.dumps(warnings, indent=2), encoding="utf-8")


warnings = load_warnings()


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
        await interaction.user.send(f"Your BuildNow login token is **{token}**\nEnter it in the userscript prompt (once, 5 min expiry). Keep it secret!")
    except discord.Forbidden:
        pass
    message = f"Token sent to your DMs: **{token}**\nEnter it in the game iframe prompt on first run. One-time use."
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
    message = "Something went wrong while running that command. Please try again."
    if isinstance(error, app_commands.MissingPermissions):
        message = "You do not have permission to use that command."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


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

if __name__ == "__main__":
    if not TOKEN:
        print("Set DISCORD_TOKEN env")
        raise SystemExit(1)
    import asyncio as _aio
    _aio.run(db.init_db())
    bot.run(TOKEN)
