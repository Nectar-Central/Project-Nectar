"""
stats.py — GET /api/stats
Returns live user stats + session state for the drawer overlay.

Query:
  ?session_id=xxx  -> resolve discord_id via bngc_sessions, return bngc_users row + live session view
  ?discord_id=xxx  -> direct lookup (fallback)

Response:
  {
    "discord_username": "...", "discord_id": "...",
    "game_username": "...", "game_id": "...", "skin": "...", "pickaxe": "...",
    "kills": 12, "deaths": 5, "wins": 3,
    "playtime_global": 1234, "playtime_per_gm": {...},
    "kd": 2.4,
    "rank": {"by": "kills", "pct": 87, "rank": 5, "total": 120},
    "is_dev": false,
    "arena": {
      "points": 750, "milestone": "Silver",
      "milestone_icon_url": "...", "milestone_color": "#C0C0C0",
      "points_to_next": 250, "next_milestone": "Gold", "next_points_pct": 50,
      "season": 1, "reset_at": "2026-10-01T00:00:00+00:00" | null
    },
    "session": { "game_mode": "zw_lg", "cur_scn": "Game", "players_count": 4, "zone": {...} } | null,
    "ranked": {
      "active": true, "rid": "R-ABC123", "short_code": "PBS05", "status": "live",
      "is_host": false, "points": 750, "milestone": "Gold",
      "milestone_icon_url": "...", "milestone_color": "#FFD700", "arena_icon_url": "...",
      "points_to_next": 250, "next_milestone": "Diamond", "next_points_pct": 50,
      "warnings": [], "match_id": "R-ABC123-2", "match_version": 2,
      "last_result": {"match_version": 2, "gained": 85, "won": true, "placement": 1, "kills": 1} | null,
      "host_game_username": "True Jew", "host_skin": "cct_0_default", "host_playing": true,
      "min_points": 0, "max_points": null, "fee": 0
    } | null
  }
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import time as _time

from ..lib.supabase_client import supabase, db, db_io, is_transient_error
from ..lib.redis_client import cache
from ..lib.logger import log_sys, log_ranked, log_dbg, log_err
from ..lib import ranked as ranked_mod
from .capture import engine as capture_engine

router = APIRouter()

# Last logged ranked verdict per discord_id: (sig, ts) for log sampling.
_stats_ranked_sig: dict = {}


def better_than_pct(total: int, above: int) -> int:
    """Percentile for the 'better than X% of players' drawer line.

    rank = 1 + players strictly above you (ties share the best rank);
    pct = % of the pool you beat. Returns 0 when the pool is meaningless.
    """
    try:
        total, above = int(total or 0), int(above or 0)
    except Exception:
        return 0
    if total < 2 or above < 0 or above >= total:
        return 0
    return round((total - 1 - above) / total * 100)


_SESSION_STALE_S = 120  # no packets this long = kicked/closed/reloaded

_LOBBY_BLIP_RESCUE_S = 20  # lone Authenticate parks current on the lobby
# pseudo-room while the game keeps capturing in the real room — the rescue
# covers that blip (seconds). A real leave also parks on lobby, but then no
# new packets arrive for the old room, so it ages out of this window fast
# instead of haunting stats (with its ranked BLOCK) for the full 120s.


def _session_fresh(live_session) -> bool:
    """False when the session stopped capturing (kick/close/reload).

    Without this the drawer keeps showing the last cached room — including
    the ranked block with its match ID — for up to the cache TTL. Prefers
    the current room's last_packet_at (stamped per engine feed), falls back
    to the persist-time _cached_at. No timestamps at all (old caches) fails
    open. Never raises (fail-open True).
    """
    try:
        import time as _t
        now = _t.time()
        rooms = (live_session or {}).get("rooms") or {}
        cur = None
        if isinstance(rooms, dict):
            cur = rooms.get((live_session or {}).get("current_room_id"))
            if not cur and rooms:
                cur = next(reversed(list(rooms.values())), None)
        if isinstance(cur, dict):
            try:
                lp = cur.get("last_packet_at")
                if lp:
                    return (now - float(lp)) <= _SESSION_STALE_S
            except Exception:
                pass
        try:
            ca = (live_session or {}).get("_cached_at")
            if ca:
                return (now - float(ca)) <= _SESSION_STALE_S
        except Exception:
            pass
        return True
    except Exception:
        return True


@router.get("/api/stats")
async def get_stats(request: Request):
    qp = request.query_params
    session_id = qp.get("session_id") or qp.get("sid")
    discord_id = qp.get("discord_id") or qp.get("did")
    # Allow POST body fallback
    if not session_id and not discord_id:
        try:
            body = await request.json()
            session_id = body.get("session_id") or body.get("sessionId")
            discord_id = body.get("discord_id")
        except Exception:
            pass

    # Resolve discord_id via session if session_id given
    live_session = None
    if session_id:
        # Try live engine first
        try:
            view = capture_engine.get_session(session_id)
            if view:
                live_session = view.to_dict()
                if not discord_id and view.me:
                    # will resolve below via DB session row
                    pass
        except Exception:
            pass
        if not live_session:
            try:
                cached = await cache.get_session_state(session_id)
                if cached:
                    live_session = cached
            except Exception:
                pass
        # Dead sessions (kick/close/reload stops captures): never serve them,
        # or the drawer shows a ghost room + stale ranked block for an hour.
        # Preserve the short_code before discarding so ranked lookup still works
        # when the capture stopped but the user is still in the ranked room.
        _stale_short_code = None
        if live_session and not _session_fresh(live_session):
            try:
                _rooms = live_session.get("rooms") or {}
                _cur_id = live_session.get("current_room_id")
                _cur = _rooms.get(_cur_id) if _cur_id and _cur_id in _rooms else None
                if not _cur and _rooms:
                    _cur = next(reversed(list(_rooms.values())), None)
                if _cur:
                    _stale_short_code = _cur.get("short_code")
            except Exception:
                pass
            live_session = None
        # Resolve discord_id from DB session row if not already
        if not discord_id:
            try:
                sres = await db_io(db.get_session_sync, session_id)
                sdata = sres.get("data") if isinstance(sres, dict) else None
                if sdata and sdata.get("discord_id"):
                    discord_id = sdata["discord_id"]
            except Exception:
                pass
        # Also try discord_id header from userscript auth
        if not discord_id:
            discord_id = qp.get("discord_id") or request.headers.get("x-discord-id")

    if not discord_id:
        return JSONResponse({"error": "discord_id or session_id required"}, status_code=400)

    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)

    try:
        res = await db_io(lambda: supabase.table("bngc_users").select("*").eq("discord_id", discord_id).single().execute())
        user = res.data
    except Exception as e:
        if "PGRST116" in str(e):
            return JSONResponse({"error": "user not found"}, status_code=404)
        if is_transient_error(e):
            try:
                log_err(f"STATS transient for {str(discord_id)[:8]} ({str(e)[:60]})")
            except Exception:
                pass
            return JSONResponse({"error": "temporarily unavailable", "retry": True}, status_code=503)
        return JSONResponse({"error": str(e)[:160]}, status_code=500)

    if not user:
        return JSONResponse({"error": "user not found"}, status_code=404)

    kills = int(user.get("kills") or 0)
    deaths = int(user.get("deaths") or 0)
    wins = int(user.get("wins") or 0)
    kd = round(kills / deaths, 2) if deaths else (float(kills) if kills else 0)

    # live session fallback: if no live view but user has game_mode hints, leave null
    session_out = None
    cur_room = None  # current room dict (for ranked match_version/warnings)
    if live_session:
        # live_session is SessionView dict — extract current room fields
        try:
            cur_room = None
            rooms = live_session.get("rooms") or {}
            cur_id = live_session.get("current_room_id")
            if cur_id and cur_id in rooms:
                cur_room = rooms[cur_id]
            elif rooms:
                # take most recent
                cur_room = next(reversed(list(rooms.values())), None)
            # Lobby-blip rescue: a lone Authenticate (or self-leave echo) parks
            # current on the lobby pseudo-room while the game keeps capturing
            # in a real room — no join follows, the player never actually left.
            # If current is the lobby pseudo-room but another room has packets
            # within _LOBBY_BLIP_RESCUE_S, serve it. Deliberately much shorter
            # than _SESSION_STALE_S: a real leave freezes the old room's
            # last_packet_at at leave time, so it drops out of the rescue
            # within seconds instead of re-serving its ranked BLOCK for 2min
            # while the player sits in no room.
            try:
                if cur_room and cur_room.get("room_id") == "lobby":
                    import time as _t2
                    now2 = _t2.time()
                    best = None
                    best_ts = 0.0
                    for r in (rooms or {}).values():
                        try:
                            if not isinstance(r, dict) or r.get("room_id") == "lobby":
                                continue
                            lp = float(r.get("last_packet_at") or 0)
                            if lp > best_ts:
                                best_ts = lp
                                best = r
                        except Exception:
                            continue
                    if best is not None and (now2 - best_ts) <= _LOBBY_BLIP_RESCUE_S:
                        cur_room = best
                        try:
                            log_dbg(f"[stats] lobby-blip rescue -> room {(best.get('short_code') or best.get('room_id'))} "
                                    f"fresh={now2 - best_ts:.0f}s")
                        except Exception:
                            pass
            except Exception:
                pass
            if cur_room:
                session_out = {
                    "game_mode": (cur_room.get("game_mode") or live_session.get("game_mode")),
                    "cur_scn": cur_room.get("cur_scn") or live_session.get("cur_scn"),
                    "players_count": cur_room.get("players_count") or live_session.get("players_count"),
                    "max_players": cur_room.get("max_players"),
                    "zone": cur_room.get("zone"),
                    "room_id": cur_room.get("room_id"),
                    "short_code": cur_room.get("short_code"),
                    "party": cur_room.get("party"),
                    "state": cur_room.get("state"),
                }
            else:
                session_out = {
                    "game_mode": live_session.get("game_mode"),
                    "cur_scn": live_session.get("cur_scn"),
                    "players_count": live_session.get("players_count"),
                    "zone": live_session.get("zone"),
                    "party": live_session.get("party"),
                    "state": live_session.get("state"),
                }
        except Exception:
            session_out = {"game_mode": live_session.get("game_mode"), "cur_scn": live_session.get("cur_scn")}

    # Room tag (scrim/tourney label for the details line + presence).
    # Best-effort single lookup by the live short_code; absent without a code.
    if session_out and session_out.get("short_code"):
        try:
            _code = str(session_out.get("short_code") or "").strip().upper()
            if len(_code) == 5 and _code.isalnum():
                _tres = supabase.table("bngc_room_tags").select(
                    "kind,name").eq("code", _code).limit(1).execute()
                _trows = _tres.data or []
                session_out["tag"] = ({"kind": str(_trows[0].get("kind") or "").lower(),
                                       "name": str(_trows[0].get("name") or _code)}
                                      if _trows else None)
            else:
                session_out["tag"] = None
        except Exception:
            try:
                session_out["tag"] = None
            except Exception:
                pass

    # Live tourney HUD: when the current room carries a tourney tag, attach
    # my points for that tourney (passthrough overlay reads this). Gated on
    # the tag so every other session pays zero extra queries. Never breaks stats.
    # Fallback: if no room tag, try a direct tourney-by-code lookup so the
    # HUD still shows even if the tag row is missing.
    comp_tourney = None
    try:
        _tag = (session_out or {}).get("tag") if session_out else None
        _tcode = (session_out or {}).get("short_code") if session_out else None
        if isinstance(_tag, dict) and str(_tag.get("kind") or "") == "tourney" and _tcode:
            from .comp import code_tourney_points as _ctp
            comp_tourney = await db_io(_ctp, _tcode, discord_id)
        elif _tcode and len(str(_tcode)) == 5:
            from .comp import code_tourney_points as _ctp
            comp_tourney = await db_io(_ctp, _tcode, discord_id)
    except Exception:
        comp_tourney = None

    # playtime_per_gm may be json string
    ppg = user.get("playtime_per_gm")
    if isinstance(ppg, str):
        try:
            import json as _j
            ppg = _j.loads(ppg)
        except Exception:
            ppg = {}

    # kills percentile for the drawer's "better than X% of players" line.
    # Lightweight exact counts (no row data); hidden when unavailable.
    rank = {"by": "kills", "pct": 0, "rank": 0, "total": 0}
    try:
        t = await db_io(lambda: supabase.table("bngc_users").select("discord_id", count="exact").execute())
        total = t.count or 0
        a = await db_io(lambda: supabase.table("bngc_users").select("discord_id", count="exact").gt("kills", kills).execute())
        above = a.count or 0
        rank = {"by": "kills", "pct": better_than_pct(total, above),
                "rank": above + 1 if total else 0, "total": total or 0}
    except Exception:
        pass

    # Ranked block: null unless the live room is a lobby/live ranked room.
    # Response-only UI: the userscript renders lobby vs match UI from this
    # (lobby = create/join bind confirmed here; match = curScn flip locally).
    # Additive — non-ranked responses keep "ranked": null.
    ranked_out = None
    try:
        code = (session_out or {}).get("short_code")
        # Fallback: if session is stale (no short_code), use the preserved
        # short_code from the last known room. This keeps the ranked block
        # visible when the capture stopped but the user is still in the room.
        if not code and _stale_short_code:
            code = _stale_short_code
        if code:
            rdata = await db_io(ranked_mod.get_ranked_room, code)
            if rdata and rdata.get("status") in ("lobby", "live"):
                pts = await db_io(ranked_mod.get_ranked_points, discord_id)
                tier = ranked_mod.milestone_for(pts)
                rid = str(rdata.get("id") or "")
                try:
                    mver = int(cur_room.get("match_version") or 0)
                except Exception:
                    mver = 0
                # last sealed result for this room (drives the points toast):
                # what I gained, whether I won, and my kills.
                last_result = None
                try:
                    lr = await db_io(lambda: supabase.table("ranked_results").select(
                        "match_version,winner_discord_id,kills,points"
                    ).eq("room_id", rid).order("match_version", desc=True).limit(1).execute())
                    rows = lr.data or []
                    if rows:
                        row = rows[0]
                        pts_map = row.get("points") or {}
                        kills_map = row.get("kills") or {}
                        me = str(discord_id or "")
                        last_result = {
                            "match_version": row.get("match_version"),
                            "gained": int(pts_map.get(me) or 0),
                            "won": bool(me) and str(row.get("winner_discord_id") or "") == me,
                            "kills": int(kills_map.get(me) or 0),
                        }
                except Exception:
                    last_result = None
                ranked_out = {
                    "active": True,
                    "rid": rid,
                    "short_code": str(code or "").strip().upper(),
                    "status": rdata.get("status"),
                    "is_host": str(discord_id or "") == str(rdata.get("host_discord_id") or ""),
                    "points": pts,
                    "milestone": tier.get("name"),
                    "milestone_icon_url": tier.get("icon_url"),
                    "milestone_color": tier.get("color"),
                    "match_id": f"{rid}-{mver}" if rid and mver > 0 else (rid or None),
                    "match_version": mver,
                    "last_result": last_result,
                }
    except Exception:
        ranked_out = None
    # Per-poll verdict log, sampled: a BLOCK line only when the verdict
    # flips (none<->BLOCK, version/points change) or every 60s. Polls land
    # every few seconds per client — an unconditional line buries real logs.
    try:
        _rk_state = (session_out or {}).get("state")
        _rk_code = (session_out or {}).get("short_code") or _stale_short_code
        _rk_sig = (str(ranked_out.get("rid")) if ranked_out else None,
                   ranked_out.get("match_version") if ranked_out else None,
                   ranked_out.get("points") if ranked_out else None)
        _rk_last = _stats_ranked_sig.get(str(discord_id or ""))
        _rk_now = _time.time()
        if _rk_last is None or _rk_last[0] != _rk_sig or _rk_now - _rk_last[1] >= 60:
            _stats_ranked_sig[str(discord_id or "")] = (_rk_sig, _rk_now)
            if ranked_out:
                log_ranked(f"[ranked] stats did={str(discord_id)[:6]} code={_rk_code} "
                        f"state={_rk_state} -> BLOCK rid={ranked_out.get('rid')} "
                        f"status={ranked_out.get('status')} host={ranked_out.get('is_host')} "
                        f"v={ranked_out.get('match_version')} pts={ranked_out.get('points')} "
                        f"tier={ranked_out.get('milestone')}"
                        f"{' stale' if _stale_short_code and not (session_out or {}).get('short_code') else ''}")
            else:
                log_ranked(f"[ranked] stats did={str(discord_id)[:6]} code={_rk_code} "
                        f"state={_rk_state} -> none")
    except Exception:
        pass

    # Arena block: session-independent points/tier/season. The ranked block
    # only exists inside a ranked room; drawer/panel/lobby-card read this.
    arena = None
    try:
        apts = await db_io(ranked_mod.get_ranked_points, discord_id)
        atier = ranked_mod.milestone_for(apts)
        anxt, aneed, apct = ranked_mod.next_milestone(apts)
        aseason, areset = ranked_mod.get_current_season()
        arena = {
            "points": apts,
            "milestone": atier.get("name"),
            "milestone_icon_url": atier.get("icon_url"),
            "milestone_color": atier.get("color"),
            "points_to_next": aneed,
            "next_milestone": (anxt.get("name") if anxt else None),
            "next_points_pct": apct,
            "season": aseason,
            "reset_at": areset,
            "streaks": {
                "daily": int(user.get("daily_streak") or 0),
                "win": int(user.get("win_streak") or 0),
                "best_win": int(user.get("best_win_streak") or 0),
                "best_match_kills": int(user.get("best_match_kills") or 0),
                "last_daily_claim": user.get("last_daily_claim"),
            },
            "ranked_sleeping": bool(getattr(ranked_mod, "RANKED_ENABLED", True) is False),
        }
    except Exception:
        arena = None
    try:
        log_dbg(f"[stats] did={str(discord_id)[:6]} arena={bool(arena)} "
                f"pts={arena['points'] if arena else '-'} "
                f"tier={arena['milestone'] if arena else '-'} "
                f"session={session_out.get('state') if session_out else 'none'} "
                f"session_code={session_out.get('short_code') if session_out else '-'} "
                f"ranked={bool(ranked_out)}")
    except Exception:
        pass

    # Identity diagnostics: kills/skins attach to view.me while playtime is
    # session-scoped — when me never resolved (null game_username in the DB
    # with playtime ticking), everything silently misses. Cheap: already in
    # the live view dict. Null me + ticking session = broken client identity.
    identity = None
    try:
        _lm = (live_session or {}).get("me") if live_session else None
        if isinstance(_lm, dict):
            identity = {"username": _lm.get("username"), "game_id": _lm.get("game_id"),
                        "skin": _lm.get("skin"), "actor_nr": _lm.get("actor_nr")}
    except Exception:
        identity = None

    # Comp points across open tourneys (drawer line + leaderboard sort read
    # this). Bounded single lookup; empty without open tourneys. Never breaks
    # stats: any failure degrades to no comp block, exactly like arena=None.
    comp = []
    try:
        from .comp import open_tourney_points as _otp
        _cp = await db_io(_otp, [discord_id])
        _mine = (_cp or {}).get(str(discord_id)) if isinstance(_cp, dict) else None
        if _mine:
            comp = [{"tourney_id": _mine.get("tourney_id"), "tourney_name": _mine.get("tourney_name"),
                     "points": int(_mine.get("points") or 0), "kills": int(_mine.get("kills") or 0),
                     "wins": int(_mine.get("wins") or 0)}]
    except Exception:
        comp = []

    # Tourney PR (latest computed rating + delta vs previous tourney).
    # Additive, never breaks stats: missing column/table -> nulls.
    tourney_pr = None
    pr_prev = None
    try:
        try:
            tourney_pr = user.get("tourney_pr")
            tourney_pr = float(tourney_pr) if tourney_pr is not None else None
        except Exception:
            tourney_pr = None
        try:
            _ph = await db_io(lambda: supabase.table("bngc_comp_pr_history").select(
                "pr").eq("discord_id", str(discord_id)).order(
                "computed_at", desc=True).limit(2).execute())
            _prs = [float(r.get("pr")) for r in ((_ph.data if hasattr(_ph, "data") else []) or [])
                    if (r or {}).get("pr") is not None]
            if _prs:
                if tourney_pr is None:
                    tourney_pr = _prs[0]
                if len(_prs) > 1:
                    pr_prev = _prs[1]
        except Exception:
            pass
    except Exception:
        tourney_pr, pr_prev = None, None
    pr_delta_pct = None
    try:
        if tourney_pr is not None and pr_prev:
            pr_delta_pct = round((tourney_pr - pr_prev) / pr_prev * 100, 1)
    except Exception:
        pr_delta_pct = None

    return JSONResponse({
        "discord_id": user.get("discord_id"),
        "discord_username": user.get("discord_username"),
        "game_username": user.get("game_username"),
        "game_id": user.get("game_id"),
        "skin": user.get("skin"),
        "pickaxe": user.get("pickaxe"),
        "kills": kills,
        "deaths": deaths,
        "wins": wins,
        "play_points": int(user.get("play_points") or 0),
        "playtime_global": int(user.get("playtime_global") or 0),
        "playtime_per_gm": ppg or {},
        "kd": kd,
        "tourney_pr": tourney_pr,
        "pr_prev": pr_prev,
        "pr_delta_pct": pr_delta_pct,
        "identity": identity,
        "comp": comp,
        "comp_tourney": comp_tourney,
        "rank": rank,
        "is_dev": bool(user.get("is_dev")),
        "arena": arena,
        "is_locked": bool(user.get("is_locked")),
        "lobby_music_url": user.get("lobby_music_url"),
        "lobby_music_volume": user.get("lobby_music_volume", 0.5),
        "session": session_out,
        "ranked": ranked_out,
    })
