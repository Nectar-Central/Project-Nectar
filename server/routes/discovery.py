"""
discovery.py — GET /api/search, GET /api/leaderboard, GET /api/user, GET /api/matches

Search: ?q=xxx → users matching discord_username / game_username / game_id
Leaderboard: ?sort=kills|wins|kd|win_rate|playtime&limit=20&offset=0 -> ordered bngc_users + computed fields
User: ?discord_id=xxx | ?game_username=xxx | ?game_id=xxx -> public player profile (no secrets)
Matches: ?discord_id=xxx | ?game_username=xxx | ?game_id=xxx -> recent grouped matches (room+version)
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase
from ..lib import ranked as ranked_mod

router = APIRouter()


def _resolve_identity(discord_id: str = "", game_username: str = "", game_id: str = "") -> dict | None:
    """Find a bngc_users row by whichever identifier is given. Pure lookup, no secrets."""
    if not supabase:
        return None
    for col, val in (("discord_id", discord_id), ("game_username", game_username), ("game_id", game_id)):
        if not val:
            continue
        try:
            res = supabase.table("bngc_users").select("discord_id").eq(col, val).limit(1).execute()
            if res.data:
                return res.data[0]
        except Exception:
            continue
    return None


def _match_key(event_data: dict, session_id: str) -> str:
    """Composite room+version key (same scheme as the bot: splits private
    zw_lg multi-match on the same room_id)."""
    gd = event_data or {}
    rid = str(gd.get("room_id") or gd.get("room") or gd.get("code") or gd.get("short_code") or session_id)
    mv = gd.get("match_version")
    if mv is not None:
        try:
            return f"{rid}_{int(mv)}"
        except Exception:
            return f"{rid}_{mv}"
    return rid


def _group_match_events(events: list[dict], session_id: str = "") -> list[dict]:
    """Collapse raw game events into per-match summaries (pure, unit-tested).

    Returns newest-first: [{room, match_version, gm, kills, deaths, win,
    defeat, created_at}] for groups that look like real matches.
    """
    groups: dict[str, list[dict]] = {}
    for ev in events or []:
        gd = ev.get("event_data") or {}
        groups.setdefault(_match_key(gd, ev.get("session_id") or session_id), []).append(ev)
    out = []
    for key, evs in groups.items():
        kinds = {e.get("event_type") for e in evs}
        # skip lobby noise / sessions with no match-level signal
        if not (kinds & {"kill", "death", "win", "defeat", "zone_change"}):
            continue
        try:
            ts = max(str(e.get("created_at") or "") for e in evs)
        except Exception:
            ts = ""
        gm = next(( (e.get("event_data") or {}).get("gm") for e in evs if (e.get("event_data") or {}).get("gm")), None)
        first_gd = (evs[0].get("event_data") or {})
        mv = first_gd.get("match_version")
        room = str(first_gd.get("room_id") or first_gd.get("short_code") or "")[:8] or key[:8]
        out.append({
            "room": room,
            "match_version": mv,
            "gm": gm,
            "kills": sum(1 for e in evs if e.get("event_type") == "kill"),
            "deaths": sum(1 for e in evs if e.get("event_type") == "death"),
            "win": any(e.get("event_type") == "win" for e in evs),
            "defeat": any(e.get("event_type") == "defeat" for e in evs),
            "created_at": ts,
        })
    out.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    return out

# ---------------------------------------------------------------------------
# GET /api/search?q=...
# ---------------------------------------------------------------------------
@router.get("/api/search")
async def search(request: Request):
    q = (request.query_params.get("q") or request.query_params.get("query") or "").strip()
    limit_raw = request.query_params.get("limit") or "10"
    try:
        limit = max(1, min(int(limit_raw), 25))
    except Exception:
        limit = 10

    if not q or len(q) < 2:
        return JSONResponse({"error": "q required (min 2 chars)"}, status_code=400)

    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)

    # supabase doesn't support OR ilike across columns in one query easily, so we do 3 queries and merge
    # Use ilike via supabase filter: column.ilike.%q%
    pattern = f"%{q}%"
    results: dict[str, dict] = {}
    try:
        for col in ("discord_username", "game_username", "game_id"):
            try:
                res = supabase.table("bngc_users").select("discord_id,discord_username,game_username,game_id,skin,kills,deaths,wins,defeats,playtime_global").ilike(col, pattern).limit(limit).execute()
                for row in (res.data or []):
                    did = row.get("discord_id")
                    if did and did not in results:
                        results[did] = row
                    if len(results) >= limit:
                        break
            except Exception:
                continue
            if len(results) >= limit:
                break
        # also try exact discord_id match
        if len(results) < limit:
            try:
                res = supabase.table("bngc_users").select("discord_id,discord_username,game_username,game_id,skin,kills,deaths,wins,defeats,playtime_global").eq("discord_id", q).limit(1).execute()
                for row in (res.data or []):
                    did = row.get("discord_id")
                    if did and did not in results:
                        results[did] = row
            except Exception:
                pass

        out = list(results.values())[:limit]
        # add computed kd/win_rate
        for r in out:
            kills = int(r.get("kills") or 0)
            deaths = int(r.get("deaths") or 0)
            wins = int(r.get("wins") or 0)
            defeats = int(r.get("defeats") or 0)
            r["kd"] = round(kills / deaths, 2) if deaths else float(kills)
            total = wins + defeats
            r["win_rate"] = round(wins / total, 3) if total else 0

        return JSONResponse({"query": q, "count": len(out), "results": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# GET /api/leaderboard?sort=kills&limit=20&offset=0
# ---------------------------------------------------------------------------
@router.get("/api/leaderboard")
async def leaderboard(request: Request):
    sort = (request.query_params.get("sort") or "kills").strip().lower()
    limit_raw = request.query_params.get("limit") or "20"
    offset_raw = request.query_params.get("offset") or "0"
    order = (request.query_params.get("order") or "desc").strip().lower()

    try:
        limit = max(1, min(int(limit_raw), 100))
    except Exception:
        limit = 20
    try:
        offset = max(0, int(offset_raw))
    except Exception:
        offset = 0

    if order not in ("asc", "desc"):
        order = "desc"

    # Ranked leaderboard (?ranked=true): arena points from ranked_points,
    # usernames merged from bngc_users, tier attached per row. Sort is always
    # points (param ignored); order asc/desc respected.
    ranked_q = (request.query_params.get("ranked") or "").strip().lower() in ("1", "true", "yes")
    if ranked_q:
        if not supabase:
            return JSONResponse({"error": "Server not configured"}, status_code=500)
        try:
            desc = order == "desc"
            res = supabase.table("ranked_points").select("discord_id,points").order("points", desc=desc).range(offset, offset + limit - 1).execute()
            prows = res.data or []
            dids = [str(r.get("discord_id")) for r in prows if r.get("discord_id")]
            users = {}
            if dids:
                try:
                    ures = supabase.table("bngc_users").select("discord_id,discord_username,game_username,game_id,skin").in_("discord_id", dids[:100]).execute()
                    for u in (ures.data or []):
                        users[str(u.get("discord_id"))] = u
                except Exception:
                    pass
            rows = []
            for i, pr in enumerate(prows):
                did = str(pr.get("discord_id"))
                try:
                    pts = int(pr.get("points") or 0)
                except Exception:
                    pts = 0
                tier = ranked_mod.milestone_for(pts)
                u = users.get(did) or {}
                rows.append({
                    "discord_id": did,
                    "discord_username": u.get("discord_username"),
                    "game_username": u.get("game_username"),
                    "game_id": u.get("game_id"),
                    "skin": u.get("skin"),
                    "points": pts,
                    "milestone": tier.get("name"),
                    "milestone_icon_url": tier.get("icon_url"),
                    "milestone_color": tier.get("color"),
                    "rank": offset + i + 1,
                })
            season, reset_at = ranked_mod.get_current_season()
            return JSONResponse({"ranked": True, "sort": "points", "order": order,
                                 "limit": limit, "offset": offset,
                                 "count": len(rows), "results": rows,
                                 "season": season, "reset_at": reset_at})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    allowed = {"kills", "deaths", "wins", "defeats", "playtime_global", "kd", "win_rate"}
    if sort not in allowed:
        return JSONResponse({"error": f"sort must be one of {', '.join(sorted(allowed))}"}, status_code=400)

    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)

    try:
        # For direct columns, let Supabase order; for computed (kd/win_rate), fetch and sort in python
        if sort in ("kills", "deaths", "wins", "defeats", "playtime_global"):
            # supabase order: .order(col, desc=True/False)
            desc = order == "desc"
            res = supabase.table("bngc_users").select("discord_id,discord_username,game_username,game_id,skin,kills,deaths,wins,defeats,playtime_global").order(sort, desc=desc).range(offset, offset + limit - 1).execute()
            rows = res.data or []
            for r in rows:
                kills = int(r.get("kills") or 0)
                deaths = int(r.get("deaths") or 0)
                wins = int(r.get("wins") or 0)
                defeats = int(r.get("defeats") or 0)
                r["kd"] = round(kills / deaths, 2) if deaths else float(kills)
                total = wins + defeats
                r["win_rate"] = round(wins / total, 3) if total else 0
            # add rank
            for i, r in enumerate(rows):
                r["rank"] = offset + i + 1
            return JSONResponse({"sort": sort, "order": order, "limit": limit, "offset": offset, "count": len(rows), "results": rows})
        else:
            # kd / win_rate — need to compute: fetch top 200 ordered by kills then sort
            # fetch a window larger than limit to get accurate ranking, then slice
            fetch_n = max(limit + offset, 200)
            res = supabase.table("bngc_users").select("discord_id,discord_username,game_username,game_id,skin,kills,deaths,wins,defeats,playtime_global").limit(fetch_n).execute()
            rows = res.data or []
            for r in rows:
                kills = int(r.get("kills") or 0)
                deaths = int(r.get("deaths") or 0)
                wins = int(r.get("wins") or 0)
                defeats = int(r.get("defeats") or 0)
                r["kd"] = round(kills / deaths, 2) if deaths else float(kills)
                total = wins + defeats
                r["win_rate"] = round(wins / total, 3) if total else 0
            reverse = order == "desc"
            rows.sort(key=lambda x: x.get(sort, 0), reverse=reverse)
            sliced = rows[offset: offset + limit]
            for i, r in enumerate(sliced):
                r["rank"] = offset + i + 1
            return JSONResponse({"sort": sort, "order": order, "limit": limit, "offset": offset, "count": len(sliced), "results": sliced})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# GET /api/ranked/verify?code=XXXXX&host=Name
# Bot start-gate: is this room code live in-game, and is the host in it?
# Evidence = recent bngc_game_events carrying the code (15-min window).
# Usernames harvested from killer/victim/winner fields + discord resolution.
# ---------------------------------------------------------------------------
_VERIFY_WINDOW_S = 900


def _strip_dc(name: str) -> str:
    try:
        s = str(name or "").strip()
        return s[3:].strip() if s.lower().startswith("dc:") else s
    except Exception:
        return ""


@router.get("/api/ranked/verify")
async def ranked_verify(request: Request):
    qp = request.query_params
    code = (qp.get("code") or "").strip().upper()
    host = (qp.get("host") or "").strip()
    if getattr(ranked_mod, "RANKED_ENABLED", True) is False:
        return JSONResponse({"code": code or None, "room_active": False,
                             "host_in_room": False, "players_seen": [],
                             "events_seen": 0, "sleeping": True})
    if len(code) != 5 or not code.isalnum():
        return JSONResponse({"error": "code must be 5 letters/digits"}, status_code=400)
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        res = supabase.table("bngc_game_events").select(
            "event_type,event_data,discord_id,created_at"
        ).order("created_at", desc=True).limit(300).execute()
        names: set = set()
        dids: set = set()
        events_seen = 0
        for row in (res.data or []):
            try:
                ed = (row or {}).get("event_data") or {}
                c = str(ed.get("code") or ed.get("short_code") or "").strip().upper()
                if c != code:
                    continue
                try:
                    ts = str((row or {}).get("created_at") or "").replace("Z", "+00:00")
                    age = (now - datetime.fromisoformat(ts)).total_seconds()
                except Exception:
                    continue
                if age < 0 or age > _VERIFY_WINDOW_S:
                    continue
                events_seen += 1
                for key in ("killer", "victim", "winner"):
                    v = ed.get(key)
                    if v and str(v).strip():
                        names.add(str(v).strip())
                if (row or {}).get("discord_id"):
                    dids.add(str(row["discord_id"]))
            except Exception:
                continue
        # resolve seen discord_ids to game usernames (catches quiet players)
        if dids:
            try:
                ures = supabase.table("bngc_users").select("game_username").in_("discord_id", sorted(dids)[:64]).execute()
                for u in (ures.data or []):
                    if u.get("game_username"):
                        names.add(str(u["game_username"]))
            except Exception:
                pass
        room_active = events_seen > 0
        host_in_room = False
        if host:
            hn = _strip_dc(host)
            for n in names:
                if n and (n == host or _strip_dc(n) == hn):
                    host_in_room = True
                    break
        return JSONResponse({
            "code": code,
            "room_active": room_active,
            "host_in_room": host_in_room,
            "players_seen": sorted(names)[:32],
            "events_seen": events_seen,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# GET /api/user?discord_id=... | ?game_username=... | ?game_id=...
# ---------------------------------------------------------------------------
@router.get("/api/user")
async def get_user(request: Request):
    """Public player profile for website embeds — stats only, never secrets."""
    qp = request.query_params
    discord_id = (qp.get("discord_id") or "").strip()
    game_username = (qp.get("game_username") or "").strip()
    game_id = (qp.get("game_id") or "").strip()
    if not discord_id and not game_username and not game_id:
        return JSONResponse({"error": "discord_id, game_username or game_id required"}, status_code=400)

    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)

    try:
        cols = "discord_id,discord_username,game_username,game_id,skin,pickaxe,kills,deaths,wins,defeats,playtime_global,playtime_per_gm"
        row = None
        for col, val in (("discord_id", discord_id), ("game_username", game_username), ("game_id", game_id)):
            if not val:
                continue
            try:
                res = supabase.table("bngc_users").select(cols).eq(col, val).limit(1).execute()
                if res.data:
                    row = res.data[0]
                    break
            except Exception:
                continue
        if not row:
            return JSONResponse({"error": "user not found"}, status_code=404)

        kills = int(row.get("kills") or 0)
        deaths = int(row.get("deaths") or 0)
        wins = int(row.get("wins") or 0)
        defeats = int(row.get("defeats") or 0)
        per_gm = row.get("playtime_per_gm")
        if isinstance(per_gm, str):
            try:
                import json as _j
                per_gm = _j.loads(per_gm)
            except Exception:
                per_gm = {}
        return JSONResponse({
            "discord_id": row.get("discord_id"),
            "discord_username": row.get("discord_username"),
            "game_username": row.get("game_username"),
            "game_id": row.get("game_id"),
            "skin": row.get("skin"),
            "pickaxe": row.get("pickaxe"),
            "kills": kills,
            "deaths": deaths,
            "wins": wins,
            "defeats": defeats,
            "kd": round(kills / deaths, 2) if deaths else float(kills),
            "win_rate": round(wins / (wins + defeats), 3) if (wins + defeats) else 0,
            "playtime_global": int(row.get("playtime_global") or 0),
            "playtime_per_gm": per_gm or {},
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# GET /api/matches?discord_id=... | ?game_username=... | ?game_id=... [&limit=10]
# ---------------------------------------------------------------------------
@router.get("/api/matches")
async def get_matches(request: Request):
    """Recent grouped matches for the website profile tab (kills/deaths per
    match + win/defeat outcome). No secrets."""
    qp = request.query_params
    ident = _resolve_identity(qp.get("discord_id") or "", qp.get("game_username") or "", qp.get("game_id") or "")
    if not ident:
        if not supabase:
            return JSONResponse({"error": "Server not configured"}, status_code=500)
        return JSONResponse({"error": "user not found"}, status_code=404)
    try:
        limit = max(1, min(int(qp.get("limit") or "10"), 25))
    except Exception:
        limit = 10
    try:
        res = supabase.table("bngc_game_events").select(
            "session_id,event_type,event_data,created_at"
        ).eq("discord_id", ident["discord_id"]).order("created_at", desc=True).limit(500).execute()
        matches = _group_match_events(res.data or [])[:limit]
        return JSONResponse({"count": len(matches), "matches": matches})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
