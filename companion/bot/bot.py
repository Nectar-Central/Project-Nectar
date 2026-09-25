import os, secrets, asyncio, discord, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
try:
    from dotenv import load_dotenv
    # Bot's own .env wins over any stale OS/parent-process env (override=True);
    # the other paths stay fallback-only so they can never clobber it.
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
    load_dotenv(override=False)
    load_dotenv(dotenv_path="../.env", override=False)
    load_dotenv(dotenv_path="vercel-api/.env", override=False)
    load_dotenv(dotenv_path="app/.env", override=False)
except: pass
from discord import app_commands
from discord.ext import commands
import asyncio
import database as db
import formatters as fmt
import views
import alerts as alert_mod
import ranked as ranked_mod
import ranked_tourney as rt_mod
import duels as duels_mod
import earn as earn_mod
import comp_results
try:
    import aiohttp
except: aiohttp = None
import urllib.request

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN") or os.getenv("BOT_TOKEN")
GUILD_ID = os.getenv("GUILD_ID") or os.getenv("DISCORD_GUILD_ID") or "1543724907651932282"  # guild sync keeps commands immediate
SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:3000")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Duel wagers (targeted + open) — commands registered here so they sync
# in on_ready alongside everything else.
try:
    duels_mod.register(bot.tree, bot)
except Exception as e:
    print(f"[duels] register failed: {e}")

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
        await db._sb_call(lambda: db.supabase.table("bngc_users").update(
            {"game_username": clean}).eq("discord_id", str(discord_id)).execute())
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


# Recently-404 session ids (unix ts): spares the engine + log spam while a
# dead session is still flagged active in Supabase. Cleared on next 200.
_dead_sessions: dict[str, float] = {}
_DEAD_SESS_TTL = 60.0


async def get_active_session(discord_id: str):
    """Get the user's most recent active session_id from bngc_sessions."""
    if not db.USE_SUPABASE or not db.supabase:
        return None
    try:
        r = await db._sb_call(lambda: db.supabase.table("bngc_sessions").select("session_id, created_at").eq("discord_id", discord_id).eq("is_active", True).order("created_at", desc=True).limit(5).execute())
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
        r = await db._sb_call(lambda: db.supabase.table("bngc_sessions").select("session_id, created_at").eq("discord_id", discord_id).eq("is_active", True).order("created_at", desc=True).limit(limit * 2).execute())
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
    """Fetch live SessionView from the engine via GET /api/session/{id}/state.

    404s (dead/unknown sessions) are cached 60s so loops stop spamming the
    engine + logs; a later capture clears the entry on the next 200.
    """
    if not session_id:
        return None
    try:
        import time as _t
        if _t.time() - float(_dead_sessions.get(str(session_id)) or 0) < _DEAD_SESS_TTL:
            return None
    except Exception:
        pass
    url = f"{SERVER_URL}/api/session/{session_id}/state"
    try:
        if aiohttp:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status == 200:
                        try:
                            _dead_sessions.pop(str(session_id), None)
                        except Exception:
                            pass
                        return await r.json()
                    if r.status == 404:
                        try:
                            import time as _t2
                            _dead_sessions[str(session_id)] = _t2.time()
                        except Exception:
                            pass
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
    # persistent duel wager buttons (Join/Accept/Decline/Forfeit, fixed custom_ids)
    try:
        bot.add_view(duels_mod.DuelView())
    except Exception as e:
        print(f"[duels] persistent view failed: {e}")
    # persistent duel THREAD buttons (Forfeit/Cancel on the live-duel embed)
    try:
        bot.add_view(duels_mod.DuelThreadView())
    except Exception as e:
        print(f"[duels] persistent thread view failed: {e}")
    # duel wager expiry (15-min open/pending) + auto-resolver (60s scoring)
    try:
        bot.loop.create_task(duels_mod.duel_expiry_loop(bot))
    except Exception as e:
        print(f"[duels] failed to start expiry loop: {e}")
    try:
        bot.loop.create_task(duels_mod.duel_resolver_loop(bot))
    except Exception as e:
        print(f"[duels] failed to start resolver loop: {e}")
    # play-to-earn auto-credit (kills/wins -> coins, daily-capped)
    try:
        bot.loop.create_task(earn_mod.earn_loop(bot))
    except Exception as e:
        print(f"[earn] failed to start loop: {e}")
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
    # ranked leaderboard poster (every 3 days, channel history is the clock)
    try:
        bot.loop.create_task(ranked_leaderboard_loop())
    except Exception as e:
        print(f"[ranked] failed to start board loop: {e}")
    # ranked-tourney watcher (code changes incl. server auto-tag + seal boards)
    try:
        bot.loop.create_task(ranked_session_watch_loop())
    except Exception as e:
        print(f"[ranked] failed to start watch loop: {e}")


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


@bot.tree.command(name="fix-empty", description="Set your in-game username when stats show playtime only (null identity)")
@app_commands.describe(game_username="Your in-game name, exactly as it appears in-game")
async def fix_empty_cmd(interaction: discord.Interaction, game_username: app_commands.Range[str, 1, 32]):
    """Heal null-identity tracking by writing bngc_users.game_username.

    When join packets never resolve view.me (me=? in logs, /api/stats identity
    null), kills/wins can't attribute and only session-scoped playtime ticks.
    The processor already falls back to user.game_username for kills, wins and
    defeats, so saving the name here fixes future matches. Past matches are
    not backfilled. Refuses tag-garbage and names already taken by someone
    else (normalized duplicate check, same rule as comp game names).
    """
    await interaction.response.defer(ephemeral=True)
    try:
        row = await db.get_by_discord(str(interaction.user.id))
    except Exception:
        row = None
    if not row:
        return await interaction.followup.send(
            "Not registered — use **/register** first, then run **/fix-empty**.", ephemeral=True)
    raw = str(game_username or "").strip()[:32]
    norm = _norm_game(raw)
    if not norm or "<" in raw or ">" in raw:
        return await interaction.followup.send(
            "Enter your in-game name (1–32 chars, exactly as it appears in-game).", ephemeral=True)
    if not db.USE_SUPABASE or not db.supabase:
        return await interaction.followup.send(
            "Database not configured — try again later.", ephemeral=True)
    try:
        current = (row.get("game_username") or "").strip()
        if current and _norm_game(current) == norm:
            return await interaction.followup.send(
                f"Already set — you are known as **{current}**. Future matches will attribute to that name.", ephemeral=True)
        # Duplicate guard: another linked account already using this name.
        try:
            dup = await db._sb_call(lambda: db.supabase.table("bngc_users").select(
                "discord_id,game_username").ilike("game_username", raw).limit(25).execute())
            for u in ((dup.data if hasattr(dup, "data") else []) or []):
                try:
                    if str((u or {}).get("discord_id")) == str(interaction.user.id):
                        continue
                    if _norm_game((u or {}).get("game_username")) == norm:
                        return await interaction.followup.send(
                            f"'{raw}' is already linked to another account — ask an admin to relink it if it's yours.", ephemeral=True)
                except Exception:
                    continue
        except Exception:
            pass
        upd = await db._sb_call(lambda: db.supabase.table("bngc_users").update(
            {"game_username": raw}).eq("discord_id", str(interaction.user.id)).execute())
        wrote = bool(getattr(upd, "data", None))
        if not wrote:
            return await interaction.followup.send(
                "Couldn't save — try again later.", ephemeral=True)
    except Exception as e:
        print(f"[fix-empty] save failed: {e}")
        return await interaction.followup.send(
            "Couldn't save — try again later.", ephemeral=True)
    # Live-session hint: if the active session still has null me, the drawer
    # keeps showing lobby/unknown until the client re-identifies (rejoin or
    # reload); the saved name covers stat attribution from here on regardless.
    hint = ""
    try:
        sid = await get_active_session(str(interaction.user.id))
        st = await get_session_state(sid) if sid else None
        me = (st or {}).get("me") if st else None
        if not (isinstance(me, dict) and me.get("username")):
            hint = "\nLive session still shows no identity — rejoin/reload the game so the drawer picks it up. "
    except Exception:
        pass
    await interaction.followup.send(
        f"Saved — **{raw}** is now your tracked game name. Future kills/wins attribute to it (past matches are not backfilled).{hint}",
        ephemeral=True)




@bot.tree.command(name="change-user", description="Change your in-game name (comp + ranked + stats)")
@app_commands.describe(game_username="Your new in-game name, exactly as it appears in-game")
async def change_user_cmd(interaction: discord.Interaction, game_username: app_commands.Range[str, 1, 32]):
    """Rename the one shared game_username used for detection everywhere.

    Comp, ranked and stats tracking all resolve kills/wins off this single
    name. Same validation + duplicate guard as profile creation; past
    matches are not backfilled. Never raises beyond followups.
    """
    await interaction.response.defer(ephemeral=True)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.", ephemeral=True)
    me = str(interaction.user.id)
    try:
        ures = await db._sb_call(lambda: sb.table("bngc_users").select(
            "discord_id,game_username").eq("discord_id", me).limit(1).execute())
        cur = (((ures.data or [{}])[0] or {}).get("game_username") or "").strip()
    except Exception:
        cur = ""
    if not cur:
        return await interaction.followup.send(
            "No game name linked yet — use **/register** first.", ephemeral=True)
    raw, err = _comp_valid_game_name(game_username)
    if err:
        return await interaction.followup.send(f"❌ {err}", ephemeral=True)
    norm = _norm_game(raw)
    if _norm_game(cur) == norm:
        return await interaction.followup.send(
            f"Already set — you are known as **{cur}**.", ephemeral=True)
    try:
        if await _ranked_name_locked(sb, me):
            return await interaction.followup.send(
                "🔒 This identity is locked — use /unlock, or ask an admin if it's admin-locked.", ephemeral=True)
    except Exception:
        pass
    try:
        dup = await db._sb_call(lambda: sb.table("bngc_users").select(
            "discord_id,game_username").ilike("game_username", raw).limit(25).execute())
        for u in ((dup.data if hasattr(dup, "data") else []) or []):
            try:
                if str((u or {}).get("discord_id")) == me:
                    continue
                if _norm_game((u or {}).get("game_username")) == norm:
                    return await interaction.followup.send(
                        f"❌ '{raw}' is already linked to another account — ask an admin to link it if it's yours.", ephemeral=True)
            except Exception:
                continue
    except Exception:
        pass
    try:
        await db._sb_call(lambda: sb.table("bngc_users").update({"game_username": raw}).eq("discord_id", me).execute())
    except Exception as e:
        print(f"[change-user] save failed: {e}")
        return await interaction.followup.send("Couldn't save — try again later.", ephemeral=True)
    return await interaction.followup.send(
        f"Updated — **{cur}** → **{raw}**. Future comp, ranked and tracked matches attribute to the new name (past matches are not backfilled).", ephemeral=True)


@bot.tree.command(name="ping", description="Check if the bot is online")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")


# /admin group (owner/dev tools) -same reason as /coin: the tree sits at the
# 100-command Discord cap, so rarely-needed tools ship as subcommands. The
# everyday commands (lock, unlock, warn, register, ...) stay flat on purpose.
admin_group = app_commands.Group(name="admin", description="Owner and dev tools (rarely needed)")


@admin_group.command(name="view_dev_team", description="Shows the main developers for Nectar")
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


@bot.tree.command(name="coinflip", description="Flip a coin (optional wager in Nectar Coins)")
@app_commands.describe(bet="Coins to wager (1-10000, win 2x)",
                       choice="Heads or Tails (required when betting)")
@app_commands.choices(choice=[app_commands.Choice(name="Heads", value="Heads"),
                              app_commands.Choice(name="Tails", value="Tails")])
async def coinflip_cmd(interaction: discord.Interaction,
                       bet: app_commands.Range[int, 1, 100000] | None = None,
                       choice: str | None = None):
    if not bet:
        result = secrets.choice(["Heads", "Tails"])
        embed = discord.Embed(
            title="Coin Flip",
            description=f"The coin landed on **{result}!**",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)
        return
    if choice not in ("Heads", "Tails"):
        return await interaction.response.send_message(
            "Pick **Heads** or **Tails** when you bet.", ephemeral=True)
    await interaction.response.defer()
    me = str(interaction.user.id)
    row = await db.get_by_discord(me)
    if not row:
        return await interaction.followup.send("You need **/register** first.")
    try:
        bal = int(row.get("nectar_coins") or 0)
    except Exception:
        return await interaction.followup.send(
            "Database migration pending — ask a host to apply add_nectar_coins_v252.sql.")
    if bet < 1:
        return await interaction.followup.send("Minimum bet is **1** coin.")
    if bet > 10000:
        return await interaction.followup.send("Maximum bet is **10,000** coins.")
    if bet > bal:
        return await interaction.followup.send(f"Insufficient balance ({bal:,} Nectar Coins).")
    result = secrets.choice(["Heads", "Tails"])
    won = (result == choice)
    try:
        new_bal = await db.add_coin_delta(me, bet if won else -bet,
                                          "flip_win" if won else "flip_loss",
                                          {"bet": int(bet), "choice": choice,
                                           "result": result})
    except Exception as e:
        return await interaction.followup.send(str(e)[:160])
    color = discord.Color.green() if won else discord.Color.red()
    verb = f"won **+{bet:,}**" if won else f"lost **-{bet:,}**"
    embed = discord.Embed(
        title=f"Coin Flip — {result}",
        description=f"You called **{choice}** and {verb} Nectar Coins\nBalance: **{new_bal:,}**",
        color=color,
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="balance", description="Show your Nectar Coin balance")
async def balance_cmd(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user
    info = await db.get_coin_balance(str(target.id))
    if not info:
        return await interaction.response.send_message(
            f"No data for {target.mention}. They need /register.", ephemeral=True)
    e = discord.Embed(title=f"{target.display_name} — {info['balance']:,} Nectar Coins",
                      color=discord.Color.gold())
    e.add_field(name="Earned", value=f"{info['total_earned']:,}", inline=True)
    e.add_field(name="Wagered", value=f"{info['total_wagered']:,}", inline=True)
    e.add_field(name="Daily streak", value=f"x{info['daily_streak']}", inline=True)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="daily", description="Claim your daily Nectar Coins (streak bonus)")
async def daily_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    me = str(interaction.user.id)
    try:
        out = await db.claim_daily_coins(me)
    except LookupError:
        return await interaction.followup.send("You need **/register** first.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(str(e)[:180], ephemeral=True)
    if not out.get("claimed"):
        return await interaction.followup.send(
            f"Already claimed today — streak x{out.get('streak', 0)}, "
            f"balance **{out.get('balance', 0):,}** Nectar Coins. Come back tomorrow.", ephemeral=True)
    await interaction.followup.send(
        f"Claimed **+{out['payout']:,}** Nectar Coins (streak x{out['streak']}) — "
        f"balance **{out['balance']:,}**.", ephemeral=True)


@bot.tree.command(name="pay", description="Send Nectar Coins to another player")
@app_commands.describe(user="Who to pay", amount="Coins to send (1-100000)")
async def pay_cmd(interaction: discord.Interaction, user: discord.User,
                  amount: app_commands.Range[int, 1, 100000]):
    if user.bot:
        return await interaction.response.send_message("You can't pay bots.", ephemeral=True)
    if str(user.id) == str(interaction.user.id):
        return await interaction.response.send_message("You can't pay yourself.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    me = str(interaction.user.id)
    sender = await db.get_by_discord(me)
    if not sender:
        return await interaction.followup.send("You need **/register** first.", ephemeral=True)
    dest = await db.get_by_discord(str(user.id))
    if not dest:
        return await interaction.followup.send(f"{user.mention} needs **/register** first.", ephemeral=True)
    try:
        bal = int(sender.get("nectar_coins") or 0)
    except Exception:
        return await interaction.followup.send(
            "Database migration pending — ask a host to apply add_nectar_coins_v252.sql.", ephemeral=True)
    if amount < 1:
        return await interaction.followup.send("Minimum transfer is 1 coin.", ephemeral=True)
    if amount > 100000:
        return await interaction.followup.send("Maximum transfer is 100,000 coins.", ephemeral=True)
    if amount > bal:
        return await interaction.followup.send(f"Insufficient balance ({bal:,} Nectar Coins).", ephemeral=True)
    try:
        await db.add_coin_delta(me, -int(amount), "pay_sent", {"to": str(user.id)})
        new_dest = await db.add_coin_delta(str(user.id), int(amount),
                                           "pay_received", {"from": me})
    except Exception as e:
        return await interaction.followup.send(str(e)[:180], ephemeral=True)
    await interaction.followup.send(
        f"Sent **{amount:,}** Nectar Coins to {user.mention} (their balance **{new_dest:,}** Nectar Coins).",
        ephemeral=True)


@bot.tree.command(name="coins-leaderboard", description="Richest Nectar Coin holders")
async def coins_leaderboard_cmd(interaction: discord.Interaction):
    rows = await db.get_coin_leaderboard(10)
    if not rows:
        return await interaction.response.send_message("No coin holders yet — claim **/daily** first.")
    lines = [f"{i}. <@{r.get('discord_id')}> — **{int(r.get('nectar_coins') or 0):,}** Nectar Coins"
             for i, r in enumerate(rows, 1)]
    e = discord.Embed(title="Nectar Coins Leaderboard", description="\n".join(lines),
                      color=discord.Color.gold())
    await interaction.response.send_message(embed=e)


# /coin group (top/reset/remove) — one top-level slot: the tree sits at the
# 100-command Discord cap, so coin moderation ships as subcommands.
coin_group = app_commands.Group(name="coin", description="Nectar Coin standings and moderation")


def _coin_admin(interaction: discord.Interaction) -> bool:
    """Pure admin check for coin moderation subcommands. Never raises."""
    try:
        return bool(interaction.user.guild_permissions.administrator)
    except Exception:
        return False


@coin_group.command(name="top", description="Top Nectar Coin holders")
@app_commands.describe(limit="How many to show (1-25)")
async def coin_top_cmd(interaction: discord.Interaction,
                       limit: app_commands.Range[int, 1, 25] = 10):
    rows = await db.get_coin_leaderboard(int(limit or 10))
    if not rows:
        return await interaction.response.send_message("No coin holders yet — claim **/daily** first.")
    medals = ["🥇", "🥈", "🥉"]
    lines = [(medals[i] + " " if i < 3 else f"{i + 1}. ") +
             f"<@{r.get('discord_id')}> — "
             f"**{int(r.get('nectar_coins') or 0):,}** Nectar Coins"
             for i, r in enumerate(rows)]
    e = discord.Embed(title="Nectar Coins Top", description="\n".join(lines),
                      color=discord.Color.gold())
    await interaction.response.send_message(embed=e)


@coin_group.command(name="reset", description="Zero a player's Nectar Coins (admin)")
@app_commands.describe(user="Player whose balance returns to 0")
@app_commands.default_permissions(administrator=True)
async def coin_reset_cmd(interaction: discord.Interaction, user: discord.User):
    if not _coin_admin(interaction):
        return await interaction.response.send_message("Administrator only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        info = await db.get_coin_balance(str(user.id))
    except Exception:
        info = None
    if not info:
        return await interaction.followup.send(
            f"No data for {user.mention}. They need /register.", ephemeral=True)
    bal = int(info.get("balance") or 0)
    if bal <= 0:
        return await interaction.followup.send(
            f"{user.mention} already holds **0** Nectar Coins.", ephemeral=True)
    try:
        await db.add_coin_delta(str(user.id), -bal, "coin_reset",
                                {"by": str(interaction.user.id)})
    except Exception as e:
        return await interaction.followup.send(str(e)[:160], ephemeral=True)
    print(f"[coins] reset {user.id} ({bal:,} -> 0) by {interaction.user.id}")
    await interaction.followup.send(
        f"Reset {user.mention}: **{bal:,}** → **0** Nectar Coins.", ephemeral=True)


@coin_group.command(name="remove", description="Take Nectar Coins from a player (admin)")
@app_commands.describe(user="Player to debit", amount="Coins to remove (1-100000)")
@app_commands.default_permissions(administrator=True)
async def coin_remove_cmd(interaction: discord.Interaction, user: discord.User,
                          amount: app_commands.Range[int, 1, 100000]):
    if not _coin_admin(interaction):
        return await interaction.response.send_message("Administrator only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        info = await db.get_coin_balance(str(user.id))
    except Exception:
        info = None
    if not info:
        return await interaction.followup.send(
            f"No data for {user.mention}. They need /register.", ephemeral=True)
    bal = int(info.get("balance") or 0)
    if int(amount) > bal:
        return await interaction.followup.send(
            f"They hold **{bal:,}** Nectar Coins — can't remove **{int(amount):,}**.", ephemeral=True)
    try:
        new_bal = await db.add_coin_delta(str(user.id), -int(amount), "coin_remove",
                                          {"by": str(interaction.user.id)})
    except Exception as e:
        return await interaction.followup.send(str(e)[:160], ephemeral=True)
    print(f"[coins] remove {amount:,} from {user.id} by {interaction.user.id}")
    await interaction.followup.send(
        f"Removed **{int(amount):,}** Nectar Coins from {user.mention} (balance **{new_bal:,}**).",
        ephemeral=True)


@coin_group.command(name="add", description="Grant Nectar Coins to a player (admin)")
@app_commands.describe(user="Player to credit", amount="Coins to grant (1-100000)")
@app_commands.default_permissions(administrator=True)
async def coin_add_cmd(interaction: discord.Interaction, user: discord.User,
                       amount: app_commands.Range[int, 1, 100000]):
    if not _coin_admin(interaction):
        return await interaction.response.send_message("Administrator only.", ephemeral=True)
    if user.bot:
        return await interaction.response.send_message("You can't grant coins to bots.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        info = await db.get_coin_balance(str(user.id))
    except Exception:
        info = None
    if not info:
        return await interaction.followup.send(
            f"No data for {user.mention}. They need /register.", ephemeral=True)
    try:
        new_bal = await db.add_coin_delta(str(user.id), int(amount), "coin_add",
                                          {"by": str(interaction.user.id)})
    except Exception as e:
        return await interaction.followup.send(str(e)[:160], ephemeral=True)
    print(f"[coins] add {int(amount):,} to {user.id} by {interaction.user.id}")
    await interaction.followup.send(
        f"Granted **{int(amount):,}** Nectar Coins to {user.mention} (balance **{new_bal:,}**).",
        ephemeral=True)


async def _coin_all_ids(limit: int = 5000):
    """All registered discord_ids. Never raises (empty on failure)."""
    try:
        if not db.USE_SUPABASE or not db.supabase:
            return []
        r = await db._sb_call(lambda: db.supabase.table("bngc_users").select(
            "discord_id").limit(int(limit)).execute())
        out = []
        for x in ((r.data if hasattr(r, "data") else []) or []):
            try:
                did = str((x or {}).get("discord_id") or "")
                if did and did not in out:
                    out.append(did)
            except Exception:
                continue
        return out
    except Exception as e:
        print(f"[coins] all-ids failed: {e}")
        return []


@coin_group.command(name="give-all", description="Grant Nectar Coins to EVERY registered player (admin)")
@app_commands.describe(amount="Coins each player receives (1-100000)")
@app_commands.default_permissions(administrator=True)
async def coin_give_all_cmd(interaction: discord.Interaction,
                            amount: app_commands.Range[int, 1, 100000]):
    if not _coin_admin(interaction):
        return await interaction.response.send_message("Administrator only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ids = await _coin_all_ids()
    if not ids:
        return await interaction.followup.send("No registered players found.", ephemeral=True)
    by = str(interaction.user.id)
    paid, failed, total = 0, [], 0
    try:
        prog = await interaction.followup.send(
            f"Granting **{int(amount):,}** Nectar Coins to **{len(ids)}** players…",
            ephemeral=True)
    except Exception:
        prog = None
    for i, did in enumerate(ids, 1):
        try:
            await db.add_coin_delta(did, int(amount), "coin_add",
                                    {"by": by, "mass": "give-all"})
            paid += 1
            total += int(amount)
        except Exception as e:
            print(f"[coins] give-all failed for {did[:6]}: {e}")
            if len(failed) < 5:
                failed.append(f"<@{did}>")
        try:
            if prog is not None and (i % 25 == 0 or i == len(ids)):
                await prog.edit(content=f"Granting… **{i}/{len(ids)}** done.")
        except Exception:
            pass
    print(f"[coins] give-all {int(amount):,} x {paid} players by {by}")
    tail = f" Failed: {', '.join(failed)}." if failed else ""
    await interaction.followup.send(
        f"Granted **{int(amount):,}** Nectar Coins to **{paid}/{len(ids)}** players "
        f"(**{total:,}** total).{tail}", ephemeral=True)


@coin_group.command(name="reset-all", description="Zero EVERY player's Nectar Coins (admin, irreversible)")
@app_commands.describe(confirm="Type RESET ALL to confirm")
@app_commands.default_permissions(administrator=True)
async def coin_reset_all_cmd(interaction: discord.Interaction, confirm: str):
    if not _coin_admin(interaction):
        return await interaction.response.send_message("Administrator only.", ephemeral=True)
    if str(confirm or "").strip().upper() != "RESET ALL":
        return await interaction.response.send_message(
            'Type `RESET ALL` in confirm to zero every balance. This cannot be undone.',
            ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        rows = await db._sb_call(lambda: db.supabase.table("bngc_users").select(
            "discord_id,nectar_coins").limit(5000).execute()) \
            if db.USE_SUPABASE and db.supabase else None
        rows = (rows.data if hasattr(rows, "data") else []) or []
    except Exception as e:
        print(f"[coins] reset-all read failed: {e}")
        return await interaction.followup.send("Couldn't read balances — try again later.",
                                               ephemeral=True)
    by = str(interaction.user.id)
    zeroed, skipped, wiped = 0, 0, 0
    try:
        prog = await interaction.followup.send(
            f"Zeroing **{len(rows)}** balances…", ephemeral=True)
    except Exception:
        prog = None
    for i, r in enumerate(rows, 1):
        try:
            did = str((r or {}).get("discord_id") or "")
            bal = int((r or {}).get("nectar_coins") or 0)
            if not did:
                continue
            if bal <= 0:
                skipped += 1
                continue
            await db.add_coin_delta(did, -bal, "coin_reset",
                                    {"by": by, "mass": "reset-all"})
            zeroed += 1
            wiped += bal
        except Exception as e:
            print(f"[coins] reset-all failed: {e}")
        try:
            if prog is not None and (i % 25 == 0 or i == len(rows)):
                await prog.edit(content=f"Zeroing… **{i}/{len(rows)}** done.")
        except Exception:
            pass
    print(f"[coins] reset-all by {by}: {zeroed} zeroed, {wiped:,} wiped")
    await interaction.followup.send(
        f"Reset **{zeroed}** players to **0** (**{wiped:,}** Nectar Coins wiped, "
        f"{skipped} already empty).", ephemeral=True)


bot.tree.add_command(coin_group)


@bot.tree.command(name="coins-history", description="Your recent Nectar Coin transactions")
async def coins_history_cmd(interaction: discord.Interaction):
    entries = await db.get_coin_ledger(str(interaction.user.id), 10)
    if not entries:
        return await interaction.response.send_message(
            "No transactions yet — claim **/daily** first.", ephemeral=True)
    lines = []
    for x in entries:
        try:
            d = int(x.get("delta") or 0)
            lines.append(f"{'+' if d >= 0 else ''}{d:,} — {x.get('reason')} (bal {int(x.get('balance_after') or 0):,})")
        except Exception:
            continue
    e = discord.Embed(title="Recent Nectar Coins Transactions", description="\n".join(lines) or "—",
                      color=discord.Color.gold())
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="slots", description="Spin 3-reel slots (1-10000 coins; trips pay up to 25x)")
@app_commands.describe(bet="Coins to wager (1-10000)")
async def slots_cmd(interaction: discord.Interaction,
                    bet: app_commands.Range[int, 1, 100000]):
    await interaction.response.defer()
    me = str(interaction.user.id)
    row = await db.get_by_discord(me)
    if not row:
        return await interaction.followup.send("You need **/register** first.")
    try:
        bal = int(row.get("nectar_coins") or 0)
    except Exception:
        return await interaction.followup.send(
            "Database migration pending — ask a host to apply add_nectar_coins_v252.sql.")
    if bet < 1:
        return await interaction.followup.send("Minimum bet is **1** coin.")
    if bet > 10000:
        return await interaction.followup.send("Maximum bet is **10,000** coins.")
    if bet > bal:
        return await interaction.followup.send(f"Insufficient balance ({bal:,} Nectar Coins).")
    symbols = db.spin_slots()
    kind = db.slots_kind(symbols)
    delta = db.slots_delta(bet, symbols)
    reason = {"trips": "slots_win", "pair": "slots_refund", "loss": "slots_loss"}[kind]
    try:
        new_bal = await db.add_coin_delta(me, delta, reason,
                                          {"bet": int(bet), "symbols": symbols,
                                           "kind": kind})
    except Exception as e:
        return await interaction.followup.send(str(e)[:160])
    reels = " | ".join(symbols)
    if kind == "trips":
        mult = db.SLOT_TRIPS_MULT.get(symbols[0], 0)
        color = discord.Color.green()
        desc = f"**{reels}**\n🎰 JACKPOT — trips pay **{mult}x**: won **+{delta:,}** Nectar Coins"
    elif kind == "pair":
        color = discord.Color.gold()
        desc = f"**{reels}**\nPair — bet refunded (Nectar Coins)"
    else:
        color = discord.Color.red()
        desc = f"**{reels}**\nNo match — lost **-{bet:,}** Nectar Coins"
    e = discord.Embed(title="🎰 Slots", description=f"{desc}\nBalance: **{new_bal:,}**",
                      color=color)
    await interaction.followup.send(embed=e)


@bot.tree.command(name="dice", description="Roll 1-100 vs 50: Over wins on 51+, Under on 49- (50 pushes)")
@app_commands.describe(bet="Coins to wager (1-10000)",
                       choice="Over (51-100) or Under (1-49); 50 refunds")
@app_commands.choices(choice=[app_commands.Choice(name="Over", value="Over"),
                              app_commands.Choice(name="Under", value="Under")])
async def dice_cmd(interaction: discord.Interaction,
                   bet: app_commands.Range[int, 1, 100000],
                   choice: str):
    if choice not in ("Over", "Under"):
        return await interaction.response.send_message(
            "Pick **Over** or **Under**.", ephemeral=True)
    await interaction.response.defer()
    me = str(interaction.user.id)
    row = await db.get_by_discord(me)
    if not row:
        return await interaction.followup.send("You need **/register** first.")
    try:
        bal = int(row.get("nectar_coins") or 0)
    except Exception:
        return await interaction.followup.send(
            "Database migration pending — ask a host to apply add_nectar_coins_v252.sql.")
    if bet < 1:
        return await interaction.followup.send("Minimum bet is **1** coin.")
    if bet > 10000:
        return await interaction.followup.send("Maximum bet is **10,000** coins.")
    if bet > bal:
        return await interaction.followup.send(f"Insufficient balance ({bal:,} Nectar Coins).")
    roll = db.roll_dice()
    delta = db.dice_delta(bet, choice, roll)
    reason = "dice_win" if delta > 0 else "dice_push" if delta == 0 else "dice_loss"
    try:
        new_bal = await db.add_coin_delta(me, delta, reason,
                                          {"bet": int(bet), "choice": choice,
                                           "roll": roll})
    except Exception as e:
        return await interaction.followup.send(str(e)[:160])
    if delta > 0:
        color, outcome = discord.Color.green(), f"won **+{delta:,}** Nectar Coins"
    elif delta == 0:
        color, outcome = discord.Color.gold(), "pushed on **50** — bet refunded (Nectar Coins)"
    else:
        color, outcome = discord.Color.red(), f"lost **-{bet:,}** Nectar Coins"
    e = discord.Embed(title=f"🎲 Dice — rolled {roll}",
                      description=f"You called **{choice}** and {outcome}\nBalance: **{new_bal:,}**",
                      color=color)
    await interaction.followup.send(embed=e)


def _bets_api_down():
    try:
        base = str(SERVER_URL or "").strip().rstrip("/")
    except Exception:
        base = ""
    return ("Server unreachable — match betting needs the API"
            + (f" ({base})" if base else "") + ". "
              "Start it (`uvicorn server.main:app`) and check SERVER_URL, then try again.")


@bot.tree.command(name="matches", description="Open matches you can bet Nectar Coins on")
async def matches_cmd(interaction: discord.Interaction):
    data = await _api_get("/api/coins/bets/open?limit=25")
    if not data:
        return await interaction.response.send_message(_bets_api_down(), ephemeral=True)
    if data.get("error"):
        return await interaction.response.send_message(str(data["error"])[:180], ephemeral=True)
    matches = data.get("matches") or []
    if not matches:
        return await interaction.response.send_message(
            "No open matches right now — any tracked live match shows up here "
            "(any mode: pubs, ranked, tourneys). Play one and check back.")
    lines = []
    for m in matches[:12]:
        try:
            lines.append(f"`{m.get('match_key') or m.get('key')}` — **{m.get('label')}** "
                         f"({m.get('participant_count', 0)} players, "
                         f"{m.get('pool', 0):,} Nectar Coins pool)")
        except Exception:
            continue
    e = discord.Embed(title="🎯 Open matches",
                      description="\n".join(lines) or "—",
                      color=discord.Color.gold())
    e.set_footer(text="Bet with /bet match_key pick:@player amount (1-10000) on any live match. Win-pool winners split it.")
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="bet", description="Bet Nectar Coins on a player (win pool, out, or kills line)")
@app_commands.describe(match_key="Match key from /matches (e.g. comp:3:2)",
                       player="Who you are betting on (must be playing)",
                       amount="Coins to stake (1-10000)",
                       bet_type="Win = pool split. Out = they get eliminated (live rooms). Kills = they reach X kills.",
                       line="Kills line 2-20 (required for Kills bets)")
@app_commands.choices(bet_type=[app_commands.Choice(name="Win", value="win"),
                                app_commands.Choice(name="Out", value="out"),
                                app_commands.Choice(name="Kills", value="kills")])
async def bet_cmd(interaction: discord.Interaction, match_key: str,
                  player: discord.User, amount: app_commands.Range[int, 1, 100000],
                  bet_type: str = "win",
                  line: app_commands.Range[int, 1, 30] | None = None):
    if player.bot:
        return await interaction.response.send_message("You can't bet on picks that are bots.", ephemeral=True)
    if bet_type not in ("win", "out", "kills"):
        return await interaction.response.send_message("Type must be Win, Out or Kills.", ephemeral=True)
    if bet_type == "kills" and line is None:
        return await interaction.response.send_message(
            "Kills bets need a line — e.g. `line:5` means they must reach 5 kills.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    payload = {
        "discord_id": str(interaction.user.id),
        "match_key": str(match_key or "").strip(),
        "pick_discord_id": str(player.id),
        "amount": int(amount),
        "bet_type": bet_type,
    }
    if line is not None:
        payload["line"] = int(line)
    status, body = await _api_post("/api/coins/bets", payload)
    if status == 0:
        return await interaction.followup.send(_bets_api_down(), ephemeral=True)
    if status != 200 or (body or {}).get("error"):
        return await interaction.followup.send(
            str((body or {}).get("error") or f"Bet failed (HTTP {status}).")[:180],
            ephemeral=True)
    if (body or {}).get("bet_type", "win") == "win":
        await interaction.followup.send(
            f"Bet **{body['amount']:,}** Nectar Coins on **{body.get('pick_name', player.display_name)}** to win "
            f"**{body.get('label', body['match_key'])}**.\n"
            f"Pool is now **{body.get('pool', 0):,}** Nectar Coins across {body.get('pool_bets', 0)} bets. "
            f"Winners split the pool (5% rake).", ephemeral=True)
    else:
        desc = (f"**{player.display_name}** to reach **{body.get('line')} kills**"
                if body.get("bet_type") == "kills" else
                f"**{player.display_name}** to get eliminated")
        await interaction.followup.send(
            f"Bet **{body['amount']:,}** Nectar Coins on {desc} in "
            f"**{body.get('label', body['match_key'])}** @ **{body.get('odds')}x** — "
            f"pays **{body.get('to_win', 0):,}** Nectar Coins.", ephemeral=True)


@bot.tree.command(name="my-bets", description="Your open and recent Nectar Coin match bets")
async def my_bets_cmd(interaction: discord.Interaction):
    data = await _api_get(f"/api/coins/bets/mine?discord_id={interaction.user.id}")
    if not data:
        return await interaction.response.send_message(_bets_api_down(), ephemeral=True)
    if data.get("error"):
        return await interaction.response.send_message(str(data["error"])[:180], ephemeral=True)
    open_b, settled = data.get("open") or [], data.get("settled") or []
    if not open_b and not settled:
        return await interaction.response.send_message(
            "No bets yet — see **/matches** and place one with **/bet**.", ephemeral=True)
    e = discord.Embed(title=f"🎟️ {interaction.user.display_name}'s bets",
                      color=discord.Color.gold())

    def _tag(b):
        try:
            t = str(b.get("bet_type") or "win")
            if t == "kills":
                return f" [{b.get('line')}K @ {b.get('odds')}x]"
            if t == "out":
                return f" [out @ {b.get('odds')}x]"
        except Exception:
            pass
        return ""

    if open_b:
        e.add_field(name="Open", value="\n".join(
            f"{b.get('amount', 0):,} Nectar Coins on **{b.get('pick_name')}**{_tag(b)} ({b.get('label')})"
            for b in open_b[:8]) or "—", inline=False)
    if settled:
        e.add_field(name="Recent", value="\n".join(
            (f"{b.get('amount', 0):,} → **+{b.get('payout', 0):,}** ✅"
             if b.get("status") == "won" else
             f"{b.get('amount', 0):,} refunded ↩️"
             if b.get("status") == "refunded" else
             f"{b.get('amount', 0):,} lost ❌")
            + f" — {b.get('pick_name')} ({b.get('label')})"
            for b in settled[:8]) or "—", inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="match-pool", description="Show the betting pool for a match")
@app_commands.describe(match_key="Match key from /matches (e.g. comp:3:2)")
async def match_pool_cmd(interaction: discord.Interaction, match_key: str):
    import urllib.parse as _up
    data = await _api_get("/api/coins/bets/pool?match_key="
                          + _up.quote(str(match_key or "").strip()))
    if not data:
        return await interaction.response.send_message(_bets_api_down(), ephemeral=True)
    if data.get("error"):
        return await interaction.response.send_message(str(data["error"])[:180], ephemeral=True)
    per = data.get("per_pick") or []
    pool = int(data.get("pool") or 0)
    lines = [f"**{p.get('name')}** — {p.get('total', 0):,} Nectar Coins ({p.get('bettors', 0)} bets)"
             for p in per[:15]]
    try:
        evd = data.get("evidence") or None
        if evd and str(data.get("status") or "") == "open":
            tgt = evd.get("kill_target")
            form = [f"{'💀' if p.get('status') != 'alive' else '🟢'} "
                    f"{p.get('name')} — {p.get('kills', 0)}K"
                    for p in (evd.get("players") or [])[:12]]
            if form:
                lines.append(f"\n__Live feed (first to {tgt} wins)__")
                lines.extend(form)
    except Exception:
        pass
    title = f"🎯 Pool — {data.get('label', match_key)}"
    if str(data.get("status") or "") != "open":
        w = f" Winner: **{data.get('winner_name') or data.get('winner')}**." \
            if data.get("winner") else " No winner — stakes refunded."
        title += f" [{data.get('status')}]" + (f" — paid {int(data.get('paid') or 0):,} Nectar Coins" if data.get("paid") else "")
        if w:
            lines.append(w)
    else:
        lines.append(f"\nTotal **{pool:,}** Nectar Coins — winners split it (5% rake).")
        try:
            sides = data.get("side_bets") or []
            if sides:
                sl = []
                for s in sides[:10]:
                    try:
                        what = (f"{s.get('line')} kills @ {s.get('odds')}x"
                                if s.get("type") == "kills" else
                                f"out @ {s.get('odds')}x")
                        sl.append(f"{s.get('amount', 0):,} on **{s.get('pick_name')}** {what}")
                    except Exception:
                        continue
                if sl:
                    lines.append("\n__Side bets__")
                    lines.extend(sl)
            offered = data.get("offered") or []
            if offered:
                ol = []
                for o in offered[:12]:
                    try:
                        ks = ", ".join(f"{k}:{v}x" for k, v in (o.get("kills") or {}).items())
                        extra = f" | out {o.get('out_odds')}x" if o.get("out_odds") else ""
                        prov = " (new)" if o.get("provisional") else ""
                        ol.append(f"**{o.get('name')}** ({o.get('avg_kills')} K/m{prov}) — {ks}{extra}")
                    except Exception:
                        continue
                if ol:
                    lines.append("\n__Odds (per coin staked)__")
                    lines.extend(ol)
        except Exception:
            pass
    e = discord.Embed(title=title, description="\n".join(lines) or "No bets yet — be the first.",
                      color=discord.Color.gold())
    await interaction.response.send_message(embed=e)


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

@admin_group.command(name="refresh-token", description="Generate a new login token (old one stays valid for 5 min)")
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

@admin_group.command(name="adminlock", description="Admin-lock a user (only admins can unlock)")
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

@admin_group.command(name="adminunlock", description="Unlock any user (admin only, bypasses admin-lock)")
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
        # Engine cold (or modeless bf/DF lobby play): combat rows still prove it.
        try:
            _act, _agm, _ats = await db.recent_combat_activity(did, 180)
        except Exception:
            _act, _agm = False, None
        if _act:
            try:
                _gm = fmt.format_game_mode(_agm) if _agm else "—"
            except Exception:
                _gm = str(_agm or "—")
            e = discord.Embed(title="Status", color=0x10b981)
            e.add_field(name="Status", value="In Match", inline=True)
            e.add_field(name="Game mode", value=_gm, inline=True)
            return await interaction.followup.send(embed=e)
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
    if not in_match:
        # Same modeless fallback as /presence.
        try:
            _act, _agm, _ats = await db.recent_combat_activity(did, 180)
        except Exception:
            _act, _agm = False, None
        if _act:
            in_match = True
            if not gm_raw and _agm:
                gm_raw = _agm
    elif not gm_raw:
        # Public matchmaking rooms start modeless (gm lives in the outbound
        # request the hook can miss) — heal from combat rows when they flow.
        try:
            _act2, _agm2, _ats2 = await db.recent_combat_activity(did, 180)
        except Exception:
            _act2, _agm2 = False, None
        if _act2 and _agm2:
            gm_raw = _agm2
    gm = fmt.format_game_mode(gm_raw) if gm_raw else "—"
    e = discord.Embed(title="Status", color=0x10b981 if in_match else 0x2b2d31)
    e.add_field(name="Status", value="In Match" if in_match else "In Lobby", inline=True)
    e.add_field(name="Game mode", value=gm, inline=True)
    if in_match and match_room:
        e.add_field(name="Players", value=f"{match_room.get('players_count', '?')}/{match_room.get('max_players') or '?'}", inline=True)
        if match_rid and match_rid != "lobby":
            e.add_field(name="Match ID", value=f"`{match_rid[:8]}`", inline=True)
    await interaction.followup.send(embed=e)


PRESENCE_ONLINE_SEC = 300  # live-companion window for /presence + wager gate default


async def _presence_context(code):
    """Tourney / Ranked context for a room code, else '—' (party is resolved
    from engine state by the caller). Never raises."""
    try:
        code = (code or "").strip().upper()
        if not code or not db.USE_SUPABASE or not db.supabase:
            return "—"
        try:
            t = await db._sb_call(lambda: db.supabase.table("bngc_comp_tourneys").select(
                "name").eq("code", code).neq("status", "ended").limit(1).execute())
            rows = (t.data if hasattr(t, "data") else []) or []
            if rows:
                return f"Tourney: {((rows[0] or {}).get('name') or code)}"
        except Exception:
            pass
        try:
            rm = await asyncio.to_thread(ranked_mod.get_open_room_by_code, code)
            if rm:
                return "Ranked"
        except Exception:
            pass
    except Exception:
        pass
    return "—"


@bot.tree.command(name="afk", description="Toggle AFK (shown in /presence)")
@app_commands.describe(message="Optional AFK note shown to others")
async def afk_cmd(interaction: discord.Interaction, message: app_commands.Range[str, 1, 140] | None = None):
    await interaction.response.defer(ephemeral=True)
    me = str(interaction.user.id)
    try:
        row = await db.get_by_discord(me)
    except Exception:
        row = None
    if not row:
        return await interaction.followup.send("You need **/register** first.", ephemeral=True)
    currently = bool((row or {}).get("is_afk"))
    try:
        on, msg = await db.set_afk(me, not currently, message)
    except RuntimeError as e:
        return await interaction.followup.send(str(e)[:180], ephemeral=True)
    except LookupError:
        return await interaction.followup.send("You need **/register** first.", ephemeral=True)
    except Exception as e:
        print(f"[afk] save failed for {me}: {e}")
        return await interaction.followup.send("Couldn't save — try again later.", ephemeral=True)
    if on:
        print(f"[afk] {me} is now AFK")
        return await interaction.followup.send(
            f"You are now AFK.{(' Note: ' + msg) if msg else ''} Run **/afk** again when back.",
            ephemeral=True)
    print(f"[afk] {me} back from AFK")
    return await interaction.followup.send("Welcome back — AFK cleared.", ephemeral=True)


@bot.tree.command(name="presence", description="Live presence: match/lobby, mode, ranked/tourney, version, online")
@app_commands.describe(user="Whose presence to show (default: you)")
async def presence_cmd(interaction: discord.Interaction, user: discord.User | None = None):
    await interaction.response.defer(ephemeral=False)
    target = user or interaction.user
    tid = str(target.id)
    try:
        row = await db.get_by_discord(tid)
    except Exception:
        row = None
    if not row:
        return await interaction.followup.send(
            f"No data for {target.mention} — they need /register.")
    # Live engine state: prefer the session with a match room.
    state, match_room, match_rid = None, None, None
    try:
        sids = await get_active_sessions(tid, limit=5)
        best = None
        for cand in sids:
            st = await get_session_state(cand)
            if not st:
                continue
            rooms = st.get("rooms") or {}
            if any(rid != "lobby" and rv.get("state") == "match" for rid, rv in rooms.items()):
                best = st
                break
            if best is None:
                best = st
        state = best
    except Exception:
        state = None
    in_match = False
    gm_raw, code, party = None, None, {}
    try:
        if state:
            rooms = state.get("rooms") or {}
            try:
                # Prefer the current room when it is a live non-lobby room
                # (match state, join code, mode, or our own creation): dict
                # order can otherwise surface a stale earlier match room first
                # and hide the live room's gm/code (private DF read as Pub/—).
                # Retired (ghost-healed) rooms never win.
                _cur_id = state.get("current_room_id")
                _cur = rooms.get(_cur_id) if _cur_id else None
                if (_cur and _cur_id != "lobby"
                        and not (_cur or {}).get("retired")
                        and ((_cur or {}).get("state") == "match"
                             or (_cur or {}).get("short_code")
                             or (_cur or {}).get("game_mode")
                             or (_cur or {}).get("created_by_me"))):
                    match_rid, match_room = _cur_id, _cur
            except Exception:
                pass
            if match_room is None:
                for rid, rv in rooms.items():
                    if (rid != "lobby" and (rv or {}).get("state") == "match"
                            and not (rv or {}).get("retired")):
                        match_rid, match_room = rid, rv
                        break
            if match_room is None:
                cur_id = state.get("current_room_id")
                if cur_id and cur_id in rooms:
                    match_rid, match_room = cur_id, rooms[cur_id]
            in_match = bool(match_room and match_room.get("state") == "match")
            gm_raw = (match_room.get("game_mode") if match_room else None) or state.get("game_mode")
            party = (match_room.get("party") if match_room else None) or state.get("party") or {}
            try:
                # Private rooms carry a join code (used for context lookup
                # only — no Room field is shown); pubs have none.
                code = (state.get("short_code")
                        or (match_room.get("short_code") if match_room else None)
                        or (match_room.get("code") if match_room else None))
            except Exception:
                code = None
    except Exception:
        pass
    try:
        gm = fmt.format_game_mode(gm_raw) if gm_raw else "—"
    except Exception:
        gm = str(gm_raw or "—")
    version = str((row or {}).get("companion_version") or "").strip()[:32] or "unknown"
    online, ts = db.online_status((row or {}).get("last_online"), PRESENCE_ONLINE_SEC)
    afk = bool((row or {}).get("is_afk"))
    afk_msg = str((row or {}).get("afk_message") or "").strip()
    if state:
        if in_match:
            status = "In Match"
            # Public matchmaking rooms start modeless (the gm lives in the
            # outbound request, which the hook can miss) — heal the display
            # from flowing combat rows once they exist.
            if not gm_raw:
                try:
                    _act2, _agm2, _ats2 = await db.recent_combat_activity(tid, 180)
                except Exception:
                    _act2, _agm2 = False, None
                if _act2 and _agm2:
                    gm_raw = _agm2
                    try:
                        gm = fmt.format_game_mode(gm_raw)
                    except Exception:
                        gm = str(gm_raw)
        else:
            status = "In Lobby"
            # Modes without scene/stG signals (bf/DF lobby play) never flip
            # room state — flowing combat rows prove a live match anyway.
            try:
                _act, _agm, _ats = await db.recent_combat_activity(tid, 180)
            except Exception:
                _act, _agm, _ats = False, None, None
            if _act:
                status, in_match = "In Match", True
                if not gm_raw and _agm:
                    gm_raw = _agm
                    try:
                        gm = fmt.format_game_mode(gm_raw)
                    except Exception:
                        gm = str(gm_raw)
    elif online:
        status = "Online"
    else:
        status = "Offline"
    if afk:
        status += f" · AFK{(': ' + afk_msg) if afk_msg else ''}"
    try:
        ctx = await _presence_context(code)
    except Exception:
        ctx = "—"
    if ctx == "—":
        try:
            _mine = bool(match_room and match_room.get("created_by_me"))
            if isinstance(party, dict) and party.get("is_party"):
                # Show the 5-char join code, never the internal room uuid that
                # the engine stores on party.code.
                _pc = code
                if not _pc:
                    try:
                        _raw = str(party.get("code") or "")
                        _pc = _raw if (len(_raw) == 5 and _raw.isalnum()) else None
                    except Exception:
                        _pc = None
                ctx = "Party" + (f" `{_pc}`" if _pc else "")
            elif code:
                # Private room with a join code (not tourney/ranked) — pubs
                # have no code, so this is never a public match.
                ctx = f"Party `{code}`"
            elif _mine:
                # Private room born from our own CreateRoom whose request
                # (gm/pID/Party) never arrived — still not a pub.
                ctx = "Party"
            elif in_match:
                # Match room, no code, no tourney/ranked/party: public match.
                ctx = "Pub"
        except Exception:
            pass
    color = 0x10b981 if (in_match or (online and state is None)) else (0x2b2d31 if not online else 0xf59e0b)
    try:
        dot = "🟢" if in_match else ("🟡" if (state or online) else "⚫")
    except Exception:
        dot = "⚫"
    e = discord.Embed(title=f"{dot} Presence — {target.display_name}", color=color)
    e.add_field(name="Status", value=status, inline=True)
    e.add_field(name="Game mode", value=gm, inline=True)
    e.add_field(name="Context", value=ctx, inline=True)
    e.add_field(name="Companion", value=version, inline=True)
    e.add_field(name="Online", value="Yes" if online else "No", inline=True)
    if ts is not None:
        try:
            e.add_field(name="Last online", value=f"<t:{int(ts.timestamp())}:R>", inline=True)
        except Exception:
            pass
    else:
        e.add_field(name="Last online", value="never", inline=True)
    await interaction.followup.send(embed=e)


@bot.tree.command(name="match", description="Show current match info")
async def match_cmd(interaction: discord.Interaction):
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
            r = await db._sb_call(lambda: db.supabase.table("bngc_alerts").select("*").order("created_at", desc=True).limit(max(1, min(20, limit))).execute())
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
        await db._sb_call(lambda: db.supabase.table("bngc_users").update({"lobby_music_url": url}).eq("discord_id", did).execute())
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
        await db._sb_call(lambda: db.supabase.table("bngc_users").update({"lobby_music_url": None}).eq("discord_id", did).execute())
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

@admin_group.command(name="precision", description="Show server-side processing precision (kills/deaths/wins)")
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
    """All registration rows for a tourney (id, members, status, source msg). Never raises."""
    try:
        sb = _comp_sb()
        if sb is None:
            return []
        res = await db._sb_call(lambda: sb.table("bngc_comp_registrations").select(
            "id,member_ids,status,source_message_id,team_color").eq("tourney_id", int(tourney_id)).limit(1000).execute())
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


def _comp_valid_game_name(raw) -> tuple[str | None, str | None]:
    """Validate a comp-profile game name. Pure: (cleaned, None) or (None, error).

    Rules: 1-32 chars after strip; no <> tags; no dc: prefix abuse;
    allowed charset letters/numbers/spaces/_.- (unicode word chars ok).
    """
    try:
        import re as _re
        s = str(raw or "").strip()[:32]
        if not s:
            return None, "Enter your in-game name (1–32 chars)."
        if "<" in s or ">" in s:
            return None, "No <> tags allowed — use your plain in-game name."
        if s.lower().startswith("dc:"):
            return None, "Don't prefix with dc: — just your in-game name."
        if not _re.match(r"^[\w .\-']+$", s, flags=_re.UNICODE):
            return None, "Letters, numbers, spaces and _ . - only."
        if not _norm_game(s):
            return None, "Enter your in-game name (1–32 chars)."
        return s, None
    except Exception:
        return None, "Enter your in-game name (1–32 chars)."


async def _comp_members_with_profile(sb, member_ids: list) -> set:
    """Discord IDs (str) that already have a comp profile (game_username set)."""
    try:
        if sb is None or not member_ids:
            return set()
        res = await db._sb_call(lambda: sb.table("bngc_users").select(
            "discord_id,game_username").in_("discord_id", [str(m) for m in member_ids]).execute())
        have = set()
        for u in (res.data or []):
            try:
                if str(((u or {}).get("game_username")) or "").strip():
                    have.add(str((u or {}).get("discord_id")))
            except Exception:
                continue
        return have
    except Exception:
        return set()


async def _comp_promote_if_complete(guild, tourney_id: int, source_message_id: str | None = None,
                                    reg_id=None) -> bool:
    """Flip a pending reg to accepted once ALL members have profiles.

    Grants the player role, swaps ❓→✅ on the source message. Row located
    by reg_id (preferred) or source_message_id. Returns True when the row
    is now accepted. Never raises.
    """
    try:
        sb = _comp_sb()
        if sb is None:
            return False
        if reg_id is not None:
            try:
                rres = await db._sb_call(lambda: sb.table("bngc_comp_registrations").select(
                    "id,tourney_id,member_ids,status,team_color,source_message_id").eq(
                    "id", reg_id).limit(1).execute())
            except Exception:
                return False
        elif source_message_id:
            try:
                rres = await db._sb_call(lambda: sb.table("bngc_comp_registrations").select(
                    "id,tourney_id,member_ids,status,team_color,source_message_id").eq(
                    "source_message_id", str(source_message_id)).limit(5).execute())
            except Exception:
                return False
        else:
            return False
        rows = rres.data or []
        if not rows:
            return False
        row = rows[0]
        if (row or {}).get("status") == "accepted":
            return True
        members = [str(x) for x in ((row or {}).get("member_ids") or []) if x]
        if not members:
            return False
        have = await _comp_members_with_profile(sb, members)
        if any(m not in have for m in members):
            return False
        await db._sb_call(lambda: sb.table("bngc_comp_registrations").update({"status": "accepted"}).eq("id", row["id"]).execute())
        # role grant: tourney override else env
        try:
            trow = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select("player_role_id").eq(
                "id", int(tourney_id)).limit(1).execute())
            rid = (((trow.data or [{}])[0] or {}).get("player_role_id")) or COMP_PLAYER_ROLE_ID
        except Exception:
            rid = COMP_PLAYER_ROLE_ID
        if guild is not None:
            for did in members:
                try:
                    await _comp_grant_role(guild, did, rid)
                except Exception:
                    continue
        # reaction swap on the signup message (skipped when unknown)
        try:
            _src = source_message_id or (row or {}).get("source_message_id")
            if guild is not None and _src:
                for ch in guild.text_channels:
                    try:
                        msg = await ch.fetch_message(int(_src))
                    except Exception:
                        continue
                    try:
                        me = guild.me or (bot.user and await guild.fetch_member(bot.user.id))
                    except Exception:
                        me = None
                    try:
                        for r in (getattr(msg, "reactions", None) or []):
                            try:
                                if str(getattr(r.emoji, "name", r.emoji)) == "❓" and me is not None:
                                    await msg.remove_reaction("❓", me)
                            except Exception:
                                continue
                    except Exception:
                        pass
                    try:
                        await msg.add_reaction((row or {}).get("team_color") or "✅")
                    except Exception:
                        pass
                    break
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[comp] promote failed: {e}")
        return False


async def _announce_comp_profile(interaction: discord.Interaction, raw: str) -> None:
    """Public mention confirming a new comp profile + change-name reminder.

    Best-effort (ephemeral confirm already sent); never raises."""
    try:
        ch = getattr(interaction, "channel", None)
        if ch is None:
            return
        mention = getattr(interaction.user, "mention", str(interaction.user.id))
        await ch.send(
            f"{mention} comp profile created as **{raw}** ✅ — if you change your "
            f"name in-game, run **/change-user** so the system keeps catching "
            f"your kills and wins.")
    except Exception as e:
        print(f"[comp] profile announce failed: {e}")


class _CompProfileView(discord.ui.View):
    """Per-message 'Create comp profile' button (times out, unlike persistent view)."""

    def __init__(self, tourney_id: int):
        super().__init__(timeout=600)
        self.tourney_id = int(tourney_id)

    @discord.ui.button(label="Create comp profile", style=discord.ButtonStyle.success,
                       custom_id="comp:create-profile")
    async def create_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            return await interaction.response.send_modal(_GameNameModal(int(self.tourney_id)))
        except Exception as e:
            print(f"[comp] open profile modal failed: {e!r}")
            try:
                return await interaction.response.send_message(
                    f"Couldn't open the form ({str(e)[:120]}), try again.", ephemeral=True)
            except Exception:
                pass


@admin_group.command(name="untag", description="Remove the scrim/tourney tag from a room code")
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


async def _comp_tourney_hosts(sb, tourney_id) -> list:
    """Per-tourney host discord IDs (hosts column + created_by fallback).
    Missing column (pre-v233 DB) reads as no extra hosts. Never raises.

    async: every caller is a coroutine, and supabase-py is blocking — a sync
    helper would stall the gateway for the length of the query."""
    try:
        r = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
            "hosts,created_by").eq(
            "id", int(tourney_id)).limit(1).execute())
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
            hosts = await _comp_tourney_hosts(sb, tourney_id)
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
                       coin_give="Coin cup: pay Nectar Coin prizes to top-3 (default False)",
                       coins_first="1st place coins (coin cup only)",
                       coins_second="2nd place coins (coin cup only)",
                       coins_third="3rd place coins (coin cup only)",
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
                             coin_give: bool = False,
                             coins_first: int = 0, coins_second: int = 0,
                             coins_third: int = 0,
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
    if mode_c == "duo" and maxp % 2 == 1:
        return await interaction.followup.send(
            "Duo tourneys need an even `max_players` (full teams of 2).", ephemeral=True)
    try:
        _p1, _p2, _p3 = max(0, int(coins_first or 0)), max(0, int(coins_second or 0)), max(0, int(coins_third or 0))
    except Exception:
        return await interaction.followup.send(
            "Coin prizes must be whole numbers (0+).", ephemeral=True)
    _coin_cup = bool(coin_give)
    if _coin_cup and _p1 <= 0 and _p2 <= 0 and _p3 <= 0:
        return await interaction.followup.send(
            "Coin cup needs at least one prize — set `coins_first`, `coins_second` or `coins_third`.",
            ephemeral=True)
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
            "coin_give": _coin_cup,
            "coins_first": _p1, "coins_second": _p2, "coins_third": _p3,
        }
        if sched_iso:
            _ins["scheduled_at"] = sched_iso
        try:
            res = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").insert(_ins).execute())
        except Exception as e:
            if "coin_give" in str(e) or "coins_" in str(e):
                return await interaction.followup.send(
                    "Database migration pending — ask a host to apply add_comp_v240_coin_cups.sql.",
                    ephemeral=True)
            raise
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
            f"{series or '∞'} matches)."
            + (f" Coin cup: 1st +{_p1:,} / 2nd +{_p2:,} / 3rd +{_p3:,}." if _coin_cup else "")
            + f"{tail} Post it: `/tourney-post {row.get('id')}`. "
            f"Registrations: `/listen-registrations`.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't create: {e}", ephemeral=True)


@bot.tree.command(name="tourney-stop", description="Close a comp tourney (host)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)",
                       role_to_remove="Player role to strip at end (defaults to the tourney's give-role)")
async def tourney_stop_cmd(interaction: discord.Interaction, tourney_id: int,
                           role_to_remove: discord.Role | None = None):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        from datetime import datetime, timezone
        old = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
            "code,player_role_id").eq("id", int(tourney_id)).limit(1).execute())
        old_row = ((old.data or [{}])[0] or {})
        old_code = old_row.get("code")
        res = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update(
            {"status": "ended", "reg_open": False, "ended_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", int(tourney_id)).execute())
        if not res.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        # drop the room tag so presence stops showing the ended tourney
        if old_code:
            try:
                await db._sb_call(lambda: sb.table("bngc_room_tags").delete().eq("code", str(old_code).upper()).execute())
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
                    rres = await db._sb_call(lambda: sb.table("bngc_comp_registrations").select("member_ids").eq(
                        "tourney_id", int(tourney_id)).eq("status", "accepted").limit(1000).execute())
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
        # auto-compute Tourney PR on close (idempotent; unattributed kills
        # simply don't count — every accepted signup now holds a profile).
        pr_board: list = []
        try:
            _st, _body = await _api_post("/api/comp/tourney/pr", {"tourney_id": int(tourney_id)})
            pr_board = ((_body or {}).get("pr_board")) or []
            if pr_board:
                tail += f" · 📊 PR calculated ({len(pr_board)} rated)"
            else:
                tail += " · 📊 no PR (no sealed matches)"
        except Exception as e:
            print(f"[comp] stop auto-PR failed: {e}")
            tail += " · 📊 PR skipped (server unreachable — run /tourney-pr)"
        # opted-in nicknames: Display Name [PR]
        try:
            _renamed = await _comp_apply_pr_nicknames(interaction.guild, pr_board)
            if _renamed:
                tail += f" · ✏️ {_renamed} nickname{'s' if _renamed != 1 else ''} updated"
        except Exception as e:
            print(f"[comp] stop nicknames failed: {e}")
        # archive the final standings in the results channel
        try:
            _posted, _note = await comp_results.post_results(
                interaction.client, int(tourney_id), _comp_tourney)
            tail += (f" · 🏆 results posted ({_note})" if _posted
                     else f" · 🏆 results not posted ({_note})")
        except Exception as e:
            print(f"[comp] stop results post failed: {e}")
            tail += " · 🏆 results not posted"
        return await interaction.followup.send(f"🏁 Tourney **#{tourney_id}** closed.{tail}", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't stop: {e}", ephemeral=True)


@bot.tree.command(name="tourney-change-code", description="Change a tourney's room code (host)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)",
                       code="New 5-letter private room code")
async def tourney_code_cmd(interaction: discord.Interaction, tourney_id: int, code: str):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    ok, msg = await _tourney_set_code(int(tourney_id), code, str(interaction.user.id))
    return await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="tourney-set-code", description="Set a tourney's room code (host, works when empty)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)",
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
        old = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select("id,code,name,status").eq(
            "id", int(tourney_id)).limit(1).execute())
        if not old.data:
            return False, "No tourney with that ID."
        t = old.data[0]
        old_code = (t.get("code") or "").upper() or None
        await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update({"code": code_c}).eq("id", int(tourney_id)).execute())
        # move the room tag so presence follows the new code (started only)
        extra = ""
        try:
            if old_code and old_code != code_c:
                await db._sb_call(lambda: sb.table("bngc_room_tags").delete().eq("code", old_code).execute())
            if str(t.get("status") or "") == "started":
                await db._sb_call(lambda: sb.table("bngc_room_tags").upsert({
                    "code": code_c, "kind": "tourney",
                    "name": str(t.get("name") or "Tourney"),
                    "created_by": str(actor_id),
                }, on_conflict="code").execute())
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
            # Drop any active roster rows containing this player first so a
            # banned duo member doesn't stay counted via their old team.
            try:
                _active = await db._sb_call(lambda: sb.table("bngc_comp_registrations").select(
                    "id,member_ids,status").eq("tourney_id", int(tourney_id)).neq(
                    "status", "banned").limit(1000).execute())
                for _r in ((_active.data or [])):
                    try:
                        if did in [str(x) for x in (((_r or {}).get("member_ids")) or [])]:
                            await db._sb_call(lambda: sb.table("bngc_comp_registrations").delete().eq(
                                "id", (_r or {}).get("id")).execute())
                    except Exception:
                        continue
            except Exception:
                pass
            try:
                await db._sb_call(lambda: sb.table("bngc_comp_registrations").insert({
                    "tourney_id": int(tourney_id), "team_label": f"<@{did}>",
                    "member_ids": [did], "captain_id": did,
                    "source_message_id": None, "status": "banned",
                }).execute())
            except Exception as e:
                return False, f"Couldn't ban: {e}"
            # strip the player role if we can resolve it
            try:
                tres = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select("player_role_id").eq(
                    "id", int(tourney_id)).limit(1).execute())
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
        try:
            _trow = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select("mode").eq(
                "id", int(tourney_id)).limit(1).execute())
            _is_duo = str((((_trow.data or [{}])[0] or {}).get("mode")) or "solo").lower() == "duo"
        except Exception:
            _is_duo = False
        for r in mine:
            try:
                if _is_duo:
                    # Duo: never flip a 1-person ban row into a solo
                    # accepted team — delete so the player re-registers
                    # with a teammate and counts stay in pairs.
                    await db._sb_call(lambda: sb.table("bngc_comp_registrations").delete().eq("id", r["id"]).execute())
                else:
                    await db._sb_call(lambda: sb.table("bngc_comp_registrations").update(
                        {"status": "accepted"}).eq("id", r["id"]).execute())
            except Exception:
                pass
        if _is_duo:
            return True, f"✅ <@{did}> unbanned in tourney **#{tourney_id}** — re-register with a teammate."
        return True, f"✅ <@{did}> unbanned (accepted) in tourney **#{tourney_id}**."
    except Exception as e:
        return False, f"Couldn't update ban: {e}"


@bot.tree.command(name="tourney-ban", description="Ban a player from a tourney (host)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)", user="Player to ban")
async def tourney_ban_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    ok, msg = await _comp_set_banned(int(tourney_id), str(user.id), True, interaction.guild)
    return await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="tourney-unban", description="Unban a player (re-accepts them) (host)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)", user="Player to unban")
async def tourney_unban_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    ok, msg = await _comp_set_banned(int(tourney_id), str(user.id), False, interaction.guild)
    return await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="tourney-host-status", description="Set whether the host plays too (host)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)",
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
        res = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update({"host_playing": hp}).eq("id", int(tourney_id)).execute())
        if not res.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        label = "Playing — your kills count" if hp else "Hosting only — your kills don't count"
        return await interaction.followup.send(f"Tourney **#{tourney_id}** host status: **{label}**.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't update: {e}", ephemeral=True)


@bot.tree.command(name="tourney-add-host", description="Add a co-host to a tourney (host)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)", user="Co-host to add")
async def tourney_add_host_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        chk = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select("id").eq("id", int(tourney_id)).limit(1).execute())
        if not chk.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        hosts = await _comp_tourney_hosts(sb, tourney_id)
        did = str(user.id)
        if did not in hosts:
            hosts.append(did)
            await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update({"hosts": hosts}).eq("id", int(tourney_id)).execute())
        return await interaction.followup.send(f"{user.mention} is now a host of tourney **#{tourney_id}**.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't add host: {e}", ephemeral=True)


@bot.tree.command(name="tourney-remove-host", description="Remove a co-host from a tourney (host)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)", user="Co-host to remove")
async def tourney_remove_host_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        chk = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select("id").eq("id", int(tourney_id)).limit(1).execute())
        if not chk.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        hosts = await _comp_tourney_hosts(sb, tourney_id)
        did = str(user.id)
        hosts = [h for h in hosts if h != did]
        await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update({"hosts": hosts}).eq("id", int(tourney_id)).execute())
        return await interaction.followup.send(f"{user.mention} is no longer a host of tourney **#{tourney_id}**.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't remove host: {e}", ephemeral=True)


@bot.tree.command(name="tourney-add-player", description="Add a player to a tourney roster (host)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)", user="Player to add",
                       teammate="Duo teammate (required for duo tourneys)")
async def tourney_add_player_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User,
                                 teammate: discord.User | None = None):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        trow = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
            "id,mode,player_role_id").eq("id", int(tourney_id)).limit(1).execute())
        if not trow.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        t = trow.data[0] or {}
        is_duo = str((t or {}).get("mode") or "solo").lower() == "duo"
        did = str(user.id)
        rows = await _comp_reg_rows(int(tourney_id))
        if is_duo:
            if teammate is None or getattr(teammate, "bot", False):
                return await interaction.followup.send(
                    "Duo tourney: pass both players (`user` + `teammate`).", ephemeral=True)
            mate_id = str(teammate.id)
            if mate_id == did:
                return await interaction.followup.send("Duo needs two different players.", ephemeral=True)
            members = [did, mate_id]
            for _m, _u in ((did, user), (mate_id, teammate)):
                if any((r or {}).get("status") != "banned" and _m in ((r or {}).get("member_ids") or []) for r in rows):
                    return await interaction.followup.send(f"<@{_m}> is already on the roster.", ephemeral=True)
            team_idx = sum(1 for r in rows if (r or {}).get("status") != "banned")
            _add_row = {
                "tourney_id": int(tourney_id),
                "team_label": f"<@{did}> <@{mate_id}>",
                "member_ids": members,
                "captain_id": did,
                "source_message_id": None,
                "status": "accepted",
                "team_color": _comp_team_color(team_idx),
                "member_names": {
                    did: {
                        "username": str(getattr(user, "name", "") or "")[:32],
                        "display_name": str(getattr(user, "display_name", "") or getattr(user, "name", "") or "")[:32],
                    },
                    mate_id: {
                        "username": str(getattr(teammate, "name", "") or "")[:32],
                        "display_name": str(getattr(teammate, "display_name", "") or getattr(teammate, "name", "") or "")[:32],
                    },
                },
            }
            try:
                await db._sb_call(lambda: sb.table("bngc_comp_registrations").insert(_add_row).execute())
            except Exception:
                _add_row.pop("member_names", None)  # pre-migration safe
                await db._sb_call(lambda: sb.table("bngc_comp_registrations").insert(_add_row).execute())
            try:
                _rid = (t or {}).get("player_role_id") or COMP_PLAYER_ROLE_ID
                await _comp_grant_role(interaction.guild, did, _rid)
                await _comp_grant_role(interaction.guild, mate_id, _rid)
            except Exception:
                pass
            try:
                _comp_reg_cache["at"] = 0.0
            except Exception:
                pass
            return await interaction.followup.send(
                f"{user.mention} + {teammate.mention} added to tourney **#{tourney_id}** roster (duo).",
                ephemeral=True)
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
            await db._sb_call(lambda: sb.table("bngc_comp_registrations").insert(_add_row).execute())
        except Exception:
            _add_row.pop("member_names", None)  # pre-migration safe
            await db._sb_call(lambda: sb.table("bngc_comp_registrations").insert(_add_row).execute())
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


@bot.tree.command(name="tourney-refresh-teams",
                     description="Re-derive duo team colors and re-grant each team's roles (host)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
async def tourney_refresh_teams_cmd(interaction: discord.Interaction, tourney_id: int):
    """Repair team colors + roles after teams were added, dropped or moved.

    Colors are frozen on the registration row at intake, so they drift as soon
    as a team is removed and later teams shift; roles are granted at signup and
    never re-checked. This re-derives both from the live roster, stripping any
    team role a member should no longer hold.
    """
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        trow = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
            "id,name,mode").eq("id", int(tourney_id)).limit(1).execute())
        if not trow.data:
            return await interaction.followup.send("No tourney with that ID.",
                                                  ephemeral=True)
        t = trow.data[0] or {}
        out = await _comp_sync_team_roles(interaction.guild, sb, t)
        if out.get("solo"):
            return await interaction.followup.send(
                f"Tourney **#{tourney_id}** is solo -no teams to refresh.",
                ephemeral=True)
        lines = [f"**Tourney #{tourney_id}** teams refreshed:",
                 f"Teams: **{out['teams']}** · members: **{out['members']}**",
                 f"Colors corrected: **{out['recolored']}**",
                 f"Roles granted: **{out['granted']}** · stale roles stripped: **{out['stripped']}**"]
        if out.get("no_role_config"):
            lines.append("_No team roles configured - set `COMP_TEAM_ROLE_1..6` "
                         "to their role ids and re-run._")
        lines.append("Team order = signup order, so colours and roles both cycle "
                     "every six teams.")
        return await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't refresh teams: {e}",
                                              ephemeral=True)


@bot.tree.command(name="tourney-remove-player", description="Remove a player from a tourney roster (host)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)", user="Player to remove")
async def tourney_remove_player_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        trow = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
            "id,mode,player_role_id").eq("id", int(tourney_id)).limit(1).execute())
        if not trow.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        is_duo = str(((trow.data[0] or {}).get("mode")) or "solo").lower() == "duo"
        did = str(user.id)
        rows = await db._sb_call(lambda: sb.table("bngc_comp_registrations").select(
            "id,member_ids").eq("tourney_id", int(tourney_id)).neq("status", "banned").limit(1000).execute())
        hit = [r for r in (rows.data or []) if did in ((r or {}).get("member_ids") or [])]
        removed_ids: list = []
        for r in hit:
            try:
                members = [str(m) for m in ((r or {}).get("member_ids") or []) if m]
                if is_duo:
                    # Duo: a player never leaves a half-team behind — drop the
                    # whole team so counts/standings stay in pairs.
                    removed_ids.extend(members)
                    await db._sb_call(lambda: sb.table("bngc_comp_registrations").delete().eq("id", (r or {}).get("id")).execute())
                else:
                    remaining = [m for m in ((r or {}).get("member_ids") or []) if m != did]
                    if not remaining:
                        await db._sb_call(lambda: sb.table("bngc_comp_registrations").delete().eq("id", (r or {}).get("id")).execute())
                    else:
                        await db._sb_call(lambda: sb.table("bngc_comp_registrations").update({"member_ids": remaining}).eq("id", (r or {}).get("id")).execute())
                    removed_ids.append(did)
            except Exception:
                continue
        try:
            from discord.utils import get as _dget
            _rid = ((trow.data[0] or {}).get("player_role_id") or COMP_PLAYER_ROLE_ID) if trow.data else COMP_PLAYER_ROLE_ID
            if _rid and interaction.guild is not None:
                role = _dget(interaction.guild.roles, id=int(_rid)) if str(_rid).isdigit() else None
                if role is not None:
                    for _rdid in (sorted(set(removed_ids)) or [did]):
                        try:
                            m = interaction.guild.get_member(int(_rdid))
                            if m is None:
                                m = await interaction.guild.fetch_member(int(_rdid))
                            if m is not None:
                                await m.remove_roles(role, reason=f"tourney #{tourney_id} roster removal")
                        except Exception:
                            continue
        except Exception:
            pass
        try:
            _comp_reg_cache["at"] = 0.0
        except Exception:
            pass
        return await interaction.followup.send(f"{user.mention} removed from tourney **#{tourney_id}** roster.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't remove player: {e}", ephemeral=True)


@bot.tree.command(name="tourney-add-back", description="Add a player back to the open match roster (host)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)", user="Player who left to add back")
async def tourney_add_back_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        did = str(user.id)
        try:
            mres = await db._sb_call(lambda: sb.table("bngc_comp_matches").select(
                "id,removed_discord_ids").eq("tourney_id", int(tourney_id)).in_(
                "status", ["open", "manual"]).order("version", desc=True).limit(1).execute())
        except Exception:
            return await interaction.followup.send(
                "Match roster removal needs DB migration add_comp_v237.sql.", ephemeral=True)
        rows = (mres.data or [])
        if not rows:
            return await interaction.followup.send("No open match for this tourney.", ephemeral=True)
        m = rows[0] or {}
        cur = [str(x) for x in ((m or {}).get("removed_discord_ids") or []) if str(x or "").strip()]
        if did not in cur:
            return await interaction.followup.send(f"{user.mention} is not on the removed list.", ephemeral=True)
        cur = [x for x in cur if x != did]
        await db._sb_call(lambda: sb.table("bngc_comp_matches").update({"removed_discord_ids": cur}).eq("id", m["id"]).execute())
        return await interaction.followup.send(f"{user.mention} added back to the match roster.", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't add back: {e}", ephemeral=True)


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
        try:
            if bool(t.get("coin_give")):
                _c1 = int(t.get("coins_first") or 0)
                _c2 = int(t.get("coins_second") or 0)
                _c3 = int(t.get("coins_third") or 0)
                lines += [
                    f"**PRIZES**:",
                    f"1st = {_c1:,} Nectar Coins",
                    f"2nd = {_c2:,} Nectar Coins",
                    f"3rd = {_c3:,} Nectar Coins",
                ]
        except Exception:
            pass
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
            await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update(
                {"announce_channel_id": str(target.id),
                 "announce_message_id": str(posted.id)}).eq("id", int((t or {}).get("id"))).execute())
        except Exception:
            pass
        return posted, True
    except Exception:
        return None, False


@bot.tree.command(name="tourney-post", description="Post the tourney announcement (host). Re-post edits it.")
@app_commands.describe(tourney_id="Numeric session ID (from 0)",
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
        tres = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select("*").eq("id", int(tourney_id)).limit(1).execute())
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
                await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update(_ov).eq("id", int(tourney_id)).execute())
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
@app_commands.describe(tourney_id="Numeric session ID (from 0)",
                       channel="Channel override (default: stored announce channel)")
async def tourney_refresh_points_cmd(interaction: discord.Interaction, tourney_id: int,
                                     channel: discord.TextChannel | None = None):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        tres = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select("*").eq("id", int(tourney_id)).limit(1).execute())
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
@app_commands.describe(tourney_id="Numeric session ID (from 0)",
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
        res = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update(
            {"scheduled_at": dt.isoformat(), "status": "scheduled"}
        ).eq("id", int(tourney_id)).execute())
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


def _comp_board_lines(board, limit=0):
    """Render aggregate standings lines (with mentions). Pure. limit=0 → all."""
    return comp_results.board_lines(board, limit)


@bot.tree.command(name="tourney-board", description="Aggregate leaderboard for a comp tourney")
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
async def tourney_board_cmd(interaction: discord.Interaction, tourney_id: int):
    await interaction.response.defer(ephemeral=False)
    data = await _comp_tourney(tourney_id)
    if not data or data.get("error"):
        return await interaction.followup.send(f"Couldn't load tourney #{tourney_id}: {(data or {}).get('error') or 'server unreachable'}.")
    t = data.get("tourney") or {}
    board = data.get("standings") or []
    if not board:
        return await interaction.followup.send(f"No sealed matches in **{t.get('name')}** yet.")
    return await _send_paginated(
        interaction, f"🏆 {t.get('name')} — #{t.get('id')} ({t.get('status')})", 0xFFD700,
        _comp_board_lines(board),
        footer=f"{len(board)} players · {t.get('sealed', 0)}/{t.get('series_len') or '∞'} matches")


@bot.tree.command(name="tourney-match", description="Per-match board for a comp tourney match")
@app_commands.describe(tourney_id="Numeric session ID (from 0)", version="Match version (0, 1, 2, ...)")
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
    if not lines:
        lines = ["No rows."]
    return await _send_paginated(
        interaction, f"{t.get('name')} — match v{version}", 0x7c3aed, lines,
        header=f"Winner: **{w or '—'}**{w_mention}")


def _comp_mention(did) -> str:
    """<@did> mention, or the raw id when empty. Pure."""
    try:
        did = str(did or "").strip()
        return f"<@{did}>" if did else "?"
    except Exception:
        return "?"


class _LBView(discord.ui.View):
    """◀/▶ pagination for leaderboard embeds (10 rows/page). Not persistent:
    each command reply owns its instance. Never raises out."""

    def __init__(self, title: str, color: int, pages: list, header: str = "", footer: str = ""):
        super().__init__(timeout=300)
        self._lb_title = title
        self._lb_color = color
        self._lb_pages = pages or [[]]
        self._lb_header = header or ""
        self._lb_footer = footer or ""
        self._lb_idx = 0
        self._lb_sync()

    def _lb_sync(self):
        try:
            self.lb_prev.disabled = self._lb_idx <= 0
            self.lb_next.disabled = self._lb_idx >= len(self._lb_pages) - 1
        except Exception:
            pass

    def _lb_embed(self):
        try:
            desc = ((self._lb_header + "\n") if self._lb_header else "") + "\n".join(self._lb_pages[self._lb_idx])
            e = discord.Embed(title=self._lb_title, color=self._lb_color, description=desc[:4000])
            bits = [f"Page {self._lb_idx + 1}/{len(self._lb_pages)}"]
            if self._lb_footer:
                bits.append(self._lb_footer)
            e.set_footer(text=" · ".join(bits)[:200])
            return e
        except Exception:
            return discord.Embed(title=self._lb_title, color=self._lb_color, description="Couldn't render page.")

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="lb:prev")
    async def lb_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            self._lb_idx = max(0, self._lb_idx - 1)
            self._lb_sync()
            await interaction.response.edit_message(embed=self._lb_embed(), view=self)
        except Exception:
            pass

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="lb:next")
    async def lb_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            self._lb_idx = min(len(self._lb_pages) - 1, self._lb_idx + 1)
            self._lb_sync()
            await interaction.response.edit_message(embed=self._lb_embed(), view=self)
        except Exception:
            pass


async def _send_paginated(interaction: discord.Interaction, title: str, color: int,
                          lines: list, header: str = "", footer: str = "", per: int = 10,
                          ephemeral: bool = False):
    """Send leaderboard lines with ◀/▶ pagination (single page: no buttons)."""
    try:
        pages = [lines[i:i + per] for i in range(0, len(lines), per)] or [[]]
        if len(pages) == 1:
            e = discord.Embed(title=title, color=color,
                              description=((header + "\n") if header else "") + "\n".join(pages[0])[:4000])
            if footer:
                e.set_footer(text=footer[:200])
            return await interaction.followup.send(embed=e, ephemeral=ephemeral)
        view = _LBView(title, color, pages, header, footer)
        return await interaction.followup.send(embed=view._lb_embed(), view=view, ephemeral=ephemeral)
    except Exception as e:
        print(f"[lb] paginated send failed: {e}")
        try:
            return await interaction.followup.send(embed=discord.Embed(
                title=title, color=color, description="\n".join(lines[:per])[:4000]), ephemeral=ephemeral)
        except Exception:
            return None


@bot.tree.command(name="tourney-winner", description="Winner of a comp tourney (most points)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
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


async def _tourney_prize_already_paid(discord_id: str, tourney_id: int, reason: str) -> bool:
    """Ledger idempotency: was this tourney prize already paid to this user?
    Never raises (False on any doubt — the caller re-checks after paying)."""
    try:
        entries = await db.get_coin_ledger(str(discord_id), 50)
        for x in (entries or []):
            try:
                if str((x or {}).get("reason")) != str(reason):
                    continue
                meta = (x or {}).get("meta") or {}
                if str(meta.get("tourney_id")) == str(tourney_id):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


@coin_group.command(name="pay-tourney", description="Pay coin-cup prizes to standings top-3 (host, once)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
async def tourney_pay_coins_cmd(interaction: discord.Interaction, tourney_id: int):
    await interaction.response.defer(ephemeral=False)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        tres = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
            "id,name,mode,coin_give,coins_first,coins_second,coins_third,coins_paid"
        ).eq("id", int(tourney_id)).limit(1).execute())
    except Exception as e:
        if "coin_give" in str(e) or "coins_" in str(e):
            return await interaction.followup.send(
                "Database migration pending — ask a host to apply add_comp_v240_coin_cups.sql.")
        return await interaction.followup.send(f"Couldn't load tourney #{tourney_id}: {e}")
    trows = (tres.data if hasattr(tres, "data") else tres or []) or []
    if not trows:
        return await interaction.followup.send(f"No tourney with ID #{tourney_id}.")
    t = trows[0] or {}
    if not bool(t.get("coin_give")):
        return await interaction.followup.send(
            f"Tourney **#{tourney_id}** is not a coin cup (`coin_give` off).")
    if bool(t.get("coins_paid")):
        return await interaction.followup.send(
            f"Coin prizes for **#{tourney_id}** were already paid — nothing to do.")
    data = await _comp_tourney(tourney_id)
    if not data or data.get("error"):
        return await interaction.followup.send(
            f"Couldn't load standings for #{tourney_id}: {(data or {}).get('error') or 'server unreachable'}.")
    board = data.get("standings") or []
    if not board:
        return await interaction.followup.send(
            f"No sealed matches in **{t.get('name')}** yet — pay after results exist.")
    prizes = [(int(t.get("coins_first") or 0), "tourney_first", "1st"),
              (int(t.get("coins_second") or 0), "tourney_second", "2nd"),
              (int(t.get("coins_third") or 0), "tourney_third", "3rd")]
    results, all_ok = [], True
    for idx, (amount, reason, label) in enumerate(prizes):
        if amount <= 0:
            continue
        if idx >= len(board):
            results.append(f"{label}: no team — skipped")
            continue
        row = board[idx] or {}
        try:
            mids = [str(x) for x in ((row or {}).get("member_ids") or []) if x]
        except Exception:
            mids = []
        if not mids and (row or {}).get("discord_id"):
            mids = [str((row or {}).get("discord_id"))]
        if not mids:
            results.append(f"{label}: no players — skipped")
            all_ok = False
            continue
        paid_here, failed_here = [], []
        for did in mids:
            try:
                if await _tourney_prize_already_paid(did, int(tourney_id), reason):
                    paid_here.append(f"<@{did}> (already)")
                    continue
                await db.add_coin_delta(did, int(amount), reason,
                                        {"tourney_id": int(tourney_id), "place": label})
                paid_here.append(f"<@{did}>")
            except LookupError:
                failed_here.append(f"<@{did}> (needs /register)")
                all_ok = False
            except Exception as e:
                print(f"[comp] coin prize failed #{tourney_id} {label} {did}: {e}")
                failed_here.append(f"<@{did}> (error, retry)")
                all_ok = False
        who = ((row or {}).get("label") or (row or {}).get("name")
               or " / ".join(f"<@{m}>" for m in mids))
        line = f"{label} (+{amount:,} Nectar Coins): {who} → {', '.join(paid_here) if paid_here else '—'}"
        if failed_here:
            line += f" | failed: {', '.join(failed_here)}"
        results.append(line)
    if all_ok:
        try:
            await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update({"coins_paid": True}).eq(
                "id", int(tourney_id)).execute())
        except Exception as e:
            print(f"[comp] coins_paid flag failed #{tourney_id}: {e}")
    e = discord.Embed(
        title=f"Coin prizes — {t.get('name')} (#{tourney_id})",
        description="\n".join(results) or "No prizes configured.",
        color=discord.Color.gold())
    if not all_ok:
        e.set_footer(text="Some prizes failed — fix them (usually /register) and re-run; already-paid members are skipped.")
    else:
        e.set_footer(text="All prizes paid. This command will now refuse (already paid).")
    await interaction.followup.send(embed=e)


@bot.tree.command(name="tourney-best", description="Best slayer of a comp tourney (most kills)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
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
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
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


@bot.tree.command(name="tourney-name", description="Opt in/out of the [PR] server nickname (Display Name [150])")
@app_commands.describe(allow="True to opt in, False to opt out")
async def tourney_name_cmd(interaction: discord.Interaction, allow: bool):
    await interaction.response.defer(ephemeral=True)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.", ephemeral=True)
    me = str(interaction.user.id)
    try:
        upd = await db._sb_call(lambda: sb.table("bngc_users").update(
            {"tourney_name_optin": bool(allow)}).eq("discord_id", me).execute())
        wrote = bool(upd.data)
    except Exception as e:
        if "tourney_name_optin" in str(e):
            return await interaction.followup.send(
                "Database migration pending — ask a host to apply add_comp_v238.sql.", ephemeral=True)
        print(f"[tourney-name] save failed: {e}")
        return await interaction.followup.send("Couldn't save — try again later.", ephemeral=True)
    if not wrote:
        try:
            await db._sb_call(lambda: sb.table("bngc_users").insert({
                "discord_id": me,
                "discord_username": getattr(interaction.user, "display_name", None) or str(interaction.user),
                "tourney_name_optin": bool(allow),
                "token": gen_token(),
            }).execute())
        except Exception as e:
            if "tourney_name_optin" in str(e):
                return await interaction.followup.send(
                    "Database migration pending — ask a host to apply add_comp_v238.sql.", ephemeral=True)
            return await interaction.followup.send("Couldn't save — try again later.", ephemeral=True)
    state = "in ✅ — your server nickname becomes **Display Name [PR]** after each tourney" if allow else "out — your nickname stays untouched"
    extra = ""
    if allow and interaction.guild is not None:
        # apply right now (not just at the next calc): current PR + name
        try:
            ures = await db._sb_call(lambda: sb.table("bngc_users").select(
                "game_username,tourney_pr").eq("discord_id", me).limit(1).execute())
            urow = ((ures.data or [{}])[0] or {})
            gname = str(urow.get("game_username") or "").strip()
            pr = urow.get("tourney_pr")
            if pr is None:
                try:
                    hres = await db._sb_call(lambda: sb.table("bngc_comp_pr_history").select("pr").eq(
                        "discord_id", me).order("computed_at", desc=True).limit(1).execute())
                    if hres.data:
                        pr = (hres.data[0] or {}).get("pr")
                except Exception:
                    pass
            if gname and pr is not None:
                nick = _comp_pr_nick(gname, pr)
                try:
                    m = interaction.guild.get_member(int(me))
                    if m is None:
                        m = await interaction.guild.fetch_member(int(me))
                    if m is not None and m.nick != nick:
                        await m.edit(nick=nick, reason="Comp PR nickname opt-in (immediate)")
                    extra = f" Nickname set to **{nick}** now."
                except Exception as e:
                    print(f"[tourney-name] immediate nick failed: {e}")
                    extra = " Couldn't set nickname now (bot needs Manage Nicknames + a higher role) — it applies at the next tourney."
            elif not gname:
                extra = " No comp profile yet, so no nickname to set — it applies once you register."
            else:
                extra = " No PR yet — nickname applies after your first rated tourney."
        except Exception as e:
            print(f"[tourney-name] immediate apply failed: {e}")
    return await interaction.followup.send(f"You opted {state}.{extra}", ephemeral=True)


async def _ranked_name_locked(sb, did: str) -> bool:
    """True when the identity row is lock-guarded against renames.

    Pre-migration DBs (no ranked_link_locked): False. Never raises."""
    try:
        if sb is None or not did:
            return False
        r = await db._sb_call(lambda: sb.table("bngc_users").select("ranked_link_locked").eq(
            "discord_id", str(did)).limit(1).execute())
        rows = r.data or []
        return bool(((rows[0] or {}).get("ranked_link_locked"))) if rows else False
    except Exception:
        return False


def _ranked_milestone_nick(game_name: str, milestone) -> str:
    """Display Name [Milestone] nickname (32-char Discord cap). Pure."""
    try:
        suffix = f" [{str(milestone or '').strip()}]"
        if suffix.strip() in ("[]", ""):
            suffix = ""
    except Exception:
        suffix = ""
    try:
        base = str(game_name or "").strip()[:max(0, 32 - len(suffix))]
        return f"{base}{suffix}".strip()[:32]
    except Exception:
        return ""


async def _comp_nick_for(sb, did: str):
    """(nick|None, game_name). Current PR nickname for a user. Never raises
    (None when no name/PR yet or DB down). async — see _comp_tourney_hosts."""
    try:
        if sb is None or not did:
            return None, ""
        ures = await db._sb_call(lambda: sb.table("bngc_users").select(
            "game_username,tourney_pr").eq("discord_id", str(did)).limit(1).execute())
        urow = ((ures.data or [{}])[0] or {})
        gname = str(urow.get("game_username") or "").strip()
        pr = urow.get("tourney_pr")
        if pr is None:
            try:
                hres = await db._sb_call(lambda: sb.table("bngc_comp_pr_history").select("pr").eq(
                    "discord_id", str(did)).order("computed_at", desc=True).limit(1).execute())
                if hres.data:
                    pr = (hres.data[0] or {}).get("pr")
            except Exception:
                pass
        if not gname or pr is None:
            return None, gname
        return _comp_pr_nick(gname, pr), gname
    except Exception:
        return None, ""


async def _ranked_nick_for(sb, did: str):
    """(nick|None, game_name, milestone). Current milestone nickname.
    Never raises (None when no name yet or DB down). async — see
    _comp_tourney_hosts."""
    try:
        if sb is None or not did:
            return None, "", None
        ures = await db._sb_call(lambda: sb.table("bngc_users").select(
            "game_username").eq("discord_id", str(did)).limit(1).execute())
        urow = ((ures.data or [{}])[0] or {})
        gname = str(urow.get("game_username") or "").strip()
        if not gname:
            return None, "", None
        pts = ranked_mod.get_points(str(did))
        milestone, _fee = ranked_mod.milestone_for(pts)
        return _ranked_milestone_nick(gname, milestone), gname, milestone
    except Exception:
        return None, "", None


async def _edit_nick_if_different(guild, did: str, nick: str, reason: str) -> bool:
    """Set a member nickname when it differs. True if attempted-ok. Never raises."""
    try:
        if guild is None or not did or not nick:
            return False
        try:
            m = guild.get_member(int(did))
        except Exception:
            m = None
        if m is None:
            try:
                m = await guild.fetch_member(int(did))
            except Exception:
                return False
        if m is None:
            return False
        if m.nick != nick:
            await m.edit(nick=nick, reason=reason)
        return True
    except Exception:
        return False


async def _ranked_sync_milestone_nicknames(seal: dict) -> int:
    """Set opted-in members' nicks to Name [Milestone] after a seal.

    Reads fresh totals (deltas in the seal row are not enough), computes the
    milestone per player, edits only when the nick differs. Pre-migration DBs
    (no ranked_name_optin): no-op, 0. Never raises.
    """
    try:
        pts_map = (seal or {}).get("points") or {}
        dids = sorted({str(d) for d in pts_map if str(d or "").strip()})[:200]
        if not dids:
            return 0
        sb = _comp_sb()
        if sb is None:
            return 0
        try:
            # Sync supabase read off the loop (this runs on seals + /ranked stop)
            ures = await asyncio.to_thread(lambda: sb.table("bngc_users").select(
                "discord_id,game_username,ranked_name_optin").in_("discord_id", dids).execute())
        except Exception as e:
            if "ranked_name_optin" in str(e):
                print("[ranked] nicknames skipped: apply add_ranked_v242.sql")
            return 0
        umap = {str((u or {}).get("discord_id")): (u or {}) for u in (ures.data or [])}
        try:
            totals = await asyncio.to_thread(ranked_mod.get_points_many, dids)
        except Exception:
            totals = {}
        done = 0
        for did in dids:
            try:
                u = umap.get(did) or {}
                if not u.get("ranked_name_optin"):
                    continue
                gname = str(u.get("game_username") or "").strip()
                if not gname:
                    continue
                try:
                    total = int((totals or {}).get(did, 0))
                except Exception:
                    total = 0
                milestone, _fee = ranked_mod.milestone_for(total)
                nick = _ranked_milestone_nick(gname, milestone)
                if not nick:
                    continue
                for guild in (bot.guilds or []):
                    try:
                        if await _edit_nick_if_different(
                                guild, did, nick, "Ranked milestone nickname (seal)"):
                            done += 1
                            break
                    except Exception:
                        continue
            except Exception:
                continue
        return done
    except Exception as e:
        print(f"[ranked] milestone nicknames failed: {e}")
        return 0


@bot.tree.command(name="ranked-name", description="Opt in/out of the [Milestone] server nickname (Name [Gold])")
@app_commands.describe(allow="True to opt in, False to opt out")
async def ranked_name_cmd(interaction: discord.Interaction, allow: bool):
    await interaction.response.defer(ephemeral=True)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.", ephemeral=True)
    me = str(interaction.user.id)
    try:
        upd = await db._sb_call(lambda: sb.table("bngc_users").update(
            {"ranked_name_optin": bool(allow)}).eq("discord_id", me).execute())
        wrote = bool(upd.data)
    except Exception as e:
        if "ranked_name_optin" in str(e):
            return await interaction.followup.send(
                "Database migration pending — ask a host to apply add_ranked_v242.sql.", ephemeral=True)
        print(f"[ranked-name] save failed: {e}")
        return await interaction.followup.send("Couldn't save — try again later.", ephemeral=True)
    if not wrote:
        try:
            await db._sb_call(lambda: sb.table("bngc_users").insert({
                "discord_id": me,
                "discord_username": getattr(interaction.user, "display_name", None) or str(interaction.user),
                "ranked_name_optin": bool(allow),
                "token": gen_token(),
            }).execute())
        except Exception as e:
            if "ranked_name_optin" in str(e):
                return await interaction.followup.send(
                    "Database migration pending — ask a host to apply add_ranked_v242.sql.", ephemeral=True)
            return await interaction.followup.send("Couldn't save — try again later.", ephemeral=True)
    state = "in ✅ — your server nickname becomes **Display Name [Milestone]** and tracks your tier" if allow else "out — your nickname stays untouched"
    extra = ""
    if allow and interaction.guild is not None:
        try:
            nick, gname, milestone = await _ranked_nick_for(sb, me)
            if nick:
                ok = await _edit_nick_if_different(
                    interaction.guild, me, nick, "Ranked milestone nickname opt-in (immediate)")
                extra = (f" Nickname set to **{nick}** now." if ok
                         else " Couldn't set nickname now (bot needs Manage Nicknames + a higher role) — it applies after your next seal.")
            elif not gname:
                extra = " No ranked profile yet — link a name with **/register** first."
        except Exception as e:
            print(f"[ranked-name] immediate apply failed: {e}")
    return await interaction.followup.send(f"You opted {state}.{extra}", ephemeral=True)


@bot.tree.command(name="comp-name", description="Opt in/out of the [PR] server nickname (Display Name [150])")
@app_commands.describe(allow="True to opt in, False to opt out")
async def comp_name_cmd(interaction: discord.Interaction, allow: bool):
    """Same flag as /tourney-name (shared opt-in), plus immediate apply."""
    await interaction.response.defer(ephemeral=True)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.", ephemeral=True)
    me = str(interaction.user.id)
    try:
        upd = await db._sb_call(lambda: sb.table("bngc_users").update(
            {"tourney_name_optin": bool(allow)}).eq("discord_id", me).execute())
        wrote = bool(upd.data)
    except Exception as e:
        if "tourney_name_optin" in str(e):
            return await interaction.followup.send(
                "Database migration pending — ask a host to apply add_comp_v238.sql.", ephemeral=True)
        print(f"[comp-name] save failed: {e}")
        return await interaction.followup.send("Couldn't save — try again later.", ephemeral=True)
    if not wrote:
        try:
            await db._sb_call(lambda: sb.table("bngc_users").insert({
                "discord_id": me,
                "discord_username": getattr(interaction.user, "display_name", None) or str(interaction.user),
                "tourney_name_optin": bool(allow),
                "token": gen_token(),
            }).execute())
        except Exception as e:
            if "tourney_name_optin" in str(e):
                return await interaction.followup.send(
                    "Database migration pending — ask a host to apply add_comp_v238.sql.", ephemeral=True)
            return await interaction.followup.send("Couldn't save — try again later.", ephemeral=True)
    state = "in ✅ — your server nickname becomes **Display Name [PR]** after each tourney" if allow else "out — your nickname stays untouched"
    extra = ""
    if allow and interaction.guild is not None:
        try:
            nick, gname = await _comp_nick_for(sb, me)
            if nick:
                ok = await _edit_nick_if_different(
                    interaction.guild, me, nick, "Comp PR nickname opt-in (immediate)")
                extra = (f" Nickname set to **{nick}** now." if ok
                         else " Couldn't set nickname now (bot needs Manage Nicknames + a higher role) — it applies at the next tourney.")
            elif not gname:
                extra = " No comp profile yet, so no nickname to set — it applies once you register."
            else:
                extra = " No PR yet — nickname applies after your first rated tourney."
        except Exception as e:
            print(f"[comp-name] immediate apply failed: {e}")
    return await interaction.followup.send(f"You opted {state}.{extra}", ephemeral=True)


def _comp_pr_nick(game_name: str, pr) -> str:
    """Display Name [PR] nickname (32-char Discord cap). Pure."""
    try:
        suffix = f" [{int(round(float(pr)))}]"
    except Exception:
        suffix = ""
    try:
        base = str(game_name or "").strip()[:max(0, 32 - len(suffix))]
        return f"{base}{suffix}".strip()[:32]
    except Exception:
        return ""


async def _comp_apply_pr_nicknames(guild, pr_board) -> int:
    """Set opted-in members' nicknames to Display Name [PR]. Returns count.

    Reads bngc_users.tourney_name_optin (pre-migration DBs: no-op, 0).
    Best-effort per member (perms/hierarchy failures skipped). Never raises.
    """
    try:
        if guild is None or not pr_board:
            return 0
        sb = _comp_sb()
        if sb is None:
            return 0
        dids = sorted({str((r or {}).get("discord_id") or "")
                       for r in (pr_board or []) if (r or {}).get("discord_id")})[:200]
        if not dids:
            return 0
        try:
            ures = await db._sb_call(lambda: sb.table("bngc_users").select(
                "discord_id,game_username,tourney_name_optin").in_("discord_id", dids).execute())
        except Exception as e:
            if "tourney_name_optin" in str(e):
                print("[comp] nicknames skipped: apply add_comp_v238.sql")
            return 0
        umap = {str((u or {}).get("discord_id")): (u or {}) for u in (ures.data or [])}
        prmap = {}
        for r in (pr_board or []):
            try:
                if (r or {}).get("pr") is not None:
                    prmap[str((r or {}).get("discord_id"))] = (r or {}).get("pr")
            except Exception:
                continue
        done = 0
        for did in dids:
            try:
                u = umap.get(did) or {}
                if not u.get("tourney_name_optin"):
                    continue
                if did not in prmap:
                    continue
                gname = str(u.get("game_username") or "").strip()
                if not gname:
                    continue
                nick = _comp_pr_nick(gname, prmap[did])
                if not nick:
                    continue
                try:
                    m = guild.get_member(int(did))
                except Exception:
                    m = None
                if m is None:
                    try:
                        m = await guild.fetch_member(int(did))
                    except Exception:
                        continue
                if m is None:
                    continue
                try:
                    if m.nick != nick:
                        await m.edit(nick=nick, reason="Comp PR nickname (opt-in)")
                    done += 1
                except Exception:
                    continue
            except Exception:
                continue
        return done
    except Exception as e:
        print(f"[comp] apply nicknames failed: {e}")
        return 0


@bot.tree.command(name="tourney-pr-set", description="Host: manually set or clear a player's tourney PR")
@app_commands.describe(tourney_id="Numeric session ID (from 0)", user="Player", pr="PR value 0-999 (omit to clear manual PR)")
async def tourney_pr_set_cmd(interaction: discord.Interaction, tourney_id: int, user: discord.User, pr: float | None = None):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        _st, body = await _api_post("/api/comp/tourney/pr-override", {
            "tourney_id": int(tourney_id), "discord_id": str(user.id), "pr": pr})
    except Exception:
        _st, body = 0, {}
    if not (body or {}).get("ok"):
        return await interaction.followup.send(
            f"Couldn't set PR: {(body or {}).get('error') or 'server unreachable'}.", ephemeral=True)
    if pr is None:
        return await interaction.followup.send(
            f"Cleared manual PR for {user.mention} in tourney **#{tourney_id}**.", ephemeral=True)
    return await interaction.followup.send(
        f"Set PR for {user.mention} to **{pr}** in tourney **#{tourney_id}** (manual, kept on recompute).", ephemeral=True)


@bot.tree.command(name="tourney-pr-reset", description="Host: delete a tourney's PR so it recomputes fresh")
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
async def tourney_pr_reset_cmd(interaction: discord.Interaction, tourney_id: int):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        _st, body = await _api_post("/api/comp/tourney/pr-reset", {"tourney_id": int(tourney_id)})
    except Exception:
        _st, body = 0, {}
    if not (body or {}).get("ok"):
        return await interaction.followup.send(
            f"Couldn't reset PR: {(body or {}).get('error') or 'server unreachable'}.", ephemeral=True)
    return await interaction.followup.send(
        f"Deleted {(body or {}).get('deleted', 0)} PR rows for tourney **#{tourney_id}** — next stop/PR call recomputes fresh.", ephemeral=True)


@bot.tree.command(name="tourney-pr", description="Tourney PR board (computed rating)")
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
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
    try:
        _renamed = await _comp_apply_pr_nicknames(interaction.guild, board)
        if _renamed:
            await interaction.followup.send(
                f"✏️ {_renamed} opted-in nickname{'s' if _renamed != 1 else ''} updated.")
    except Exception as ex:
        print(f"[comp] pr nicknames failed: {ex}")


@bot.tree.command(name="pr-leaderboard", description="Top Tourney PR ratings (latest per player)")
async def pr_leaderboard_cmd(interaction: discord.Interaction, limit: int = 10):
    await interaction.response.defer(ephemeral=False)
    limit = max(1, min(int(limit or 10), 25))
    try:
        data = await _api_get(f"/api/leaderboard?sort=pr&limit=100")
    except Exception:
        data = None
    rows = (data or {}).get("results") or []
    ranked = [r for r in rows if (r or {}).get("tourney_pr") is not None][:limit]
    if not ranked and not rows:
        return await interaction.followup.send("No PR ratings yet — they compute when a tourney ends.")
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(ranked, 1):
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        nm = (r or {}).get("game_username") or (r or {}).get("discord_username") or "?"
        lines.append(f"{prefix} {_comp_mention((r or {}).get('discord_id'))} **{nm}** — {(r or {}).get('tourney_pr')} PR")
    # every comp profile shows up: ranked first, then profiles still awaiting PR
    try:
        seen = {str((r or {}).get("discord_id")) for r in ranked}
        fresh = [r for r in rows
                 if str((r or {}).get("discord_id") or "") not in seen
                 and str((r or {}).get("game_username") or "").strip()]
        for r in fresh:
            nm = (r or {}).get("game_username") or (r or {}).get("discord_username") or "?"
            lines.append(f"• {_comp_mention((r or {}).get('discord_id'))} **{nm}** — no PR yet")
    except Exception:
        pass
    return await _send_paginated(interaction, "📊 PR Leaderboard", 0x10b981, lines)


@bot.tree.command(name="comp-account", description="See your comp stats (kills, wins, placements, PR)")
@app_commands.describe(user="Player to look up (default: you)")
async def comp_account_cmd(interaction: discord.Interaction, user: discord.User | None = None):
    await interaction.response.defer(ephemeral=False)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.")
    target = user or interaction.user
    did = str(target.id)
    try:
        ures = await db._sb_call(lambda: sb.table("bngc_users").select(
            "discord_id,game_username,discord_username,tourney_pr").eq(
            "discord_id", did).limit(1).execute())
        urow = ((ures.data or [{}])[0] or {})
    except Exception:
        urow = {}
    gname = (urow.get("game_username") or "").strip()
    if not gname:
        return await interaction.followup.send(
            f"{getattr(target, 'mention', 'That user')} has no comp profile yet.")
    kills, mids = 0, set()
    try:
        eres = await db._sb_call(lambda: sb.table("bngc_comp_events").select("match_id,etype").eq(
            "discord_id", did).eq("etype", "kill").limit(5000).execute())
        for e in (eres.data or []):
            kills += 1
            if (e or {}).get("match_id") is not None:
                mids.add((e or {}).get("match_id"))
    except Exception:
        pass
    # also count matches where the player appears without kills (alv rows)
    try:
        ares = await db._sb_call(lambda: sb.table("bngc_comp_events").select("match_id").eq(
            "discord_id", did).limit(5000).execute())
        for e in (ares.data or []):
            if (e or {}).get("match_id") is not None:
                mids.add((e or {}).get("match_id"))
    except Exception:
        pass
    wins, places = 0, []
    try:
        for chunk in [sorted(mids)[i:i + 200] for i in range(0, len(mids), 200)] or []:
            mres = await db._sb_call(lambda: sb.table("bngc_comp_matches").select(
                "id,winner_discord_id,placements").in_("id", chunk).limit(200).execute())
            for m in (mres.data or []):
                try:
                    if str((m or {}).get("winner_discord_id") or "") == did:
                        wins += 1
                    pl = ((m or {}).get("placements") or {}).get(did)
                    if pl is not None:
                        places.append(int(pl))
                except Exception:
                    continue
            break  # cap: first 200 matches is plenty for a career line
    except Exception:
        pass
    matches = len(mids)
    avg = round(sum(places) / len(places), 1) if places else None
    # PR + delta (same rule as GET /api/stats: last two history rows)
    pr, prev, delta = None, None, None
    try:
        hres = await db._sb_call(lambda: sb.table("bngc_comp_pr_history").select("pr").eq(
            "discord_id", did).order("computed_at", desc=True).limit(2).execute())
        hist = [float((h or {}).get("pr")) for h in (hres.data or []) if (h or {}).get("pr") is not None]
        if hist:
            pr = hist[0]
            if len(hist) > 1 and hist[1]:
                prev = hist[1]
                delta = round((pr - prev) / prev * 100, 1)
        if pr is None and urow.get("tourney_pr") is not None:
            pr = float(urow.get("tourney_pr"))
    except Exception:
        try:
            if urow.get("tourney_pr") is not None:
                pr = float(urow.get("tourney_pr"))
        except Exception:
            pr = None
    if delta is None:
        dline = "(=)"
    elif delta > 0:
        dline = f"(+{delta}%)"
    elif delta < 0:
        dline = f"({delta}%)"
    else:
        dline = "(=)"
    e = discord.Embed(
        title=f"🎮 Comp account — {gname}", color=0x7c3aed,
        description=(f"{getattr(target, 'mention', '')}\n"
                     f"**PR:** {pr if pr is not None else '—'} {dline}\n"
                     f"**Kills:** {kills} · **Wins:** {wins} · **Matches:** {matches}\n"
                     f"**Avg placement:** {avg if avg is not None else '—'}"))
    return await interaction.followup.send(embed=e)


@bot.tree.command(name="ranked-account", description="See your ranked stats (points, milestone, wins, placements)")
@app_commands.describe(user="Player to look up (default: you)")
async def ranked_account_cmd(interaction: discord.Interaction, user: discord.User | None = None):
    """Ranked career line from the comp-merge seal data. Never raises beyond
    the followup."""
    await interaction.response.defer(ephemeral=False)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.")
    target = user or interaction.user
    did = str(target.id)
    try:
        ures = await db._sb_call(lambda: sb.table("bngc_users").select(
            "discord_id,game_username,discord_username").eq(
            "discord_id", did).limit(1).execute())
        urow = ((ures.data or [{}])[0] or {})
    except Exception:
        urow = {}
    gname = (urow.get("game_username") or "").strip()
    if not gname:
        return await interaction.followup.send(
            f"{getattr(target, 'mention', 'That user')} has no ranked profile yet — link a game name with **/register**.")
    pts = ranked_mod.get_points(did)
    milestone, _fee = ranked_mod.milestone_for(pts)
    kills, match_keys = 0, set()
    try:
        eres = await db._sb_call(lambda: sb.table("bngc_ranked_events").select(
            "room_id,match_version,etype").eq(
            "discord_id", did).limit(5000).execute())
        for e in (eres.data or []):
            try:
                if (e or {}).get("etype") == "kill":
                    kills += 1
                if (e or {}).get("room_id") is not None:
                    match_keys.add((str((e or {}).get("room_id")),
                                    int((e or {}).get("match_version") or 0)))
            except Exception:
                continue
    except Exception:
        pass
    wins, places = 0, []
    try:
        rres = await db._sb_call(lambda: sb.table("ranked_results").select(
            "room_id,match_version,winner_discord_id,kills,placements").order(
            "match_version", desc=True).limit(1000).execute())
        for m in (rres.data or []):
            try:
                key = (str((m or {}).get("room_id")),
                       int((m or {}).get("match_version") or 0))
                mk = (m or {}).get("kills") or {}
                pl = ((m or {}).get("placements") or {}).get(did)
                if key in match_keys or did in mk or pl is not None:
                    match_keys.add(key)
                    if str((m or {}).get("winner_discord_id") or "") == did:
                        wins += 1
                    if pl is not None:
                        places.append(int(pl))
            except Exception:
                continue
    except Exception:
        pass
    matches = len(match_keys)
    avg = round(sum(places) / len(places), 1) if places else None
    rate = f"{round(100 * wins / matches)}%" if matches else "—"
    e = discord.Embed(
        title=f"👑 Ranked account — {gname}", color=0xFFD700,
        description=(f"{getattr(target, 'mention', '')}\n"
                     f"**Points:** {pts} · **{milestone}**\n"
                     f"**Kills:** {kills} · **Wins:** {wins} · **Matches:** {matches} · **Win rate:** {rate}\n"
                     f"**Avg placement:** {avg if avg is not None else '—'}"))
    try:
        e.set_thumbnail(url=ranked_mod.milestone_icon(milestone))
    except Exception:
        pass
    return await interaction.followup.send(embed=e)


@bot.tree.command(name="rank", description="Your ranked card (points, milestone, leaderboard position)")
@app_commands.describe(user="Player to look up (default: you)")
async def rank_cmd(interaction: discord.Interaction, user: discord.User | None = None):
    """Rank card with tier icon. Never raises beyond the followup."""
    await interaction.response.defer(ephemeral=False)
    try:
        target = user or interaction.user
        did = str(target.id)
        pts = ranked_mod.get_points(did)
        milestone, _fee = ranked_mod.milestone_for(pts)
        try:
            board = ranked_mod.top_points(1000) or []
        except Exception:
            board = []
        pos = next((i + 1 for i, (d, _p) in enumerate(board) if str(d) == did), None)
        total = len(board)
        try:
            nxt = min((t for t, _n, _f in ranked_mod.MILESTONE_FEES if t > pts), default=None)
        except Exception:
            nxt = None
        if nxt is None:
            prog = "Max rank 👑"
        else:
            try:
                _nn = next(n for t, n, _f in ranked_mod.MILESTONE_FEES if t == nxt)
            except Exception:
                _nn = "?"
            prog = f"{nxt - pts} pts to **{_nn}**"
        e = discord.Embed(
            title=f"👑 Rank — {getattr(target, 'display_name', None) or getattr(target, 'name', '?')}",
            color=0xFFD700,
            description=(f"{getattr(target, 'mention', '')}\n"
                         f"**{milestone}** · **{pts}** pts\n"
                         f"Leaderboard: **#{pos}** of {total}\n"
                         f"{prog}"))
        try:
            e.set_thumbnail(url=ranked_mod.milestone_icon(milestone))
        except Exception:
            pass
        return await interaction.followup.send(embed=e)
    except Exception as e:
        print(f"[rank] failed: {e}")
        return await interaction.followup.send("Couldn't load rank — try again later.")


async def _ranked_sync_member_roles(guild, did: str, milestone: str):
    """Grant the tier role, strip other tier roles. (ok, role|reason).

    Unconfigured tier (no env id): no-op False. Best-effort per role
    (perms/hierarchy failures skipped). Never raises.
    """
    try:
        if guild is None or not did:
            return False, "no guild"
        want = ranked_mod.tier_role_id(milestone)
        try:
            m = guild.get_member(int(did))
        except Exception:
            m = None
        if m is None:
            try:
                m = await guild.fetch_member(int(did))
            except Exception:
                return False, "member not found"
        if m is None:
            return False, "member not found"
        try:
            have_ids = {int(r.id) for r in (m.roles or [])}
        except Exception:
            have_ids = set()
        try:
            tier_ids = {int(i) for i in ranked_mod.MILESTONE_ROLE_IDS.values() if i}
        except Exception:
            tier_ids = set()
        for rid in tier_ids:
            if rid != (want or 0) and rid in have_ids:
                try:
                    r = guild.get_role(rid)
                    if r is not None:
                        await m.remove_roles(r, reason="Ranked tier sync")
                except Exception:
                    continue
        if not want:
            return False, f"no role configured for {milestone} (set RANKED_ROLE_{str(milestone or '').upper()})"
        if int(want) in have_ids:
            return True, str(milestone)
        try:
            r = guild.get_role(int(want))
            if r is None:
                return False, "role missing from this server"
            await m.add_roles(r, reason="Ranked tier sync")
            return True, str(milestone)
        except discord.errors.Forbidden:
            return False, "bot needs Manage Roles + a higher role"
        except Exception as e:
            return False, str(e)[:80]
    except Exception as e:
        return False, str(e)[:80]


async def _ranked_sync_room_roles(room: dict) -> int:
    """Sync tier roles for a room's roster. Guild via thread, else lobby
    channel. Returns synced count. Never raises."""
    try:
        guild = None
        try:
            tid = room.get("thread_id")
            if tid:
                th = bot.get_channel(int(tid))
                if th is None:
                    th = await bot.fetch_channel(int(tid))
                guild = getattr(th, "guild", None)
        except Exception:
            pass
        if guild is None:
            try:
                cid = room.get("lobby_channel_id")
                if cid:
                    ch = bot.get_channel(int(cid))
                    if ch is None:
                        ch = await bot.fetch_channel(int(cid))
                    guild = getattr(ch, "guild", None)
            except Exception:
                pass
        if guild is None:
            print(f"[ranked] room role sync skipped (no guild) for {room.get('id')}")
            return 0
        try:
            dids = [str(d) for d in ranked_mod.roster_ids(room)]
        except Exception:
            return 0
        if not dids:
            return 0
        try:
            totals = ranked_mod.get_points_many(dids)
        except Exception:
            totals = {}
        done = 0
        for did in dids:
            try:
                ms, _fee = ranked_mod.milestone_for(int((totals or {}).get(did, 0)))
            except Exception:
                ms = "Unranked"
            try:
                ok, _why = await _ranked_sync_member_roles(guild, did, ms)
                if ok:
                    done += 1
            except Exception:
                continue
        return done
    except Exception as e:
        print(f"[ranked] room role sync failed: {e}")
        return 0


@bot.tree.command(name="rank-sync", description="Sync your tier role (admins can sync others)")
@app_commands.describe(user="Player to sync (default: you, admins only for others)")
async def rank_sync_cmd(interaction: discord.Interaction, user: discord.User | None = None):
    await interaction.response.defer(ephemeral=True)
    try:
        target = user or interaction.user
        if target.id != interaction.user.id:
            try:
                admin = bool(interaction.guild and interaction.user.guild_permissions.administrator)
            except Exception:
                admin = False
            if not admin:
                return await interaction.followup.send("Only admins can sync other players.", ephemeral=True)
        did = str(target.id)
        pts = ranked_mod.get_points(did)
        milestone, _fee = ranked_mod.milestone_for(pts)
        ok, why = await _ranked_sync_member_roles(interaction.guild, did, milestone)
        if ok:
            return await interaction.followup.send(
                f"✅ {getattr(target, 'mention', '')} synced to **{why}** ({pts} pts).", ephemeral=True)
        return await interaction.followup.send(f"Couldn't sync: {why}.", ephemeral=True)
    except Exception as e:
        print(f"[rank-sync] failed: {e}")
        return await interaction.followup.send("Couldn't sync — try again later.", ephemeral=True)


def _is_guild_admin(interaction: discord.Interaction) -> bool:
    """Administrator check for privileged rank commands. Never raises."""
    try:
        return bool(interaction.guild and interaction.user.guild_permissions.administrator)
    except Exception:
        return False


@bot.tree.command(name="rank-sync-all", description="Sync tier roles for every ranked player (admin)")
async def rank_sync_all_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not _is_guild_admin(interaction):
        return await interaction.followup.send("Admins only.", ephemeral=True)
    try:
        board = ranked_mod.top_points(10000) or []
    except Exception:
        board = []
    if not board:
        return await interaction.followup.send("No ranked points yet — nothing to sync.", ephemeral=True)
    ok, fail, skipped = 0, 0, 0
    total = len(board)
    for i, (did, pts) in enumerate(board):
        try:
            milestone, _fee = ranked_mod.milestone_for(int(pts))
        except Exception:
            milestone = "Unranked"
        try:
            good, _why = await _ranked_sync_member_roles(interaction.guild, str(did), milestone)
            if good:
                ok += 1
            else:
                skipped += 1
        except Exception:
            fail += 1
        if (i + 1) % 20 == 0:
            try:
                await asyncio.sleep(1)  # breathe for rate limits on big rosters
            except Exception:
                pass
    return await interaction.followup.send(
        f"👑 Rank sync all: **{ok}/{total}** synced"
        f"{f', {skipped} skipped (left, locked roles, or unconfigured tiers)' if skipped else ''}"
        f"{f', {fail} failed' if fail else ''}.", ephemeral=True)


@bot.tree.command(name="rank-reset", description="Zero ALL ranked points and resync roles (admin, irreversible)")
@app_commands.describe(confirm="Must be True — wipes every player's points")
async def rank_reset_cmd(interaction: discord.Interaction, confirm: bool):
    await interaction.response.defer(ephemeral=True)
    if not _is_guild_admin(interaction):
        return await interaction.followup.send("Admins only.", ephemeral=True)
    if not confirm:
        return await interaction.followup.send(
            "⚠️ This zeroes **every** player's ranked points. Re-run with `confirm: True` if you mean it.",
            ephemeral=True)
    try:
        try:
            board = ranked_mod.top_points(10000) or []
        except Exception:
            board = []
        dids = [str(d) for d, _p in board if str(d or "").strip()]
        removed = ranked_mod.reset_points_all()
        # everyone is Unranked now: strip old tiers, grant Unranked
        synced, failed = 0, 0
        for did in dids:
            try:
                good, _why = await _ranked_sync_member_roles(interaction.guild, did, "Unranked")
                if good:
                    synced += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        return await interaction.followup.send(
            f"🧹 Rank reset: **{removed}** point row(s) wiped, **{synced}/{len(dids)}** synced to Unranked"
            f"{f', {failed} failed' if failed else ''}. Leaderboards update on next read (server cache ≤60s).",
            ephemeral=True)
    except Exception as e:
        print(f"[rank-reset] failed: {e}")
        return await interaction.followup.send("Reset failed partway — check points before retrying.", ephemeral=True)


async def _ranked_board_rows(limit: int = 50):
    """Top holders [(did, pts, name, milestone)] with display names. Never raises."""
    try:
        board = ranked_mod.top_points(max(1, min(int(limit or 50), 200))) or []
    except Exception:
        return []
    dids = [str(d) for d, _p in board if str(d or "").strip()]
    names: dict = {}
    try:
        sb = _comp_sb()
        if sb is not None and dids:
            ures = await db._sb_call(lambda: sb.table("bngc_users").select(
                "discord_id,game_username,discord_username").in_("discord_id", dids[:200]).execute())
            for u in (ures.data or []):
                try:
                    if (u or {}).get("discord_id"):
                        names[str((u or {}).get("discord_id"))] = str(
                            (u or {}).get("game_username") or (u or {}).get("discord_username")
                            or (u or {}).get("discord_id"))
                except Exception:
                    continue
    except Exception:
        pass
    out = []
    for did, pts in board:
        try:
            milestone, _fee = ranked_mod.milestone_for(int(pts))
        except Exception:
            milestone = "Unranked"
        out.append((str(did), int(pts), names.get(str(did), str(did)[:6]), milestone))
    return out


def _ranked_board_lines(rows, offset: int = 0) -> list:
    """Text lines for a board slice (shared by command + scheduler). Pure."""
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    try:
        for i, (did, pts, nm, ms) in enumerate(rows, 1):
            n = offset + i
            prefix = medals[n - 1] if n <= 3 else f"{n}."
            lines.append(f"{prefix} {_comp_mention(did)} **{nm}** — {pts} pts ({ms})")
    except Exception:
        pass
    return lines


@bot.tree.command(name="ranked-board", description="Ranked points leaderboard (paginated)")
@app_commands.describe(limit="Rows to show (default 25)")
async def ranked_board_cmd(interaction: discord.Interaction, limit: int = 25):
    await interaction.response.defer(ephemeral=False)
    try:
        limit = max(1, min(int(limit or 25), 50))
    except Exception:
        limit = 25
    rows = await _ranked_board_rows(limit)
    if not rows:
        return await interaction.followup.send("No ranked points yet — play a ranked match first.")
    return await _send_paginated(interaction, "👑 Ranked Leaderboard", 0xFFD700,
                                 _ranked_board_lines(rows))


@bot.tree.command(name="tourney-list", description="List comp tourneys (id, status, matches)")
async def tourney_list_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.")
    try:
        res = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
            "id,code,name,status,mode,series_len").order("id").limit(25).execute())
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


@admin_group.command(name="listen-registrations", description="Open signups in a channel (host)")
@app_commands.describe(channel="Signups channel", tourney_id="Numeric session ID",
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
        tres = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
            "id,name,mode,player_role_id,ping_role_id").eq(
            "id", int(tourney_id)).in_("status", ["created", "scheduled", "started"]).limit(1).execute())
        if not tres.data:
            return await interaction.followup.send(
                "No registrable tourney with that ID (created/scheduled/started).", ephemeral=True)
        t = tres.data[0]
        print(t)
        ping = str(ping_role.id) if ping_role else (t.get("ping_role_id") or t.get("player_role_id") or COMP_PLAYER_ROLE_ID or "")
        await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update(
            {"reg_open": True, "reg_channel_id": str(channel.id),
             "ping_role_id": ping or None}).eq("id", int(tourney_id)).execute())
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


@admin_group.command(name="stop-registration", description="Close signups: lock typing + post counts (host)")
@app_commands.describe(tourney_id="Numeric session ID")
async def stop_reg_cmd(interaction: discord.Interaction, tourney_id: int):
    await interaction.response.defer(ephemeral=True)
    me = await _comp_member(interaction, interaction.user.id)
    if not _comp_is_host(me):
        return await interaction.followup.send("Host only.", ephemeral=True)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.", ephemeral=True)
    try:
        res = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
            "id,name,mode,reg_channel_id,ping_role_id,player_role_id").eq(
            "id", int(tourney_id)).limit(1).execute())
        if not res.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        t = res.data[0]
        await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update({"reg_open": False}).eq("id", int(tourney_id)).execute())
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
            f"🔇 Registration closed for tourney **#{tourney_id}** ({players} players"
            f"{f' / {teams} teams' if str((t or {}).get('mode') or 'solo').lower() == 'duo' else ''}).",
            ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't stop: {e}", ephemeral=True)


async def _comp_accepted_counts(tourney_id: int):
    """(accepted players, accepted teams). Never raises."""
    try:
        sb = _comp_sb()
        if sb is None:
            return 0, 0
        res = await db._sb_call(lambda: sb.table("bngc_comp_registrations").select("member_ids").eq(
            "tourney_id", int(tourney_id)).eq("status", "accepted").limit(1000).execute())
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
        tres = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
            "id,name,mode,reg_open,reg_channel_id,max_players").eq(
            "id", int(tourney_id)).limit(1).execute())
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
        await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update({"reg_open": False}).eq("id", int(tourney_id)).execute())
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
@app_commands.describe(tourney_id="Numeric session ID (from 0)",
                       role_to_give="Player role (default: stored)")
async def tourney_start_cmd(interaction: discord.Interaction, tourney_id: int,
                            role_to_give: discord.Role | None = None):
    await interaction.response.defer(ephemeral=True)
    sb, me = await _comp_require_manager(interaction, tourney_id)
    if sb is None:
        return
    try:
        from datetime import datetime, timezone
        tres = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
            "id,code,name,status,player_role_id").eq("id", int(tourney_id)).limit(1).execute())
        if not tres.data:
            return await interaction.followup.send("No tourney with that ID.", ephemeral=True)
        t = tres.data[0]
        role_id = (str(role_to_give.id) if role_to_give else None) or t.get("player_role_id") or COMP_PLAYER_ROLE_ID
        if role_to_give is not None:
            try:
                await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update(
                    {"player_role_id": str(role_to_give.id)}).eq("id", int(tourney_id)).execute())
            except Exception:
                pass
        rres = await db._sb_call(lambda: sb.table("bngc_comp_registrations").select("member_ids").eq(
            "tourney_id", int(tourney_id)).eq("status", "accepted").limit(1000).execute())
        dids = sorted({str(x) for r in (rres.data or []) for x in ((r or {}).get("member_ids") or []) if x})
        if not role_id:
            try:
                await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update(
                    {"started_at": datetime.now(timezone.utc).isoformat(),
                     "status": "started"}).eq("id", int(tourney_id)).execute())
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
            await db._sb_call(lambda: sb.table("bngc_comp_tourneys").update(
                {"started_at": datetime.now(timezone.utc).isoformat(),
                 "status": "started"}).eq("id", int(tourney_id)).execute())
        except Exception:
            pass
        # auto-tag the room code so presence + drawer show the tourney state
        if t.get("code"):
            try:
                await db._sb_call(lambda: sb.table("bngc_room_tags").upsert({
                    "code": str(t["code"]).upper(), "kind": "tourney",
                    "name": str(t.get("name") or "Tourney"),
                    "created_by": str(interaction.user.id),
                }, on_conflict="code").execute())
            except Exception as e:
                print(f"[comp] start tag failed: {e}")
        extra = "" if ok == len(dids) else f" ({len(dids) - ok} failed — check role order/perms)"
        return await interaction.followup.send(
            f"🚀 Tourney **#{tourney_id}** started — role to {ok}/{len(dids)} players.{extra}",
            ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't start: {e}", ephemeral=True)


TEAM_COLORS = ["🔵", "🔴", "🟠", "🟡", "🟢", "🟣"]

# One Discord role per team slot, same order as TEAM_COLORS. The mapping and
# the role-id config live in teams.py so they can be unit-tested without the
# bot's command tree; these names keep the existing call sites readable.
from teams import (configured_team_role_ids as _comp_team_role_ids_set,  # noqa: E402
                   team_color as _comp_team_color,
                   team_role_id as _comp_team_role_id)


async def _comp_sync_team_roles(guild, sb, tourney) -> dict:
    """Re-derive duo team colors and re-grant each team's Discord roles.

    Colors are keyed by team order (the same intake order that produced them)
    and are FROZEN on the registration row, so after a team is added or
    dropped the stored values drift. Roles are keyed by the same index and
    are stripped-then-granted, mirroring _ranked_sync_member_roles, so a
    player who moved teams never keeps the old colour role.

    Returns {"teams", "members", "granted", "stripped", "recolored",
    "no_role_config", "solo"}. Never raises.
    """
    out = {"teams": 0, "members": 0, "granted": 0, "stripped": 0,
           "recolored": 0, "no_role_config": not _comp_team_role_ids_set(),
           "solo": False}
    try:
        t = tourney or {}
        tid = int(t.get("id") or 0)
        if not tid:
            return out
        if str(t.get("mode") or "solo").lower() != "duo":
            out["solo"] = True
            return out
        rows = await _comp_reg_rows(tid)
        live = [r for r in (rows or []) if str((r or {}).get("status") or "") != "banned"]
        out["teams"] = len(live)
        if not live:
            return out
        # first pass: colours (a write, so do it before the rate-limited grants)
        for idx, r in enumerate(live):
            try:
                want = _comp_team_color(idx)
                if str((r or {}).get("team_color") or "") != want:
                    await db._sb_call(lambda: sb.table("bngc_comp_registrations")
                                      .update({"team_color": want})
                                      .eq("id", (r or {}).get("id")).execute())
                    out["recolored"] += 1
            except Exception:
                continue
        if guild is None:
            return out
        every = _comp_team_role_ids_set()
        for idx, r in enumerate(live):
            want_role = _comp_team_role_id(idx)
            for did in ((r or {}).get("member_ids") or []):
                did = str(did or "").strip()
                if not did:
                    continue
                out["members"] += 1
                for other in every:
                    if other == want_role:
                        continue
                    if await _comp_remove_role(guild, did, other,
                                               reason=f"comp team #{tid} refresh"):
                        out["stripped"] += 1
                if want_role and await _comp_grant_role(guild, did, want_role):
                    out["granted"] += 1
                if out["members"] % 20 == 0:
                    await asyncio.sleep(1)  # stay under the role rate limit
        return out
    except Exception as e:
        print(f"[comp] team role sync failed: {e}")
        return out


def _comp_open_text(mode: str, ping_role_id: str | None = None, name: str = "Tourney") -> str:
    """Registration-open announcement. Pure. Ping role mention prepended."""
    try:
        is_duo = str(mode or "").lower() == "duo"
        if is_duo:
            head = (f"## {str(name).upper()} SIGNUPS REGISTRATIONS OPEN:\n\n"
                    f"*@yourself AND your teammate to register — one message with both mentions, "
                    f"your own mention first (type @ and pick from the popup — "
                    f"typed-out names don't count).*\n"
                    f"*Both players then press **Create comp profile** below so kills map to you.*")
        else:
            head = (f"## {str(name).upper()} SIGNUPS REGISTRATIONS OPEN:\n\n"
                    f"*@yourself to register (type @ and pick your name from the popup — "
                    f"typed-out names don't count).*\n"
                    f"*Then press **Create comp profile** below so kills map to you.*")
        ping = str(ping_role_id or "").strip()
        return (f"<@&{ping}>\n" if ping else "") + head
    except Exception:
        return "SIGNUPS OPEN"


def _comp_closed_text(mode: str, players: int, teams: int, name: str) -> str:
    """Registration-closed announcement. Pure."""
    try:
        if str(mode or "").lower() == "duo":
            return (f"## {str(name).upper()} SIGNUPS CLOSED:\n\n"
                    f"{int(players or 0)} Players / {int(teams or 0)} Teams")
        return (f"## {str(name).upper()} SIGNUPS CLOSED:\n\n"
                f"{int(players or 0)} Players")
    except Exception:
        return "SIGNUPS CLOSED"


class _GameNameModal(discord.ui.Modal):
    """Comp-profile prompt. Writes bngc_users.game_username.

    Duplicates (normalized, cross-tourney-roster) are refused so the engine
    mapping stays 1:1; twins fail closed and resolve via dashboard /link.
    On success a pending signup is promoted to accepted (❓→✅).
    """

    def __init__(self, tourney_id: int):
        super().__init__(title="Create your comp profile")
        self.tourney_id = int(tourney_id)
        self.game_name = discord.ui.TextInput(
            label="Current in-game username",
            placeholder="Exactly as it appears in-game",
            min_length=1, max_length=32, required=True)
        # instance-assigned inputs are NOT auto-collected by discord.py
        # (only class-level attributes are) — an empty modal gets a 400
        # from the API, so register explicitly.
        self.add_item(self.game_name)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        try:
            raw, err = _comp_valid_game_name(self.game_name.value)
            if err:
                try:
                    await self._mark_signup(False, interaction)
                except Exception:
                    pass
                return await interaction.followup.send(
                    f"❌ {err} Press **Create comp profile** and try again.", ephemeral=True)
            norm = _norm_game(raw)
            sb = _comp_sb()
            if sb is None:
                return await interaction.followup.send("Supabase not configured.", ephemeral=True)
            me = str(interaction.user.id)
            rows = await _comp_reg_rows(self.tourney_id)
            mine = [r for r in rows
                    if me in ((r or {}).get("member_ids") or [])
                    and (r or {}).get("status") != "banned"]
            if not mine:
                return await interaction.followup.send("Register for this tourney first (@yourself in the signup channel).", ephemeral=True)
            dids = sorted({str(x) for r in rows for x in ((r or {}).get("member_ids") or [])
                           if (r or {}).get("status") != "banned" and x and str(x) != me})[:100]
            taken_by = None
            if dids:
                try:
                    gres = await db._sb_call(lambda: sb.table("bngc_users").select(
                        "discord_id,game_username").in_("discord_id", dids).execute())
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
                try:
                    await self._mark_signup(False, interaction)
                except Exception:
                    pass
                return await interaction.followup.send(
                    f"❌ '{raw}' is already registered — ask a host to link it if it's yours.", ephemeral=True)
            try:
                upd = await db._sb_call(lambda: sb.table("bngc_users").update(
                    {"game_username": raw}).eq("discord_id", me).execute())
                wrote = bool(upd.data)
            except Exception:
                wrote = False
            if not wrote:
                try:
                    await db._sb_call(lambda: sb.table("bngc_users").insert({
                        "discord_id": me,
                        "discord_username": getattr(interaction.user, "display_name", None) or str(interaction.user),
                        "game_username": raw,
                        # token is NOT NULL: mint an inert one (same as /register;
                        # unknowable, so it can never be presented for login).
                        "token": gen_token(),
                    }).execute())
                    wrote = True
                except Exception as e:
                    return await interaction.followup.send(f"Couldn't save: {e}", ephemeral=True)
            # promote any pending signup for this user (❓→✅ on success)
            try:
                pend = None
                for r in (mine or []):
                    if (r or {}).get("status") == "pending":
                        pend = r
                        break
                if pend is not None:
                    try:
                        ok = await _comp_promote_if_complete(
                            interaction.guild, int(self.tourney_id),
                            str((pend or {}).get("source_message_id") or "") or None,
                            reg_id=(pend or {}).get("id"))
                    except Exception:
                        ok = False
                    if ok:
                        try:
                            await self._mark_signup(True, interaction)
                        except Exception:
                            pass
                        try:
                            await _announce_comp_profile(interaction, raw)
                        except Exception:
                            pass
                        return await interaction.followup.send(
                            f"Saved — {interaction.user.mention} known as **{raw}** for tourney **#{self.tourney_id}**. You're in ✅", ephemeral=True)
            except Exception:
                pass
            try:
                await _announce_comp_profile(interaction, raw)
            except Exception:
                pass
            return await interaction.followup.send(
                f"Saved — {interaction.user.mention} known as **{raw}** for tourney **#{self.tourney_id}**.", ephemeral=True)
        except Exception as e:
            try:
                return await interaction.followup.send(f"Couldn't save: {e}", ephemeral=True)
            except Exception:
                pass

    async def _mark_signup(self, ok: bool, interaction: discord.Interaction) -> None:
        """Swap ❓→✅ (ok) or add ❌ (invalid) on the user's pending signup message."""
        try:
            sb = _comp_sb()
            if sb is None:
                print("[comp] mark_signup: no supabase")
                return
            me = str(interaction.user.id)
            rres = await db._sb_call(lambda: sb.table("bngc_comp_registrations").select(
                "id,member_ids,status,source_message_id,team_color").eq(
                "tourney_id", int(self.tourney_id)).neq("status", "banned").limit(200).execute())
            hit = None
            for r in (rres.data or []):
                try:
                    if me in [str(x) for x in ((r or {}).get("member_ids") or [])] and (r or {}).get("source_message_id"):
                        hit = r
                        break
                except Exception:
                    continue
            if not hit or interaction.guild is None:
                print(f"[comp] mark_signup: no row/guild me={me} tid={self.tourney_id}")
                return
            for ch in interaction.guild.text_channels:
                try:
                    msg = await ch.fetch_message(int(hit["source_message_id"]))
                except Exception:
                    continue
                try:
                    if ok:
                        try:
                            me_m = interaction.guild.me
                            if me_m is None and bot.user is not None:
                                me_m = await ch.guild.fetch_member(bot.user.id)
                        except Exception as e:
                            print(f"[comp] mark_signup: bot member lookup failed: {e}")
                            me_m = None
                        for r in (getattr(msg, "reactions", None) or []):
                            try:
                                if str(getattr(r.emoji, "name", r.emoji)) == "❓" and me_m is not None:
                                    await msg.remove_reaction("❓", me_m)
                            except Exception as e:
                                print(f"[comp] mark_signup: remove ❓ failed: {e}")
                                continue
                        await msg.add_reaction((hit or {}).get("team_color") or "✅")
                    else:
                        await msg.add_reaction("❌")
                except Exception as e:
                    print(f"[comp] mark_signup: react failed: {e}")
                break
        except Exception as e:
            print(f"[comp] mark_signup failed: {e}")


class _CompPlayersView(discord.ui.View):
    """Persistent 'see players' button (survives restarts via fixed custom_id).

    Tourney resolved at click time: the open registration in this channel.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create comp profile", style=discord.ButtonStyle.success,
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
        except Exception as e:
            print(f"[comp] open game-name modal failed: {e!r}")
            try:
                return await interaction.response.send_message(
                    f"Couldn't open the form ({str(e)[:120]}), try again.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="See players", style=discord.ButtonStyle.primary,
                       custom_id="comp:players")
    async def show_players(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=False)
        except Exception:
            pass
        try:
            regs = await _comp_open_regs()
            mine = [r for r in (regs or [])
                    if str((r or {}).get("reg_channel_id") or "") == str(getattr(interaction.channel, "id", ""))]
            if not mine:
                return await interaction.followup.send("No open registration here.")
            e, err = await _comp_players_embed(int(mine[0]["id"]), interaction.guild)
            if err:
                return await interaction.followup.send(err)
            return await interaction.followup.send(embed=e)
        except Exception as e:
            try:
                return await interaction.followup.send(f"Couldn't load: {e}")
            except Exception:
                pass


async def _comp_players_embed(tourney_id: int, guild=None):
    """Accepted-roster embed for a tourney. Returns (embed, None) or (None, err). Never raises."""
    try:
        sb = _comp_sb()
        if sb is None:
            return None, "Supabase not configured."
        try:
            trow = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select("id,name").eq(
                "id", int(tourney_id)).limit(1).execute())
            tname = (((trow.data or [{}])[0] or {}).get("name")) or f"#{tourney_id}"
        except Exception:
            tname = f"#{tourney_id}"
        rows = await _comp_reg_rows(int(tourney_id))
        acc = [r for r in rows if (r or {}).get("status") == "accepted"]
        if not acc:
            return None, f"No accepted players in tourney **#{tourney_id}** yet."
        names = []
        try:
            dids = sorted({str(x) for r in acc for x in ((r or {}).get("member_ids") or []) if x})[:100]
            umap, gmap = {}, {}
            if sb is not None and dids:
                ures = await db._sb_call(lambda: sb.table("bngc_users").select(
                    "discord_id,game_username,discord_username").in_("discord_id", dids).execute())
                for u in (ures.data or []):
                    try:
                        dd = str((u or {}).get("discord_id"))
                        umap[dd] = (u.get("discord_username") or dd[:6])
                        if (u or {}).get("game_username"):
                            gmap[dd] = str((u or {}).get("game_username"))
                    except Exception:
                        continue
            missing = [d for d in dids if not umap.get(d)][:25]
            if missing and guild is not None:
                for d in missing:
                    try:
                        m = guild.get_member(int(d))
                        if m is None:
                            m = await guild.fetch_member(int(d))
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
        e = discord.Embed(title=f"Players — tourney #{tourney_id} ({tname})", color=0x7c3aed,
                          description="\n".join(names[:50])[:4000])
        e.set_footer(text=f"{sum(len((r or {}).get('member_ids') or []) for r in acc)} players · {len(acc)} teams")
        return e, None
    except Exception as e:
        return None, f"Couldn't load: {e}"


@bot.tree.command(name="tourney-players", description="List the accepted players of a comp tourney")
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
async def tourney_players_cmd(interaction: discord.Interaction, tourney_id: int):
    await interaction.response.defer(ephemeral=False)
    e, err = await _comp_players_embed(int(tourney_id), interaction.guild)
    if err:
        return await interaction.followup.send(err)
    return await interaction.followup.send(embed=e)


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
            res = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select(
                "id,mode,reg_channel_id,player_role_id").eq("reg_open", True).neq(
                "status", "ended").execute())
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

        # self-registration only: solo = your own mention; duo = you + one
        # teammate in ANY order. Discord does not guarantee message.mentions
        # order, so a members[0] check misfires on correct "@you @teammate"
        # messages — the set is what matters. Registering strangers is refused.
        try:
            author_id = str(getattr(message.author, "id", ""))
        except Exception:
            author_id = ""
        if err is None and author_id not in (members or []):
            err = "mention yourself to register (solo: @you, duo: @you + @teammate in any order)"
        elif err is None and str((t or {}).get("mode") or "solo").lower() == "duo":
            # Normalize author-first internally so captain/team label stay
            # deterministic no matter which mention Discord listed first.
            try:
                members = [author_id] + [m for m in (members or []) if m != author_id]
            except Exception:
                pass

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
        # already in (any status but banned) -> dup, EXCEPT your own pending
        # row: retrying signup after creating a comp profile promotes it
        # (accept + role + ❓→✅) instead of refusing.
        try:
            _rows_d = await _comp_reg_rows(t["id"])
            _hits = [r for r in _rows_d
                     if (r or {}).get("status") != "banned"
                     and any(m in members for m in ((r or {}).get("member_ids") or []))]
            if _hits:
                _own_pending = [r for r in _hits
                                if (r or {}).get("status") == "pending"
                                and set(str(x) for x in ((r or {}).get("member_ids") or [])) == set(members)]
                if _own_pending and sb is not None:
                    _have = await _comp_members_with_profile(sb, members)
                    _missing = [m for m in members if m not in _have]
                    if not _missing:
                        _prow = _own_pending[0] or {}
                        _src = _prow.get("source_message_id")
                        try:
                            await _comp_promote_if_complete(
                                message.guild, int(t["id"]),
                                str(_src) if _src else None,
                                reg_id=_prow.get("id"))
                        except Exception:
                            pass
                        try:
                            await message.add_reaction("✅")
                        except Exception:
                            pass
                        try:
                            await message.reply("You're in ✅ — comp profile complete.")
                        except Exception:
                            pass
                        return
                    try:
                        _pings = " ".join(f"<@{m}>" for m in _missing)
                        await message.reply(
                            f"{_pings} you still need a comp profile — press **Create comp profile** "
                            f"and set your current in-game username.",
                            view=_CompProfileView(int(t["id"])))
                    except Exception:
                        pass
                    return
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
                _gres = await db._sb_call(lambda: sb.table("bngc_users").select(
                    "discord_id,game_username").in_("discord_id", _dids_g).execute())
                for _u in ((_gres.data or [])):
                    try:
                        _gn = _norm_game((_u or {}).get("game_username"))
                        if _gn:
                            _gmap[_gn] = str((_u or {}).get("discord_id"))
                    except Exception:
                        continue
            if sb is not None and members:
                _nres = await db._sb_call(lambda: sb.table("bngc_users").select(
                    "discord_id,game_username").in_("discord_id", members).execute())
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
            # comp-profile gate: members without bngc_users.game_username sign
            # up as pending (slot reserved) and must create a comp profile.
            _have = await _comp_members_with_profile(sb, members)
            _missing = [m for m in (members or []) if m not in _have]
            _status = "accepted" if not _missing else "pending"
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
                "status": _status,
                "team_color": color,
            }
            if member_names:
                _reg_row["member_names"] = member_names
            try:
                await db._sb_call(lambda: sb.table("bngc_comp_registrations").insert(_reg_row).execute())
            except Exception:
                _reg_row.pop("member_names", None)  # pre-migration safe
                await db._sb_call(lambda: sb.table("bngc_comp_registrations").insert(_reg_row).execute())
        except Exception as e:
            # unique violation = same message processed twice (retry)
            print(f"[comp] reg insert failed: {e}")
            return
        if _status == "pending":
            # hold the slot: ❓ + reply with the comp-profile button.
            try:
                await message.add_reaction("❓")
            except Exception as e:
                print(f"[comp] intake reaction failed: {e}")
            try:
                _pings = " ".join(f"<@{m}>" for m in _missing)
                await message.reply(
                    f"{_pings} you need a comp profile to sign up — press **Create comp profile** "
                    f"and set your current in-game username.",
                    view=_CompProfileView(int(t["id"])))
            except Exception:
                try:
                    _pings = " ".join(f"<@{m}>" for m in _missing)
                    await message.reply(
                        f"{_pings} you need a comp profile to sign up — press **Create comp profile** "
                        f"and set your current in-game username.")
                except Exception:
                    pass
            return
        # accepted: grant the player role immediately.
        try:
            _rid = (t or {}).get("player_role_id") or COMP_PLAYER_ROLE_ID
            for _did in members:
                await _comp_grant_role(message.guild, _did, _rid)
        except Exception as e:
            print(f"[comp] intake role grant failed: {e}")
        # duo: also grant THIS team's role (and strip any other team role, so a
        # player who re-signed with a new partner never keeps the old one).
        if color:
            try:
                await _comp_sync_team_roles(message.guild, sb, {
                    "id": int(t["id"]), "mode": (t or {}).get("mode") or "duo"})
            except Exception as e:
                print(f"[comp] intake team role grant failed: {e}")
        # intake verdict right on the message: team color (duo) / ✅ solo,
        # ❌ only on failure paths above. Host ✅/❌ moderation still works:
        # hosts are different users, so their reactions are distinct.
        try:
            await message.add_reaction(color or "✅")
        except Exception as e:
            print(f"[comp] intake reaction failed: {e}")
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
            rres = await db._sb_call(lambda: sb.table("bngc_comp_registrations").select(
                "id,tourney_id,member_ids,status").eq(
                "source_message_id", str(payload.message_id)).limit(5).execute())
        except Exception:
            return
        rows = [r for r in (rres.data or []) if (r or {}).get("status") == "pending"]
        if not rows:
            return
        row = rows[0]
        if str(payload.emoji.name) == "✅":
            try:
                await db._sb_call(lambda: sb.table("bngc_comp_registrations").update({"status": "accepted"}).eq("id", row["id"]).execute())
            except Exception:
                return
            # player role: tourney override, else env
            role_id = ""
            try:
                tres = await db._sb_call(lambda: sb.table("bngc_comp_tourneys").select("player_role_id").eq("id", row["tourney_id"]).limit(1).execute())
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
                await db._sb_call(lambda: sb.table("bngc_comp_registrations").update({"status": "banned"}).eq("id", row["id"]).execute())
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
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows, 1):
        name = r.get("game_username") or r.get("discord_username") or "?"
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
    return await _send_paginated(interaction, title, 0x7c3aed, lines)


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
            r = await db._sb_call(lambda: db.supabase.table("bngc_users").select("discord_id").limit(10000).execute())
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
# Ranked v1 rooms: retired (see note below). /ranked is the series system;
# v2.19.0 ranked sleep: commands stay registered but reply asleep unless
# RANKED_ENABLED=1. Canonical API: project-nectar.onrender.com primary,
# api-compbng.vercel.app fallback (see docs/api.md).
# ---------------------------------------------------------------------------

RANKED_ENABLED_BOT = os.getenv("RANKED_ENABLED", "1") == "1"

# ---------------------------------------------------------------------------
# Ranked rooms: retired. /ranked (series) replaces the room lifecycle;
# the points/milestone displays (/rank, /ranked-board, /ranked-account,
# /ranked-name, /rank-sync-all, /rank-reset) stay — ranked_points survives
# via tourney payouts. Server room endpoints stay for the userscript.
# ---------------------------------------------------------------------------


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
            return await interaction.followup.send("Link your game name first — **/register**.", ephemeral=True)
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
            return await interaction.followup.send("Host can't leave — ask an admin to end the lobby.", ephemeral=True)
        ranked_mod.roster_remove(room, did)
        await self._refresh_embed(interaction)
        await interaction.followup.send(f"You left **{self.rid}**.", ephemeral=True)




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
        # session end: tier roles follow final points (best-effort, logged)
        try:
            synced = await _ranked_sync_room_roles(room)
            print(f"[ranked] auto-end {rid}: {synced} tier role(s) synced")
        except Exception as e:
            print(f"[ranked] auto-end role sync failed for {rid}: {e}")
        if await _close_thread(room):
            _thread_cleaned.add(rid)
    except Exception as e:
        print(f"[ranked] auto-end finalize failed: {e}")


async def ranked_leaderboard_loop():
    """Hourly tick: post the ranked board when the last board post is 3+ days
    old. Channel history is the clock (no stored state — restarts safe)."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await asyncio.sleep(3600)
            if not RANKED_ENABLED_BOT:
                continue
            try:
                cid = ranked_mod.LB_CHANNEL_ID
                channel = bot.get_channel(cid)
                if channel is None:
                    channel = await bot.fetch_channel(cid)
            except Exception as e:
                print(f"[ranked] board loop: channel unreachable: {e}")
                continue
            fresh = False
            try:
                async for msg in channel.history(limit=10):
                    try:
                        if (msg.author and bot.user and msg.author.id == bot.user.id
                                and msg.embeds
                                and "Ranked Leaderboard" in str((msg.embeds[0] or {}).title or "")):
                            import datetime as _dt
                            age = (_dt.datetime.now(_dt.timezone.utc) - msg.created_at).total_seconds()
                            if age < ranked_mod.LB_POST_EVERY_S:
                                fresh = True
                            break
                    except Exception:
                        continue
            except Exception as e:
                print(f"[ranked] board loop: history read failed: {e}")
                continue
            if fresh:
                continue
            rows = await _ranked_board_rows(20)
            if not rows:
                continue
            lines = _ranked_board_lines(rows)
            pages = [lines[i:i + 10] for i in range(0, len(lines), 10)] or [[]]
            if len(pages) == 1:
                e = discord.Embed(title="👑 Ranked Leaderboard", color=0xFFD700,
                                  description="\n".join(pages[0])[:4000])
                e.set_footer(text="Season standings · every 3 days")
                await channel.send(embed=e)
            else:
                view = _LBView("👑 Ranked Leaderboard", 0xFFD700, pages, "",
                               "Season standings · every 3 days")
                await channel.send(embed=view._lb_embed(), view=view)
            print(f"[ranked] scheduled board posted ({len(rows)} rows)")
        except Exception as e:
            print(f"[ranked] leaderboard loop failed: {e}")


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
    _idle_cycles = 0
    while not bot.is_closed():
        _active = False
        try:
            for room in ranked_mod.list_due_starts():
                _active = True
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
            if live or _pending_end:
                _active = True
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
                    if fresh:
                        _active = True
                    for seal in fresh:
                        try:
                            await _post_match_board(room, seal)
                            # opted-in milestone nicknames track the new totals
                            try:
                                renamed = await _ranked_sync_milestone_nicknames(seal)
                                if renamed:
                                    print(f"[ranked] {renamed} milestone nickname(s) updated for {rid}")
                            except Exception as e:
                                print(f"[ranked] milestone nicknames failed for {rid}: {e}")
                        finally:
                            # mark seen even when the post failed: no 30s retry
                            # spam, the next seal carries the session forward
                            # (the /ranked dashboard shows it all)
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
        # Idle backoff: no live/due/pending rooms most of the day — 90s
        # instead of 30s cuts baseline Supabase polls 3x with zero UX cost.
        # Any activity resets to 30s immediately.
        if _active:
            _idle_cycles = 0
            await asyncio.sleep(30)
        else:
            _idle_cycles += 1
            await asyncio.sleep(90 if _idle_cycles >= 2 else 30)


# Restored: shared by ranked loops + /ranked (room commands retired).
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


# Restored: shared by ranked loops + /ranked (room commands retired).
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
            lines.append(f"**{i}.** {_rt_known(did, nm)} — {pts} pts ({tier})")
        e = discord.Embed(title="Arena leaderboard", color=0x7c3aed,
                          description="\n".join(lines))
        e.set_footer(text=f"{rid} · live standings")
        await thread.send(embed=e)
    except Exception as e:
        print(f"[ranked] thread leaderboard failed: {e}")


# Restored: shared by ranked loops + /ranked (room commands retired).
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


# Restored: shared by ranked loops + /ranked (room commands retired).
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


# Restored: shared by ranked loops + /ranked (room commands retired).
async def _display_name(did: str) -> str:
    """Game username > discord username > raw did. Never raises."""
    try:
        row = await db.get_by_discord(str(did))
        return (row or {}).get("game_username") or (row or {}).get("discord_username") or str(did)
    except Exception:
        return str(did)


# Restored: shared by ranked loops + /ranked (room commands retired).
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
            lines.append(f"**{ptxt}** {_rt_known(did, nm)} — {'+' if g >= 0 else ''}{g} pts ({k} kill{'s' if k != 1 else ''}){crown}")
        e = discord.Embed(title=f"Match v{v} sealed — {ranked_mod.gm_name(seal.get('gm') or room.get('gm'))}",
                          color=0x10b981,
                          description=f"Winner {_rt_known(winner, wnm)}\n" + ("\n".join(lines) or "No points awarded."))
        e.set_footer(text=f"{room['id']} · match-only results")
        await _post_thread(room, embed=e)
        print(f"[ranked] match board posted {room['id']} v{v}")
    except Exception as e:
        print(f"[ranked] match board failed: {e}")


# Restored: shared by ranked loops + /ranked (room commands retired).
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


# Restored: shared by ranked loops + /ranked (room commands retired).
async def _defer(interaction: discord.Interaction) -> bool:
    """Defer an interaction. False = interaction already dead (starved loop),
    caller must abort silently (there is nobody left to answer)."""
    try:
        await interaction.response.defer(ephemeral=True)
        return True
    except discord.errors.NotFound:
        try:
            cmd = interaction.command.name if interaction.command else "?"
        except Exception:
            cmd = "?"
        print(f"[ranked] interaction expired before defer (loop starved?) cmd={cmd}")
        return False
    except Exception as e:
        print(f"[ranked] defer failed: {e}")
        return False


async def _ranked_admin_only(interaction: discord.Interaction) -> bool:
    """True when the caller is a server admin (refusal sent otherwise).

    Every /ranked subcommand is admin-only. Safe to call before or after
    defer. Never raises.
    """
    try:
        admin = bool(interaction.user.guild_permissions.administrator)
    except Exception:
        admin = False
    if admin:
        return True
    try:
        msg = "Administrator only — /ranked commands are host tools."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass
    return False


# Restored: shared by ranked loops + /ranked (room commands retired).
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
        # roster + host: the host often doesn't play (not on roster) but must
        # still see the private thread (was silently left out).
        _invite = [d for d in dict.fromkeys(
            [str(x) for x in (roster or []) if x]
            + [str(room.get("host_discord_id") or "")]) if d]
        for did in _invite:
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

# ---------------------------------------------------------------------------
# Ranked onboarding (/ranked register) — minimal ranked identity.
# The room lifecycle commands are retired; this stays because the lobby
# buttons, tourney joins and nickname syncs all point here for mobile
# players (no companion, no stats tracking).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Ranked sessions (/ranked create/start/...) — series play on the
# bngc_ranked_tourney_* tables, plus /ranked register onboarding.
# Same embed + Join/Leave + thread UX as ranked rooms; comp /tourney-*
# untouched.
# ---------------------------------------------------------------------------

ranked_group = app_commands.Group(
    name="ranked", description="Ranked sessions (series)")


@ranked_group.command(name="register", description="Link your in-game name for ranked (admin)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(game_username="Your in-game name, exactly as it appears in-game")
async def ranked_register_cmd(interaction: discord.Interaction, game_username: app_commands.Range[str, 1, 32]):
    """Minimal ranked identity: {discord_id, game_username} only.

    For mobile players (no companion) and anyone who wants ranked without
    stats tracking. No login token, no counters/playtime/insights — kills and
    points count only inside ranked rooms. Existing /register users and comp
    profiles already satisfy this (same shared game_username); running this
    with the same name is a no-op confirm. Renames keep flowing through
    /change-user. Never raises beyond followups.
    """
    if not await _defer(interaction):
        return
    if not await _ranked_admin_only(interaction):
        return
    if not RANKED_ENABLED_BOT:
        return await interaction.followup.send("Ranked is sleeping in v2.19.0 — streak rewards are live. Back soon.", ephemeral=True)
    sb = _comp_sb()
    if sb is None:
        return await interaction.followup.send("Supabase not configured.", ephemeral=True)
    me = str(interaction.user.id)
    try:
        ures = await db._sb_call(lambda: sb.table("bngc_users").select(
            "discord_id,game_username").eq("discord_id", me).limit(1).execute())
        cur = (((ures.data or [{}])[0] or {}).get("game_username") or "").strip()
        has_row = bool((ures.data or []))
    except Exception:
        cur, has_row = "", False
    raw, err = _comp_valid_game_name(game_username)
    if err:
        return await interaction.followup.send(f"❌ {err}", ephemeral=True)
    norm = _norm_game(raw)
    if cur and _norm_game(cur) == norm:
        return await interaction.followup.send(
            f"Already set — you are known as **{cur}**.", ephemeral=True)
    try:
        dup = await db._sb_call(lambda: sb.table("bngc_users").select(
            "discord_id,game_username").ilike("game_username", raw).limit(25).execute())
        for u in ((dup.data if hasattr(dup, "data") else []) or []):
            try:
                if str((u or {}).get("discord_id")) == me:
                    continue
                if _norm_game((u or {}).get("game_username")) == norm:
                    return await interaction.followup.send(
                        f"❌ '{raw}' is already linked to another account — ask an admin to relink it if it's yours.", ephemeral=True)
            except Exception:
                continue
    except Exception:
        pass
    try:
        if has_row:
            await db._sb_call(lambda: sb.table("bngc_users").update({"game_username": raw}).eq("discord_id", me).execute())
        else:
            await db._sb_call(lambda: sb.table("bngc_users").insert({
                "discord_id": me,
                "discord_username": str(interaction.user.name or "")[:32] or me,
                "game_username": raw,
                "token": "ranked-" + secrets.token_hex(16),
            }).execute())
    except Exception as e:
        print(f"[ranked-register] save failed: {e}")
        return await interaction.followup.send("Couldn't save — try again later.", ephemeral=True)
    return await interaction.followup.send(
        f"Linked — **{raw}** is now your ranked game name. No stats tracking: your kills and points count only inside ranked rooms.",
        ephemeral=True)


# ---------------------------------------------------------------------------
# Ranked-session helpers (lobby embeds, threads, DMs, watch loop)
# ---------------------------------------------------------------------------


def _rt_names_sync(dids) -> dict:
    """One batched bngc_users read: discord_id -> game (or discord) username.

    Sync supabase IO — call it via asyncio.to_thread. One round trip beats N
    per-player reads inside a handler, which is what starves the loop.
    {} on any failure (callers fall back to the raw id).
    """
    try:
        ids = sorted({str(d) for d in (dids or []) if str(d or "").strip()})[:200]
        if not ids or not db.USE_SUPABASE or not db.supabase:
            return {}
        r = db.supabase.table("bngc_users").select(
            "discord_id,game_username,discord_username").in_("discord_id", ids).execute()
        out = {}
        for u in (r.data or []):
            try:
                d = str((u or {}).get("discord_id") or "")
                nm = str((u or {}).get("game_username")
                         or (u or {}).get("discord_username") or "").strip()
                if d and nm:
                    out[d] = nm
            except Exception:
                continue
        return out
    except Exception:
        return {}


def _rt_known(did, name) -> str:
    """One identity format for every ranked list/board: mention + game name.

    `<@did> known as **name**` — falls back to the raw id when nameless.
    Never raises.
    """
    try:
        d = str(did or "").strip()
        nm = str(name or "").strip() or d
        if not d:
            return f"**{nm}**"
        return f"<@{d}> known as **{nm}**"
    except Exception:
        return str(name or did or "?")


def _rt_lobby_embed(t: dict) -> discord.Embed:
    spec = rt_mod.lobby_spec(t or {})
    e = discord.Embed(title=spec["title"], color=0x7c3aed)
    for name, value, inline in spec["fields"]:
        e.add_field(name=name, value=value, inline=inline)
    e.set_footer(text=spec["footer"])
    return e


async def _rt_post_thread(t: dict, embed: discord.Embed, content: str | None = None) -> bool:
    """Post an embed to the session's thread. Best-effort.

    No thread yet (never started) -> False, caller falls back to DMs only.
    Never raises.
    """
    try:
        tid = (t or {}).get("thread_id")
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


async def _rt_dm_roster(tid: int, text: str) -> int:
    """DM text to every roster member. Returns sent count. Best-effort."""
    try:
        rids = await asyncio.to_thread(rt_mod.roster_ids, int(tid))
    except Exception:
        return 0
    sent = 0
    for did in (rids or []):
        try:
            user = await bot.fetch_user(int(did))
            await user.send(text)
            sent += 1
        except Exception:
            continue
    return sent


# Watcher baseline: tid -> {"code": str|None, "sealed": max sealed version}.
# Seeded on boot; refreshed by every broadcast (manual or watcher-driven),
# so only genuinely new transitions announce. Never persisted — a restart
# re-seeds silently instead of replaying history.
# Per-seal match embeds are OFF: the thread stays quiet on every seal
# (boards live on the dashboard + session command). The series counter on
# the lobby message still refreshes. Flip to True to restore the embed.
RT_SEAL_RESULTS_POST = False


_rt_watch_state: dict = {}


async def _rt_match_deltas(tid: int, ver: int) -> list:
    """Per-match ladder deltas for one sealed version. [] when empty/down.

    Ranked-session economy: embeds report what the match paid (signed
    deltas), never session standings. Rows come pre-sorted desc.
    """
    try:
        code, j = await _rt_api("GET", f"/api/ranked/tourney?id={int(tid)}")
        if code != 200:
            return []
        boards = (j or {}).get("match_boards") or []
        rows = []
        for b in boards:
            try:
                if int((b or {}).get("version")) == int(ver):
                    rows = (b or {}).get("rows") or []
                    break
            except Exception:
                continue
        lines = []
        for i, r in enumerate(rows[:10], start=1):
            nm = (r or {}).get("name") or str((r or {}).get("discord_id") or "?")
            did = str((r or {}).get("discord_id") or "")
            pts = int((r or {}).get("points") or 0)
            tag = "win" if (r or {}).get("won") else (
                f"P{(r or {}).get('placement')}" if (r or {}).get("placement") else "—")
            try:
                _bd = (r or {}).get("breakdown") or None
                if isinstance(_bd, dict) and _bd:
                    _kb = int(_bd.get("kills") or 0)
                    _wb = int(_bd.get("win") or 0)
                    _pp = int(_bd.get("placement") or 0)
                    _lb = int(_bd.get("loss") or 0)
                    _gained = _kb + _wb + _pp
                    _parts = [f"{(r or {}).get('kills', 0)}K"]
                    _parts.append(tag)
                    _parts.append(f"gained +{_gained}")
                    # Always show the loss leg separately (0 when the winner
                    # paid nothing) so a -8 never reads as "only -8".
                    _parts.append(f"lost {_lb}" if _lb else "lost 0")
                    if _bd.get("capped"):
                        _parts.append("capped")
                    lines.append(f"**{i}.** {_rt_known(did, nm)} — {'+' if pts >= 0 else ''}{pts} pts "
                                 f"({' · '.join(_parts)})")
                    continue
            except Exception:
                pass
            lines.append(f"**{i}.** {_rt_known(did, nm)} — {'+' if pts >= 0 else ''}{pts} pts "
                         f"({(r or {}).get('kills', 0)}K · {tag})")
        return lines
    except Exception:
        return []


async def _rt_broadcast_code(tid: int, cleared: bool = False) -> tuple[bool, int]:
    """Announce a code set/clear to the session thread + roster DMs.

    Live code changes ping @here in the thread (players are mid-session);
    clearing just informs. Returns (thread_posted, dm_count). Never raises.
    """
    try:
        t = await asyncio.to_thread(rt_mod.get_tourney, int(tid))
    except Exception:
        return False, 0
    if not t:
        return False, 0
    try:
        name = str(t.get("name") or "")
        if cleared:
            desc = (f"**{name}** · back to Wait for start — "
                    f"sit tight for the new code.")
            embed = discord.Embed(title="Room code cleared", color=0xf59e0b,
                                  description=desc)
            embed.set_footer(text=f"ranked session #{tid} · backed out")
            dm = (f"**ranked session #{tid}** backed out — code cleared, "
                  f"wait for the new code.")
            # Clearing a live session is a change like any other — ping it.
            ping = "@here" if str((t or {}).get("status") or "") == "started" else None
            posted = await _rt_post_thread(t, embed, content=ping)
        else:
            code = str(t.get("code") or "").strip()
            series = t.get("series_len") or "∞"
            embed = discord.Embed(
                title=f"Room code: {code}",
                color=0x10b981,
                description=f"**{name}** · first to finish {series} matches\n```{code}```")
            embed.set_footer(text=f"ranked session #{tid} · code updated")
            dm = (f"**ranked session #{tid}** — code updated: **{code}**. "
                  f"Join in game now.")
            posted = await _rt_post_thread(t, embed, content="@here")
        sent = await _rt_dm_roster(int(tid), dm)
        print(f"[ranked] broadcast #{tid} cleared={cleared} "
              f"thread={posted} dms={sent}")
        # Sync the watcher baseline so it doesn't re-announce this same
        # transition 30s later (manual commands broadcast synchronously).
        try:
            _rt_watch_state[int(tid)] = {
                "code": (str(t.get("code") or "").strip().upper() or None),
                "sealed": await asyncio.to_thread(_rt_sealed_max_sync, int(tid)),
            }
        except Exception:
            pass
        return posted, sent
    except Exception as e:
        print(f"[ranked] broadcast failed: {e}")
        return False, 0


def _rt_open_tourneys_sync() -> list:
    """Non-ended tourneys (id + code). Sync (call via to_thread)."""
    try:
        return [t for t in (rt_mod.list_tourneys() or [])
                if str((t or {}).get("status")) != "ended"]
    except Exception:
        return []


def _rt_sealed_max_sync(tid: int) -> int:
    """Max sealed/manual match version. -1 when none/unavailable. Sync."""
    try:
        r = db.supabase.table("bngc_ranked_tourney_matches").select(
            "version").eq("tourney_id", int(tid)).in_(
            "status", ["sealed", "manual"]).order(
            "version", desc=True).limit(1).execute()
        rows = r.data or []
        return int(rows[0].get("version")) if rows else -1
    except Exception:
        return -1


async def _rt_post_seal_board(tid: int, ver: int) -> None:
    """Per-match deltas for a newly sealed version. Thread only (no DMs —
    seals fire all session long; the roster checks the thread). Best-effort."""
    try:
        t = await asyncio.to_thread(rt_mod.get_tourney, int(tid))
        if not t:
            return
        lines = await _rt_match_deltas(int(tid), int(ver))
        desc = f"Match v{ver} sealed."
        if lines:
            desc += "\n" + "\n".join(lines)
        e = discord.Embed(
            title=f"Ranked Session #{tid} — match results",
            color=0x10b981, description=desc)
        e.set_footer(text=f"ranked session #{tid} · ladder paid per match")
        await _rt_post_thread(t, embed=e)
        # Series counter lives on the lobby message — refresh it every seal.
        try:
            await _rt_refresh_lobby_live(int(tid))
        except Exception:
            pass
        print(f"[ranked] seal results #{tid} v{ver} posted")
    except Exception as e:
        print(f"[ranked] seal results failed: {e}")


async def ranked_session_watch_loop():
    """Every 30s: announce tourney code changes + new seals. Seeded on boot
    so only FUTURE transitions announce (no replay spam after restarts).
    Catches server-side auto-tags, which have no interaction to broadcast
    from. Never raises (a dead loop would silently stop announcing)."""
    await bot.wait_until_ready()
    try:
        for t in await asyncio.to_thread(_rt_open_tourneys_sync):
            try:
                tid = int(t.get("id"))
                _rt_watch_state[tid] = {
                    "code": (str(t.get("code") or "").strip().upper() or None),
                    "sealed": await asyncio.to_thread(_rt_sealed_max_sync, tid),
                }
            except Exception:
                continue
        print(f"[ranked] watch seeded ({len(_rt_watch_state)} open)")
    except Exception as e:
        print(f"[ranked] watch seed failed: {e}")
    while not bot.is_closed():
        try:
            await asyncio.sleep(30)
            tours = await asyncio.to_thread(_rt_open_tourneys_sync)
            seen = set()
            for t in tours:
                try:
                    tid = int(t.get("id"))
                except Exception:
                    continue
                seen.add(tid)
                code = str(t.get("code") or "").strip().upper() or None
                st = _rt_watch_state.get(tid)
                if st is None:
                    # noticed silently; transitions from here announce
                    try:
                        _rt_watch_state[tid] = {
                            "code": code,
                            "sealed": await asyncio.to_thread(
                                _rt_sealed_max_sync, tid),
                        }
                    except Exception:
                        pass
                    continue
                if code != st.get("code"):
                    # State-gated codes: a code landing on a created session
                    # is the silent start setup (autotag) — baseline only.
                    # A code changing on a live session is a change-code:
                    # embed + roster DMs + @here. Ended sessions never reach
                    # here (filtered from the open list) and are ignored.
                    if str((t or {}).get("status") or "") == "started":
                        # manual change-code/backout broadcast synchronously
                        # and refresh this same state, so reaching here means
                        # NOBODY announced it — typically the server auto-tag.
                        posted, sent = await _rt_broadcast_code(
                            tid, cleared=(code is None))
                        print(f"[ranked] watch code #{tid} "
                              f"{st.get('code')}->{code} "
                              f"thread={posted} dms={sent}")
                    else:
                        try:
                            _rt_watch_state[tid] = {
                                "code": code,
                                "sealed": await asyncio.to_thread(
                                    _rt_sealed_max_sync, tid),
                            }
                        except Exception:
                            pass
                        print(f"[ranked] watch code #{tid} "
                              f"{st.get('code')}->{code} (silent: not live)")
                    continue  # state refreshed inside broadcast
                try:
                    smax = await asyncio.to_thread(_rt_sealed_max_sync, tid)
                except Exception:
                    continue
                if smax > int(st.get("sealed", -1)):
                    if RT_SEAL_RESULTS_POST:
                        await _rt_post_seal_board(tid, smax)
                    else:
                        try:
                            await _rt_refresh_lobby_live(int(tid))
                        except Exception:
                            pass
                    st["sealed"] = smax
            for tid in [k for k in _rt_watch_state if k not in seen]:
                _rt_watch_state.pop(tid, None)
        except Exception as e:
            print(f"[ranked] watch loop failed: {e}")


async def _rt_refresh_lobby(tid: int):
    """Re-render a ranked-session lobby message. Best-effort.

    Every rt_mod call is blocking supabase IO, so it runs in a worker thread:
    blocking the event loop here is exactly what makes the *next* interaction
    expire before its own defer ("loop starved?").
    """
    try:
        t = await asyncio.to_thread(rt_mod.get_tourney, tid)
        if not t or not t.get("lobby_channel_id") or not t.get("lobby_message_id"):
            return
        ch = bot.get_channel(int(t["lobby_channel_id"]))
        if ch is None:
            ch = await bot.fetch_channel(int(t["lobby_channel_id"]))
        msg = await ch.fetch_message(int(t["lobby_message_id"]))
        embed = await asyncio.to_thread(_rt_lobby_embed, t)
        await msg.edit(embed=embed, view=RankedSessionLobbyView(int(tid)))
    except Exception as e:
        print(f"[ranked] lobby refresh failed: {e}")


async def _rt_refresh_lobby_live(tid: int):
    """Re-render the lobby message with the live spec (Series counter).

    Called on every seal so `played/series` advances as matches get sealed.
    Falls back to the created-state embed when not live. Best-effort.
    """
    try:
        t = await asyncio.to_thread(rt_mod.get_tourney, tid)
        if not t or not t.get("lobby_channel_id") or not t.get("lobby_message_id"):
            return
        if str(t.get("status") or "") == "started":
            spec = await asyncio.to_thread(
                rt_mod.live_spec, t, t.get("thread_id"))
            color = 0x10b981
        else:
            await _rt_refresh_lobby(tid)
            return
        ch = bot.get_channel(int(t["lobby_channel_id"]))
        if ch is None:
            ch = await bot.fetch_channel(int(t["lobby_channel_id"]))
        msg = await ch.fetch_message(int(t["lobby_message_id"]))
        e = discord.Embed(title=spec["title"], color=color)
        for name, value, inline in spec["fields"]:
            e.add_field(name=name, value=value, inline=inline)
        e.set_footer(text=spec["footer"])
        await msg.edit(embed=e, view=RankedSessionLobbyView(int(tid)))
    except Exception as e:
        print(f"[ranked] live lobby refresh failed: {e}")


class RankedSessionLobbyView(discord.ui.View):
    """Join/Leave/Players buttons for a ranked session lobby (timeout=None)."""

    def __init__(self, tid: int):
        super().__init__(timeout=None)
        self.tid = int(tid)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="rt-join")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        did = str(interaction.user.id)
        try:
            row = await db.get_by_discord(did)
        except Exception:
            row = None
        if not row:
            return await interaction.followup.send(
                "Link your game name first — **/register**.",
                ephemeral=True)
        t = await asyncio.to_thread(rt_mod.get_tourney, self.tid)
        if not t or str(t.get("status")) not in ("created", "scheduled"):
            return await interaction.followup.send("This session is no longer open.", ephemeral=True)
        try:
            disp = str(interaction.user.display_name or interaction.user.name or "")
        except Exception:
            disp = ""
        ok, msg = await asyncio.to_thread(rt_mod.reg_add, self.tid, did, disp)
        await _rt_refresh_lobby(self.tid)
        await interaction.followup.send(
            (f"You're in **#{self.tid}**. " if ok else "") + msg, ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.danger, custom_id="rt-leave")
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        did = str(interaction.user.id)
        t = await asyncio.to_thread(rt_mod.get_tourney, self.tid)
        if not t or str(t.get("status")) not in ("created", "scheduled"):
            return await interaction.followup.send("This session is no longer open.", ephemeral=True)
        # The creator may leave the roster: they step back to spectating
        # (still creator, still stops the tourney) without playing.
        was_host = did == str(t.get("created_by") or "")
        ok, msg = await asyncio.to_thread(rt_mod.reg_remove, self.tid, did)
        await _rt_refresh_lobby(self.tid)
        if ok and was_host:
            msg += " You're now spectating."
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Players", style=discord.ButtonStyle.secondary, custom_id="rs-players")
    async def players_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        t = await asyncio.to_thread(rt_mod.get_tourney, self.tid)
        if not t:
            return await interaction.followup.send("No ranked session with that ID.", ephemeral=True)
        lines = await _rt_players_lines(int(self.tid))
        if not lines:
            return await interaction.followup.send("No players yet.", ephemeral=True)
        e = discord.Embed(title=f"Ranked Session #{self.tid} — players ({len(lines)})",
                          color=0x7c3aed, description="\n".join(lines[:50]))
        await interaction.followup.send(embed=e, ephemeral=True)


async def _rt_players_lines(tid: int) -> list:
    """Roster lines in mention-known-as format. [] when empty/down."""
    try:
        rids = await asyncio.to_thread(rt_mod.roster_ids, int(tid))
        if not rids:
            return []
        elos = await asyncio.to_thread(rt_mod.rank_map, rids)
        names = await asyncio.to_thread(_rt_names_sync, rids)
        lines = []
        for did in rids:
            nm = names.get(str(did)) or str(did)
            try:
                ms, _fee = ranked_mod.milestone_for(int(elos.get(did, 0)))
            except Exception:
                ms = "Unranked"
            lines.append(f"{_rt_known(did, nm)} — {elos.get(did, '?')} pts ({ms})")
        return lines
    except Exception:
        return []


@ranked_group.command(name="create", description="Create a ranked session (admin)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(series_len="Matches in the series, 0 = open-ended",
                       mode="Solo or duo",
                       game_mode="Game mode (flavor only, shown in lobby)",
                       ping_role="Role pinged when the match goes live",
                       starts_in="Start in X minutes from now (0 = now)")
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
    app_commands.Choice(name="Free Mode", value="df")])
async def ranked_session_create_cmd(interaction: discord.Interaction,
                              series_len: int = 0, mode: str = "solo",
                              game_mode: str | None = None,
                              ping_role: discord.Role | None = None,
                              starts_in: int = 0):
    if not await _defer(interaction):
        return
    if not await _ranked_admin_only(interaction):
        return
    did = str(interaction.user.id)
    try:
        row = await db.get_by_discord(did)
    except Exception:
        row = None
    if not row:
        return await interaction.followup.send(
            "Link your game name first — **/register**.",
            ephemeral=True)
    if await asyncio.to_thread(ranked_mod.is_banned, did):
        return await interaction.followup.send("You are banned from ranked.", ephemeral=True)
    try:
        series = max(0, int(series_len or 0))
    except Exception:
        series = 0
    try:
        _mins = max(0, int(starts_in or 0))
    except Exception:
        _mins = 0
    try:
        scheduled = ((datetime.now(timezone.utc) + timedelta(minutes=_mins)).isoformat()
                     if _mins > 0 else None)
    except Exception:
        scheduled = None
    try:
        t = await asyncio.to_thread(
            rt_mod.create_tourney, series_len=series, mode=mode,
            game_mode=game_mode,
            ping_role_id=(str(ping_role.id) if ping_role else None),
            scheduled_at=scheduled,
            created_by=did)
    except ValueError as exc:
        return await interaction.followup.send(str(exc), ephemeral=True)
    except Exception as exc:
        print(f"[ranked] create failed: {exc}")
        return await interaction.followup.send("Couldn't create the session (DB error).", ephemeral=True)
    print(f"[ranked] session created #{t.get('id')} name={t.get('name')} host={did}")
    target = None
    try:
        cid = ranked_mod.CREATE_CHANNEL_ID or interaction.channel_id
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
    try:
        content = f"<@&{ping_role.id}>" if ping_role else None
        lobby_embed = await asyncio.to_thread(_rt_lobby_embed, t)
        msg = await target.send(content=content, embed=lobby_embed,
                                view=RankedSessionLobbyView(int(t["id"])))
        try:
            await asyncio.to_thread(rt_mod.update_tourney, int(t["id"]), {
                "lobby_channel_id": str(getattr(target, "id", "")),
                "lobby_message_id": str(msg.id)})
        except Exception:
            pass
    except Exception as exc:
        print(f"[ranked] lobby post failed: {exc}")
        return await interaction.followup.send(
            f"Session **#{t.get('id')}** created, but I couldn't post the lobby — check my channel access.",
            ephemeral=True)
    await interaction.followup.send(
        f"**{t.get('name')}** open — lobby posted. Create your room in game (I pick up the code), then **/ranked start**. Manage it on the **/ranked** dashboard.",
        ephemeral=True)


async def _rt_set_lobby(tid: int, spec: dict, color: int, buttons: bool = False):
    """Edit a ranked-tourney lobby message (own view, not the room one)."""
    try:
        t = await asyncio.to_thread(rt_mod.get_tourney, tid)
        if not t or not t.get("lobby_channel_id") or not t.get("lobby_message_id"):
            return
        e = discord.Embed(title=spec["title"], color=color)
        for name, value, inline in spec["fields"]:
            e.add_field(name=name, value=value, inline=inline)
        e.set_footer(text=spec["footer"])
        ch = bot.get_channel(int(t["lobby_channel_id"]))
        if ch is None:
            ch = await bot.fetch_channel(int(t["lobby_channel_id"]))
        msg = await ch.fetch_message(int(t["lobby_message_id"]))
        await msg.edit(embed=e, view=RankedSessionLobbyView(int(tid)) if buttons else None)
    except Exception as e:
        print(f"[ranked] lobby flip failed: {e}")


def _rt_host_or_admin(interaction: discord.Interaction, t: dict) -> bool:
    if rt_mod.is_host_row(t, interaction.user.id):
        return True
    try:
        return bool(interaction.guild and interaction.user.guild_permissions.administrator)
    except Exception:
        return False


@ranked_group.command(name="start", description="Start your ranked session (admin)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(tourney_id="Numeric session ID (from create)")
async def ranked_session_start_cmd(interaction: discord.Interaction, tourney_id: int):
    if not await _defer(interaction):
        return
    if not await _ranked_admin_only(interaction):
        return
    t = await asyncio.to_thread(rt_mod.get_tourney, tourney_id)
    if not t:
        return await interaction.followup.send("No ranked session with that ID.", ephemeral=True)
    if not _rt_host_or_admin(interaction, t):
        return await interaction.followup.send("Only the session creator (or an admin) can start.", ephemeral=True)
    if str(t.get("status")) == "ended":
        return await interaction.followup.send("Session already ended.", ephemeral=True)
    if str(t.get("status")) == "started":
        return await interaction.followup.send("Session already started.", ephemeral=True)
    # Codeless start: the host creates the room in game, the server picks
    # up the code (autotag on the created session, silent) — start just
    # consumes it. Never type a code here.
    code = str(t.get("code") or "").strip().upper()
    if not code:
        return await interaction.followup.send(
            "No room code yet — create your room in game first (I pick it up automatically), then run **/ranked start** again.",
            ephemeral=True)
    rids = await asyncio.to_thread(rt_mod.roster_ids, int(tourney_id))
    if len(rids) < 2:
        return await interaction.followup.send("Need at least 2 players to start.", ephemeral=True)
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
        return await interaction.followup.send(
            "Matches channel not reachable — check my access.", ephemeral=True)
    try:
        thread = await parent.create_thread(
            name=f"ranked session #{tourney_id}".strip(),
            type=discord.ChannelType.private_thread)
        private = True
    except Exception:
        try:
            thread = await parent.create_thread(
                name=f"ranked session #{tourney_id}".strip(),
                type=discord.ChannelType.public_thread)
            private = False
        except Exception as e:
            print(f"[ranked] thread create failed: {e}")
            return await interaction.followup.send(
                "Couldn't create the session thread — check my thread perms.", ephemeral=True)
    if private:
        # roster + session hosts (created_by + hosts): hosts rarely play, but
        # a private thread they can't see is useless to them.
        _host_ids = []
        try:
            if t.get("created_by"):
                _host_ids.append(str(t.get("created_by")))
            _host_ids += [str(x) for x in (t.get("hosts") or []) if x]
        except Exception:
            pass
        _invite = [d for d in dict.fromkeys(
            [str(x) for x in (rids or []) if x] + _host_ids) if d]
        for did in _invite:
            try:
                member = parent.guild.get_member(int(did)) if hasattr(parent, "guild") and parent.guild else None
                if member is None and hasattr(parent, "guild") and parent.guild:
                    member = await parent.guild.fetch_member(int(did))
                if member is not None:
                    await thread.add_user(member)
            except Exception:
                pass
    code = (t.get("code") or "").strip() if t.get("code") else ""
    series = t.get("series_len") or 0
    try:
        ping_role_id = str(t.get("ping_role_id") or "").strip() or None
    except Exception:
        ping_role_id = None
    # Rules go out quietly; the code embed carries the @here right after it,
    # with the code in a code block (desktop copy button) for easy copying.
    rules_embed = discord.Embed(
        title=f"Ranked Session #{tourney_id} — rules",
        color=0x7c3aed, description=ranked_mod.RULES_TEXT)
    rules_embed.set_footer(text=f"ranked session #{tourney_id} · scoring on the /ranked dashboard")
    code_embed = discord.Embed(
        title=f"Room code: {code}",
        color=0x10b981,
        description=f"**{t.get('name')}** · first to finish {series or '∞'} matches\n```{code}```")
    code_embed.set_footer(text=f"ranked session #{tourney_id} · started")
    try:
        await thread.send(embed=rules_embed)
        await thread.send(content="@here", embed=code_embed)
    except Exception as e:
        print(f"[ranked] thread post failed: {e}")
    for did in rids:
        try:
            user = await bot.fetch_user(int(did))
            tag = " (host)" if did == str(t.get("created_by") or "") else ""
            await user.send(f"**ranked session #{tourney_id}** is live{tag} — code: **{code}**. Join in game now.")
        except Exception:
            pass
    print(f"[ranked] session live #{tourney_id} players={len(rids)}")
    from datetime import datetime, timezone
    await asyncio.to_thread(rt_mod.update_tourney, int(tourney_id), {
        "status": "started", "started_at": datetime.now(timezone.utc).isoformat(),
        "thread_id": str(thread.id)})
    fresh = (await asyncio.to_thread(rt_mod.get_tourney, tourney_id)) or t
    await _rt_set_lobby(int(tourney_id),
                        await asyncio.to_thread(rt_mod.live_spec, fresh, str(thread.id)),
                        0x10b981)
    await interaction.followup.send(
        f"**ranked session #{tourney_id}** live with {len(rids)} players.", ephemeral=True)


@ranked_group.command(name="change-code", description="Change the session room code (admin, live only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(tourney_id="Numeric session ID (from create)",
                       code="New 5-char room code")
async def ranked_session_change_code_cmd(interaction: discord.Interaction, tourney_id: int, code: str):
    if not await _defer(interaction):
        return
    if not await _ranked_admin_only(interaction):
        return
    # Sync supabase calls run off the loop: a blocked loop is what starves the
    # next interaction past its defer window (the reported change-code error).
    t = await asyncio.to_thread(rt_mod.get_tourney, tourney_id)
    if not t:
        return await interaction.followup.send("No ranked session with that ID.", ephemeral=True)
    if not _rt_host_or_admin(interaction, t):
        return await interaction.followup.send("Only the session creator (or an admin) can change the code.", ephemeral=True)
    if str(t.get("status")) == "ended":
        return await interaction.followup.send("Session already ended — codes are ignored there.", ephemeral=True)
    # State-gated codes: created sessions take a code silently (start setup),
    # live sessions announce it (embed + roster DMs + @here). Ended is
    # rejected above and never touched.
    live = str(t.get("status")) == "started"
    ok, msg = await asyncio.to_thread(rt_mod.set_code, int(tourney_id), code)
    if not ok:
        return await interaction.followup.send(msg, ephemeral=True)
    await _rt_refresh_lobby(int(tourney_id))
    if live:
        posted, sent = await _rt_broadcast_code(int(tourney_id), cleared=False)
        await interaction.followup.send(
            f"**ranked session #{tourney_id}** — {msg} "
            f"(announced: thread={'yes' if posted else 'no'}, {sent} DM(s), @here pinged).",
            ephemeral=True)
    else:
        await interaction.followup.send(
            f"**ranked session #{tourney_id}** — {msg} (silent: session not live yet).",
            ephemeral=True)


@ranked_group.command(name="backout", description="Clear the room code (admin)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
async def ranked_session_backout_cmd(interaction: discord.Interaction, tourney_id: int):
    if not await _defer(interaction):
        return
    if not await _ranked_admin_only(interaction):
        return
    t = await asyncio.to_thread(rt_mod.get_tourney, tourney_id)
    if not t:
        return await interaction.followup.send("No ranked session with that ID.", ephemeral=True)
    if not _rt_host_or_admin(interaction, t):
        return await interaction.followup.send("Only the session creator (or an admin) can back out.", ephemeral=True)
    if str(t.get("status")) == "ended":
        return await interaction.followup.send("Session already ended.", ephemeral=True)
    if not (t.get("code") or "").strip():
        return await interaction.followup.send("Code already blank.", ephemeral=True)
    ok, msg = await asyncio.to_thread(rt_mod.set_code, int(tourney_id), None)
    if not ok:
        return await interaction.followup.send(msg, ephemeral=True)
    await _rt_refresh_lobby(int(tourney_id))
    posted, sent = await _rt_broadcast_code(int(tourney_id), cleared=True)
    await interaction.followup.send(
        f"**ranked session #{tourney_id}** backed out — code cleared (lobby shows Wait for start). "
        f"(announced: thread={'yes' if posted else 'no'}, {sent} DM(s)).",
        ephemeral=True)


@ranked_group.command(name="players", description="List ranked session players (admin)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
async def ranked_session_players_cmd(interaction: discord.Interaction, tourney_id: int):
    if not await _defer(interaction):
        return
    if not await _ranked_admin_only(interaction):
        return
    t = await asyncio.to_thread(rt_mod.get_tourney, tourney_id)
    if not t:
        return await interaction.followup.send("No ranked session with that ID.", ephemeral=True)
    lines = await _rt_players_lines(int(tourney_id))
    if not lines:
        return await interaction.followup.send("No players yet.", ephemeral=True)
    e = discord.Embed(title=f"Ranked Session #{tourney_id} — players ({len(lines)})",
                      color=0x7c3aed, description="\n".join(lines[:50]))
    await interaction.followup.send(embed=e, ephemeral=True)


@ranked_group.command(name="stop", description="End your ranked session (admin)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
async def ranked_session_stop_cmd(interaction: discord.Interaction, tourney_id: int):
    if not await _defer(interaction):
        return
    if not await _ranked_admin_only(interaction):
        return
    t = await asyncio.to_thread(rt_mod.get_tourney, tourney_id)
    if not t:
        return await interaction.followup.send("No ranked session with that ID.", ephemeral=True)
    if not _rt_host_or_admin(interaction, t):
        return await interaction.followup.send("Only the session creator (or an admin) can stop.", ephemeral=True)
    if str(t.get("status")) == "ended":
        return await interaction.followup.send("Session already ended.", ephemeral=True)
    print(f"[ranked] stop #{tourney_id} by={interaction.user.id}")
    # Settle first: refresh boards (also resolves pending seal bundles),
    # then flip ended. Auto-sealed matches already paid the ladder per seal
    # (the room seal IS the arena pay), so there is no lump payout anymore:
    # only dashboard-owned manual matches settle here (never room-sealed,
    # never ladder-paid). points_paid keeps its exactly-once role for that.
    # A payout failure aborts cleanly and a retry skips the pay but still
    # finalizes.
    pay_line = ""
    try:
        code, _c = await _rt_api("POST", "/api/ranked/calculate",
                                 {"tourney_id": int(tourney_id),
                                  "computed_by": "ranked-stop"})
        if code != 200:
            print(f"[ranked] stop calculate failed #{tourney_id}: {(_c or {}).get('error', code)}")
        code, det = await _rt_api("GET", f"/api/ranked/tourney?id={int(tourney_id)}")
        manual_rows = []
        if code == 200:
            try:
                _mstat = {m.get("version"): m.get("status")
                          for m in ((det or {}).get("matches") or [])}
                for _b in ((det or {}).get("match_boards") or []):
                    if _mstat.get((_b or {}).get("version")) == "manual":
                        manual_rows.extend((_b or {}).get("rows") or [])
            except Exception:
                manual_rows = []
        deltas = rt_mod.standings_deltas(manual_rows)
        if deltas and not await asyncio.to_thread(rt_mod.points_paid, int(tourney_id)):
            try:
                totals = await asyncio.to_thread(rt_mod.pay_ranked_points, deltas)
                await asyncio.to_thread(rt_mod.mark_points_paid, int(tourney_id))
                moved = sum(1 for d in deltas if d in (totals or {}))
                pay_line = (f" Settled **{sum(deltas.values())}** manual-match pts "
                            f"to **{moved}** players (sealed matches already paid per match).")
                print(f"[ranked] stop paid manual #{tourney_id}: {moved} players, {sum(deltas.values())} pts")
            except Exception as exc:
                print(f"[ranked] stop payout failed #{tourney_id}: {exc}")
                return await interaction.followup.send(
                    "Couldn't settle manual-match points (DB error) — session left running, retry **/ranked stop**.",
                    ephemeral=True)
        elif deltas:
            pay_line = " Manual corrections already settled (sealed matches paid per match)."
        else:
            pay_line = " Ladder points were paid per sealed match — no lump payout."
    except Exception as exc:
        print(f"[ranked] stop settle failed #{tourney_id}: {exc}")
        return await interaction.followup.send(
            "Couldn't settle points (error) — session left running, retry **/ranked stop**.",
            ephemeral=True)
    from datetime import datetime, timezone
    t = (await asyncio.to_thread(rt_mod.update_tourney, int(tourney_id), {
        "status": "ended", "ended_at": datetime.now(timezone.utc).isoformat()})) or t
    sealed = await asyncio.to_thread(rt_mod.sealed_count, int(tourney_id))
    await _post_thread(t, embed=discord.Embed(
        title="Session ended", color=0xef4444,
        description=f"**ranked session #{tourney_id}** closed with {sealed} matches sealed.{pay_line}"))
    await _rt_set_lobby(int(tourney_id),
                        await asyncio.to_thread(rt_mod.ended_spec, t), 0xef4444)
    closed = await _close_thread(t)
    tail = " Thread closed and deleted." if closed else " Couldn't delete the thread — check my thread perms."
    try:
        synced = await _rt_sync_roles(t)
        if synced:
            tail += f" {synced} tier role(s) synced."
    except Exception as e:
        print(f"[ranked] stop role sync failed for #{tourney_id}: {e}")
    try:
        rids = await asyncio.to_thread(rt_mod.roster_ids, int(tourney_id))
        renamed = await _ranked_sync_milestone_nicknames(
            {"points": {d: 1 for d in rids}}) if rids else 0
        if renamed:
            tail += f" {renamed} nickname(s) updated."
    except Exception as e:
        print(f"[ranked] stop nicknames failed for #{tourney_id}: {e}")
    await interaction.followup.send(
        f"**ranked session #{tourney_id}** ended ({sealed} matches sealed).{pay_line}{tail}", ephemeral=True)


async def _rt_sync_roles(t: dict) -> int:
    """Sync tier roles for a tourney roster from fresh ranked totals.

    Mirrors _ranked_sync_room_roles with the roster from the tourney regs.
    Returns synced count. Never raises."""
    try:
        guild = None
        try:
            tid = t.get("thread_id")
            if tid:
                th = bot.get_channel(int(tid))
                if th is None:
                    th = await bot.fetch_channel(int(tid))
                guild = getattr(th, "guild", None)
        except Exception:
            pass
        if guild is None:
            try:
                cid = t.get("lobby_channel_id")
                if cid:
                    ch = bot.get_channel(int(cid))
                    if ch is None:
                        ch = await bot.fetch_channel(int(cid))
                    guild = getattr(ch, "guild", None)
            except Exception:
                pass
        if guild is None:
            print(f"[ranked] role sync skipped (no guild) for #{t.get('id')}")
            return 0
        try:
            dids = await asyncio.to_thread(rt_mod.roster_ids, int(t.get("id")))
        except Exception:
            return 0
        if not dids:
            return 0
        try:
            totals = await asyncio.to_thread(ranked_mod.get_points_many, dids)
        except Exception:
            totals = {}
        done = 0
        for did in dids:
            try:
                ms, _fee = ranked_mod.milestone_for(int((totals or {}).get(did, 0)))
            except Exception:
                ms = "Unranked"
            try:
                ok, _why = await _ranked_sync_member_roles(guild, did, ms)
                if ok:
                    done += 1
            except Exception:
                continue
        return done
    except Exception as e:
        print(f"[ranked] role sync failed: {e}")
        return 0


async def _rt_api(method: str, path: str, body: dict | None = None):
    """Call the ranked-tourney API. Returns (status, json). Never raises."""
    try:
        if aiohttp is None:
            return 0, {"error": "no http client"}
        async with aiohttp.ClientSession() as s:
            if (method or "GET").upper() == "POST":
                async with s.post(f"{SERVER_URL}{path}", json=body or {},
                                  timeout=20) as r:
                    try:
                        j = await r.json()
                    except Exception:
                        j = {"error": f"api {r.status}"}
                    return r.status, j
            async with s.get(f"{SERVER_URL}{path}", timeout=20) as r:
                try:
                    j = await r.json()
                except Exception:
                    j = {"error": f"api {r.status}"}
                return r.status, j
    except Exception as e:
        return 0, {"error": str(e)[:100]}


@ranked_group.command(name="open", description="Open a blank match (admin)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
async def ranked_session_open_cmd(interaction: discord.Interaction, tourney_id: int):
    if not await _defer(interaction):
        return
    if not await _ranked_admin_only(interaction):
        return
    t = await asyncio.to_thread(rt_mod.get_tourney, tourney_id)
    if not t:
        return await interaction.followup.send("No ranked session with that ID.", ephemeral=True)
    if not _rt_host_or_admin(interaction, t):
        return await interaction.followup.send("Only the session creator (or an admin) can open matches.", ephemeral=True)
    if str(t.get("status")) == "ended":
        return await interaction.followup.send("Session already ended.", ephemeral=True)
    code, j = await _rt_api("POST", "/api/ranked/match/open", {"tourney_id": int(tourney_id)})
    if code != 200:
        return await interaction.followup.send(
            f"Open failed: {(j or {}).get('error', code)}", ephemeral=True)
    await interaction.followup.send(
        f"Match **v{j.get('version')}** opened for **ranked session #{tourney_id}** — score it on the /ranked dashboard.",
        ephemeral=True)


@ranked_group.command(name="seal", description="Seal the live match (admin)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(tourney_id="Numeric session ID (from 0)")
async def ranked_session_seal_cmd(interaction: discord.Interaction, tourney_id: int):
    if not await _defer(interaction):
        return
    if not await _ranked_admin_only(interaction):
        return
    t = await asyncio.to_thread(rt_mod.get_tourney, tourney_id)
    if not t:
        return await interaction.followup.send("No ranked session with that ID.", ephemeral=True)
    if not _rt_host_or_admin(interaction, t):
        return await interaction.followup.send("Only the session creator (or an admin) can seal.", ephemeral=True)
    code, j = await _rt_api("GET", f"/api/ranked/tourney?id={int(tourney_id)}")
    if code != 200:
        return await interaction.followup.send("Couldn't load matches.", ephemeral=True)
    live = [m for m in ((j or {}).get("matches") or [])
            if str((m or {}).get("status")) in ("open", "manual")]
    if not live:
        return await interaction.followup.send("No live match to seal.", ephemeral=True)
    mid = live[0].get("id")
    code, r = await _rt_api("POST", "/api/ranked/match/seal", {"match_id": mid})
    if code != 200:
        return await interaction.followup.send(
            f"Seal failed: {(r or {}).get('error', code)}", ephemeral=True)
    await interaction.followup.send(
        f"Match sealed for **ranked session #{tourney_id}** — boards refreshed, ladder paid.",
        ephemeral=True)


@ranked_group.command(name="winner", description="Set the match winner (admin)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(tourney_id="Numeric session ID (from 0)",
                       player="Game name or @mention of the winner (blank to clear)")
async def ranked_session_winner_cmd(interaction: discord.Interaction, tourney_id: int, player: str = ""):
    if not await _defer(interaction):
        return
    if not await _ranked_admin_only(interaction):
        return
    t = await asyncio.to_thread(rt_mod.get_tourney, tourney_id)
    if not t:
        return await interaction.followup.send("No ranked session with that ID.", ephemeral=True)
    if not _rt_host_or_admin(interaction, t):
        return await interaction.followup.send("Only the session creator (or an admin) can set the winner.", ephemeral=True)
    code, j = await _rt_api("GET", f"/api/ranked/tourney?id={int(tourney_id)}")
    if code != 200:
        return await interaction.followup.send("Couldn't load matches.", ephemeral=True)
    matches = (j or {}).get("matches") or []
    live = [m for m in matches if str((m or {}).get("status")) in ("open", "manual")]
    target = (live or matches[-1:]) or []
    if not target:
        return await interaction.followup.send("No matches yet.", ephemeral=True)
    mid = target[0].get("id")
    who = None
    raw = (player or "").strip()
    if raw:
        if raw.startswith("<@"):
            try:
                who = str(int("".join(c for c in raw if c.isdigit())))
            except Exception:
                who = None
        else:
            try:
                rids = await asyncio.to_thread(rt_mod.roster_ids, int(tourney_id))
                low = raw.lower()
                names = await asyncio.to_thread(_rt_names_sync, rids)
                for did in rids:
                    nm = str(names.get(str(did)) or "").lower()
                    if nm and (nm == low or low in nm):
                        who = did
                        break
            except Exception:
                pass
        if not who:
            return await interaction.followup.send(
                "Unknown player — @mention them or type their exact game name.", ephemeral=True)
    code, r = await _rt_api("POST", "/api/ranked/match/winner",
                            {"match_id": mid, "winner_discord_id": who})
    if code != 200:
        return await interaction.followup.send(
            f"Winner failed: {(r or {}).get('error', code)}", ephemeral=True)
    await interaction.followup.send(
        f"Winner {'cleared' if not who else 'set'} for **ranked session #{tourney_id}**.", ephemeral=True)




bot.tree.add_command(ranked_group)
bot.tree.add_command(admin_group)


_bot_lock_fh = None


def _claim_bot_lock():
    """Refuse to start when another bot instance holds the lock.

    Two live instances share one token: every slash command double-fires
    (second defer -> 40060, followup races -> 10062). Fail loud instead.
    Returns True when this process owns the lock. Never raises.
    """
    global _bot_lock_fh
    try:
        from pathlib import Path as _P
        lock_path = _P(__file__).resolve().parent / ".bot.lock"
        _bot_lock_fh = open(lock_path, "w")
        try:
            import msvcrt as _ms
            _ms.locking(_bot_lock_fh.fileno(), _ms.LK_NBLCK, 1)
            return True
        except ImportError:
            pass
        try:
            import fcntl as _fc
            _fc.flock(_bot_lock_fh.fileno(), _fc.LOCK_EX | _fc.LOCK_NB)
            return True
        except ImportError:
            pass
        # No file-locking available on this platform — single instance
        # cannot be enforced; let it start (documented risk).
        return True
    except Exception:
        return False


if __name__ == "__main__":
    if not TOKEN:
        print("Set DISCORD_TOKEN env")
        raise SystemExit(1)
    if not _claim_bot_lock():
        print("Another bot instance is already running (lock held) — "
              "not starting a duplicate. Stop the other one first.")
        raise SystemExit(1)
    import asyncio as _aio
    _aio.run(db.init_db())
    bot.run(TOKEN)
