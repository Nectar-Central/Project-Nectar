"""
comp.py — comp/tourney read API (pure compute + thin endpoints).

  GET /api/comp/tourneys?status=scheduled|running|ended
      -> [{id, code, name, status, series_len, sealed, players, leader, ...}]
  GET /api/comp/tourney?id=
      -> {tourney, registrations, matches, standings}

Statuses are DERIVED (never stored beyond scheduled/open/closed):
  closed -> ended; scheduled -> scheduled; open + >=1 sealed match -> running;
  open + 0 matches -> scheduled.

Standings math (shared by bot, dashboard, Events tab — one implementation):
  per sealed match: kills per discord (kill events, mapped only) +
  win bonus (manual winner_discord_id, else last alive from alv order) +
  placement table by reverse alv-death order (last alive = 1st).
  Aggregate = sum across sealed matches. Points table from the tourney row,
  default kill 1 / win 3 / placement [10,7,5,3,1].
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..lib.supabase_client import supabase

router = APIRouter()

DEFAULT_TABLE = {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}


def derive_status(tourney: dict, sealed_count: int) -> str:
    """created | scheduled | running | ended. Pure, unit-tested.

    Stored lifecycle: created -> scheduled -> started -> ended.
    Display mirrors storage 1:1 (a fresh row reads Created, not Scheduled);
    callers that only know the old three buckets treat created as scheduled.
    """
    try:
        s = str((tourney or {}).get("status") or "created").lower()
    except Exception:
        s = "created"
    if s == "ended":
        return "ended"
    if s == "started":
        return "running"
    if s == "scheduled":
        return "scheduled"
    return "created"


def _table_of(tourney: dict | None) -> dict:
    try:
        t = ((tourney or {}).get("points_table")) or {}
        if isinstance(t, str):
            import json as _j
            t = _j.loads(t)
        kill = int(t.get("kill", DEFAULT_TABLE["kill"]))
        win = int(t.get("win", DEFAULT_TABLE["win"]))
        place = list(t.get("placement", DEFAULT_TABLE["placement"]))
        return {"kill": kill, "win": win, "placement": [int(x) for x in place]}
    except Exception:
        return dict(DEFAULT_TABLE)


def compute_match_points(events: list[dict], table: dict | None = None,
                         winner_override: str | None = None,
                         placements: dict | None = None) -> list[dict]:
    """Points for ONE sealed match. Pure, unit-tested.

    events: comp event rows {etype, discord_id, actor_name, alive}.
    Kills count per mapped discord_id only (twins stay null upstream).
    Placement: manual map first ({did: place}), else reverse alv-death order
    (last alive first). Winner: override, else manual P1, else last alive,
    else None (kills-only scoring).
    Returns [{discord_id, name, kills, placement, won, points}] sorted desc.
    """
    table = table or dict(DEFAULT_TABLE)
    try:
        place_pts = list(table.get("placement") or [])
        kill_pts = int(table.get("kill", 1))
        win_pts = int(table.get("win", 3))
    except Exception:
        place_pts, kill_pts, win_pts = list(DEFAULT_TABLE["placement"]), 1, 3
    kills: dict[str, int] = {}
    names: dict[str, str] = {}
    death_order: list[str] = []  # first out -> last out
    seen_dead: set[str] = set()
    for e in (events or []):
        try:
            et = (e or {}).get("etype")
            did = (e or {}).get("discord_id")
            if et == "kill" and did:
                kills[did] = kills.get(did, 0) + 1
                if (e or {}).get("actor_name") and did not in names:
                    names[did] = str(e["actor_name"])
            elif et == "alv" and did and (e or {}).get("alive") is False:
                if did not in seen_dead:
                    seen_dead.add(did)
                    death_order.append(did)
                if (e or {}).get("actor_name") and did not in names:
                    names[did] = str(e["actor_name"])
            elif did and (e or {}).get("actor_name") and did not in names:
                names[did] = str(e["actor_name"])
        except Exception:
            continue
    order = list(reversed(death_order))  # last alive first
    winner = None
    try:
        if winner_override and str(winner_override) in set(list(kills) + order):
            winner = str(winner_override)
        elif order:
            winner = order[0]
    except Exception:
        winner = None
    placement_of = {did: i + 1 for i, did in enumerate(order)}
    # Manual placement override (dashboard): exact places beat alv order;
    # winner falls back to manual P1 when no override is set.
    try:
        man = {str(k): int(v) for k, v in (placements or {}).items()
               if str(k or "").strip()}
    except Exception:
        man = {}
    if man:
        try:
            known = set(list(kills) + order + list(man))
            placement_of = {did: man[did] for did in sorted(man, key=lambda d: man[d]) if did in known}
            if not winner:
                first = sorted(man.items(), key=lambda kv: kv[1])
                if first and first[0][0] in known:
                    winner = first[0][0]
        except Exception:
            pass
    rows = []
    for did in sorted(set(list(kills) + order + list(man))):
        k = kills.get(did, 0)
        p = placement_of.get(did)
        pts = k * kill_pts
        if winner and did == winner:
            pts += win_pts
        if p and p <= len(place_pts):
            pts += place_pts[p - 1]
        rows.append({"discord_id": did, "name": names.get(did) or did[:6],
                     "kills": k, "placement": p, "won": bool(winner and did == winner),
                     "points": pts})
    rows.sort(key=lambda r: (r["points"], r["kills"]), reverse=True)
    return rows


def aggregate_standings(per_match: list[list[dict]]) -> list[dict]:
    """Sum match rows into a series board. Pure, unit-tested."""
    tot: dict[str, dict] = {}
    try:
        for rows in (per_match or []):
            for r in (rows or []):
                did = str((r or {}).get("discord_id") or "")
                if not did:
                    continue
                d = tot.setdefault(did, {"discord_id": did, "name": (r or {}).get("name") or did[:6],
                                         "kills": 0, "wins": 0, "points": 0, "matches": 0})
                d["kills"] += int((r or {}).get("kills") or 0)
                d["wins"] += 1 if (r or {}).get("won") else 0
                d["points"] += int((r or {}).get("points") or 0)
                d["matches"] += 1
    except Exception:
        pass
    out = list(tot.values())
    out.sort(key=lambda r: (r["points"], r["kills"]), reverse=True)
    return out


def standings_for_matches(tourney: dict | None, sealed: list[dict], events_by_match: dict):
    """(per_match, board) for sealed matches. Pure core, unit-tested.

    sealed: match rows {id, winner_discord_id}; events_by_match: match_id ->
    event rows. Points table from the tourney row.
    """
    try:
        table = _table_of(tourney)
    except Exception:
        table = dict(DEFAULT_TABLE)
    per_match = []
    try:
        for m in (sealed or []):
            try:
                per_match.append(compute_match_points(
                    (events_by_match or {}).get((m or {}).get("id"), []),
                    table, (m or {}).get("winner_discord_id"),
                    (m or {}).get("placements")))
            except Exception:
                per_match.append([])
    except Exception:
        per_match = []
    try:
        board = aggregate_standings(per_match)
    except Exception:
        board = []
    return per_match, board


def code_tourney_points(code: str | None, discord_id: str | None) -> dict | None:
    """My live points for the tourney tagged to this room code. For the
    passthrough HUD. Returns {tourney_id, name, points, kills, wins, rank,
    players} or None (no tourney / no rows / DB down). Never raises."""
    try:
        code = str(code or "").strip().upper()
        did = str(discord_id or "").strip()
        if not code or not did or not supabase:
            return None
        tres = supabase.table("bngc_comp_tourneys").select(
            "id,name,points_table").eq("code", code).neq(
            "status", "ended").limit(2).execute()
        rows = tres.data or []
        if len(rows) != 1:
            return None
        t = rows[0]
        mres = supabase.table("bngc_comp_matches").select(
            "id,winner_discord_id,placements").eq("tourney_id", t["id"]).in_(
            "status", ["sealed", "manual"]).order("version").limit(200).execute()
        sealed = mres.data or []
        if not sealed:
            return {"tourney_id": t["id"], "name": t.get("name"), "points": 0,
                    "kills": 0, "wins": 0, "rank": None, "players": 0}
        table = _table_of(t)
        mids = [m["id"] for m in sealed]
        eres = supabase.table("bngc_comp_events").select(
            "match_id,etype,discord_id,actor_name,alive").in_(
            "match_id", mids[:200]).limit(5000).execute()
        by_mid: dict = {}
        for e in (eres.data or []):
            by_mid.setdefault((e or {}).get("match_id"), []).append(e)
        per_match, board = standings_for_matches(t, sealed, by_mid)
        me = next((r for r in board if str(r.get("discord_id")) == did), None)
        rank = next((i + 1 for i, r in enumerate(board) if str(r.get("discord_id")) == did), None)
        if me is None:
            return {"tourney_id": t["id"], "name": t.get("name"), "points": 0,
                    "kills": 0, "wins": 0, "rank": None, "players": len(board)}
        return {"tourney_id": t["id"], "name": t.get("name"),
                "points": int(me.get("points") or 0), "kills": int(me.get("kills") or 0),
                "wins": int(me.get("wins") or 0), "rank": rank, "players": len(board)}
    except Exception:
        return None


def open_tourney_points(discord_ids: list[str]):
    """Comp points per user across OPEN tourneys. For drawer/leaderboard display.

    Returns {did: {points, kills, wins, tourney_id, tourney_name}}. Empty dict
    (never raises) when no open tourneys, no sealed matches, or DB down.
    Bounded: 20 tourneys, 200 matches, 5000 events.
    """
    try:
        if not supabase or not discord_ids:
            return {}
        want = sorted({str(d) for d in discord_ids if str(d or "").strip()})[:200]
        if not want:
            return {}
        tres = supabase.table("bngc_comp_tourneys").select(
            "id,name,points_table").eq("status", "started").limit(20).execute()
        trous = tres.data or []
        if not trous:
            return {}
        tids = [t["id"] for t in trous]
        mres = supabase.table("bngc_comp_matches").select(
            "id,tourney_id,winner_discord_id,placements").in_("tourney_id", tids).in_(
            "status", ["sealed", "manual"]).limit(200).execute()
        sealed = mres.data or []
        if not sealed:
            return {}
        mids = [m["id"] for m in sealed]
        eres = supabase.table("bngc_comp_events").select(
            "match_id,etype,discord_id,actor_name,alive").in_(
            "match_id", mids[:200]).in_("discord_id", want).limit(5000).execute()
        by_mid: dict = {}
        for e in (eres.data or []):
            by_mid.setdefault((e or {}).get("match_id"), []).append(e)
        by_tid: dict = {}
        for m in sealed:
            by_tid.setdefault((m or {}).get("tourney_id"), []).append(m)
        out: dict[str, dict] = {}
        for t in trous:
            tmap = {m["id"]: m for m in by_tid.get(t["id"], [])}
            evs = {mid: by_mid.get(mid, []) for mid in tmap}
            per_match, board = standings_for_matches(t, list(tmap.values()), evs)
            _ = per_match
            for r in board:
                did = str(r.get("discord_id"))
                if did in want:
                    cur = out.get(did)
                    if cur is None or r.get("points", 0) > cur.get("points", 0):
                        out[did] = {"points": r.get("points", 0), "kills": r.get("kills", 0),
                                    "wins": r.get("wins", 0), "tourney_id": t["id"],
                                    "tourney_name": t.get("name")}
        return out
    except Exception:
        return {}


def _need():
    if not supabase:
        return JSONResponse({"error": "Server not configured"}, status_code=500)
    return None


@router.get("/api/comp/tourneys")
async def list_tourneys(request: Request):
    err = _need()
    if err:
        return err
    want = str(request.query_params.get("status") or "all").strip().lower()
    if want not in ("all", "created", "scheduled", "running", "ended"):
        return JSONResponse({"error": "status must be all|created|scheduled|running|ended"}, status_code=400)
    try:
        tres = supabase.table("bngc_comp_tourneys").select(
            "id,code,name,status,series_len,scheduled_at,started_at,ended_at,created_at"
        ).order("created_at", desc=False).limit(100).execute()
        rows = tres.data or []
        tids = [r["id"] for r in rows]
        sealed_by: dict = {}
        players_by: dict = {}
        if tids:
            try:
                mres = supabase.table("bngc_comp_matches").select(
                    "tourney_id,status").in_("tourney_id", tids).limit(2000).execute()
                for m in (mres.data or []):
                    if (m or {}).get("status") in ("sealed", "manual"):
                        sealed_by[m["tourney_id"]] = sealed_by.get(m["tourney_id"], 0) + 1
            except Exception:
                pass
            try:
                rres = supabase.table("bngc_comp_registrations").select(
                    "tourney_id").eq("status", "accepted").in_("tourney_id", tids).limit(2000).execute()
                for r in (rres.data or []):
                    players_by[r["tourney_id"]] = players_by.get(r["tourney_id"], 0) + 1
            except Exception:
                pass
        out = []
        for t in rows:
            st = derive_status(t, sealed_by.get(t["id"], 0))
            if want != "all" and st != want:
                continue
            out.append({"id": t["id"], "code": t.get("code"), "name": t.get("name"),
                        "status": st, "series_len": t.get("series_len") or 0,
                        "sealed": sealed_by.get(t["id"], 0),
                        "players": players_by.get(t["id"], 0),
                        "scheduled_at": t.get("scheduled_at"),
                        "started_at": t.get("started_at"),
                        "ended_at": t.get("ended_at")})
        return JSONResponse({"count": len(out), "tourneys": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/comp/tourney")
async def tourney_detail(request: Request):
    err = _need()
    if err:
        return err
    try:
        tid = int(request.query_params.get("id"))
    except Exception:
        return JSONResponse({"error": "id required (numeric, from 0)"}, status_code=400)
    try:
        tres = supabase.table("bngc_comp_tourneys").select("*").eq("id", tid).limit(1).execute()
        if not tres.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        t = tres.data[0]
        try:
            mres = supabase.table("bngc_comp_matches").select(
                "id,version,room_id,status,winner_discord_id,placements,sealed_at"
            ).eq("tourney_id", tid).order("version").limit(200).execute()
            matches = mres.data or []
        except Exception:
            matches = []
        sealed = [m for m in matches if (m or {}).get("status") in ("sealed", "manual")]
        table = _table_of(t)
        per_match, board = [], []
        try:
            mids = [m["id"] for m in sealed]
            by_mid: dict = {}
            if mids:
                eres = supabase.table("bngc_comp_events").select(
                    "match_id,etype,discord_id,actor_name,alive"
                ).in_("match_id", mids[:200]).limit(5000).execute()
                for e in (eres.data or []):
                    by_mid.setdefault((e or {}).get("match_id"), []).append(e)
            per_match, board = standings_for_matches(t, sealed, by_mid)
        except Exception:
            per_match, board = [], []
        try:
            rres = supabase.table("bngc_comp_registrations").select(
                "team_label,member_ids,captain_id,status"
            ).eq("tourney_id", tid).eq("status", "accepted").limit(500).execute()
            regs = rres.data or []
        except Exception:
            regs = []
        names: dict[str, str] = {}
        try:
            dids = sorted({str(x) for r in regs for x in (r.get("member_ids") or []) if x})[:200]
            if dids:
                ures = supabase.table("bngc_users").select(
                    "discord_id,game_username,discord_username").in_("discord_id", dids).execute()
                for u in (ures.data or []):
                    names[str(u.get("discord_id"))] = str(
                        u.get("game_username") or u.get("discord_username") or u.get("discord_id"))
        except Exception:
            pass
        return JSONResponse({
            "tourney": {"id": t["id"], "code": t.get("code"), "name": t.get("name"),
                        "status": derive_status(t, len(sealed)),
                        "series_len": t.get("series_len") or 0, "sealed": len(sealed),
                        "points_table": table, "scheduled_at": t.get("scheduled_at"),
                        "started_at": t.get("started_at"), "ended_at": t.get("ended_at")},
            "players": [{"discord_id": str(x), "name": names.get(str(x), str(x)[:6])}
                        for r in regs for x in (r.get("member_ids") or []) if x],
            "matches": [{"id": m.get("id"), "version": m.get("version"), "status": m.get("status"),
                         "room_id": (str(m.get("room_id") or "")[:8] or None),
                         "winner": next((rw.get("name") for rw in (per_match[i] if i < len(per_match) else [])
                                         if rw.get("won")), None)}
                        for i, m in enumerate(sealed)],
            "match_boards": [{"version": m.get("version"),
                              "rows": (per_match[i] if i < len(per_match) else [])[:10]}
                             for i, m in enumerate(sealed)],
            "standings": board,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def _json(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def _next_version(tourney_id: int) -> int:
    try:
        res = supabase.table("bngc_comp_matches").select("version").eq(
            "tourney_id", int(tourney_id)).order("version", desc=True).limit(1).execute()
        rows = res.data or []
        return int(rows[0].get("version", -1)) + 1 if rows else 0
    except Exception:
        return 0


@router.post("/api/comp/match/open")
async def comp_match_open(request: Request):
    """Manual blank match (dashboard). Never raises beyond JSON errors."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
    except Exception:
        return JSONResponse({"error": "tourney_id required"}, status_code=400)
    try:
        ver = _next_version(tid)
        res = supabase.table("bngc_comp_matches").insert({
            "tourney_id": tid, "version": ver,
            "room_id": (str(b.get("room_id") or "").strip() or None),
            "status": "manual",
        }).execute()
        row = (res.data or [{}])[0]
        return JSONResponse({"ok": True, "match_id": row.get("id"), "version": ver})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/seal")
async def comp_match_seal(request: Request):
    """Seal an open/manual match (dashboard end-freeze)."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    try:
        from datetime import datetime, timezone
        res = supabase.table("bngc_comp_matches").update(
            {"status": "sealed", "sealed_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", mid).in_("status", ["open", "manual"]).execute()
        if not res.data:
            return JSONResponse({"error": "match not found or already sealed"}, status_code=404)
        return JSONResponse({"ok": True, "match_id": mid})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/winner")
async def comp_match_winner(request: Request):
    """Set/clear the manual winner override (dashboard)."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    try:
        who = b.get("winner_discord_id")
        who = str(who).strip() if who else None
        res = supabase.table("bngc_comp_matches").update(
            {"winner_discord_id": who}).eq("id", mid).execute()
        if not res.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        return JSONResponse({"ok": True, "match_id": mid, "winner_discord_id": who})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _valid_table(t) -> dict | None:
    """Validated points table or None. Pure."""
    try:
        if not isinstance(t, dict):
            return None
        kill = int(t.get("kill"))
        win = int(t.get("win"))
        place = [int(x) for x in (t.get("placement") or [])]
        if kill < 0 or win < 0 or not place or len(place) > 10 or any(p < 0 for p in place):
            return None
        return {"kill": kill, "win": win, "placement": place}
    except Exception:
        return None


@router.post("/api/comp/tourney/table")
async def comp_tourney_table(request: Request):
    """Replace a tourney's points table (dashboard editor)."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        tid = int(b.get("tourney_id"))
    except Exception:
        return JSONResponse(
            {"error": "tourney_id required (pick a tourney in the dashboard list first)"},
            status_code=400)
    table = _valid_table(b.get("points_table"))
    if table is None:
        return JSONResponse(
            {"error": "points_table needs {kill>=0, win>=0, placement:[1..10 non-neg ints]}"},
            status_code=400)
    try:
        res = supabase.table("bngc_comp_tourneys").update(
            {"points_table": table}).eq("id", tid).execute()
        if not res.data:
            return JSONResponse({"error": "tourney not found"}, status_code=404)
        return JSONResponse({"ok": True, "tourney_id": tid, "points_table": table})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/calculate")
async def comp_calculate(request: Request):
    """Compute standings with the shared math and snapshot per-match rows
    into bngc_comp_results (dashboard Calculate button). Accepts
    {tourney_id} (all sealed) or {match_id} (one). Returns the board."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        from datetime import datetime, timezone
        tids: list[int] = []
        mids: list[int] | None = None
        if b.get("match_id") is not None:
            try:
                mids = [int(b.get("match_id"))]
            except Exception:
                return JSONResponse({"error": "match_id must be a number"}, status_code=400)
            mres = supabase.table("bngc_comp_matches").select(
                "id,tourney_id,version,room_id,status,winner_discord_id,placements").in_("id", mids).execute()
            matches = [m for m in (mres.data or []) if (m or {}).get("status") in ("sealed", "manual")]
            if not matches:
                return JSONResponse({"error": "match not found or not sealed"}, status_code=404)
            tmap = {m["id"]: m for m in matches}
            trows = supabase.table("bngc_comp_tourneys").select(
                "id,points_table").in_("id", list({m["tourney_id"] for m in matches})).execute()
            trous = {t["id"]: t for t in (trows.data or [])}
        else:
            try:
                tid = int(b.get("tourney_id"))
            except Exception:
                return JSONResponse(
                    {"error": "tourney_id required (pick a tourney in the dashboard list first)"},
                    status_code=400)
            tids = [tid]
            trows = supabase.table("bngc_comp_tourneys").select(
                "id,points_table").eq("id", tid).limit(1).execute()
            if not trows.data:
                return JSONResponse({"error": "tourney not found"}, status_code=404)
            trous = {tid: trows.data[0]}
            mres = supabase.table("bngc_comp_matches").select(
                "id,tourney_id,version,room_id,status,winner_discord_id,placements").eq(
                "tourney_id", tid).in_("status", ["sealed", "manual"]).order("version").limit(200).execute()
            matches = mres.data or []
            mids = [m["id"] for m in matches]
            tmap = {m["id"]: m for m in matches}
        by_mid: dict = {}
        if mids:
            eres = supabase.table("bngc_comp_events").select(
                "match_id,etype,discord_id,actor_name,alive").in_(
                "match_id", mids[:200]).limit(5000).execute()
            for e in (eres.data or []):
                by_mid.setdefault((e or {}).get("match_id"), []).append(e)
        by_tid: dict = {}
        for m in matches:
            by_tid.setdefault((m or {}).get("tourney_id"), []).append(m)
        boards: dict = {}
        for tid, ms in by_tid.items():
            trow = trous.get(tid) or {}
            table = _table_of(trow)
            per: dict = {}
            for m in ms:
                try:
                    per[m["id"]] = compute_match_points(
                        by_mid.get(m["id"], []), table, (m or {}).get("winner_discord_id"),
                        (m or {}).get("placements"))
                except Exception:
                    per[m["id"]] = []
            _pm = [per[m["id"]] for m in ms]
            boards[str(tid)] = aggregate_standings(_pm)
            for m in ms:
                try:
                    supabase.table("bngc_comp_results").upsert({
                        "match_id": m["id"], "standings": per[m["id"]],
                        "computed_at": datetime.now(timezone.utc).isoformat(),
                        "computed_by": str(b.get("computed_by") or "dashboard"),
                    }, on_conflict="match_id").execute()
                except Exception:
                    pass
        first = boards.get(str((list(by_tid) or [None])[0]), [])
        return JSONResponse({"ok": True, "standings": first, "tourneys": list(boards)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/comp/regs")
async def comp_regs(request: Request):
    """Registration queue for a tourney (dashboard moderation view)."""
    err = _need()
    if err:
        return err
    try:
        tid = int(request.query_params.get("tourney_id"))
    except Exception:
        return JSONResponse({"error": "tourney_id required"}, status_code=400)
    want = str(request.query_params.get("status") or "").lower() or None
    if want and want not in ("pending", "accepted", "rejected", "banned"):
        return JSONResponse({"error": "bad status"}, status_code=400)
    try:
        q = supabase.table("bngc_comp_registrations").select(
            "id,team_label,member_ids,captain_id,status,created_at").eq("tourney_id", tid)
        if want:
            q = q.eq("status", want)
        res = q.order("created_at").limit(500).execute()
        return JSONResponse({"count": len(res.data or []), "regs": res.data or []})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/comp/names")
async def comp_names(request: Request):
    """Batch discord_id -> display name (dashboard player lists)."""
    err = _need()
    if err:
        return err
    try:
        raw = str(request.query_params.get("ids") or "")
        dids = sorted({x.strip() for x in raw.split(",") if x.strip()})[:100]
        if not dids:
            return JSONResponse({"names": {}})
        res = supabase.table("bngc_users").select(
            "discord_id,game_username,discord_username").in_("discord_id", dids).execute()
        out = {}
        for u in (res.data or []):
            out[str(u.get("discord_id"))] = str(
                u.get("game_username") or u.get("discord_username") or u.get("discord_id"))
        return JSONResponse({"names": out})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/reg/status")
async def comp_reg_status(request: Request):
    """Dashboard moderation mirror (accept/reject/ban a registration)."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        rid = int(b.get("reg_id"))
    except Exception:
        return JSONResponse({"error": "reg_id required"}, status_code=400)
    st = str(b.get("status") or "").lower()
    if st not in ("accepted", "rejected", "banned", "pending"):
        return JSONResponse({"error": "status must be accepted|rejected|banned|pending"}, status_code=400)
    try:
        res = supabase.table("bngc_comp_registrations").update(
            {"status": st}).eq("id", rid).execute()
        if not res.data:
            return JSONResponse({"error": "registration not found"}, status_code=404)
        return JSONResponse({"ok": True, "reg_id": rid, "status": st})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _comp_display_name(discord_id: str) -> str:
    try:
        if not supabase or not discord_id:
            return str(discord_id or "")[:6]
        res = supabase.table("bngc_users").select(
            "game_username,discord_username").eq("discord_id", str(discord_id)).limit(1).execute()
        rows = res.data or []
        if rows:
            return str(rows[0].get("game_username") or rows[0].get("discord_username")
                       or discord_id)[:32]
    except Exception:
        pass
    return str(discord_id or "")[:6]


@router.post("/api/comp/match/kills")
async def comp_match_kills(request: Request):
    """Hand-adjust a player's kills in a match (dashboard +kill button).

    Positive delta inserts manual kill rows; negative removes that many of
    the player's manual rows (newest first). Tracked (non-manual) rows are
    never touched. Returns the player's new kill total for the match.
    """
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
        did = str(b.get("discord_id") or "").strip()
        delta = int(b.get("delta") or 0)
    except Exception:
        return JSONResponse({"error": "match_id, discord_id and delta required"}, status_code=400)
    if not did or delta == 0:
        return JSONResponse({"error": "discord_id and non-zero delta required"}, status_code=400)
    if abs(delta) > 50:
        return JSONResponse({"error": "delta capped at ±50"}, status_code=400)
    try:
        mres = supabase.table("bngc_comp_matches").select("id").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        if delta > 0:
            name = _comp_display_name(did)
            supabase.table("bngc_comp_events").insert([{
                "match_id": mid, "etype": "kill", "discord_id": did,
                "actor_name": name, "target_name": "manual", "manual": True,
            }] * delta).execute()
        else:
            eres = supabase.table("bngc_comp_events").select("id").eq(
                "match_id", mid).eq("discord_id", did).eq("etype", "kill").eq(
                "manual", True).order("id", desc=True).limit(abs(delta)).execute()
            for r in (eres.data or []):
                try:
                    supabase.table("bngc_comp_events").delete().eq("id", r["id"]).execute()
                except Exception:
                    pass
        cres = supabase.table("bngc_comp_events").select("id").eq(
            "match_id", mid).eq("discord_id", did).eq("etype", "kill").execute()
        return JSONResponse({"ok": True, "match_id": mid, "discord_id": did,
                             "kills": len(cres.data or [])})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/place")
async def comp_match_place(request: Request):
    """Set/clear a manual placement for a player in a match (dashboard
    1st–5th buttons). Stored on the match placements map; beats alv order."""
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
        did = str(b.get("discord_id") or "").strip()
    except Exception:
        return JSONResponse({"error": "match_id and discord_id required"}, status_code=400)
    if not did:
        return JSONResponse({"error": "discord_id required"}, status_code=400)
    try:
        place = b.get("place")
        place = int(place) if place is not None else None
        if place is not None and (place < 1 or place > 30):
            return JSONResponse({"error": "place must be 1..30 or null"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "place must be 1..30 or null"}, status_code=400)
    try:
        mres = supabase.table("bngc_comp_matches").select(
            "id,placements").eq("id", mid).limit(1).execute()
        if not mres.data:
            return JSONResponse({"error": "match not found"}, status_code=404)
        cur = dict((mres.data[0] or {}).get("placements") or {})
        if place is None:
            cur.pop(did, None)
        else:
            # one player per place: evict whoever held it
            for k in [k for k, v in cur.items() if int(v) == place and k != did]:
                del cur[k]
            cur[did] = place
        supabase.table("bngc_comp_matches").update({"placements": cur}).eq("id", mid).execute()
        return JSONResponse({"ok": True, "match_id": mid, "placements": cur})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/comp/match/unmapped")
async def comp_match_unmapped(request: Request):
    """Actor names in a match with no discord link (dashboard link UI).

    Returns [{name, kills, deaths}] so the host can attach each to a player.
    """
    err = _need()
    if err:
        return err
    try:
        mid = int(request.query_params.get("match_id"))
    except Exception:
        return JSONResponse({"error": "match_id required"}, status_code=400)
    try:
        eres = supabase.table("bngc_comp_events").select(
            "etype,discord_id,actor_name").eq("match_id", mid).limit(5000).execute()
        agg: dict[str, dict] = {}
        for e in (eres.data or []):
            try:
                if (e or {}).get("discord_id"):
                    continue
                nm = str((e or {}).get("actor_name") or "").strip()
                if not nm:
                    continue
                d = agg.setdefault(nm, {"name": nm, "kills": 0, "deaths": 0})
                if (e or {}).get("etype") == "kill":
                    d["kills"] += 1
                elif (e or {}).get("etype") in ("death", "knock"):
                    d["deaths"] += 1
            except Exception:
                continue
        return JSONResponse({"match_id": mid,
                             "unmapped": sorted(agg.values(), key=lambda r: -r["kills"])})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/comp/match/link")
async def comp_match_link(request: Request):
    """Link (or unlink) an actor name to a player for a match (dashboard).

    Writes discord_id directly onto the null rows, so boards, HUD and stored
    results all agree with zero special cases. {match_id, actor_name,
    discord_id|null}. Unlink passes null.
    """
    err = _need()
    if err:
        return err
    b = await _json(request)
    try:
        mid = int(b.get("match_id"))
        name = str(b.get("actor_name") or "").strip()
    except Exception:
        return JSONResponse({"error": "match_id and actor_name required"}, status_code=400)
    if not name:
        return JSONResponse({"error": "actor_name required"}, status_code=400)
    try:
        did = b.get("discord_id")
        did = str(did).strip() if did else None
        q = supabase.table("bngc_comp_events").update(
            {"discord_id": did}).eq("match_id", mid).is_("discord_id", "null").eq(
            "actor_name", name)
        res = q.execute()
        n = len(res.data or [])
        return JSONResponse({"ok": True, "match_id": mid, "actor_name": name,
                             "discord_id": did, "linked": n})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/comp/tourney/by-code")
async def comp_tourney_by_code(request: Request):
    """Tourney carrying this room code (userscript join detection + bridge).

    Returns the single non-ended tourney for the code, else {tourney: null}.
    Ambiguous (2+ open rows, same code) resolves to null — never guess.
    """
    err = _need()
    if err:
        return err
    try:
        code = str(request.query_params.get("code") or "").strip().upper()
    except Exception:
        code = ""
    if len(code) != 5 or not code.isalnum():
        return JSONResponse({"tourney": None})
    try:
        res = supabase.table("bngc_comp_tourneys").select(
            "id,code,name,status,mode,series_len").eq("code", code).neq(
            "status", "ended").limit(2).execute()
        rows = res.data or []
        if len(rows) != 1:
            return JSONResponse({"tourney": None})
        t = rows[0]
        return JSONResponse({"tourney": {
            "id": t.get("id"), "code": t.get("code"), "name": t.get("name"),
            "status": t.get("status"), "mode": t.get("mode"),
            "series_len": t.get("series_len") or 0}})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
