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
SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:3000")

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


async def _seed_game_username(discord_id: str, handle: str) -> bool:
    """Seed bngc_users.game_username from the /register handle when empty.

    Gives every linked account an identity baseline so kills attribute even
    when join packets never resolve view.me (the null-identity class).
    Never overwrites a tracked name. Never raises (returns bool).
    """
    try:
        clean = str(handle or "").strip()[:32]
        if not clean or not db.USE_SUPABASE or not db.supabase:
            return False
        row = await db.get_by_discord(str(discord_id))
        if row and (row.get("game_username") or "").strip():
            return False
        db.supabase.table("bngc_users").update(
            {"game_username": clean}).eq("discord_id", str(discord_id)).execute()
        return True
    except Exception as e:
        print(f"[register] game_username seed failed: {e}")
        return False


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

async def get_insights(discord_id: str):
    """Fetch computed insights via GET /api/insights?discord_id=. None on error."""
    if not discord_id:
        return None
    from urllib.parse import quote
    url = f"{SERVER_URL}/api/insights?discord_id={quote(str(discord_id))}"
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
        print(f"[insights] fetch failed for {str(discord_id)[:6]}: {e}")
    return None


def _fmt_session(m):
    """One-line summary for a grouped match. Never raises."""
    try:
        if not m:
            return "—"
        k = m.get("kills", 0)
        d = m.get("deaths", 0)
        try:
            gm = fmt.format_game_mode(m.get("gm"))
        except Exception:
            gm = m.get("gm") or "?"
        res = "W" if m.get("win") else ("L" if m.get("defeat") else "–")
        return f"{k}K/{d}D {res} ({gm})"
    except Exception:
        return "—"


async def _api_get(path: str):
    """GET SERVER_URL + path. Returns parsed JSON or None. Never raises."""
    url = f"{SERVER_URL}{path}"
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
        print(f"[api] GET {path} failed: {e}")
    return None


async def _api_post(path: str, payload: dict):
    """POST JSON to SERVER_URL + path. Returns (status, parsed body). Never raises."""
    import json as _json
    url = f"{SERVER_URL}{path}"
    try:
        if aiohttp:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    try:
                        body = await r.json()
                    except Exception:
                        body = {}
                    return r.status, body
        else:
            import urllib.request
            import urllib.error as _uerr
            req = urllib.request.Request(url, data=_json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    return r.status, _json.loads(r.read().decode() or "{}")
            except _uerr.HTTPError as he:
                try:
                    return he.code, _json.loads(he.read().decode() or "{}")
                except Exception:
                    return he.code, {}
    except Exception as e:
        print(f"[api] POST {path} failed: {e}")
    return 0, {}

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
    # persistent comp players button (restart-safe, fixed custom_id)
    try:
        bot.add_view(_CompPlayersView())
    except Exception as e:
        print(f"[comp] persistent view failed: {e}")
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
            seeded_handle = False
            if game_handle:
                clean_handle = str(game_handle).strip()
                try:
                    display_name = getattr(interaction.user, "global_name", None) or interaction.user.display_name
                    await asyncio.to_thread(save_registered_player, interaction.user.id, interaction.user.name, display_name, clean_handle, interaction.user.display_avatar.url)
                except Exception as exc:
                    profile_error = exc
                    print(f"Player profile save failed for {interaction.user.id}: {exc}")
                try:
                    seeded_handle = await _seed_game_username(str(interaction.user.id), clean_handle)
                except Exception:
                    seeded_handle = False
            msg = f"You are already registered. Use **/refresh-token** to get a new login token.\n"
            msg += f"**{total_sessions} sessions · {total_matches} matches · last played {last_played} ({last_sid_short})**"
            if game_handle and profile_error is None:
                msg += f"\nDashboard profile updated for **{game_handle}**."
            elif game_handle:
                msg += "\nI could not save the dashboard profile yet; run the Supabase schema and try again."
            if seeded_handle:
                msg += "\nGame name saved for stat tracking."
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
    seeded_handle = False
    if game_handle:
        clean_handle = str(game_handle).strip()
        try:
            display_name = getattr(interaction.user, "global_name", None) or interaction.user.display_name
            await asyncio.to_thread(save_registered_player, interaction.user.id, interaction.user.name, display_name, clean_handle, interaction.user.display_avatar.url)
        except Exception as exc:
            profile_error = exc
            print(f"Player profile save failed for {interaction.user.id}: {exc}")
        try:
            seeded_handle = await _seed_game_username(str(interaction.user.id), clean_handle)
        except Exception:
            seeded_handle = False

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
    if seeded_handle:
        message += "\nGame name saved for stat tracking."
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

@bot.tree.command(name="precision", description="Show server-side processing precision (kills/deaths/wins)")
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


@bot.tree.command(name="insights", description="Show your gameplay insights")
async def insights_cmd(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    await interaction.response.defer(ephemeral=False)
    ins = await get_insights(str(target.id))
    if not ins:
        return await interaction.followup.send(f"No insights for {target.mention} yet. Play a few matches first.")
    e = discord.Embed(title=f"Insights — {(await db.get_by_discord(str(target.id)) or {}).get('discord_username') or target.name}",
                      color=0x7c3aed,
                      description=f"Playstyle: **{ins.get('playstyle') or '—'}** · {ins.get('matches_analyzed', 0)} matches")
    e.add_field(name="🔥 Best session", value=_fmt_session(ins.get("best_session")), inline=True)
    e.add_field(name="🧊 Worst session", value=_fmt_session(ins.get("worst_session")), inline=True)
    fear = ins.get("biggest_fear") or {}
    farm = ins.get("biggest_free_kill") or {}
    e.add_field(name="😱 Biggest fear", value=f"{fear.get('player', '—')} ({fear.get('deaths', 0)} deaths)" if fear.get("player") else "—", inline=True)
    e.add_field(name="🎯 Biggest free kill", value=f"{farm.get('player', '—')} ({farm.get('kills', 0)} kills)" if farm.get("player") else "—", inline=True)
    e.add_field(name="🎮 Fav mode", value=str(ins.get("fav_game_mode") or "—"), inline=True)
    e.add_field(name="🧤 Clutch wins (≤1 kill)", value=str(ins.get("clutch_factor", 0)), inline=True)
    await interaction.followup.send(embed=e)


@bot.tree.command(name="form", description="Show recent match form (last 5)")
async def form_cmd(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    await interaction.response.defer(ephemeral=False)
    ins = await get_insights(str(target.id))
    if not ins or not ins.get("recent_form"):
        return await interaction.followup.send(f"No recent matches for {target.mention}.")
    lines = [f"{i}. {_fmt_session(m)}" for i, m in enumerate(ins["recent_form"][:5], 1)]
    e = discord.Embed(title=f"Form — {target.name} (last {len(lines)})", color=0x06b6d4,
                      description="\n".join(lines)[:4000])
    await interaction.followup.send(embed=e)


@bot.tree.command(name="nemesis", description="Show who kills you the most")
async def nemesis_cmd(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    await interaction.response.defer(ephemeral=False)
    ins = await get_insights(str(target.id))
    fear = (ins or {}).get("biggest_fear") or {}
    if not fear.get("player"):
        return await interaction.followup.send(f"No nemesis for {target.mention} yet.")
    await interaction.followup.send(f"😱 {target.mention}'s biggest fear: **{fear['player']}** ({fear.get('deaths', 0)} deaths)")


@bot.tree.command(name="farm", description="Show who you kill the most")
async def farm_cmd(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    await interaction.response.defer(ephemeral=False)
    ins = await get_insights(str(target.id))
    farm = (ins or {}).get("biggest_free_kill") or {}
    if not farm.get("player"):
        return await interaction.followup.send(f"No free kills for {target.mention} yet.")
    await interaction.followup.send(f"🎯 {target.mention}'s biggest free kill: **{farm['player']}** ({farm.get('kills', 0)} kills)")


@bot.tree.command(name="synergy", description="Show your best teammate (duo/team modes)")
async def synergy_cmd(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    await interaction.response.defer(ephemeral=False)
    ins = await get_insights(str(target.id))
    mate = (ins or {}).get("fav_teammate") or {}
    syn = (ins or {}).get("duo_synergy") or {}
    if not mate.get("player"):
        return await interaction.followup.send(
            f"No best teammate for {target.mention} yet — play duo/team modes together first.")
    wr = syn.get("win_rate")
    wr_txt = f"{round(wr * 100)}% joint win" if wr is not None else "—"
    await interaction.followup.send(
        f"🤝 {target.mention}'s best teammate: **{mate['player']}** "
        f"({mate.get('shared', 0)} shared, {mate.get('joint_wins', 0)} wins together, {wr_txt})")


def _cmp_line(row):
    """Compact stat line for /compare. Never raises."""
    try:
        k = int(row.get("kills") or 0)
        d = int(row.get("deaths") or 0)
        w = int(row.get("wins") or 0)
        kd = f"{(k / d):.2f}" if d else str(k)
        pt = fmt.format_playtime(row.get("playtime_global") or 0)
        pp = int(row.get("play_points") or 0)
        return f"{k}K / {d}D ({kd} K/D) · {w}W · {pp} pts · {pt}"
    except Exception:
        return "—"


@bot.tree.command(name="compare", description="Compare two players side by side")
async def compare_cmd(interaction: discord.Interaction, a: discord.User, b: discord.User):
    await interaction.response.defer(ephemeral=False)
    ra = await db.get_by_discord(str(a.id))
    rb = await db.get_by_discord(str(b.id))
    missing = [u.mention for u, r in ((a, ra), (b, rb)) if not r]
    if missing:
        return await interaction.followup.send(f"No data for {' '.join(missing)}. They need /register.")
    e = discord.Embed(title="Compare", color=0x7c3aed)
    e.add_field(name=(ra.get("discord_username") or a.name)[:256], value=_cmp_line(ra), inline=False)
    e.add_field(name=(rb.get("discord_username") or b.name)[:256], value=_cmp_line(rb), inline=False)
    await interaction.followup.send(embed=e)


@bot.tree.command(name="friends", description="Show your friends and pending requests")
async def friends_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    from urllib.parse import quote
    data = await _api_get(f"/api/friends/list?discord_id={quote(str(interaction.user.id))}")
    if not data:
        return await interaction.followup.send("Couldn't load friends (server unreachable or friends not set up yet).")
    if data.get("error"):
        return await interaction.followup.send(f"Couldn't load friends: {data['error']}")
    def _line(entries):
        out = []
        for x in (entries or [])[:15]:
            nm = x.get("discord_username") or x.get("game_username") or x["discord_id"][:6]
            out.append(f"• **{nm}**")
        return "\n".join(out) or "—"
    e = discord.Embed(title=f"Friends — {interaction.user.name}", color=0x7c3aed)
    e.add_field(name=f"✅ Friends ({data.get('counts', {}).get('friends', 0)})",
                value=_line(data.get("friends"))[:1024], inline=False)
    e.add_field(name=f"📥 Incoming ({data.get('counts', {}).get('incoming', 0)})",
                value=_line(data.get("incoming"))[:1024], inline=False)
    e.add_field(name=f"📤 Outgoing ({data.get('counts', {}).get('outgoing', 0)})",
                value=_line(data.get("outgoing"))[:1024], inline=False)
    await interaction.followup.send(embed=e)


@app_commands.describe(friend="Friend to invite", code="Your 5-letter private party code (from the drawer)")
async def _party_invite_impl(interaction: discord.Interaction, friend: discord.User, code: str):
    status, body = await _api_post("/api/party/invite", {
        "from_discord_id": str(interaction.user.id),
        "to_discord_id": str(friend.id),
        "code": code,
    })
    if status == 200 and body.get("ok"):
        return await interaction.followup.send(
            f"📨 Party invite sent to {friend.mention} — code **{body.get('code')}** (expires in 5 min).")
    err = (body or {}).get("error") or f"HTTP {status}"
    hint = (" — hold a private room first (the drawer shows your pID)" if "code" in err.lower()
            else (" — you must be friends first" if "friend" in err.lower() else ""))
    return await interaction.followup.send(f"Couldn't send invite: {err}{hint}")


@bot.tree.command(name="party_invite", description="Invite a friend to your private party (needs your pID code)")
@app_commands.describe(friend="Friend to invite", code="Your 5-letter private party code (from the drawer)")
async def party_invite_cmd(interaction: discord.Interaction, friend: discord.User, code: str):
    await interaction.response.defer(ephemeral=False)
    await _party_invite_impl(interaction, friend, code)


@bot.tree.command(name="dm", description="Send a DM to a friend")
@app_commands.describe(friend="Friend to message", message="Message (max 500 chars)")
async def dm_cmd(interaction: discord.Interaction, friend: discord.User, message: str):
    await interaction.response.defer(ephemeral=True)
    text = (message or "").strip()
    if not text:
        return await interaction.followup.send("Message can't be empty.", ephemeral=True)
    status, body = await _api_post("/api/friends/message", {
        "from_discord_id": str(interaction.user.id),
        "to_discord_id": str(friend.id),
        "body": text[:500],
    })
    if status == 200 and body.get("ok"):
        return await interaction.followup.send(f"📨 Sent to {friend.mention}.", ephemeral=True)
    err = (body or {}).get("error") or f"HTTP {status}"
    hint = " — you must be friends first" if "friend" in err.lower() else ""
    return await interaction.followup.send(f"Couldn't send: {err}{hint}", ephemeral=True)


@bot.tree.command(name="dms", description="Read recent DMs (optionally with one friend)")
@app_commands.describe(friend="Friend to read conversation with (omit for unread summary)")
async def dms_cmd(interaction: discord.Interaction, friend: discord.User = None):
    await interaction.response.defer(ephemeral=True)
    me = str(interaction.user.id)
    from urllib.parse import quote
    if friend is None:
        data = await _api_get(f"/api/friends/unread?discord_id={quote(me)}")
        if not data or data.get("error"):
            return await interaction.followup.send("Couldn't load unread.", ephemeral=True)
        if not data.get("total"):
            return await interaction.followup.send("No unread messages.", ephemeral=True)
        lines = []
        for did, n in (data.get("by_friend") or {}).items():
            u = await db.get_by_discord(str(did))
            nm = (u or {}).get("discord_username") or (u or {}).get("game_username") or str(did)[:6]
            lines.append(f"• **{nm}** — {n} unread (`/dms @user` to read)")
        e = discord.Embed(title="Unread DMs", color=0x7c3aed, description="\n".join(lines)[:4000])
        return await interaction.followup.send(embed=e, ephemeral=True)
    data = await _api_get(f"/api/friends/messages?discord_id={quote(me)}&with={quote(str(friend.id))}&limit=10")
    if not data or data.get("error"):
        return await interaction.followup.send(f"Couldn't load: {(data or {}).get('error') or 'server unreachable'}", ephemeral=True)
    msgs = data.get("messages") or []
    if not msgs:
        return await interaction.followup.send(f"No messages with {friend.mention} yet.", ephemeral=True)
    me_row = await db.get_by_discord(me)
    me_name = (me_row or {}).get("discord_username") or interaction.user.name
    lines = []
    for m in msgs:
        who = me_name if str(m.get("from_discord_id")) == me else friend.name
        lines.append(f"**{who}:** {m.get('body')}")
    e = discord.Embed(title=f"DMs — {friend.name}", color=0x7c3aed, description="\n".join(lines)[:4000])
    await interaction.followup.send(embed=e, ephemeral=True)


async def _room_tag_impl(interaction: discord.Interaction, kind: str, code: str, name: str | None):
    if kind == "untag":
        status, body = await _api_post("/api/room/untag", {"code": code})
        if status == 200 and body.get("ok"):
            return await interaction.followup.send(f"🏷️ Tag removed from **{body.get('code')}**.")
        return await interaction.followup.send(f"Couldn't untag: {(body or {}).get('error') or f'HTTP {status}'}")
    status, body = await _api_post("/api/room/tag", {
        "code": code, "kind": kind, "name": (name or "").strip()[:64],
        "created_by": str(interaction.user.id),
    })
    if status == 200 and body.get("ok"):
        label = "Tourney" if kind == "tourney" else "Scrim"
        return await interaction.followup.send(
            f"🏷️ **{body.get('code')}** tagged as {label} — **{body.get('name')}**. Friends now see it in your status.")
    return await interaction.followup.send(f"Couldn't tag: {(body or {}).get('error') or f'HTTP {status}'}")


@bot.tree.command(name="scrim", description="Tag a room code as scrims (friends see 'In Scrims - mode')")
@app_commands.describe(code="5-letter private room code", name="Scrim label, e.g. Zone scrims")
async def scrim_cmd(interaction: discord.Interaction, code: str, name: str = "Scrims"):
    await interaction.response.defer(ephemeral=False)
    await _room_tag_impl(interaction, "scrim", code, name)


@bot.tree.command(name="tourney", description="Tag a room code as a tournament (friends see 'In Tourney - name')")
@app_commands.describe(code="5-letter private room code", name="Tournament name, e.g. Friday Cup")
async def tourney_cmd(interaction: discord.Interaction, code: str, name: str):
    await interaction.response.defer(ephemeral=False)
    await _room_tag_impl(interaction, "tourney", code, name)


async def _comp_reg_rows(tourney_id):
    """All registration rows for a tourney (id, members, status). Never raises."""
    try:
        sb = _comp_sb()
        if sb is None:
            return []
        res = sb.table("bngc_comp_registrations").select(
            "id,member_ids,status").eq("tourney_id", int(tourney_id)).limit(1000).execute()
        return res.data or []
    except Exception:
        return []


def _norm_game(n) -> str:
    """Normalize a game name for duplicate checks (tags/dc:/case). Pure."""
    try:
        import re as _re
        s = _re.sub(r"<[^>]*>", "", str(n or "")).strip()
        if s.lower().startswith("dc:"):
            s = s[3:].strip()
        return s.lower()
    except Exception:
        return ""


@bot.tree.command(name="untag", description="Remove the scrim/tourney tag from a room code")
@app_commands.describe(code="5-letter private party code")
async def untag_cmd(interaction: discord.Interaction, code: str):
    await interaction.response.defer(ephemeral=False)
    await _room_tag_impl(interaction, "untag", code, None)


# ---------------------------------------------------------------------------
# Comp tourneys (objects with numeric IDs from 0 — NOT the /tourney room tag
# above, which stays untouched). Host-gated at runtime (administrator or
# COMP_HOST_ROLE_ID) so non-admin hosts work. Channels/role per tourney with
# env fallbacks (COMP_REG_CHANNEL_ID, COMP_BOARD_CHANNEL_ID,
# COMP_PLAYER_ROLE_ID).
# ---------------------------------------------------------------------------

COMP_HOST_ROLE_ID = os.getenv("COMP_HOST_ROLE_ID", "") or ""
COMP_PLAYER_ROLE_ID = os.getenv("COMP_PLAYER_ROLE_ID", "") or ""
COMP_REG_CHANNEL_ID = os.getenv("COMP_REG_CHANNEL_ID", "") or ""
COMP_BOARD_CHANNEL_ID = os.getenv("COMP_BOARD_CHANNEL_ID", "") or ""

_comp_reg_cache = {"at": 0.0, "rows": []}


def _comp_is_host(member) -> bool:
    """Administrator or holder of COMP_HOST_ROLE_ID. Never raises."""
    try:
        perms = getattr(member, "guild_permissions", None)
        if perms and getattr(perms, "administrator", False):
            return True
        rid = str(COMP_HOST_ROLE_ID or "").strip()
        roles = getattr(member, "roles", None) or []
        if rid:
            return any(str(getattr(r, "id", "")) == rid for r in roles)
    except Exception:
        pass
    return False


def _comp_tourney_hosts(sb, tourney_id) -> list:
    """Per-tourney host discord IDs (hosts column + created_by fallback).
    Missing column (pre-v233 DB) reads as no extra hosts. Never raises."""
    try:
        r = sb.table("bngc_comp_tourneys").select("hosts,created_by").eq(
            "id", int(tourney_id)).limit(1).execute()
        row = (r.data or [{}])[0] or {}
        hs = [str(x) for x in (row.get("hosts") or []) if str(x or "").strip()]
        cb = str(row.get("created_by") or "").strip()
        if cb and cb not in hs:
            hs.append(cb)
        return hs
    except Exception:
        return []


async def _comp_require_manager(interaction, tourney_id):
    """Gate for per-tourney management: global host (admin/role) OR a host
    listed on the tourney. Returns (sb, me); sends the refusal and returns
    (None, None) otherwise. Never raises."""
    try:
        me = await _comp_member(interaction, interaction.user.id)
        sb = _comp_sb()
        if sb is None:
            await interaction.followup.send("Supabase not configured.", ephemeral=True)
            return None, None
        if _comp_is_host(me):
            return sb, me
        try:
            hosts = _comp_tourney_hosts(sb, tourney_id)
        except Exception:
            hosts = []
        if str(interaction.user.id) in [str(h) for h in hosts]:
            return sb, me
        await interaction.followup.send("Host only.", ephemeral=True)
        return None, None
    except Exception:
        try:
            await interaction.followup.send("Host only.", ephemeral=True)
        except Exception:
            pass
        return None, None


async def _comp_member(interaction, user_id) -> object | None:
    """Fetch guild member for role checks/grants. None on failure."""
    try:
        guild = interaction.guild
        if guild is None:
            return None
        try:
            m = guild.get_member(int(user_id))
            if m is not None:
                return m
        except Exception:
            pass
        return await guild.fetch_member(int(user_id))
    except Exception:
        return None


async def _comp_grant_role(guild, user_id, role_id) -> bool:
    """Best-effort role grant. Never raises (returns bool)."""
    try:
        if guild is None or not role_id:
            return False
        role = guild.get_role(int(role_id))
        if role is None:
            return False
        try:
            member = guild.get_member(int(user_id))
        except Exception:
            member = None
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except Exception:
                return False
        if member is None:
            return False
        await member.add_roles(role, reason="Comp tourney registration")
        return True
    except Exception as e:
        print(f"[comp] role grant failed: {e}")
        return False


async def _comp_remove_role(guild, user_id, role_id, reason="Comp tourney ended") -> bool:
    """Best-effort role strip. Never raises (returns bool)."""
    try:
        if guild is None or not role_id:
            return False
        role = guild.get_role(int(role_id))
        if role is None:
            return False
        try:
            member = guild.get_member(int(user_id))
        except Exception:
            member = None
        if member is None:
            try:
                member = await guild.fetch_member(int(user_id))
            except Exception:
                return False
        if member is None:
            return False
        await member.remove_roles(role, reason=reason)
        return True
    except Exception as e:
        print(f"[comp] role remove failed: {e}")
        return False


def _comp_team_members(user_ids: list, mode: str):
    """Validate mention list for the mode. Pure: ([ids], None) or ([], error).

    Solo wants exactly 1, duo exactly 2 (any separator — separators never
    reach us, only the mention list does). Bots and dupes rejected.
    """
    try:
        ids = [str(x) for x in (user_ids or []) if str(x or "").strip()]
    except Exception:
        return [], "unreadable mentions"
    try:
        if len(ids) != len(set(ids)):
            return [], "duplicate mentions"
        want = 2 if str(mode or "").lower() == "duo" else 1
        if len(ids) != want:
            return [], (f"{mode} needs exactly {want} real mention(s), got {len(ids)} — "
                        f"type @ and pick from the popup, typed-out names don't count")
        return ids, None
    except Exception:
        return [], "invalid mentions"


def _comp_sb():
    if not db.USE_SUPABASE or not db.supabase:
        return None
    return db.supabase


@bot.tree.command(name="tourney-create", description="Create a comp tourney (host). Returns its numeric ID.")
@app_commands.describe(code="5-letter private room code (optional)",
                       name="Tourney name", series_len="Match count, 0 = open-ended",
                       mode="Solo or duo teams", game_mode="Game code, e.g. zw_lg",
                       region="Region, e.g. EU", rules_channel="Rules channel",
                       shoot_timer="Shoot timer, e.g. 4:30",
                       ping_role="Role pinged on open/close",
                       max_players="Auto-close intake at N accepted players (0 = unlimited)",
                       host_playing="Host plays too (their kills count)",
                       start_time="Start time YYYY-MM-DD HH:MM UTC (optional)")
@app_commands.choices(mode=[app_commands.Choice(name="Solo", value="solo"),
                            app_commands.Choice(name="Duo", value="duo")])
@app_commands.choices(game_mode=[
    app_commands.Choice(name="Late Game Zonewars", value="zw_lg"),
    app_commands.Choice(name="Zone Wars", value="zw"),
    app_commands.Choice(name="Duo Wild West", value="wwzw"),
    app_commands.Choice(name="Solo Wild West", value="ww"),
    app_commands.Choice(name="Boxfights", value="bf"),
    app_commands.Choice(name="Boxfights 2v2", value="BF2V2"),
    app_commands.Choice(name="V-Arena", value="VA"),
    app_commands.Choice(name="Red Vs Blue", value="tdm_bvr"),
    app_commands.Choice(name="Realistic 1v1", value="r_1v1"),
    app_commands.Choice(name="Free Mode", value="df"),
])
async def tourney_create_cmd(interaction: discord.Interaction, name: str,
                             code: str | None = None, series_len: int = 0,
                             mode: str = "solo", game_mode: str | None = None,
                             region: str | None = None,
                             rules_channel: discord.TextChannel | None = None,
                             shoot_timer: str | None = None,
                             ping_role: discord.Role | None = None,
                             max_players: int = 0, host_playing: bool = True,
                             start_time: str | None = None):
    await interaction.response.defer(ephemeral=True)
    me = await _comp_member(interaction, interaction.user.id)
    if not _comp_is_host(me):
        return await interaction.followup.send("Host only.", ephemeral=True)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.", ephemeral=True)
    code_c = (code or "").strip().upper() or None
    if code_c and (len(code_c) != 5 or not code_c.isalnum()):
        return await interaction.followup.send("Code must be 5 letters/digits.", ephemeral=True)
    mode_c = "duo" if str(mode or "").lower() == "duo" else "solo"
    try:
        series = max(0, int(series_len or 0))
    except Exception:
        series = 0
    try:
        maxp = max(0, int(max_players or 0))
    except Exception:
        maxp = 0
    sched_iso = None
    if (start_time or "").strip():
        try:
            from datetime import datetime, timezone
            _dt = datetime.strptime(str(start_time).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            if _dt <= datetime.now(timezone.utc):
                return await interaction.followup.send("Start time is in the past.", ephemeral=True)
            sched_iso = _dt.isoformat()
        except Exception:
            return await interaction.followup.send(
                "Bad start time. Use exactly `YYYY-MM-DD HH:MM` in UTC (e.g. `2026-09-12 18:30`).", ephemeral=True)
    try:
        _ins = {
            "code": code_c, "name": str(name or "").strip()[:64] or "Tourney",
            "status": "created", "mode": mode_c, "series_len": series,
            "game_mode": (game_mode or "").strip() or None,
            "region": (region or "").strip()[:16] or "EU",
            "rules_channel_id": str(rules_channel.id) if rules_channel else None,
            "shoot_timer": (shoot_timer or "").strip()[:16] or "4:30",
            "ping_role_id": str(ping_role.id) if ping_role else None,
            "max_players": maxp,
            "host_playing": bool(host_playing),
            "reg_channel_id": COMP_REG_CHANNEL_ID or None,
            "board_channel_id": COMP_BOARD_CHANNEL_ID or None,
            "player_role_id": COMP_PLAYER_ROLE_ID or None,
            "created_by": str(interaction.user.id),
        }
        if sched_iso:
            _ins["scheduled_at"] = sched_iso
        res = sb.table("bngc_comp_tourneys").insert(_ins).execute()
        row = (res.data or [{}])[0]
        tail = ""
        try:
            if sched_iso:
                from datetime import datetime
                tail = f" Starts <t:{int(datetime.fromisoformat(sched_iso).timestamp())}:R>."
        except Exception:
            pass
        return await interaction.followup.send(
            f"🏆 Tourney **#{row.get('id')} {row.get('name')}** created ({mode_c}, "
            f"{series or '∞'} matches).{tail} Post it: `/tourney-post {row.get('id')}`. "
            f"Registrations: `/listen-registrations`.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't create: {e}", ephemeral=True)


@bot.tree.command(name="tourney-stop", description="Close a comp tourney (host)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)",
                       role_to_remove="Player role to strip at end (defaults to the tourney's give-role)")
async def tourney_stop_cmd(interaction: discord.Interaction, tourney_id: int,
                           role_to_remove: discord.Role | None = None):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        from datetime import datetime, timezone
        old = sb.table("bngc_comp_tourneys").select(
            "code,player_role_id").eq("id", int(tourney_id)).limit(1).execute()
        old_row = ((old.data or [{}])[0] or {})
        old_code = old_row.get("code")
        res = sb.table("bngc_comp_tourneys").update(
            {"status": "ended", "reg_open": False, "ended_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", int(tourney_id)).execute()
        if not res.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        # drop the room tag so presence stops showing the ended tourney
        if old_code:
            try:
                sb.table("bngc_room_tags").delete().eq("code", str(old_code).upper()).execute()
            except Exception as e:
                print(f"[comp] stop untag failed: {e}")
        # strip the give-role from the accepted roster (best-effort)
        stripped = 0
        try:
            rid = None
            try:
                rid = str(role_to_remove.id) if role_to_remove is not None else None
            except Exception:
                rid = None
            if not rid:
                rid = str(old_row.get("player_role_id") or "") or None
            if not rid:
                try:
                    rid = str(COMP_PLAYER_ROLE_ID or "") or None
                except Exception:
                    rid = None
            if rid and interaction.guild is not None:
                dids: list = []
                try:
                    rres = sb.table("bngc_comp_registrations").select("member_ids").eq(
                        "tourney_id", int(tourney_id)).eq("status", "accepted").limit(1000).execute()
                    dids = sorted({str(x) for r in (rres.data or []) for x in ((r or {}).get("member_ids") or []) if x})
                except Exception:
                    dids = []
                for _did in dids:
                    try:
                        if await _comp_remove_role(interaction.guild, _did, rid,
                                                   reason=f"Comp tourney #{tourney_id} ended"):
                            stripped += 1
                    except Exception:
                        continue
        except Exception as e:
            print(f"[comp] stop role strip failed: {e}")
        tail = f" ({stripped} role{'s' if stripped != 1 else ''} stripped)" if stripped else ""
        return await interaction.followup.send(f"🏁 Tourney **#{tourney_id}** closed.{tail}", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't stop: {e}", ephemeral=True)


@bot.tree.command(name="tourney-change-code", description="Change a tourney's room code (host)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)",
                       code="New 5-letter private room code")
async def tourney_code_cmd(interaction: discord.Interaction, tourney_id: int, code: str):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    ok, msg = await _tourney_set_code(int(tourney_id), code, str(interaction.user.id))
    return await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="tourney-set-code", description="Set a tourney's room code (host, works when empty)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)",
                       code="5-letter private room code")
async def tourney_set_code_cmd(interaction: discord.Interaction, tourney_id: int, code: str):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    ok, msg = await _tourney_set_code(int(tourney_id), code, str(interaction.user.id))
    return await interaction.followup.send(msg, ephemeral=True)


async def _tourney_set_code(tourney_id: int, code: str, actor_id: str):
    """Shared set/change-code impl. Returns (ok, message). Never raises."""
    try:
        code_c = (code or "").strip().upper()
        if len(code_c) != 5 or not code_c.isalnum():
            return False, "Code must be 5 letters/digits."
        sb = _comp_sb()
        if sb is None:
            return False, "Supabase not configured."
        old = sb.table("bngc_comp_tourneys").select("id,code,name,status").eq(
            "id", int(tourney_id)).limit(1).execute()
        if not old.data:
            return False, "No tourney with that ID."
        t = old.data[0]
        old_code = (t.get("code") or "").upper() or None
        sb.table("bngc_comp_tourneys").update({"code": code_c}).eq("id", int(tourney_id)).execute()
        # move the room tag so presence follows the new code (started only)
        extra = ""
        try:
            if old_code and old_code != code_c:
                sb.table("bngc_room_tags").delete().eq("code", old_code).execute()
            if str(t.get("status") or "") == "started":
                sb.table("bngc_room_tags").upsert({
                    "code": code_c, "kind": "tourney",
                    "name": str(t.get("name") or "Tourney"),
                    "created_by": str(actor_id),
                }, on_conflict="code").execute()
                extra = " (room tag moved)"
        except Exception as e:
            print(f"[comp] set-code retag failed: {e}")
        verb = "changed" if old_code else "set"
        return True, f"🔁 Tourney **#{tourney_id}** code {verb}: `{old_code or '—'}` → `{code_c}`{extra}."
    except Exception as e:
        return False, f"Couldn't set code: {e}"


async def _comp_set_banned(tourney_id: int, user_id: str, banned: bool, guild=None):
    """Ban (or unban -> accepted) one player. Returns (ok, message). Never raises."""
    try:
        sb = _comp_sb()
        if sb is None:
            return False, "Supabase not configured."
        did = str(user_id)
        if banned:
            try:
                sb.table("bngc_comp_registrations").insert({
                    "tourney_id": int(tourney_id), "team_label": f"<@{did}>",
                    "member_ids": [did], "captain_id": did,
                    "source_message_id": None, "status": "banned",
                }).execute()
            except Exception as e:
                return False, f"Couldn't ban: {e}"
            # strip the player role if we can resolve it
            try:
                tres = sb.table("bngc_comp_tourneys").select("player_role_id").eq(
                    "id", int(tourney_id)).limit(1).execute()
                rid = ((tres.data or [{}])[0] or {}).get("player_role_id") or COMP_PLAYER_ROLE_ID
                if guild is not None and rid:
                    try:
                        m = guild.get_member(int(did))
                        if m is None:
                            m = await guild.fetch_member(int(did))
                        role = guild.get_role(int(rid))
                        if m is not None and role is not None:
                            await m.remove_roles(role, reason="Comp tourney ban")
                    except Exception:
                        pass
            except Exception:
                pass
            return True, f"⛔ <@{did}> banned from tourney **#{tourney_id}**."
        rows = await _comp_reg_rows(int(tourney_id))
        mine = [r for r in rows if (r or {}).get("status") == "banned"
                and did in ((r or {}).get("member_ids") or [])]
        if not mine:
            return False, "No ban row for that user in this tourney."
        for r in mine:
            try:
                sb.table("bngc_comp_registrations").update(
                    {"status": "accepted"}).eq("id", r["id"]).execute()
            except Exception:
                pass
        return True, f"✅ <@{did}> unbanned (accepted) in tourney **#{tourney_id}**."
    except Exception as e:
        return False, f"Couldn't update ban: {e}"


@bot.tree.command(name="tourney-ban", description="Ban a player from a tourney (host)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)", user="Player to ban")
async def tourney_ban_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    ok, msg = await _comp_set_banned(int(tourney_id), str(user.id), True, interaction.guild)
    return await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="tourney-unban", description="Unban a player (re-accepts them) (host)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)", user="Player to unban")
async def tourney_unban_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    ok, msg = await _comp_set_banned(int(tourney_id), str(user.id), False, interaction.guild)
    return await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="tourney-host-status", description="Set whether the host plays too (host)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)",
                       status="Playing = host kills count, Hosting only = spectate")
@app_commands.choices(status=[app_commands.Choice(name="Playing", value="playing"),
                              app_commands.Choice(name="Hosting only", value="hosting_only")])
async def tourney_host_status_cmd(interaction: discord.Interaction, tourney_id: int, status: str):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    hp = str(status or "").lower() != "hosting_only"
    try:
        res = sb.table("bngc_comp_tourneys").update({"host_playing": hp}).eq("id", int(tourney_id)).execute()
        if not res.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        label = "Playing — your kills count" if hp else "Hosting only — your kills don't count"
        return await interaction.followup.send(f"Tourney **#{tourney_id}** host status: **{label}**.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't update: {e}", ephemeral=True)


@bot.tree.command(name="tourney-add-host", description="Add a co-host to a tourney (host)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)", user="Co-host to add")
async def tourney_add_host_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        chk = sb.table("bngc_comp_tourneys").select("id").eq("id", int(tourney_id)).limit(1).execute()
        if not chk.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        hosts = _comp_tourney_hosts(sb, tourney_id)
        did = str(user.id)
        if did not in hosts:
            hosts.append(did)
            sb.table("bngc_comp_tourneys").update({"hosts": hosts}).eq("id", int(tourney_id)).execute()
        return await interaction.followup.send(f"{user.mention} is now a host of tourney **#{tourney_id}**.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't add host: {e}", ephemeral=True)


@bot.tree.command(name="tourney-remove-host", description="Remove a co-host from a tourney (host)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)", user="Co-host to remove")
async def tourney_remove_host_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        chk = sb.table("bngc_comp_tourneys").select("id").eq("id", int(tourney_id)).limit(1).execute()
        if not chk.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        hosts = _comp_tourney_hosts(sb, tourney_id)
        did = str(user.id)
        hosts = [h for h in hosts if h != did]
        sb.table("bngc_comp_tourneys").update({"hosts": hosts}).eq("id", int(tourney_id)).execute()
        return await interaction.followup.send(f"{user.mention} is no longer a host of tourney **#{tourney_id}**.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't remove host: {e}", ephemeral=True)


@bot.tree.command(name="tourney-add-player", description="Add a player to a tourney roster (host)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)", user="Player to add")
async def tourney_add_player_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        trow = sb.table("bngc_comp_tourneys").select(
            "id,mode,player_role_id").eq("id", int(tourney_id)).limit(1).execute()
        if not trow.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        t = trow.data[0] or {}
        did = str(user.id)
        rows = await _comp_reg_rows(int(tourney_id))
        if any((r or {}).get("status") != "banned" and did in ((r or {}).get("member_ids") or []) for r in rows):
            return await interaction.followup.send(f"{user.mention} is already on the roster.", ephemeral=True)
        _add_row = {
            "tourney_id": int(tourney_id),
            "team_label": f"<@{did}>",
            "member_ids": [did],
            "captain_id": did,
            "source_message_id": None,
            "status": "accepted",
            "member_names": {did: {
                "username": str(getattr(user, "name", "") or "")[:32],
                "display_name": str(getattr(user, "display_name", "") or getattr(user, "name", "") or "")[:32],
            }},
        }
        try:
            sb.table("bngc_comp_registrations").insert(_add_row).execute()
        except Exception:
            _add_row.pop("member_names", None)  # pre-migration safe
            sb.table("bngc_comp_registrations").insert(_add_row).execute()
        try:
            _rid = (t or {}).get("player_role_id") or COMP_PLAYER_ROLE_ID
            await _comp_grant_role(interaction.guild, did, _rid)
        except Exception:
            pass
        try:
            _comp_reg_cache["at"] = 0.0
        except Exception:
            pass
        return await interaction.followup.send(f"{user.mention} added to tourney **#{tourney_id}** roster.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't add player: {e}", ephemeral=True)


@bot.tree.command(name="tourney-remove-player", description="Remove a player from a tourney roster (host)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)", user="Player to remove")
async def tourney_remove_player_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        trow = sb.table("bngc_comp_tourneys").select(
            "id,player_role_id").eq("id", int(tourney_id)).limit(1).execute()
        if not trow.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        did = str(user.id)
        rows = sb.table("bngc_comp_registrations").select(
            "id,member_ids").eq("tourney_id", int(tourney_id)).neq("status", "banned").limit(1000).execute()
        hit = [r for r in (rows.data or []) if did in ((r or {}).get("member_ids") or [])]
        for r in hit:
            try:
                remaining = [m for m in ((r or {}).get("member_ids") or []) if m != did]
                if not remaining:
                    sb.table("bngc_comp_registrations").delete().eq("id", (r or {}).get("id")).execute()
                else:
                    sb.table("bngc_comp_registrations").update({"member_ids": remaining}).eq("id", (r or {}).get("id")).execute()
            except Exception:
                continue
        try:
            from discord.utils import get as _dget
            _rid = ((trow.data[0] or {}).get("player_role_id") or COMP_PLAYER_ROLE_ID) if trow.data else COMP_PLAYER_ROLE_ID
            if _rid and interaction.guild is not None:
                role = _dget(interaction.guild.roles, id=int(_rid)) if str(_rid).isdigit() else None
                if role is not None:
                    try:
                        m = interaction.guild.get_member(int(did))
                        if m is None:
                            m = await interaction.guild.fetch_member(int(did))
                        if m is not None:
                            await m.remove_roles(role, reason=f"tourney #{tourney_id} roster removal")
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            _comp_reg_cache["at"] = 0.0
        except Exception:
            pass
        return await interaction.followup.send(f"{user.mention} removed from tourney **#{tourney_id}** roster.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't remove player: {e}", ephemeral=True)


def _ord(n):
    """1 -> 1st, 2 -> 2nd, ... Pure."""
    try:
        n = int(n)
        if 10 <= n % 100 <= 20:
            suf = "th"
        else:
            suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suf}"
    except Exception:
        return str(n)


def _points_table_lines(t: dict) -> list:
    """POINTS section lines from the tourney points_table. Pure.

    Returns [] when there is no table (caller skips the section).
    Missing keys fall back to server defaults (kill 1 / win 3 /
    placement [10,7,5,3,1]) so the post always mirrors live scoring.
    """
    try:
        pt = (t or {}).get("points_table")
        if isinstance(pt, str):
            import json as _j
            pt = _j.loads(pt)
        if not isinstance(pt, dict):
            return []
        place = pt.get("placement")
        if not isinstance(place, list) or not place:
            place = [10, 7, 5, 3, 1]
        try:
            kill = int(pt.get("kill", 1))
        except Exception:
            kill = 1
        try:
            win = int(pt.get("win", 3))
        except Exception:
            win = 3
        # WIN owns P1: 1st placement is dropped, WIN + KILLS lead, then
        # 2ND.. in bold caps.
        lines = [f"**WIN = {win} PTS**", f"**KILLS = {kill} PTS**"]
        for i, p in enumerate(place):
            if i == 0:
                continue  # 1st place points removed — WIN owns P1
            lines.append(f"**{_ord(i + 1).upper()} = {p} PTS**")
        return lines
    except Exception:
        return []


def _comp_announce_text(t: dict):
    """Announcement message matching the host template exactly. Pure-ish."""
    try:
        mode = str(t.get("mode") or "solo").upper()
        name = str(t.get("name") or "Tourney")
        try:
            gm = fmt.format_game_mode(t.get("game_mode")) if t.get("game_mode") else "—"
        except Exception:
            gm = str(t.get("game_mode") or "—")
        gm = str(gm).upper() if str(gm) != "—" else "—"
        code = (t.get("code") or "").strip() or ""
        region = str(t.get("region") or "EU")
        timer = str(t.get("shoot_timer") or "4:30")
        rules = f"<#{t.get('rules_channel_id')}>" if t.get("rules_channel_id") else "—"
        signups = f"<#{t.get('reg_channel_id')}>" if t.get("reg_channel_id") else "—"
        ping = (t.get("ping_role_id") or t.get("player_role_id")
                or COMP_PLAYER_ROLE_ID or "")
        ping = str(ping).strip()
        ping_line = f"<@&{ping}>" if ping else "@role"
        try:
            _hosts = [str(h) for h in ((t or {}).get("hosts") or []) if str(h or "").strip()]
            _creator = str((t or {}).get("created_by") or "").strip()
            if _creator and _creator not in _hosts:
                _hosts.append(_creator)
            hosted_by = " ".join(f"<@{h}>" for h in _hosts) if _hosts else "—"
        except Exception:
            hosted_by = "—"
        start_line = None
        try:
            _iso = (t or {}).get("scheduled_at") or (t or {}).get("started_at")
            if _iso:
                from datetime import datetime
                _s = str(_iso).strip().replace("Z", "+00:00")
                _dd = datetime.fromisoformat(_s)
                if _dd.tzinfo is None:
                    from datetime import timezone as _tz
                    _dd = _dd.replace(tzinfo=_tz.utc)
                start_line = f"**START TIME**: Starts at <t:{int(_dd.timestamp())}:R>"
        except Exception:
            start_line = None
        lines = [
            f"## {mode} {name}",
            f"**GAME MODE**: {gm}",
            f"**REGION**: {region}",
            f"**CODE**: {code if code else 'Wait For Match'}",
            f"**RULES**: {rules}",
            f"**SIGNUPS**: {signups}",
            f"**SHOOT TIMER**: {timer}",
            f"**HOSTED BY**: {hosted_by}",
        ]
        if start_line:
            lines.append(start_line)
        pts = _points_table_lines(t)
        if pts:
            lines += ["", "**POINTS**:"] + pts
        lines += [
            "",
            "**WINNER IS HAVING TOURNAMENT MVP UNTIL THE NEXT TOURNEY**",
            "",
            "✅  IF YOU ARE GONNA JOIN",
            "",
            ping_line,
        ]
        return "\n".join(lines)
    except Exception:
        return str((t or {}).get("name") or "Tourney")


async def _announce_upsert(sb, guild, target, t, text):
    """Edit the stored announce message, else send new + persist ids.

    Returns (message, is_new). None message on failure. Never raises."""
    try:
        mid, cid = (t or {}).get("announce_message_id"), (t or {}).get("announce_channel_id")
        if mid and cid and guild is not None:
            try:
                old_ch = guild.get_channel(int(cid))
                if old_ch is not None:
                    posted = await (await old_ch.fetch_message(int(mid))).edit(content=text)
                    return posted, False
            except Exception:
                pass
        posted = await target.send(content=text)
        try:
            sb.table("bngc_comp_tourneys").update(
                {"announce_channel_id": str(target.id),
                 "announce_message_id": str(posted.id)}).eq("id", int((t or {}).get("id"))).execute()
        except Exception:
            pass
        return posted, True
    except Exception:
        return None, False


@bot.tree.command(name="tourney-post", description="Post the tourney announcement (host). Re-post edits it.")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)",
                       channel="Channel to post in (default: board channel)",
                       ping_role="Role to ping (default: stored)",
                       signups_channel="Signups channel override (stored)")
async def tourney_post_cmd(interaction: discord.Interaction, tourney_id: int,
                           channel: discord.TextChannel | None = None,
                           ping_role: discord.Role | None = None,
                           signups_channel: discord.TextChannel | None = None):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        tres = sb.table("bngc_comp_tourneys").select("*").eq("id", int(tourney_id)).limit(1).execute()
        if not tres.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        t = tres.data[0]
        # field overrides from this post (persisted so re-posts keep them)
        try:
            _ov = {}
            if ping_role is not None:
                _ov["ping_role_id"] = str(ping_role.id)
            if signups_channel is not None:
                _ov["reg_channel_id"] = str(signups_channel.id)
            if _ov:
                sb.table("bngc_comp_tourneys").update(_ov).eq("id", int(tourney_id)).execute()
                t.update(_ov)
        except Exception as e:
            print(f"[comp] post field persist failed: {e}")
        target = channel or (interaction.guild.get_channel(int(t.get("board_channel_id")))
                             if interaction.guild and t.get("board_channel_id") else None)
        if target is None:
            target = interaction.channel
        embed = None
        text = _comp_announce_text(t)
        # re-post = edit (code filled in later without spam)
        posted, is_new = await _announce_upsert(sb, interaction.guild, target, t, text)
        if posted is None:
            return await interaction.followup.send("Couldn't post.", ephemeral=True)
        if is_new:
            try:
                await posted.add_reaction("✅")
            except Exception:
                pass
        return await interaction.followup.send(
            f"📢 Announcement for **#{tourney_id}** is live in {target.mention}.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't post: {e}", ephemeral=True)


@bot.tree.command(name="tourney-refresh-points", description="Push the current points table into the posted announcement (host).")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)",
                       channel="Channel override (default: stored announce channel)")
async def tourney_refresh_points_cmd(interaction: discord.Interaction, tourney_id: int,
                                     channel: discord.TextChannel | None = None):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        tres = sb.table("bngc_comp_tourneys").select("*").eq("id", int(tourney_id)).limit(1).execute()
        if not tres.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        t = tres.data[0]
        text = _comp_announce_text(t)
        target = None
        try:
            if channel is not None:
                target = channel
            elif interaction.guild and t.get("announce_channel_id"):
                target = interaction.guild.get_channel(int(t.get("announce_channel_id")))
        except Exception:
            target = None
        if target is None:
            target = interaction.channel
        posted, _ = await _announce_upsert(sb, interaction.guild, target, t, text)
        if posted is None:
            return await interaction.followup.send("Couldn't update the post.", ephemeral=True)
        lines = _points_table_lines(t)
        return await interaction.followup.send(
            "✅ Points refreshed: " + (" · ".join(lines) if lines else "no table"),
            ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't refresh: {e}", ephemeral=True)


@bot.tree.command(name="tourney-schedule", description="Schedule a tourney (host). Shows it in Events.")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)",
                       when="UTC start, exactly: YYYY-MM-DD HH:MM")
async def tourney_schedule_cmd(interaction: discord.Interaction, tourney_id: int, when: str):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        from datetime import datetime, timezone
        dt = datetime.strptime(str(when or "").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except Exception:
        return await interaction.followup.send(
            "Bad datetime. Use exactly `YYYY-MM-DD HH:MM` in UTC (e.g. `2026-09-12 18:30`).", ephemeral=True)
    try:
        from datetime import datetime, timezone
        if dt <= datetime.now(timezone.utc):
            return await interaction.followup.send("That time is in the past.", ephemeral=True)
        res = sb.table("bngc_comp_tourneys").update(
            {"scheduled_at": dt.isoformat(), "status": "scheduled"}
        ).eq("id", int(tourney_id)).execute()
        if not res.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        return await interaction.followup.send(
            f"📅 Tourney **#{tourney_id}** scheduled for <t:{int(dt.timestamp())}:F> — visible under Scheduled in Events.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't schedule: {e}", ephemeral=True)


async def _comp_tourney(tourney_id) -> dict | None:
    """Fetch GET /api/comp/tourney?id=. None on error. Never raises."""
    from urllib.parse import quote
    try:
        return await _api_get(f"/api/comp/tourney?id={quote(str(tourney_id))}")
    except Exception:
        return None


def _comp_board_lines(board, limit=15):
    """Render aggregate standings lines (with mentions). Pure."""
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    try:
        for i, r in enumerate((board or [])[:limit], 1):
            name = (r or {}).get("name") or "?"
            did = (r or {}).get("discord_id")
            who = f"{_comp_mention(did)} **{name}**" if did else f"**{name}**"
            pts = (r or {}).get("points", 0)
            kills = (r or {}).get("kills", 0)
            wins = (r or {}).get("wins", 0)
            avg = (r or {}).get("avg_placement")
            tail = f" ({kills}K, {wins}W" + (f", {avg} avg" if avg is not None else "") + ")"
            prefix = medals[i - 1] if i <= 3 else f"{i}."
            lines.append(f"{prefix} {who} — {pts} pts{tail}")
    except Exception:
        pass
    return lines


@bot.tree.command(name="tourney-board", description="Aggregate leaderboard for a comp tourney")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)")
async def tourney_board_cmd(interaction: discord.Interaction, tourney_id: int):
    await interaction.response.defer(ephemeral=False)
    data = await _comp_tourney(tourney_id)
    if not data or data.get("error"):
        return await interaction.followup.send(f"Couldn't load tourney #{tourney_id}: {(data or {}).get('error') or 'server unreachable'}.")
    t = data.get("tourney") or {}
    board = data.get("standings") or []
    if not board:
        return await interaction.followup.send(f"No sealed matches in **{t.get('name')}** yet.")
    e = discord.Embed(title=f"🏆 {t.get('name')} — #{t.get('id')} ({t.get('status')})", color=0xFFD700,
                      description="\n".join(_comp_board_lines(board))[:4000])
    e.set_footer(text=f"{len(board)} players · {t.get('sealed', 0)}/{t.get('series_len') or '∞'} matches")
    await interaction.followup.send(embed=e)


@bot.tree.command(name="tourney-match", description="Per-match board for a comp tourney match")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)", version="Match version (0, 1, 2, ...)")
async def tourney_match_cmd(interaction: discord.Interaction, tourney_id: int, version: int):
    await interaction.response.defer(ephemeral=False)
    data = await _comp_tourney(tourney_id)
    if not data or data.get("error"):
        return await interaction.followup.send(f"Couldn't load tourney #{tourney_id}: {(data or {}).get('error') or 'server unreachable'}.")
    t = data.get("tourney") or {}
    matches = data.get("matches") or []
    m = next((x for x in matches if (x or {}).get("version") == int(version)), None)
    if m is None:
        return await interaction.followup.send(f"No sealed match v{version} in **{t.get('name')}**.")
    w = m.get("winner")
    boards = data.get("match_boards") or []
    b = next((x for x in boards if (x or {}).get("version") == int(version)), None)
    rows = (b or {}).get("rows") or []
    by_did = {}
    try:
        for _rw in rows:
            if (_rw or {}).get("discord_id"):
                by_did[str(_rw.get("discord_id"))] = (_rw or {}).get("name")
    except Exception:
        pass
    w_mention = ""
    try:
        _wd = next((str(_rw.get("discord_id")) for _rw in rows
                    if (_rw or {}).get("won") and (_rw or {}).get("discord_id")), "")
        w_mention = (f" {_comp_mention(_wd)}" if _wd else "")
    except Exception:
        pass
    lines = []
    for i, r in enumerate(rows, 1):
        nm = (r or {}).get("name") or "?"
        did = (r or {}).get("discord_id")
        who = f"{_comp_mention(did)} **{nm}**" if did else f"**{nm}**"
        pts = (r or {}).get("points", 0)
        k = (r or {}).get("kills", 0)
        p = (r or {}).get("placement")
        lines.append(f"{i}. {who} — {pts} pts ({k}K{', P' + str(p) if p else ''})")
    e = discord.Embed(title=f"{t.get('name')} — match v{version}", color=0x7c3aed,
                      description=(f"Winner: **{w or '—'}**{w_mention}\n" + ("\n".join(lines) if lines else "No rows."))[:4000])
    await interaction.followup.send(embed=e)


def _comp_mention(did) -> str:
    """<@did> mention, or the raw id when empty. Pure."""
    try:
        did = str(did or "").strip()
        return f"<@{did}>" if did else "?"
    except Exception:
        return "?"


@bot.tree.command(name="tourney-winner", description="Winner of a comp tourney (most points)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)")
async def tourney_winner_cmd(interaction: discord.Interaction, tourney_id: int):
    await interaction.response.defer(ephemeral=False)
    data = await _comp_tourney(tourney_id)
    if not data or data.get("error"):
        return await interaction.followup.send(f"Couldn't load tourney #{tourney_id}: {(data or {}).get('error') or 'server unreachable'}.")
    t = data.get("tourney") or {}
    board = data.get("standings") or []
    if not board:
        return await interaction.followup.send(f"No sealed matches in **{t.get('name')}** yet.")
    w = board[0] or {}
    await interaction.followup.send(
        f"🏆 **{t.get('name')}** winner: {_comp_mention(w.get('discord_id'))} "
        f"**{(w or {}).get('name') or '?'}** — {(w or {}).get('points', 0)} pts "
        f"({(w or {}).get('kills', 0)}K, {(w or {}).get('wins', 0)}W)")


@bot.tree.command(name="tourney-best", description="Best slayer of a comp tourney (most kills)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)")
async def tourney_best_cmd(interaction: discord.Interaction, tourney_id: int):
    await interaction.response.defer(ephemeral=False)
    data = await _comp_tourney(tourney_id)
    if not data or data.get("error"):
        return await interaction.followup.send(f"Couldn't load tourney #{tourney_id}: {(data or {}).get('error') or 'server unreachable'}.")
    t = data.get("tourney") or {}
    board = data.get("standings") or []
    if not board:
        return await interaction.followup.send(f"No sealed matches in **{t.get('name')}** yet.")
    try:
        b = max(board, key=lambda r: int((r or {}).get("kills") or 0))
    except Exception:
        b = board[0]
    await interaction.followup.send(
        f"🔪 **{t.get('name')}** best slayer: {_comp_mention(b.get('discord_id'))} "
        f"**{(b or {}).get('name') or '?'}** — {(b or {}).get('kills', 0)} kills "
        f"({(b or {}).get('points', 0)} pts)")


@bot.tree.command(name="tourney-worst", description="Bottom of a comp tourney board (fewest points)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)")
async def tourney_worst_cmd(interaction: discord.Interaction, tourney_id: int):
    await interaction.response.defer(ephemeral=False)
    data = await _comp_tourney(tourney_id)
    if not data or data.get("error"):
        return await interaction.followup.send(f"Couldn't load tourney #{tourney_id}: {(data or {}).get('error') or 'server unreachable'}.")
    t = data.get("tourney") or {}
    board = data.get("standings") or []
    if not board:
        return await interaction.followup.send(f"No sealed matches in **{t.get('name')}** yet.")
    w = board[-1] or {}
    await interaction.followup.send(
        f"💀 **{t.get('name')}** bottom: {_comp_mention(w.get('discord_id'))} "
        f"**{(w or {}).get('name') or '?'}** — {(w or {}).get('points', 0)} pts "
        f"({(w or {}).get('kills', 0)}K, {(w or {}).get('wins', 0)}W)")


@bot.tree.command(name="tourney-pr", description="Tourney PR board (computed rating)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)")
async def tourney_pr_cmd(interaction: discord.Interaction, tourney_id: int):
    await interaction.response.defer(ephemeral=False)
    try:
        _st, body = await _api_post("/api/comp/tourney/pr", {"tourney_id": int(tourney_id)})
    except Exception:
        _st, body = 0, {}
    board = (body or {}).get("pr_board") or []
    if not board:
        return await interaction.followup.send(
            f"No PR for tourney #{tourney_id} yet ({(body or {}).get('error') or 'no sealed matches'}).")
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(board[:10], 1):
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        nm = (r or {}).get("name") or "?"
        lines.append(f"{prefix} {_comp_mention((r or {}).get('discord_id'))} **{nm}** — {(r or {}).get('pr')} PR")
    e = discord.Embed(title=f"📊 Tourney PR — #{tourney_id}", color=0x10b981,
                      description="\n".join(lines)[:4000])
    await interaction.followup.send(embed=e)


@bot.tree.command(name="pr-leaderboard", description="Top Tourney PR ratings (latest per player)")
async def pr_leaderboard_cmd(interaction: discord.Interaction, limit: int = 10):
    await interaction.response.defer(ephemeral=False)
    try:
        data = await _api_get(f"/api/leaderboard?sort=pr&limit={max(1, min(int(limit or 10), 25))}")
    except Exception:
        data = None
    rows = (data or {}).get("results") or []
    rows = [r for r in rows if (r or {}).get("tourney_pr") is not None]
    if not rows:
        return await interaction.followup.send("No PR ratings yet — they compute when a tourney ends.")
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows, 1):
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        nm = (r or {}).get("game_username") or (r or {}).get("discord_username") or "?"
        lines.append(f"{prefix} {_comp_mention((r or {}).get('discord_id'))} **{nm}** — {(r or {}).get('tourney_pr')} PR")
    e = discord.Embed(title="📊 PR Leaderboard", color=0x10b981, description="\n".join(lines)[:4000])
    await interaction.followup.send(embed=e)


@bot.tree.command(name="tourney-list", description="List comp tourneys (id, status, matches)")
async def tourney_list_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.")
    try:
        res = sb.table("bngc_comp_tourneys").select(
            "id,code,name,status,mode,series_len").order("id").limit(25).execute()
        rows = res.data or []
        if not rows:
            return await interaction.followup.send("No tourneys yet. Hosts: `/tourney-create`.")
        lines = [f"**#{r.get('id')}** {r.get('name')} — {r.get('status')} ({r.get('mode')}, "
                 f"{r.get('series_len') or '∞'} matches)" + (f" `{r.get('code')}`" if r.get("code") else "")
                 for r in rows]
        e = discord.Embed(title="Comp tourneys", color=0x7c3aed, description="\n".join(lines)[:4000])
        return await interaction.followup.send(embed=e)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't list: {e}")


@bot.tree.command(name="listen-registrations", description="Open signups in a channel (host)")
@app_commands.describe(channel="Signups channel", tourney_id="Numeric tourney ID",
                       ping_role="Role to ping (default: tourney player role)")
async def listen_reg_cmd(interaction: discord.Interaction, channel: discord.TextChannel,
                         tourney_id: int, ping_role: discord.Role | None = None):
    await interaction.response.defer(ephemeral=True)
    me = await _comp_member(interaction, interaction.user.id)
    if not _comp_is_host(me):
        return await interaction.followup.send("Host only.", ephemeral=True)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.", ephemeral=True)
    try:
        tres = sb.table("bngc_comp_tourneys").select(
            "id,name,mode,player_role_id,ping_role_id").eq(
            "id", int(tourney_id)).in_("status", ["created", "scheduled", "started"]).limit(1).execute()
        if not tres.data:
            return await interaction.followup.send(
                "No registrable tourney with that ID (created/scheduled/started).", ephemeral=True)
        t = tres.data[0]
        print(t)
        ping = str(ping_role.id) if ping_role else (t.get("ping_role_id") or t.get("player_role_id") or COMP_PLAYER_ROLE_ID or "")
        sb.table("bngc_comp_tourneys").update(
            {"reg_open": True, "reg_channel_id": str(channel.id),
             "ping_role_id": ping or None}).eq("id", int(tourney_id)).execute()
        try:
            _comp_reg_cache["at"] = 0.0
        except Exception:
            pass
        # unlock typing so players can post their mentions
        try:
            await channel.set_permissions(interaction.guild.default_role, send_messages=True,
                                          reason="Comp signups open")
        except Exception as e:
            print(f"[comp] unlock typing failed: {e}")
        mode = (t or {}).get("mode") or "solo"
        try:
            posted = await channel.send(_comp_open_text(mode, ping, t.get("name")), view=_CompPlayersView())
        except Exception as e:
            return await interaction.followup.send(
                f"Listening, but the open message failed: {e}", ephemeral=True)
        return await interaction.followup.send(
            f"👂 Listening {channel.mention} for tourney **#{tourney_id}** ({mode}). "
            f"Typing unlocked, open message posted.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't listen: {e}", ephemeral=True)


@bot.tree.command(name="stop-registration", description="Close signups: lock typing + post counts (host)")
@app_commands.describe(tourney_id="Numeric tourney ID")
async def stop_reg_cmd(interaction: discord.Interaction, tourney_id: int):
    await interaction.response.defer(ephemeral=True)
    me = await _comp_member(interaction, interaction.user.id)
    if not _comp_is_host(me):
        return await interaction.followup.send("Host only.", ephemeral=True)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.", ephemeral=True)
    try:
        res = sb.table("bngc_comp_tourneys").select(
            "id,name,mode,reg_channel_id,ping_role_id,player_role_id").eq(
            "id", int(tourney_id)).limit(1).execute()
        if not res.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        t = res.data[0]
        sb.table("bngc_comp_tourneys").update({"reg_open": False}).eq("id", int(tourney_id)).execute()
        try:
            _comp_reg_cache["at"] = 0.0
        except Exception:
            pass
        players, teams = await _comp_accepted_counts(int(tourney_id))
        # lock typing + closed message in the signups channel
        try:
            ch = interaction.guild.get_channel(int(t.get("reg_channel_id"))) if interaction.guild and t.get("reg_channel_id") else None
            if ch is not None:
                try:
                    await ch.set_permissions(interaction.guild.default_role, send_messages=False,
                                             reason="Comp signups closed")
                except Exception as e:
                    print(f"[comp] lock typing failed: {e}")
                try:
                    await ch.send(_comp_closed_text(t.get("mode"), players, teams, t.get("name")))
                except Exception as e:
                    print(f"[comp] closed message failed: {e}")
        except Exception:
            pass
        return await interaction.followup.send(
            f"🔇 Registration closed for tourney **#{tourney_id}** ({players} players / {teams} teams).",
            ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't stop: {e}", ephemeral=True)


async def _comp_accepted_counts(tourney_id: int):
    """(accepted players, accepted teams). Never raises."""
    try:
        sb = _comp_sb()
        if sb is None:
            return 0, 0
        res = sb.table("bngc_comp_registrations").select("member_ids").eq(
            "tourney_id", int(tourney_id)).eq("status", "accepted").limit(1000).execute()
        teams = len(res.data or [])
        players = sum(len((r or {}).get("member_ids") or []) for r in (res.data or []))
        return players, teams
    except Exception:
        return 0, 0


async def _comp_autoclose(tourney_id: int, guild=None, channel=None):
    """Close intake when max_players accepted is reached. Posts the closed
    message like /stop-registration. Returns True when it closed. Never raises."""
    try:
        sb = _comp_sb()
        if sb is None:
            return False
        tres = sb.table("bngc_comp_tourneys").select(
            "id,name,mode,reg_open,reg_channel_id,max_players").eq(
            "id", int(tourney_id)).limit(1).execute()
        if not tres.data:
            return False
        t = tres.data[0]
        try:
            maxp = int(t.get("max_players") or 0)
        except Exception:
            maxp = 0
        if maxp <= 0 or not t.get("reg_open"):
            return False
        players, teams = await _comp_accepted_counts(int(tourney_id))
        if players < maxp:
            return False
        sb.table("bngc_comp_tourneys").update({"reg_open": False}).eq("id", int(tourney_id)).execute()
        try:
            _comp_reg_cache["at"] = 0.0
        except Exception:
            pass
        ch = channel
        try:
            if ch is None and guild is not None and t.get("reg_channel_id"):
                ch = guild.get_channel(int(t["reg_channel_id"]))
        except Exception:
            ch = None
        if ch is not None:
            try:
                await ch.set_permissions(guild.default_role, send_messages=False,
                                         reason="Comp full: max players reached")
            except Exception:
                pass
            try:
                await ch.send(_comp_closed_text(t.get("mode"), players, teams, t.get("name")) + " (full)")
            except Exception:
                pass
        print(f"[comp] tourney #{tourney_id} auto-closed at {players} players")
        return True
    except Exception as e:
        print(f"[comp] autoclose failed: {e}")
        return False


@bot.tree.command(name="tourney-start", description="Start a tourney: role to accepted players (host)")
@app_commands.describe(tourney_id="Numeric tourney ID (from 0)",
                       role_to_give="Player role (default: stored)")
async def tourney_start_cmd(interaction: discord.Interaction, tourney_id: int,
                            role_to_give: discord.Role | None = None):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        from datetime import datetime, timezone
        tres = sb.table("bngc_comp_tourneys").select(
            "id,code,name,status,player_role_id").eq("id", int(tourney_id)).limit(1).execute()
        if not tres.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        t = tres.data[0]
        role_id = (str(role_to_give.id) if role_to_give else None) or t.get("player_role_id") or COMP_PLAYER_ROLE_ID
        if role_to_give is not None:
            try:
                sb.table("bngc_comp_tourneys").update(
                    {"player_role_id": str(role_to_give.id)}).eq("id", int(tourney_id)).execute()
            except Exception:
                pass
        rres = sb.table("bngc_comp_registrations").select("member_ids").eq(
            "tourney_id", int(tourney_id)).eq("status", "accepted").limit(1000).execute()
        dids = sorted({str(x) for r in (rres.data or []) for x in ((r or {}).get("member_ids") or []) if x})
        if not role_id:
            try:
                sb.table("bngc_comp_tourneys").update(
                    {"started_at": datetime.now(timezone.utc).isoformat(),
                     "status": "started"}).eq("id", int(tourney_id)).execute()
            except Exception:
                pass
            return await interaction.followup.send(
                f"🚀 Tourney **#{tourney_id}** started ({len(dids)} players) — "
                f"no player role configured, nobody to give it to. Pass `role_to_give`.",
                ephemeral=True)
        ok = 0
        for did in dids:
            if await _comp_grant_role(interaction.guild, did, role_id):
                ok += 1
        try:
            sb.table("bngc_comp_tourneys").update(
                {"started_at": datetime.now(timezone.utc).isoformat(),
                 "status": "started"}).eq("id", int(tourney_id)).execute()
        except Exception:
            pass
        # auto-tag the room code so presence + drawer show the tourney state
        if t.get("code"):
            try:
                sb.table("bngc_room_tags").upsert({
                    "code": str(t["code"]).upper(), "kind": "tourney",
                    "name": str(t.get("name") or "Tourney"),
                    "created_by": str(interaction.user.id),
                }, on_conflict="code").execute()
            except Exception as e:
                print(f"[comp] start tag failed: {e}")
        extra = "" if ok == len(dids) else f" ({len(dids) - ok} failed — check role order/perms)"
        return await interaction.followup.send(
            f"🚀 Tourney **#{tourney_id}** started — role to {ok}/{len(dids)} players.{extra}",
            ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't start: {e}", ephemeral=True)


TEAM_COLORS = ["🔵", "🔴", "🟠", "🟡", "🟢", "🟣"]


def _comp_team_color(index: int) -> str:
    """Team color by intake order (cycles 6). Pure."""
    try:
        return TEAM_COLORS[int(index or 0) % len(TEAM_COLORS)]
    except Exception:
        return TEAM_COLORS[0]


def _comp_open_text(mode: str, ping_role_id: str | None = None, name: str = "Tourney") -> str:
    """Registration-open announcement. Pure. Ping role mention prepended."""
    try:
        head = (f"## {str(name).upper()} SIGNUPS REGISTRATIONS OPEN:\n\n"
                f"*@yourself to register (type @ and pick your name from the popup — "
                f"typed-out names don't count).*\n"
                f"*Then press **Set game name** below so kills map to you.*")
        ping = str(ping_role_id or "").strip()
        return (f"<@&{ping}>\n" if ping else "") + head
    except Exception:
        return "SIGNUPS OPEN"


def _comp_closed_text(mode: str, players: int, teams: int, name: str) -> str:
    """Registration-closed announcement. Pure."""
    try:
        return (f"## {str(name).upper()} SIGNUPS CLOSED:\n\n"
                f"{int(players or 0)} Players/ {int(teams or 0)} Teams")
    except Exception:
        return "SIGNUPS CLOSED"


class _GameNameModal(discord.ui.Modal):
    """Game-name prompt for comp mapping. Writes bngc_users.game_username.

    Duplicates (normalized, cross-tourney-roster) are refused so the engine
    mapping stays 1:1; twins fail closed and resolve via dashboard /link.
    """

    def __init__(self, tourney_id: int):
        super().__init__(title="Set your in-game name")
        self.tourney_id = int(tourney_id)
        self.game_name = discord.ui.TextInput(
            label="In-game name",
            placeholder="Exactly as it appears in-game",
            min_length=1, max_length=32, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        try:
            raw = str(self.game_name.value or "").strip()[:32]
            norm = _norm_game(raw)
            if not norm:
                return await interaction.followup.send("Enter your in-game name (1–32 chars).", ephemeral=True)
            sb = _comp_sb()
            if sb is None:
                return await interaction.followup.send("Supabase not configured.", ephemeral=True)
            me = str(interaction.user.id)
            rows = await _comp_reg_rows(self.tourney_id)
            if not any(me in ((r or {}).get("member_ids") or []) and (r or {}).get("status") != "banned" for r in rows):
                return await interaction.followup.send("Register for this tourney first (@yourself in the signup channel).", ephemeral=True)
            dids = sorted({str(x) for r in rows for x in ((r or {}).get("member_ids") or [])
                           if (r or {}).get("status") != "banned" and x and str(x) != me})[:100]
            taken_by = None
            if dids:
                try:
                    gres = sb.table("bngc_users").select(
                        "discord_id,game_username").in_("discord_id", dids).execute()
                    for u in (gres.data or []):
                        try:
                            if _norm_game((u or {}).get("game_username")) == norm:
                                taken_by = str((u or {}).get("discord_id"))
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
            if taken_by:
                return await interaction.followup.send(
                    f"'{raw}' is already registered — ask a host to link it if it's yours.", ephemeral=True)
            try:
                upd = sb.table("bngc_users").update(
                    {"game_username": raw}).eq("discord_id", me).execute()
                wrote = bool(upd.data)
            except Exception:
                wrote = False
            if not wrote:
                try:
                    sb.table("bngc_users").insert({
                        "discord_id": me,
                        "discord_username": getattr(interaction.user, "display_name", None) or str(interaction.user),
                        "game_username": raw,
                    }).execute()
                    wrote = True
                except Exception as e:
                    return await interaction.followup.send(f"Couldn't save: {e}", ephemeral=True)
            return await interaction.followup.send(
                f"Saved — {interaction.user.mention} known as **{raw}** for tourney **#{self.tourney_id}**.", ephemeral=True)
        except Exception as e:
            try:
                return await interaction.followup.send(f"Couldn't save: {e}", ephemeral=True)
            except Exception:
                pass


class _CompPlayersView(discord.ui.View):
    """Persistent 'see players' button (survives restarts via fixed custom_id).

    Tourney resolved at click time: the open registration in this channel.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Set game name", style=discord.ButtonStyle.success,
                       custom_id="comp:gamename")
    async def set_game_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            regs = await _comp_open_regs()
            mine = [r for r in (regs or [])
                    if str((r or {}).get("reg_channel_id") or "") == str(getattr(interaction.channel, "id", ""))]
            if not mine:
                try:
                    return await interaction.response.send_message(
                        "No open registration here.", ephemeral=True)
                except Exception:
                    return
            return await interaction.response.send_modal(_GameNameModal(int(mine[0]["id"])))
        except Exception:
            try:
                return await interaction.response.send_message(
                    "Couldn't open the form, try again.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="See players", style=discord.ButtonStyle.primary,
                       custom_id="comp:players")
    async def show_players(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        try:
            regs = await _comp_open_regs()
            mine = [r for r in (regs or [])
                    if str((r or {}).get("reg_channel_id") or "") == str(getattr(interaction.channel, "id", ""))]
            if not mine:
                return await interaction.followup.send("No open registration here.", ephemeral=True)
            t = mine[0]
            rows = await _comp_reg_rows(t["id"])
            acc = [r for r in rows if (r or {}).get("status") == "accepted"]
            if not acc:
                return await interaction.followup.send("No accepted players yet.", ephemeral=True)
            names = []
            try:
                sb = _comp_sb()
                dids = sorted({str(x) for r in acc for x in ((r or {}).get("member_ids") or []) if x})[:100]
                umap = {}
                gmap = {}
                if sb is not None and dids:
                    ures = sb.table("bngc_users").select(
                        "discord_id,game_username,discord_username").in_("discord_id", dids).execute()
                    for u in (ures.data or []):
                        try:
                            dd = str((u or {}).get("discord_id"))
                            umap[dd] = (u.get("discord_username") or dd[:6])
                            if (u or {}).get("game_username"):
                                gmap[dd] = str((u or {}).get("game_username"))
                        except Exception:
                            continue
                # misses (no bngc_users row) resolve live via Discord itself
                missing = [d for d in dids if not umap.get(d)][:25]
                if missing and interaction.guild is not None:
                    for d in missing:
                        try:
                            m = interaction.guild.get_member(int(d))
                            if m is None:
                                m = await interaction.guild.fetch_member(int(d))
                            if m is not None:
                                umap[d] = m.display_name
                        except Exception:
                            continue
                for r in acc:
                    ms = []
                    for x in ((r or {}).get("member_ids") or []):
                        base = umap.get(str(x), str(x)[:6])
                        ms.append(f"{base} (known as {gmap[str(x)]})" if gmap.get(str(x)) else base)
                    tag = (r or {}).get("team_color") or ""
                    names.append(f"{tag + ' ' if tag else ''}{' / '.join(ms)}")
            except Exception:
                names = [", ".join((r or {}).get("member_ids") or []) for r in acc]
            e = discord.Embed(title=f"Players — tourney #{t['id']}", color=0x7c3aed,
                              description="\n".join(names[:50])[:4000])
            return await interaction.followup.send(embed=e, ephemeral=True)
        except Exception as e:
            try:
                return await interaction.followup.send(f"Couldn't load: {e}", ephemeral=True)
            except Exception:
                pass


async def _comp_open_regs():
    """Tourney rows currently listening (cached 30s). Never raises."""
    try:
        import time as _t
        if _t.time() - float(_comp_reg_cache.get("at") or 0) < 30 and _comp_reg_cache.get("rows") is not None:
            return _comp_reg_cache["rows"]
    except Exception:
        pass
    rows = []
    try:
        sb = _comp_sb()
        if sb is not None:
            res = sb.table("bngc_comp_tourneys").select(
                "id,mode,reg_channel_id,player_role_id").eq("reg_open", True).neq(
                "status", "ended").execute()
            rows = res.data or []
    except Exception:
        rows = []
    try:
        import time as _t2
        _comp_reg_cache["at"] = _t2.time()
        _comp_reg_cache["rows"] = rows
    except Exception:
        pass
    return rows


@bot.event
async def on_message(message):
    # Registration intake: solo = 1 mention, duo = 2 mentions (separators
    # never reach us, only the mention list does). Duplicates, wrong counts
    # and banned members are refused with an auto-deleted hint.
    try:
        if message.author is None or getattr(message.author, "bot", False):
            return
        if message.guild is None:
            return
        regs = await _comp_open_regs()
        mine = [r for r in (regs or [])
                if str((r or {}).get("reg_channel_id") or "") == str(getattr(message.channel, "id", ""))]
        if not mine:
            return
        t = mine[0]
        ids = [str(u.id) for u in (message.mentions or []) if not getattr(u, "bot", False)]
        # dedupe preserving order
        seen, clean = set(), []
        for i in ids:
            if i not in seen:
                seen.add(i)
                clean.append(i)
        members, err = _comp_team_members(clean, (t or {}).get("mode") or "solo")
        sb = _comp_sb()

        async def _refuse(text):
            try:
                await message.add_reaction("❌")
            except Exception:
                pass
            try:
                hint = await message.reply(text)
                await hint.delete(delay=60)
            except Exception:
                pass

        # self-registration only: solo = your own mention; duo = your mention
        # first, then your teammate. Registering strangers is refused.
        try:
            author_id = str(getattr(message.author, "id", ""))
        except Exception:
            author_id = ""
        if err is None and author_id not in (members or []):
            err = "mention yourself to register (solo: @you, duo: @you @teammate first)"
        elif err is None and str((t or {}).get("mode") or "solo").lower() == "duo" and members[0] != author_id:
            err = "duos: your own mention must come first (@you @teammate)"

        if err or sb is None:
            await _refuse(err or "Registration unavailable, try later.")
            return
        # banned member anywhere on the team -> refuse (python-side overlap;
        # postgrest array ops vary by version, so never trust them blindly)
        try:
            banned = any(
                (r or {}).get("status") == "banned"
                and any(m in members for m in ((r or {}).get("member_ids") or []))
                for r in await _comp_reg_rows(t["id"]))
            if banned:
                await _refuse("❌ A member of this team is banned from this tourney.")
                return
        except Exception:
            pass
        # already in (any status but banned) -> dup
        try:
            dup = any(
                (r or {}).get("status") != "banned"
                and any(m in members for m in ((r or {}).get("member_ids") or []))
                for r in await _comp_reg_rows(t["id"]))
            if dup:
                await _refuse("Already registered for this tourney.")
                return
        except Exception:
            pass
        # duplicate game name across the tourney -> refuse (user input is
        # checked here so the engine mapping stays 1:1; twins fail closed).
        try:
            _rows_g = await _comp_reg_rows(t["id"])
            _dids_g = sorted({str(x) for r in _rows_g for x in ((r or {}).get("member_ids") or [])
                              if (r or {}).get("status") != "banned" and x})[:100]
            _gmap = {}
            if sb is not None and _dids_g:
                _gres = sb.table("bngc_users").select(
                    "discord_id,game_username").in_("discord_id", _dids_g).execute()
                for _u in ((_gres.data or [])):
                    try:
                        _gn = _norm_game((_u or {}).get("game_username"))
                        if _gn:
                            _gmap[_gn] = str((_u or {}).get("discord_id"))
                    except Exception:
                        continue
            if sb is not None and members:
                _nres = sb.table("bngc_users").select(
                    "discord_id,game_username").in_("discord_id", members).execute()
                for _u in ((_nres.data or [])):
                    try:
                        _gn = _norm_game((_u or {}).get("game_username"))
                        if _gn and _gn in _gmap and _gmap[_gn] not in members:
                            await _refuse(f"Game name '{(_u or {}).get('game_username')}' is already registered — ask a host to link it.")
                            return
                    except Exception:
                        continue
        except Exception:
            pass
        try:
            existing = await _comp_reg_rows(t["id"])
            team_idx = sum(1 for r in existing if (r or {}).get("status") != "banned")
            color = _comp_team_color(team_idx) if str((t or {}).get("mode") or "solo") == "duo" else None
            # display names at intake (dashboards list these, no raw IDs)
            member_names: dict = {}
            try:
                for u in (message.mentions or []):
                    try:
                        uid = str(getattr(u, "id", ""))
                        if not uid or uid not in (members or []):
                            continue
                        member_names[uid] = {
                            "username": str(getattr(u, "name", "") or "")[:32],
                            "display_name": str(getattr(u, "display_name", "") or getattr(u, "name", "") or "")[:32],
                        }
                    except Exception:
                        continue
            except Exception:
                pass
            _reg_row = {
                "tourney_id": t["id"],
                "team_label": " / ".join(f"<@{i}>" for i in members),
                "member_ids": members,
                "captain_id": members[0],
                "source_message_id": str(message.id),
                "status": "accepted",
                "team_color": color,
            }
            if member_names:
                _reg_row["member_names"] = member_names
            try:
                sb.table("bngc_comp_registrations").insert(_reg_row).execute()
            except Exception:
                _reg_row.pop("member_names", None)  # pre-migration safe
                sb.table("bngc_comp_registrations").insert(_reg_row).execute()
        except Exception as e:
            # unique violation = same message processed twice (retry)
            print(f"[comp] reg insert failed: {e}")
            return
        # auto-accept: grant the player role immediately (no pending state;
        # bans are the only gate and were checked above).
        try:
            _rid = (t or {}).get("player_role_id") or COMP_PLAYER_ROLE_ID
            for _did in members:
                await _comp_grant_role(message.guild, _did, _rid)
        except Exception as e:
            print(f"[comp] intake role grant failed: {e}")
        # intake verdict right on the message: team color (duo) / ✅ solo,
        # ❌ only on failure paths above. Host ✅/❌ moderation still works:
        # hosts are different users, so their reactions are distinct.
        try:
            await message.add_reaction(color or "✅")
        except Exception as e:
            print(f"[comp] intake reaction failed: {e}")
        # nudge members with no game name to the Set-game-name button so
        # the engine can map their kills (registration without a game name
        # scores nothing until linked).
        try:
            if sb is not None and members:
                _mres = sb.table("bngc_users").select(
                    "discord_id,game_username").in_("discord_id", members).execute()
                _have = set()
                for _u in ((_mres.data or [])):
                    try:
                        if str((_u or {}).get("game_username") or "").strip():
                            _have.add(str((_u or {}).get("discord_id")))
                    except Exception:
                        continue
                _missing = [m for m in members if m not in _have]
                if _missing:
                    _pings = " ".join(f"<@{m}>" for m in _missing)
                    try:
                        await message.reply(
                            f"{_pings} registered — press **Set game name** below "
                            f"(exactly as in-game) so your kills count.")
                    except Exception:
                        pass
        except Exception:
            pass
    except Exception as e:
        print(f"[comp] on_message failed: {e}")
    try:
        await bot.process_commands(message)
    except Exception:
        pass


@bot.event
async def on_raw_reaction_add(payload):
    # Host moderation: ✅ accepts (grants player role), ❌ bans. Only host
    # role members count; only pending rows on reg-listened messages move.
    try:
        if payload.guild_id is None:
            return
        if str(payload.emoji.name) not in ("✅", "❌"):
            return
        if payload.user_id == (bot.user.id if bot.user else 0):
            return
        sb = _comp_sb()
        if sb is None:
            return
        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return
        try:
            reactor = await guild.fetch_member(payload.user_id)
        except Exception:
            return
        if reactor is None or getattr(reactor, "bot", False):
            return
        if not _comp_is_host(reactor):
            return
        try:
            rres = sb.table("bngc_comp_registrations").select(
                "id,tourney_id,member_ids,status").eq(
                "source_message_id", str(payload.message_id)).limit(5).execute()
        except Exception:
            return
        rows = [r for r in (rres.data or []) if (r or {}).get("status") == "pending"]
        if not rows:
            return
        row = rows[0]
        if str(payload.emoji.name) == "✅":
            try:
                sb.table("bngc_comp_registrations").update({"status": "accepted"}).eq("id", row["id"]).execute()
            except Exception:
                return
            # player role: tourney override, else env
            role_id = ""
            try:
                tres = sb.table("bngc_comp_tourneys").select("player_role_id").eq("id", row["tourney_id"]).limit(1).execute()
                role_id = ((tres.data or [{}])[0] or {}).get("player_role_id") or ""
            except Exception:
                pass
            role_id = role_id or COMP_PLAYER_ROLE_ID
            for did in (row.get("member_ids") or []):
                await _comp_grant_role(guild, did, role_id)
            # max-players auto-close (posts the closed message like /stop-registration)
            try:
                ch = guild.get_channel(payload.channel_id) if payload.channel_id else None
                await _comp_autoclose(row["tourney_id"], guild, ch)
            except Exception:
                pass
        else:
            try:
                sb.table("bngc_comp_registrations").update({"status": "banned"}).eq("id", row["id"]).execute()
            except Exception:
                pass
    except Exception as e:
        print(f"[comp] reaction failed: {e}")


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
        did = r.get("discord_id")
        who = f"{_comp_mention(did)} **{name}**" if did else f"**{name}**"
        kills = r.get("kills") or 0
        deaths = r.get("deaths") or 0
        wins = r.get("wins") or 0
        if stat == "wins":
            val = f"{wins} wins"
        else:
            kd = f"{(kills / deaths):.2f} K/D" if deaths else (f"{kills} K/D" if kills else "—")
            val = f"{kills} kills ({kd})"
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{prefix} {who} — {val}")
    e.description = "\n".join(lines)[:4000]
    await interaction.followup.send(embed=e)


@bot.tree.command(name="streaks", description="Show daily / win streaks and best match")
async def streaks_cmd(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    row = await db.get_by_discord(str(target.id))
    if not row:
        return await interaction.response.send_message(f"No data for {target.mention}. They need /register.")
    thumb = fmt.skin_url(row.get("skin"))
    e = discord.Embed(title=f"Streaks — {row.get('discord_username') or target.name}", color=0xf59e0b)
    if thumb:
        e.set_thumbnail(url=thumb)
    e.add_field(name="🔥 Win streak", value=f"x{row.get('win_streak') or 0} (best {row.get('best_win_streak') or 0})", inline=True)
    e.add_field(name="📅 Daily streak", value=f"x{row.get('daily_streak') or 0}", inline=True)
    e.add_field(name="💀 Best match", value=f"{row.get('best_match_kills') or 0} kills", inline=True)
    await interaction.response.send_message(embed=e)


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
