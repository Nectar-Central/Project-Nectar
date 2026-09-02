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
    """Get multiple recent active sessions ordered by created_at desc."""
    if not db.USE_SUPABASE or not db.supabase:
        return []
    try:
        r = db.supabase.table("bngc_sessions").select("session_id, created_at").eq("discord_id", discord_id).eq("is_active", True).order("created_at", desc=True).limit(limit).execute()
        return [x.get("session_id") for x in (r.data or []) if x.get("session_id")]
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


@bot.tree.command(name="register", description="Get a login token and optionally publish your game profile")
@app_commands.describe(game_handle="Optional in-game handle for the Nectar dashboard")
async def register_cmd(interaction: discord.Interaction, game_handle: app_commands.Range[str, 1, 32] | None = None):
    await interaction.response.defer(ephemeral=True)
    token = gen_token()
    try:
        for _ in range(5):
            if not await db.get_login_token(token) and not await db.get_by_token(token):
                break
            token = gen_token()
        row = await db.get_by_discord(str(interaction.user.id))
        if not row:
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

@bot.tree.command(name="unlock", description="Unlock your account if it was locked for security")
async def unlock_cmd(interaction: discord.Interaction):
    did = str(interaction.user.id)
    row = await db.get_by_discord(did)
    if not row:
        return await interaction.response.send_message("No account found. Use /register.", ephemeral=True)
    locked = row.get("is_locked") if isinstance(row.get("is_locked"), bool) else bool(row.get("is_locked"))
    # supabase stores boolean, sqlite int
    if not locked:
        # also check via helper for supabase sync
        try:
            if await db.is_locked(did) is False:
                return await interaction.response.send_message("Your account is not locked.", ephemeral=True)
        except: pass
        if not locked:
            return await interaction.response.send_message("Your account is not locked.", ephemeral=True)
    await db.unlock_user(did)
    # DM confirmation
    try:
        await interaction.user.send("🔓 Your BuildNow account has been unlocked. You can continue playing or use /register for a new token.")
    except: pass
    await interaction.response.send_message("🔓 Account unlocked. You can continue now.", ephemeral=True)
    # also clear Vercel lock if using Supabase directly
    try:
        import os as _os
        from supabase import create_client as _cc
        url=_os.getenv("SUPABASE_URL"); key=_os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if url and key:
            _cc(url,key).table("bngc_users").update({"is_locked": False, "lock_reason": None}).eq("discord_id", did).execute()
    except: pass

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
    state = None
    sid = None
    for cand in sids:
        st = await get_session_state(cand)
        if st:
            state = st
            sid = cand
            break
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

    if state_val == "lobby":
        e = discord.Embed(title="In Lobby", color=0x2b2d31)
        if thumb: e.set_thumbnail(url=thumb)
        e.add_field(name="Status", value="In Lobby", inline=True)
        e.add_field(name="Game mode", value=gm, inline=True)
        e.add_field(name="Scene", value=cur_scn, inline=True)
        if rooms: e.add_field(name="Matches in session", value=str(len(rooms)), inline=True)
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
    if rooms: e.add_field(name="Matches", value=f"{len(rooms)} in session", inline=True)
    if party.get("is_party"): e.add_field(name="Party", value="In Party", inline=True)
    if skin: e.add_field(name="Skin", value=fmt.skin_display(skin), inline=True)
    e.add_field(name="Players", value=kills_lines[:1024], inline=False)
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


@bot.tree.command(name="matches", description="List your recent matches")
async def matches_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    did = str(interaction.user.id)
    matches = await db.get_recent_matches(did, limit=10)
    if not matches:
        return await interaction.followup.send("No matches found. Play a game first so events get logged.")

    e = discord.Embed(title="Recent Matches", color=0x06b6d4)
    for i, m in enumerate(matches[:10]):
        sid_short = m["session_id"][:12]
        ts = fmt.fmt_date(m.get("created_at"))
        gms_raw = m.get("game_modes", [])[:2]
        gms = ", ".join(fmt.format_game_mode(g) for g in gms_raw) or "Unknown"
        stats = f"K:{m['kills']} D:{m['deaths']} W:{m['wins']} L:{m['defeats']}"
        value = f"`{sid_short}` — {gms}\n{stats}"
        e.add_field(name=f"{i+1}. {ts}", value=value, inline=False)

    e.set_footer(text="Tip: use /sessions for a guided picker, or /replay <id> /killfeed <id>")
    await interaction.followup.send(embed=e)


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


@bot.tree.command(name="replay", description="Show replay for a match")
async def replay_cmd(interaction: discord.Interaction, matchid: str):
    await interaction.response.defer(ephemeral=False)
    did = str(interaction.user.id)
    full_sid = await db.get_match_by_short_id(did, matchid)
    if not full_sid:
        return await interaction.followup.send(f"No match found for `{matchid}`. Try /sessions or /matches.", ephemeral=True)
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
        return await interaction.followup.send(f"No match found for `{matchid}`. Try /sessions or /matches.", ephemeral=True)
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

if __name__ == "__main__":
    if not TOKEN:
        print("Set DISCORD_TOKEN env")
        raise SystemExit(1)
    import asyncio as _aio
    _aio.run(db.init_db())
    bot.run(TOKEN)
