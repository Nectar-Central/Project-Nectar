import os, secrets, asyncio, discord, json
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
try:
    import aiohttp
except: aiohttp = None
import urllib.request

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional for faster sync
SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:8000")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def gen_token(): return secrets.token_hex(8).upper()  # 16-char hex, e.g. A1B2C3D4E5F6A7B8 (one-time login)

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
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
    except Exception as e: print(f"sync failed {e}")
    print(f"Bot ready as {bot.user} — /register etc")

@bot.tree.command(name="register", description="Get a login token to link BuildNow capture")
async def register_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    token = gen_token()
    for _ in range(5):
        if not await db.get_login_token(token) and not await db.get_by_token(token): break
        token = gen_token()
    row = await db.get_by_discord(str(interaction.user.id))
    if not row:
        await db.upsert_user(str(interaction.user.id), str(interaction.user), token)
    await db.create_login_token(str(interaction.user.id), token, expires_in=300)
    try:
        await interaction.user.send(f"Your BuildNow login token is **{token}**\nEnter it in the userscript prompt (once, 5 min expiry). Keep it secret!")
    except: pass
    await interaction.followup.send(f"Token sent to your DMs: **{token}**\nEnter it in the game iframe prompt on first run. One-time use.", ephemeral=True)

@bot.tree.command(name="token", description="Show your current token")
async def token_cmd(interaction: discord.Interaction):
    row = await db.get_by_discord(str(interaction.user.id))
    if not row: return await interaction.response.send_message("Not registered. Use /register first.", ephemeral=True)
    await interaction.response.send_message(f"Your token: **{row['token']}**", ephemeral=True)

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
    # Prefer Engine live data if available (view.me is authoritative, DB is synced by processor)
    live_me = None
    try:
        sid = await get_active_session(str(interaction.user.id))
        st = await get_session_state(sid) if sid else None
        if st and st.get("me"):
            live_me = st.get("me")
    except: pass
    game_username = (live_me.get("username") if live_me else None) or row.get("game_username") or "-"
    game_id = (live_me.get("game_id") if live_me else None) or row.get("game_id") or "-"
    skin_val = (live_me.get("skin") if live_me else None) or row.get("skin")
    pickaxe_val = (live_me.get("pickaxe") if live_me else None) or row.get("pickaxe") or "-"
    gm_val = (st.get("game_mode") if 'st' in locals() and st else None) or row.get("game_mode") or "-"
    skin_url = f"https://buildnow-api.netlify.app/skins/{skin_val}.png" if skin_val else None
    e = discord.Embed(title="Your BuildNow Link", color=0x7c3aed)
    if skin_url: e.set_thumbnail(url=skin_url)
    e.add_field(name="Discord", value=row["discord_username"])
    e.add_field(name="Token", value=f"`{row['token']}`")
    e.add_field(name="Game username", value=game_username)
    e.add_field(name="Game ID", value=game_id)
    e.add_field(name="Skin", value=f"[{skin_val}]({skin_url})" if skin_url else "-")
    e.add_field(name="Pickaxe", value=pickaxe_val)
    e.add_field(name="Game mode", value=gm_val)
    if live_me:
        e.set_footer(text="Live from Engine • DB synced")
    await interaction.response.send_message(embed=e)

@bot.tree.command(name="stats", description="Show your BuildNow stats")
async def stats_cmd(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    row = await db.get_by_discord(str(target.id))
    if not row: return await interaction.response.send_message(f"No data for {target.mention}. They need /register.")
    import json as _json
    pp = _json.loads(row["playtime_per_gm"] or "{}")
    # Merge live username/skin from Engine if available (processor syncs DB, but engine is fresher)
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
    skin_url = f"https://buildnow-api.netlify.app/skins/{row['skin']}.png" if row.get("skin") else None
    e = discord.Embed(title=f"Stats — {row['discord_username']}", color=0x06b6d4)
    if skin_url: e.set_thumbnail(url=skin_url)
    e.add_field(name="Kills", value=str(row["kills"]))
    e.add_field(name="Deaths", value=str(row["deaths"]))
    e.add_field(name="Wins", value=str(row["wins"]))
    e.add_field(name="Defeats", value=str(row["defeats"]))
    e.add_field(name="Playtime global", value=f"{row['playtime_global']//60}m {row['playtime_global']%60}s")
    per = "\n".join(f"{k}: {v//60}m" for k,v in pp.items()) or "-"
    e.add_field(name="Per mode", value=per, inline=False)
    e.add_field(name="Game username", value=row["game_username"] or "-")
    if row.get("game_mode"):
        e.add_field(name="Game mode", value=row["game_mode"], inline=True)
    await interaction.response.send_message(embed=e)

@bot.tree.command(name="currentmatch", description="Show current match info (mode, players, kills, zone, alive/dead)")
async def currentmatch_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    did = str(interaction.user.id)

    # 1. Find active sessions (try each until engine has state)
    sids = await get_active_sessions(did, limit=5)
    if not sids:
        return await interaction.followup.send("No active session found. Start a game first (link token in userscript).")

    # 2. Fetch live state from engine — try each sid until one works
    state = None
    sid = None
    for cand in sids:
        st = await get_session_state(cand)
        if st:
            state = st
            sid = cand
            break
    if not state:
        # No engine state for any active session — likely server restarted.
        # Show DB fallback with recent match info.
        sid = sids[0]
        row = await db.get_by_discord(did)
        if row:
            # Try to show last match from DB events
            try:
                events = await db.get_match_events(sid, did)
                alive_hint = f"{len(events)} events logged" if events else "No events yet"
            except:
                alive_hint = "No events yet"
            e = discord.Embed(title="Session Active (engine offline)", color=0x2b2d31)
            e.add_field(name="Session", value=f"`{sid[:12]}`", inline=True)
            e.add_field(name="Status", value=alive_hint, inline=True)
            if row.get("skin"):
                skin_url = f"https://buildnow-api.netlify.app/skins/{row['skin']}.png"
                e.set_thumbnail(url=skin_url)
            e.add_field(name="Tip", value="Restart Python server or play a match to repopulate engine. DB data is still available via /matches.", inline=False)
            e.set_footer(text=f"Engine 404 for {sid[:8]} — tried {len(sids)} session(s)")
            return await interaction.followup.send(embed=e)
        return await interaction.followup.send(f"Engine offline and no user data. Tried sessions: {', '.join(s[:8] for s in sids)}")

    # 3. Build embed from engine state (room-scoped)
    me = state.get("me") or {}
    # rooms dict is new structure: Session → rooms[room_id]
    rooms = state.get("rooms") or {}
    current_room_id = state.get("current_room_id")
    # back-compat: top-level proxies to current room
    players = state.get("players") or {}
    state_val = state.get("state", "lobby")
    gm = state.get("game_mode") or "?"
    cur_scn = state.get("cur_scn") or "?"
    zone = state.get("zone") or {}
    players_count = state.get("players_count", 0)
    max_players = state.get("max_players") or "?"
    party = state.get("party") or {}
    # room code from RoomView
    room_code = None
    if rooms and current_room_id and current_room_id in rooms:
        room_code = rooms[current_room_id].get("code") or current_room_id
    elif state.get("room", {}).get("code"):
        room_code = state["room"]["code"]

    gm_map = {"DF":"Free Mode","zw_lg":"Lategame Zone Wars","zw":"Zone Wars","wwzw":"Duo Wild West","WW":"Wild West","BF":"Boxfights","BF2V2":"Boxfights 2v2"}
    gm_label = gm_map.get(gm) or gm_map.get(str(gm).lower()) or gm

    skin = me.get("skin")
    skin_url = f"https://buildnow-api.netlify.app/skins/{skin}.png" if skin else None

    if state_val == "lobby":
        e = discord.Embed(title="In Lobby", color=0x2b2d31)
        if skin_url: e.set_thumbnail(url=skin_url)
        e.add_field(name="Status", value="In Lobby", inline=True)
        e.add_field(name="Game mode", value=gm_label, inline=True)
        e.add_field(name="Scene", value=cur_scn, inline=True)
        if room_code: e.add_field(name="Room", value=f"`{str(room_code)[:8]}`", inline=True)
        if rooms: e.add_field(name="Rooms in session", value=str(len(rooms)), inline=True)
        if skin: e.add_field(name="Skin", value=f"[{skin}]({skin_url})", inline=True)
        if party.get("is_party"): e.add_field(name="Party", value="In Party", inline=True)
        e.set_footer(text=f"Session {sid[:12]} | Room {str(room_code)[:8] if room_code else '?'}")
        return await interaction.followup.send(embed=e)

    # In match
    alive_count = sum(1 for p in players.values() if p.get("is_alive", True))
    dead_count = len(players) - alive_count

    # Player list sorted by alive status
    player_lines = []
    for p in sorted(players.values(), key=lambda x: (not x.get("is_alive", True), x.get("username", ""))):
        name = p.get("username", "?")
        alive = "alive" if p.get("is_alive", True) else "dead"
        is_self = " (you)" if p.get("is_self") else ""
        player_lines.append(f"{name} — {alive}{is_self}")

    kills_lines = "\n".join(player_lines[:12]) or "No data"

    zone_time = zone.get("time")
    zone_phase = zone.get("phase")
    zone_str = f"Phase {zone_phase}" if zone_phase else "—"
    if zone_time is not None:
        zone_str += f" ({zone_time}s)"

    e = discord.Embed(title="Current Match", color=0x7c3aed)
    if skin_url: e.set_thumbnail(url=skin_url)
    e.add_field(name="Game mode", value=gm_label, inline=True)
    e.add_field(name="Players", value=f"{players_count}/{max_players}", inline=True)
    e.add_field(name="Zone", value=zone_str, inline=True)
    e.add_field(name="Alive / Dead", value=f"{alive_count} alive, {dead_count} dead", inline=True)
    e.add_field(name="Scene", value=cur_scn, inline=True)
    if room_code: e.add_field(name="Room", value=f"`{str(room_code)[:8]}`", inline=True)
    if rooms: e.add_field(name="Rooms", value=f"{len(rooms)} in session", inline=True)
    if party.get("is_party"): e.add_field(name="Party", value="In Party", inline=True)
    if skin: e.add_field(name="Skin", value=f"[{skin}]({skin_url})", inline=True)
    e.add_field(name="Players", value=kills_lines[:1024], inline=False)
    e.set_footer(text=f"Live from engine | Session {sid[:12]} | Room {str(room_code)[:8] if room_code else '?'}")
    await interaction.followup.send(embed=e)

async def _players_logic(interaction: discord.Interaction):
    did = str(interaction.user.id)

    # Try engine first
    sid = await get_active_session(did)
    state = await get_session_state(sid) if sid else None

    if state:
        # room-scoped: players are from current room (proxy), but also expose rooms dict
        players = state.get("players") or {}
        me = state.get("me") or {}
        gm = state.get("game_mode") or "?"
        rooms = state.get("rooms") or {}
        current_room_id = state.get("current_room_id")
        room = state.get("room") or {}
        if rooms and current_room_id and current_room_id in rooms:
            room = rooms[current_room_id]
            # use room's gm if top-level empty
            if gm == "?" and room.get("game_mode"):
                gm = room.get("game_mode")

        if not players:
            return await interaction.followup.send("No players in room yet. Join a match first.")

        lines = []
        for p in sorted(players.values(), key=lambda x: (not x.get("is_alive", True), x.get("username", ""))):
            name = p.get("username", "?")
            skin = p.get("skin")
            gid = p.get("game_id")
            alive = "alive" if p.get("is_alive", True) else "dead"
            is_self = " (you)" if p.get("is_self") else ""
            skin_str = f" | [{skin}](https://buildnow-api.netlify.app/skins/{skin}.png)" if skin else ""
            gid_str = f" | `{str(gid)[:6]}`" if gid else ""
            lines.append(f"**{name}**{is_self} — {alive}{skin_str}{gid_str}")

        e = discord.Embed(title=f"Players — {gm}", color=0x7c3aed, description="\n".join(lines)[:4096])
        footer_room = room.get("code") or room.get("room_id") or current_room_id
        if footer_room:
            e.set_footer(text=f"Room {str(footer_room)[:12]} | {len(rooms)} room(s) in session")
        return await interaction.followup.send(embed=e)

    # Fallback: Supabase events (old format)
    players = []
    try:
        if db.USE_SUPABASE and db.supabase:
            r = db.supabase.table("bngc_game_events").select("*").eq("discord_id", did).in_("event_type", ["killfeed"]).order("created_at", desc=True).limit(20).execute()
            seen = set()
            for ev in (r.data or []):
                gd = ev.get("event_data") or {}
                for name in [gd.get("instigator"), gd.get("instigated")]:
                    if name and name not in seen:
                        seen.add(name)
                        players.append(name)
    except Exception as e:
        print(f"[players] fallback failed {e}")

    if not players:
        return await interaction.followup.send("No players found. Start a game first (link token in userscript).")

    embed = discord.Embed(title="Players in room", color=0x7c3aed, description="\n".join(f"{i+1}. {p}" for i,p in enumerate(players[:20])))
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="players", description="List players in current room with skins and alive status")
async def players_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    await _players_logic(interaction)

@bot.tree.command(name="rooms", description="List all rooms in your current session")
async def rooms_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    did = str(interaction.user.id)
    sid = await get_active_session(did)
    state = await get_session_state(sid) if sid else None
    if not state or not state.get("rooms"):
        return await interaction.followup.send("No rooms found. Play a match to create one (CreateRoom/JoinRoom).")
    rooms = state.get("rooms") or {}
    cur = state.get("current_room_id")
    lines = []
    for rid, rv in rooms.items():
        is_cur = " ← current" if rid == cur else ""
        gm = rv.get("game_mode") or "?"
        cnt = rv.get("players_count", 0)
        maxp = rv.get("max_players") or "?"
        state_v = rv.get("state", "?")
        kills = len(rv.get("recent_kills", []))
        code = rv.get("code") or rid
        lines.append(f"`{str(code)[:8]}` {gm} {cnt}/{maxp} {state_v} {kills} kills{is_cur}")
    e = discord.Embed(title=f"Rooms — Session {sid[:12]}", color=0x7c3aed, description="\n".join(lines)[:4096])
    e.set_footer(text=f"{len(rooms)} room(s) | current: {str(cur)[:8] if cur else '?'}")
    await interaction.followup.send(embed=e)

@bot.tree.command(name="list", description="Alias for /players")
async def list_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    await _players_logic(interaction)

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
    skin_url = f"https://buildnow-api.netlify.app/skins/{skin}.png"
    embed = discord.Embed(title=f"Skin — {target.display_name}", color=0x7c3aed)
    embed.set_image(url=skin_url)
    embed.add_field(name="Skin ID", value=f"`{skin}`", inline=True)
    embed.add_field(name="Preview", value=f"[Open]({skin_url})", inline=True)
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
        ts = ""
        if m.get("created_at"):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
                ts = dt.strftime("%b %d %H:%M")
            except Exception:
                ts = m["created_at"][:16]
        gms = ", ".join(m.get("game_modes", [])[:2]) or "?"
        stats = f"K:{m['kills']} D:{m['deaths']} W:{m['wins']} L:{m['defeats']}"
        value = f"`{sid_short}` — {gms}\n{stats}"
        e.add_field(name=f"{i+1}. {ts}", value=value, inline=False)

    e.set_footer(text="Use /replay <matchID> or /killfeed <matchID> with the short ID")
    await interaction.followup.send(embed=e)


@bot.tree.command(name="replay", description="Show a human-readable replay of a match with zone phases and events")
async def replay_cmd(interaction: discord.Interaction, matchid: str):
    await interaction.response.defer(ephemeral=False)
    did = str(interaction.user.id)

    full_sid = await db.get_match_by_short_id(did, matchid)
    if not full_sid:
        return await interaction.followup.send(f"No match found starting with `{matchid}`. Use /matches to see IDs.")

    events = await db.get_match_events(full_sid, did)
    if not events:
        return await interaction.followup.send(f"No events found for match `{full_sid[:12]}`.")

    lines = []
    me_name = None
    # Try to find self name from kill/death events
    for ev in events:
        et = ev.get("event_type")
        gd = ev.get("event_data") or {}
        if et == "kill":
            # We are the instigator
            me_name = me_name or interaction.user.display_name
        elif et == "death":
            # We are the victim — instigator is the killer
            pass

    for ev in events:
        et = ev.get("event_type", "?")
        gd = ev.get("event_data") or {}
        ts = ""
        if ev.get("created_at"):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(ev["created_at"].replace("Z", "+00:00"))
                ts = dt.strftime("%H:%M:%S")
            except Exception:
                ts = ev["created_at"][11:19]

        if et == "killfeed":
            kf_type = gd.get("kill_type", "killed")
            ig = gd.get("instigator", "?")
            ed = gd.get("instigated", "?")
            if kf_type == "killed":
                lines.append(f"`{ts}` {ig} eliminated {ed}")
            elif kf_type == "knocked":
                lines.append(f"`{ts}` {ig} knocked {ed}")
            elif kf_type == "left":
                lines.append(f"`{ts}` {ig} left the match")
        elif et == "zone_change":
            phase = gd.get("phase", "?")
            t = gd.get("time")
            time_str = f" ({t}s)" if t is not None else ""
            lines.append(f"`{ts}` Zone Phase {phase} begins{time_str}")
        elif et == "win":
            gm = gd.get("gm", "")
            lines.append(f"`{ts}` **Victory!** Won the match ({gm})")
        elif et == "defeat":
            gm = gd.get("gm", "")
            lines.append(f"`{ts}` **Defeated.** Eliminated from the match ({gm})")
        elif et == "kill":
            victim = gd.get("victim", "?")
            lines.append(f"`{ts}` You eliminated {victim}")
        elif et == "death":
            killer = gd.get("killer", "?")
            lines.append(f"`{ts}` You were eliminated by {killer}")

    if not lines:
        return await interaction.followup.send(f"No replay data for match `{full_sid[:12]}`.")

    sid_short = full_sid[:12]
    e = discord.Embed(title=f"Match Replay — {sid_short}", color=0x7c3aed)
    text = "\n".join(lines)
    if len(text) <= 1024:
        e.description = text
    else:
        chunks = []
        current = []
        current_len = 0
        for line in lines:
            if current_len + len(line) + 1 > 1000:
                chunks.append("\n".join(current))
                current = [line]
                current_len = len(line)
            else:
                current.append(line)
                current_len += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        for i, chunk in enumerate(chunks[:4]):
            e.add_field(name=f"Part {i+1}" if i > 0 else "Events", value=chunk[:1024], inline=False)

    kills = sum(1 for ev in events if ev.get("event_type") == "kill")
    deaths = sum(1 for ev in events if ev.get("event_type") == "death")
    kf = [ev for ev in events if ev.get("event_type") == "killfeed"]
    zones = sum(1 for ev in events if ev.get("event_type") == "zone_change")
    wins = sum(1 for ev in events if ev.get("event_type") == "win")
    e.set_footer(text=f"{len(kf)} kills in lobby | {zones} zone changes | You: {kills}K {deaths}D {'WIN' if wins else ''}")
    await interaction.followup.send(embed=e)


@bot.tree.command(name="killfeed", description="Show the kill feed from a match (all players)")
async def killfeed_cmd(interaction: discord.Interaction, matchid: str):
    await interaction.response.defer(ephemeral=False)
    did = str(interaction.user.id)

    full_sid = await db.get_match_by_short_id(did, matchid)
    if not full_sid:
        return await interaction.followup.send(f"No match found starting with `{matchid}`. Use /matches to see IDs.")

    events = await db.get_match_events(full_sid, did, event_types=["killfeed"])
    if not events:
        return await interaction.followup.send(f"No killfeed events found for match `{full_sid[:12]}`.")

    lines = []
    for ev in events:
        gd = ev.get("event_data") or {}
        ts = ""
        if ev.get("created_at"):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(ev["created_at"].replace("Z", "+00:00"))
                ts = dt.strftime("%H:%M:%S")
            except Exception:
                ts = ev["created_at"][11:19]

        kf_type = gd.get("kill_type", "killed")
        ig = gd.get("instigator", "?")
        ed = gd.get("instigated")

        if kf_type == "killed" and ed:
            lines.append(f"`{ts}` {ig} killed {ed}")
        elif kf_type == "knocked" and ed:
            lines.append(f"`{ts}` {ig} knocked {ed}")
        elif kf_type == "left":
            lines.append(f"`{ts}` {ig} left the match")

    if not lines:
        return await interaction.followup.send(f"No killfeed for match `{full_sid[:12]}`.")

    sid_short = full_sid[:12]
    e = discord.Embed(title=f"Killfeed — {sid_short}", color=0xdc2626)
    text = "\n".join(lines)
    if len(text) <= 1024:
        e.description = text
    else:
        chunks = []
        current = []
        current_len = 0
        for line in lines:
            if current_len + len(line) + 1 > 1000:
                chunks.append("\n".join(current))
                current = [line]
                current_len = len(line)
            else:
                current.append(line)
                current_len += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        for i, chunk in enumerate(chunks[:4]):
            e.add_field(name=f"Feed {i+1}" if i > 0 else "Feed", value=chunk[:1024], inline=False)

    total_kills = sum(1 for ev in events if (ev.get("event_data") or {}).get("kill_type") == "killed")
    total_knocks = sum(1 for ev in events if (ev.get("event_data") or {}).get("kill_type") == "knocked")
    total_left = sum(1 for ev in events if (ev.get("event_data") or {}).get("kill_type") == "left")
    e.set_footer(text=f"{total_kills} killed | {total_knocks} knocked | {total_left} left")
    await interaction.followup.send(embed=e)

if __name__ == "__main__":
    if not TOKEN:
        print("Set DISCORD_TOKEN env")
        raise SystemExit(1)
    import asyncio as _aio
    _aio.run(db.init_db())
    bot.run(TOKEN)
