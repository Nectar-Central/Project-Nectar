"""
discovery.py — GET /api/search and GET /api/leaderboard

Search: ?q=xxx → users matching discord_username / game_username / game_id
Leaderboard: ?sort=kills|wins|kd|win_rate|playtime&limit=20&offset=0 -> ordered bngc_users + computed fields
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase

router = APIRouter()

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
