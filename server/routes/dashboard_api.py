"""
dashboard_api.py — everything the React dashboard calls.

Design rule: the dashboard is now the only place a tourney is created,
configured or closed. The Discord bot keeps the fast in-chat actions (join,
report, board) but no longer owns the long argument list — that lives here as
a form.

Every route is guild-scoped and permission-checked:
    GET/PATCH  read  -> any staff (owner, admin, moderator)
    POST/PATCH write -> owner or admin only

Auth is the dashboard cookie minted by routes/auth.py. Errors are consistent:
    401 not logged in
    403 not staff for that guild
    404 unknown guild / tourney
    503 database unreachable
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib import guilds as guilds_mod
from ..lib.logger import log_err, log_sys
from ..lib.supabase_client import supabase, db_io, is_transient_error
from .auth import COOKIE_NAME

router = APIRouter()

DEFAULT_POINTS = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _err(msg: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"error": msg, **extra}, status_code=status)


def _db_fail(e: Exception, what: str) -> JSONResponse:
    log_err(f"DASH {what}: {e}")
    if is_transient_error(e):
        return JSONResponse({"error": "temporarily unavailable", "retry": True}, status_code=503)
    return JSONResponse({"error": str(e)[:200]}, status_code=500)


async def _session(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return await guilds_mod.get_session(token)


async def _require(request: Request, guild_id: int, write: bool = False):
    """-> (session, guild_row, error_response_or_None)."""
    session = await _session(request)
    if not session:
        return None, None, _err("not logged in", 401)
    discord_id = str(session.get("discord_id") or "")
    guild = await guilds_mod.get_guild(guild_id)
    if not guild:
        return session, None, _err("unknown server", 404)
    if write:
        if not await guilds_mod.can_manage(guild_id, discord_id):
            return session, guild, _err("you do not have permission to change this server", 403)
    else:
        if not await guilds_mod.is_staff(guild_id, discord_id):
            return session, guild, _err("you are not staff on this server", 403)
    return session, guild, None


async def _sb(fn):
    return await db_io(fn)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _clean_code(raw) -> str | None:
    code = str(raw or "").strip().upper()
    if not code:
        return None
    if len(code) != 5 or not code.isalnum():
        return ""
    return code


def _points_table(raw) -> dict:
    """Validate a points table payload; fall back to defaults per field."""
    out = dict(DEFAULT_POINTS)
    if isinstance(raw, dict):
        out["kill"] = max(0, _int(raw.get("kill"), DEFAULT_POINTS["kill"]))
        out["win"] = max(0, _int(raw.get("win"), DEFAULT_POINTS["win"]))
        place = raw.get("placement")
        if isinstance(place, list) and place:
            try:
                out["placement"] = [max(0, _int(x)) for x in place][:20]
            except Exception:
                pass
    return out


# ---------------------------------------------------------------------------
# Session / identity
# ---------------------------------------------------------------------------

@router.get("/api/dash/me")
async def dash_me(request: Request):
    session = await _session(request)
    if not session:
        return _err("not logged in", 401)
    discord_id = str(session.get("discord_id") or "")
    await guilds_mod.touch_session(request.cookies.get(COOKIE_NAME) or "")
    return {
        "user": {
            "id": discord_id,
            "username": session.get("discord_username"),
            "avatar_url": session.get("avatar_url"),
        },
        "guilds": await guilds_mod.list_user_guilds(discord_id),
    }


# ---------------------------------------------------------------------------
# Guild
# ---------------------------------------------------------------------------

@router.get("/api/dash/guild/{guild_id}")
async def guild_overview(guild_id: int, request: Request):
    session, guild, error = await _require(request, guild_id)
    if error:
        return error
    out = {"guild": guild, "stats": {}, "recent_tourneys": [], "staff": []}
    if not supabase:
        return out
    try:
        tres = await _sb(lambda: supabase.table("bngc_comp_tourneys")
                         .select("id,name,status,mode,series_len,code,guild_id,created_at,started_at,ended_at")
                         .eq("guild_id", guild_id)
                         .order("created_at", desc=True)
                         .limit(10).execute())
        rows = tres.data or []
        out["recent_tourneys"] = rows
        out["stats"]["tourneys_total"] = len(rows)
        out["stats"]["active"] = sum(1 for r in rows if (r or {}).get("status") in ("created", "scheduled", "started"))
        out["stats"]["ended"] = sum(1 for r in rows if (r or {}).get("status") == "ended")
    except Exception as e:
        log_err(f"DASH overview tourneys {guild_id}: {e}")
    try:
        pres = await _sb(lambda: supabase.table("nectar_guild_players")
                         .select("discord_id", count="exact")
                         .eq("guild_id", guild_id).limit(1).execute())
        out["stats"]["players"] = getattr(pres, "count", None) or len(pres.data or [])
    except Exception:
        out["stats"]["players"] = None
    out["staff"] = await guilds_mod.list_admins(guild_id)
    return out


@router.patch("/api/dash/guild/{guild_id}/settings")
async def guild_settings(guild_id: int, request: Request):
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    patch = body.get("settings") if isinstance(body.get("settings"), dict) else body
    if not isinstance(patch, dict):
        return _err("settings object required")
    allowed = {"bot_nickname", "accent_color", "results_channel_id",
               "announce_channel_id", "leaderboard_public", "default_points_table"}
    clean = {k: v for k, v in patch.items() if k in allowed}
    if not clean:
        return _err("nothing to update")
    if "default_points_table" in clean:
        clean["default_points_table"] = _points_table(clean["default_points_table"])
    merged = await guilds_mod.patch_guild_settings(guild_id, clean)
    if merged is None:
        return _err("could not save settings", 500)
    log_sys(f"DASH settings guild={guild_id} by={session.get('discord_id')}")
    return {"ok": True, "settings": merged}


@router.post("/api/dash/guild/{guild_id}/nickname")
async def guild_nickname(guild_id: int, request: Request):
    """Ask the bot to rename itself in this guild on its next sync.

    The bot polls this flag on startup and on a timer, so the dashboard never
    needs a Discord token.
    """
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    nick = str(body.get("nickname") or "").strip()[:32]
    merged = await guilds_mod.patch_guild_settings(guild_id, {"bot_nickname": nick})
    if merged is None:
        return _err("could not save nickname", 500)
    return {"ok": True, "bot_nickname": nick}


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------

@router.get("/api/dash/guild/{guild_id}/admins")
async def list_admins(guild_id: int, request: Request):
    session, guild, error = await _require(request, guild_id)
    if error:
        return error
    return {"admins": await guilds_mod.list_admins(guild_id)}


@router.post("/api/dash/guild/{guild_id}/admins")
async def add_admin(guild_id: int, request: Request):
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    user_id = str(body.get("user_id") or "").strip()
    if not user_id.isdigit():
        return _err("user_id must be a Discord ID")
    role = str(body.get("role") or "admin").lower()
    if role not in ("admin", "moderator"):
        # Owner is set at registration and never granted through the UI.
        return _err("role must be admin or moderator")
    ok = await guilds_mod.add_admin(guild_id, user_id, role, added_by=str(session.get("discord_id") or ""))
    if not ok:
        return _err("could not add admin", 500)
    log_sys(f"DASH admin+ guild={guild_id} user={user_id} role={role}")
    return {"ok": True, "admins": await guilds_mod.list_admins(guild_id)}


@router.delete("/api/dash/guild/{guild_id}/admins/{user_id}")
async def remove_admin(guild_id: int, user_id: str, request: Request):
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    ok = await guilds_mod.remove_admin(guild_id, user_id)
    if not ok:
        return _err("cannot remove (owner is permanent or user not found)")
    log_sys(f"DASH admin- guild={guild_id} user={user_id}")
    return {"ok": True, "admins": await guilds_mod.list_admins(guild_id)}


# ---------------------------------------------------------------------------
# Tourneys — list / create
# ---------------------------------------------------------------------------

@router.get("/api/dash/guild/{guild_id}/tourneys")
async def list_tourneys(guild_id: int, request: Request):
    session, guild, error = await _require(request, guild_id)
    if error:
        return error
    if not supabase:
        return {"tourneys": []}
    try:
        res = await _sb(lambda: supabase.table("bngc_comp_tourneys")
                        .select("*")
                        .eq("guild_id", guild_id)
                        .order("created_at", desc=True)
                        .limit(200).execute())
        return {"tourneys": res.data or []}
    except Exception as e:
        return _db_fail(e, f"list tourneys {guild_id}")


@router.post("/api/dash/guild/{guild_id}/tourneys")
async def create_tourney(guild_id: int, request: Request):
    """Create a tourney. This replaces the `/tourney-create` wall of options."""
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    if not supabase:
        return _err("database not configured", 503)
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")

    name = str(body.get("name") or "").strip()[:64]
    if len(name) < 2:
        return _err("name is required (2-64 characters)")

    mode = str(body.get("mode") or "solo").lower()
    if mode not in ("solo", "duo"):
        return _err("mode must be solo or duo")

    code = _clean_code(body.get("code"))
    if code == "":
        return _err("code must be 5 letters/digits (or leave it empty)")

    max_players = max(0, _int(body.get("max_players")))
    if mode == "duo" and max_players and max_players % 2:
        return _err("duo tourneys need an even player limit")

    settings = guild.get("settings") or {}
    default_table = settings.get("default_points_table") or DEFAULT_POINTS
    points_table = _points_table(body.get("points_table") or default_table)

    row = {
        "guild_id": guild_id,
        "name": name,
        "description": str(body.get("description") or "").strip()[:500] or None,
        "code": code,
        "mode": mode,
        "status": "created",
        "series_len": max(0, _int(body.get("series_len"))),
        "game_mode": str(body.get("game_mode") or "").strip() or None,
        "region": str(body.get("region") or "EU").strip()[:16] or "EU",
        "shoot_timer": str(body.get("shoot_timer") or "4:30").strip()[:16] or "4:30",
        "rules_channel_id": str(body.get("rules_channel_id") or "").strip() or None,
        "max_players": max_players,
        "host_playing": bool(body.get("host_playing", True)),
        "pr_enabled": bool(body.get("pr_enabled", True)),
        "registration_open": bool(body.get("registration_open", True)),
        "points_table": points_table,
        "created_by": str(session.get("discord_id") or ""),
        "hosts": [str(session.get("discord_id") or "")],
    }
    scheduled = str(body.get("scheduled_at") or "").strip()
    if scheduled:
        try:
            row["scheduled_at"] = datetime.fromisoformat(
                scheduled.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        except Exception:
            return _err("scheduled_at must be an ISO timestamp")

    try:
        res = await _sb(lambda: supabase.table("bngc_comp_tourneys").insert(row).execute())
        created = (res.data or [None])[0]
    except Exception as e:
        return _db_fail(e, f"create tourney {guild_id}")

    if not created:
        return _err("could not create tourney", 500)

    # Tag the room code so presence + the host bridge know which tourney is
    # live the moment it starts (same behaviour the bot had).
    if code:
        try:
            await _sb(lambda: supabase.table("bngc_room_tags").upsert({
                "code": code,
                "kind": "tourney",
                "name": name,
                "guild_id": guild_id,
                "created_by": str(session.get("discord_id") or ""),
            }, on_conflict="code").execute())
        except Exception as e:
            log_err(f"DASH create tourney tag {code}: {e}")

    log_sys(f"DASH tourney+ guild={guild_id} id={created.get('id')} name={name} mode={mode}")
    return {"ok": True, "tourney": created}


# ---------------------------------------------------------------------------
# Tourneys — detail / update / lifecycle
# ---------------------------------------------------------------------------

async def _tourney_of_guild(guild_id: int, tourney_id: int) -> dict | None:
    if not supabase:
        return None
    try:
        res = await _sb(lambda: supabase.table("bngc_comp_tourneys")
                        .select("*")
                        .eq("id", int(tourney_id))
                        .eq("guild_id", guild_id)
                        .limit(1).execute())
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        return None


async def _standings_for(tourney: dict) -> list[dict]:
    """Final board for one tourney — the one implementation, via routes/comp.py.

    Shared by the detail view and the close handler on purpose: the numbers a
    host sees on screen are the numbers that get banked into the server board.
    """
    from .comp import load_standings
    return await load_standings(tourney)


@router.get("/api/dash/guild/{guild_id}/tourneys/{tourney_id}")
async def tourney_detail(guild_id: int, tourney_id: int, request: Request):
    session, guild, error = await _require(request, guild_id)
    if error:
        return error
    tourney = await _tourney_of_guild(guild_id, tourney_id)
    if not tourney:
        return _err("tourney not found", 404)
    out = {"tourney": tourney, "registrations": [], "matches": [], "standings": [], "pr": []}
    try:
        rres = await _sb(lambda: supabase.table("bngc_comp_registrations")
                         .select("*").eq("tourney_id", tourney_id)
                         .order("created_at").limit(1000).execute())
        out["registrations"] = rres.data or []
    except Exception as e:
        log_err(f"DASH regs {tourney_id}: {e}")
    try:
        mres = await _sb(lambda: supabase.table("bngc_comp_matches")
                         .select("id,version,status,room_id,started_at,sealed_at,winner_discord_id,placements,point_awards")
                         .eq("tourney_id", tourney_id)
                         .order("version").limit(500).execute())
        out["matches"] = mres.data or []
    except Exception as e:
        log_err(f"DASH matches {tourney_id}: {e}")
    # Standings + PR reuse the one implementation in routes/comp.py so the
    # dashboard can never disagree with the bot or the HUD.
    try:
        from .comp import derive_status
        out["status"] = derive_status(tourney, len(out["matches"]))
    except Exception:
        pass
    out["standings"] = await _standings_for(tourney)
    try:
        pres = await _sb(lambda: supabase.table("bngc_comp_pr_history")
                         .select("discord_id,pr,kills,wins,matches,avg_placement,computed_at")
                         .eq("tourney_id", tourney_id)
                         .order("pr", desc=True).limit(500).execute())
        out["pr"] = pres.data or []
    except Exception:
        pass
    return out


@router.patch("/api/dash/guild/{guild_id}/tourneys/{tourney_id}")
async def update_tourney(guild_id: int, tourney_id: int, request: Request):
    """Edit a tourney that has not ended yet."""
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    tourney = await _tourney_of_guild(guild_id, tourney_id)
    if not tourney:
        return _err("tourney not found", 404)
    if str(tourney.get("status")) == "ended":
        return _err("this tourney has ended and can no longer be edited")
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")

    patch: dict = {}
    if "name" in body:
        name = str(body.get("name") or "").strip()[:64]
        if len(name) < 2:
            return _err("name must be at least 2 characters")
        patch["name"] = name
    if "description" in body:
        patch["description"] = str(body.get("description") or "").strip()[:500] or None
    if "mode" in body:
        mode = str(body.get("mode") or "").lower()
        if mode not in ("solo", "duo"):
            return _err("mode must be solo or duo")
        patch["mode"] = mode
    if "series_len" in body:
        patch["series_len"] = max(0, _int(body.get("series_len")))
    if "game_mode" in body:
        patch["game_mode"] = str(body.get("game_mode") or "").strip() or None
    if "region" in body:
        patch["region"] = str(body.get("region") or "EU").strip()[:16] or "EU"
    if "shoot_timer" in body:
        patch["shoot_timer"] = str(body.get("shoot_timer") or "4:30").strip()[:16] or "4:30"
    if "max_players" in body:
        patch["max_players"] = max(0, _int(body.get("max_players")))
    if "host_playing" in body:
        patch["host_playing"] = bool(body.get("host_playing"))
    if "pr_enabled" in body:
        patch["pr_enabled"] = bool(body.get("pr_enabled"))
    if "registration_open" in body:
        patch["registration_open"] = bool(body.get("registration_open"))
    if "points_table" in body:
        patch["points_table"] = _points_table(body.get("points_table"))
    if "code" in body:
        code = _clean_code(body.get("code"))
        if code == "":
            return _err("code must be 5 letters/digits (or empty)")
        patch["code"] = code
    if "scheduled_at" in body:
        raw = str(body.get("scheduled_at") or "").strip()
        if not raw:
            patch["scheduled_at"] = None
        else:
            try:
                patch["scheduled_at"] = datetime.fromisoformat(
                    raw.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            except Exception:
                return _err("scheduled_at must be an ISO timestamp")

    if not patch:
        return _err("nothing to update")
    patch["updated_at"] = _iso()

    try:
        res = await _sb(lambda: supabase.table("bngc_comp_tourneys")
                        .update(patch).eq("id", int(tourney_id))
                        .eq("guild_id", guild_id).execute())
    except Exception as e:
        return _db_fail(e, f"update tourney {tourney_id}")

    # A changed code must move the room tag too, or presence keeps pointing
    # at the old room.
    if "code" in patch:
        old_code = str(tourney.get("code") or "").upper()
        new_code = patch["code"]
        try:
            if old_code and old_code != new_code:
                await _sb(lambda: supabase.table("bngc_room_tags")
                          .delete().eq("code", old_code).execute())
            if new_code:
                await _sb(lambda: supabase.table("bngc_room_tags").upsert({
                    "code": new_code,
                    "kind": "tourney",
                    "name": patch.get("name") or tourney.get("name") or "Tourney",
                    "guild_id": guild_id,
                    "created_by": str(session.get("discord_id") or ""),
                }, on_conflict="code").execute())
        except Exception as e:
            log_err(f"DASH retag {new_code}: {e}")

    updated = (res.data or [None])[0] if 'res' in dir() else None
    log_sys(f"DASH tourney~ guild={guild_id} id={tourney_id} fields={list(patch)}")
    return {"ok": True, "tourney": updated or {**tourney, **patch}}


@router.post("/api/dash/guild/{guild_id}/tourneys/{tourney_id}/start")
async def start_tourney(guild_id: int, tourney_id: int, request: Request):
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    tourney = await _tourney_of_guild(guild_id, tourney_id)
    if not tourney:
        return _err("tourney not found", 404)
    if str(tourney.get("status")) not in ("created", "scheduled", "started"):
        return _err("this tourney cannot be started from its current status")
    try:
        await _sb(lambda: supabase.table("bngc_comp_tourneys").update({
            "status": "started",
            "registration_open": False,
            "started_at": tourney.get("started_at") or _iso(),
            "updated_at": _iso(),
        }).eq("id", int(tourney_id)).eq("guild_id", guild_id).execute())
    except Exception as e:
        return _db_fail(e, f"start tourney {tourney_id}")
    log_sys(f"DASH tourney> guild={guild_id} id={tourney_id} started")
    return {"ok": True, "status": "started"}


@router.post("/api/dash/guild/{guild_id}/tourneys/{tourney_id}/stop")
async def stop_tourney(guild_id: int, tourney_id: int, request: Request):
    """Close the tourney and compute PR when the tourney has it enabled."""
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    tourney = await _tourney_of_guild(guild_id, tourney_id)
    if not tourney:
        return _err("tourney not found", 404)
    try:
        await _sb(lambda: supabase.table("bngc_comp_tourneys").update({
            "status": "ended",
            "registration_open": False,
            "ended_at": _iso(),
            "updated_at": _iso(),
        }).eq("id", int(tourney_id)).eq("guild_id", guild_id).execute())
    except Exception as e:
        return _db_fail(e, f"stop tourney {tourney_id}")

    code = str(tourney.get("code") or "").upper()
    if code:
        try:
            await _sb(lambda: supabase.table("bngc_room_tags").delete().eq("code", code).execute())
        except Exception as e:
            log_err(f"DASH stop untag {code}: {e}")

    pr_note = "disabled"
    pr_board: list[dict] = []
    if tourney.get("pr_enabled") is not False:
        try:
            # Same code path as POST /api/comp/tourney/pr — one implementation,
            # so the dashboard PR can never differ from the bot's.
            from .comp import compute_and_store_tourney_pr_sync
            pr_board = await _sb(lambda: compute_and_store_tourney_pr_sync(int(tourney_id))) or []
            pr_note = f"calculated ({len(pr_board)} rated)" if pr_board else "no sealed matches"
        except Exception as e:
            log_err(f"DASH stop PR {tourney_id}: {e}")
            pr_note = "skipped"

    # Bank the result into this server's own scoreboard. Idempotent: a second
    # close is a no-op, so nobody's totals can be counted twice.
    from .comp import bank_tourney
    applied = await bank_tourney(guild_id, tourney)

    log_sys(f"DASH tourney< guild={guild_id} id={tourney_id} ended "
            f"pr={pr_note} players={applied}")
    return {"ok": True, "status": "ended", "pr": pr_note, "players_updated": applied}


@router.post("/api/dash/guild/{guild_id}/tourneys/{tourney_id}/bank")
async def bank_tourney_endpoint(guild_id: int, tourney_id: int, request: Request):
    """Re-bank an ended tourney's results into the server leaderboard.

    Exists because a tournament can be closed from Discord, or a close can be
    interrupted. Banking is idempotent, so pressing this twice is harmless —
    it only ever fills in what is missing.
    """
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    tourney = await _tourney_of_guild(guild_id, tourney_id)
    if not tourney:
        return _err("tourney not found", 404)
    if str(tourney.get("status")) != "ended":
        return _err("close the tournament before banking its results")
    from .comp import bank_tourney
    try:
        applied = await bank_tourney(guild_id, tourney)
    except Exception as e:
        return _db_fail(e, f"bank tourney {tourney_id}")
    log_sys(f"DASH bank guild={guild_id} id={tourney_id} players={applied}")
    return {"ok": True, "players_updated": applied}


@router.delete("/api/dash/guild/{guild_id}/tourneys/{tourney_id}")
async def delete_tourney(guild_id: int, tourney_id: int, request: Request):
    """Hard-delete a tourney and everything under it (FK cascade).

    Only allowed while it is still 'created' — deleting a played tourney
    would silently rewrite history and any PR it already produced.
    """
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    tourney = await _tourney_of_guild(guild_id, tourney_id)
    if not tourney:
        return _err("tourney not found", 404)
    if str(tourney.get("status")) != "created":
        return _err("only an unstarted tourney can be deleted — end it instead")
    try:
        await _sb(lambda: supabase.table("bngc_comp_tourneys")
                  .delete().eq("id", int(tourney_id)).eq("guild_id", guild_id).execute())
    except Exception as e:
        return _db_fail(e, f"delete tourney {tourney_id}")
    code = str(tourney.get("code") or "").upper()
    if code:
        try:
            await _sb(lambda: supabase.table("bngc_room_tags").delete().eq("code", code).execute())
        except Exception:
            pass
    log_sys(f"DASH tourney- guild={guild_id} id={tourney_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Registrations
# ---------------------------------------------------------------------------

@router.post("/api/dash/guild/{guild_id}/tourneys/{tourney_id}/registrations")
async def add_registration(guild_id: int, tourney_id: int, request: Request):
    """Add a player or duo straight from the dashboard (no Discord button)."""
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    tourney = await _tourney_of_guild(guild_id, tourney_id)
    if not tourney:
        return _err("tourney not found", 404)
    if not tourney.get("registration_open") and str(tourney.get("status")) == "ended":
        return _err("registration is closed")
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")

    members = [str(x).strip() for x in (body.get("member_ids") or []) if str(x).strip()]
    if not members:
        single = str(body.get("discord_id") or "").strip()
        if single:
            members = [single]
    members = [m for m in members if m.isdigit()]
    if not members:
        return _err("discord_id or member_ids required")
    if tourney.get("mode") == "solo" and len(members) > 1:
        return _err("solo tourneys take one player per signup")
    if len(members) > 2:
        return _err("a team is at most two players")

    max_players = _int(tourney.get("max_players"))
    if max_players:
        try:
            cres = await _sb(lambda: supabase.table("bngc_comp_registrations")
                             .select("member_ids").eq("tourney_id", tourney_id)
                             .eq("status", "accepted").execute())
            taken = sum(len((r or {}).get("member_ids") or []) for r in (cres.data or []))
            if taken + len(members) > max_players:
                return _err(f"only {max(0, max_players - taken)} slot(s) left", 409)
        except Exception:
            pass

    row = {
        "tourney_id": tourney_id,
        "team_label": str(body.get("team_label") or "")[:40] or None,
        "member_ids": members,
        "captain_id": members[0],
        "status": "accepted" if body.get("accept", True) else "pending",
        "team_color": str(body.get("team_color") or "")[:16] or None,
    }
    try:
        res = await _sb(lambda: supabase.table("bngc_comp_registrations").insert(row).execute())
    except Exception as e:
        return _db_fail(e, f"add registration {tourney_id}")

    for did in members:
        await guilds_mod.ensure_guild_player(guild_id, did)
    return {"ok": True, "registration": (res.data or [None])[0]}


@router.patch("/api/dash/guild/{guild_id}/tourneys/{tourney_id}/registrations/{reg_id}")
async def set_registration_status(guild_id: int, tourney_id: int, reg_id: int,
                                  request: Request):
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    if not await _tourney_of_guild(guild_id, tourney_id):
        return _err("tourney not found", 404)
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    status = str(body.get("status") or "").lower()
    if status not in ("pending", "accepted", "rejected", "banned"):
        return _err("status must be pending, accepted, rejected or banned")
    try:
        await _sb(lambda: supabase.table("bngc_comp_registrations")
                  .update({"status": status})
                  .eq("id", int(reg_id))
                  .eq("tourney_id", tourney_id).execute())
    except Exception as e:
        return _db_fail(e, f"reg status {reg_id}")
    return {"ok": True, "status": status}


@router.delete("/api/dash/guild/{guild_id}/tourneys/{tourney_id}/registrations/{reg_id}")
async def delete_registration(guild_id: int, tourney_id: int, reg_id: int, request: Request):
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    if not await _tourney_of_guild(guild_id, tourney_id):
        return _err("tourney not found", 404)
    try:
        await _sb(lambda: supabase.table("bngc_comp_registrations")
                  .delete().eq("id", int(reg_id)).eq("tourney_id", tourney_id).execute())
    except Exception as e:
        return _db_fail(e, f"reg delete {reg_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Leaderboard / players
# ---------------------------------------------------------------------------

@router.get("/api/dash/guild/{guild_id}/leaderboard")
async def leaderboard(guild_id: int, request: Request, limit: int = 50):
    session, guild, error = await _require(request, guild_id)
    if error:
        return error
    rows = await guilds_mod.list_guild_players(guild_id, limit=limit)
    return {"players": rows, "points_table": (guild.get("settings") or {}).get("default_points_table")}


@router.get("/api/dash/guild/{guild_id}/players")
async def players(guild_id: int, request: Request, limit: int = 500, q: str | None = None):
    session, guild, error = await _require(request, guild_id)
    if error:
        return error
    return {"players": await guilds_mod.list_guild_players(guild_id, limit=limit, search=q)}


@router.patch("/api/dash/guild/{guild_id}/players/{discord_id}")
async def update_player(guild_id: int, discord_id: str, request: Request):
    session, guild, error = await _require(request, guild_id, write=True)
    if error:
        return error
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json")
    patch: dict = {}
    if "is_banned" in body:
        patch["is_banned"] = bool(body.get("is_banned"))
        patch["ban_reason"] = str(body.get("ban_reason") or "")[:200] or None
    if "notes" in body:
        patch["notes"] = str(body.get("notes") or "")[:500] or None
    if "game_username" in body:
        patch["game_username"] = str(body.get("game_username") or "")[:32] or None
    if "pr" in body:
        patch["pr"] = max(0, _int(body.get("pr"), 1000))
    if not patch:
        return _err("nothing to update")
    ok = await guilds_mod.update_guild_player(guild_id, discord_id, patch)
    if not ok:
        return _err("could not update player", 500)
    return {"ok": True, "player": await guilds_mod.get_guild_player(guild_id, discord_id)}
