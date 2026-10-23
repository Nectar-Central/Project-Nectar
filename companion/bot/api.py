import time, os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import database as db

async def _dm_lock(discord_id: str, reason: str):
    tok = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
    if not tok or not discord_id: return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.post("https://discord.com/api/v10/users/@me/channels", headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json"}, json={"recipient_id": str(discord_id)}) as r:
                if r.status != 200: return
                ch = await r.json()
            await s.post(f"https://discord.com/api/v10/channels/{ch['id']}/messages", headers={"Authorization": f"Bot {tok}", "Content-Type": "application/json"}, json={"embeds": [{"title": "🔒 Account locked", "color": 0xED4245, "description": f"Your BuildNow account was locked.\n**Reason:** {reason}\n\nUse **/unlock** in the server to unlock it yourself.", "timestamp": __import__('datetime').datetime.utcnow().isoformat()}]} )
    except: pass

app = FastAPI(title="Photon Sniffer API")

class ValidateReq(BaseModel):
    token: str

class PlayerSyncReq(BaseModel):
    token: str
    game_username: Optional[str] = None
    game_id: Optional[str] = None
    skin: Optional[str] = None
    pickaxe: Optional[str] = None

class EventReq(BaseModel):
    token: str
    type: str  # kill|death|win|defeat|knock
    gm: Optional[str] = None

class PlaytimeReq(BaseModel):
    token: str
    gm: str
    seconds: int

@app.post("/api/validate-token")
async def validate_token(req: ValidateReq):
    # enforce one-time login token
    lt = await db.get_login_token(req.token)
    if lt:
        # check expiry
        exp = lt.get("expires_at")
        # supabase returns iso string, sqlite returns int
        try:
            if isinstance(exp, str):
                from datetime import datetime
                exp_ts = datetime.fromisoformat(exp.replace("Z","+00:00")).timestamp()
            else:
                exp_ts = int(exp)
            if exp_ts < time.time():
                raise HTTPException(401, "Token expired")
        except HTTPException: raise
        except: pass
        if lt.get("used"):
            await db.lock_user(lt["discord_id"], "Login token reuse", None)
            await _dm_lock(lt["discord_id"], "Login token reuse -token already used. If this was you, ignore; otherwise your account was locked for security.")
            raise HTTPException(403, "Token already used - account locked. Use /unlock to unlock.")
        await db.use_login_token(req.token, None)
        # ensure user exists and mark first_seen false after first use
        user = await db.get_by_discord(lt["discord_id"])
        if not user:
            await db.upsert_user(lt["discord_id"], f"User_{lt['discord_id']}", req.token)
            user = await db.get_by_token(req.token)
        else:
            # also store token for game events
            await db.update_player_fields(req.token, {})  # touch
        return {"valid": True, "discord_username": user["discord_username"] if user else lt["discord_id"], "discord_id": lt["discord_id"],
                "first_seen": bool((user or {}).get("first_seen", True)), "game_username": (user or {}).get("game_username"), "game_id": (user or {}).get("game_id")}
    # fallback: check long-lived users.token (for backwards compat, but also enforce once via login_tokens if exists)
    user = await db.get_by_token(req.token)
    if not user: raise HTTPException(401, "token incorrect")
    # if this token was ever a login token and is now marked used, reject second login attempt
    # check if there's a login_tokens entry for same token that is now used
    lt2 = await db.get_login_token(req.token)
    if lt2 and lt2.get("used"):
        await db.lock_user(user["discord_id"], "Login token reuse (long-lived)", None)
        await _dm_lock(user["discord_id"], "Login token reuse")
        raise HTTPException(403, "Token already used - account locked. Use /unlock to unlock.")
    if await db.is_locked(user["discord_id"]):
        raise HTTPException(403, "Account locked. Use /unlock to unlock.")
    return {"valid": True, "discord_username": user["discord_username"], "discord_id": user["discord_id"],
            "first_seen": bool(user["first_seen"]), "game_username": user["game_username"], "game_id": user["game_id"]}

@app.post("/api/player-sync")
async def player_sync(req: PlayerSyncReq):
    user = await db.get_by_token(req.token)
    if not user: raise HTTPException(401, "token incorrect")
    fields = {}
    if req.game_username: fields["game_username"] = req.game_username
    if req.game_id: fields["game_id"] = req.game_id
    if req.skin: fields["skin"] = req.skin
    if req.pickaxe: fields["pickaxe"] = req.pickaxe
    if fields: await db.update_player_fields(req.token, fields)
    # return first_seen before update (for welcome logic)
    return {"ok": True, "first_seen": bool(user["first_seen"]), "discord_username": user["discord_username"]}

@app.post("/api/event")
async def event(req: EventReq):
    user = await db.get_by_token(req.token)
    if not user: raise HTTPException(401, "token incorrect")
    t = req.type.lower()
    if t == "kill": await db.inc_stat(req.token, "kills", 1)
    elif t == "death": await db.inc_stat(req.token, "deaths", 1)
    elif t == "win": await db.inc_stat(req.token, "wins", 1)
    elif t == "defeat": await db.inc_stat(req.token, "defeats", 1)
    elif t == "knock": pass  # detected but do nothing
    else: raise HTTPException(400, "unknown type")
    return {"ok": True}

@app.post("/api/playtime")
async def playtime(req: PlaytimeReq):
    user = await db.get_by_token(req.token)
    if not user: raise HTTPException(401, "token incorrect")
    await db.add_playtime(req.token, req.gm or "unknown", req.seconds)
    return {"ok": True}

@app.get("/api/health")
async def health():
    return {"ok": True}
